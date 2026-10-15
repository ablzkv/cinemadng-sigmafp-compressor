"""CinemaDNG converter for uncompressed Sigma-style Bayer DNG frames (8/10/12-bit).

The lossy modes write a 12-bit tiled JPEG CinemaDNG.  Before JPEG coding it
compands source stored codes to 12-bit camera-linear values (via the source
LinearizationTable when present, otherwise by scaling white to 4095), compands
them into a log-like code space and embeds the matching DNG LinearizationTable.
A raw application can therefore restore the original linear domain on import
while JPEG spends fewer bits in fragile shadow values.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path


TIFF_SHORT = 3
TIFF_LONG = 4
TAG_IMAGE_WIDTH = 256
TAG_IMAGE_LENGTH = 257
TAG_BITS_PER_SAMPLE = 258
TAG_COMPRESSION = 259
TAG_SAMPLES_PER_PIXEL = 277
TAG_STRIP_OFFSETS = 273
TAG_ROWS_PER_STRIP = 278
TAG_STRIP_BYTE_COUNTS = 279
TAG_LINEARIZATION_TABLE = 50712
TAG_BLACK_LEVEL_REPEAT = 50713
TAG_BLACK_LEVEL = 50714
TAG_BLACK_DELTA_H = 50715
TAG_BLACK_DELTA_V = 50716
TAG_WHITE_LEVEL = 50717
DEFAULT_LOG_BASE = 16.0

# Tags written by tifffile itself; never copied as extra tags.
IMAGE_TAGS = {
    254, 256, 257, 258, 259, 262, 273, 277, 278, 279, 284, 322, 323, 324,
    325, 330, 34665, 34853,
}
# Common TIFF/DNG interpretation metadata that lives below the DNG private-tag
# range.  These are not image-layout tags and must survive the rewritten IFD.
PRESERVED_METADATA_TAGS = {
    269, 270, 271, 272, 274, 282, 283, 286, 287, 288, 296, 305, 306,
    315, 316, 317, 318, 319, 320, 338, 339,
}

# ITU T.81, Annex K: the standard luminance DC Huffman table.
DC_BITS = (0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0)
DC_VALUES = tuple(range(12))


def u16(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes | bytearray, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def write_short(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<H", data, offset, value)
    struct.pack_into("<H", data, offset + 2, 0)


def write_long(data: bytearray, offset: int, value: int) -> None:
    struct.pack_into("<I", data, offset, value)


def parse_uncompressed_dng(source: Path) -> tuple[bytearray, bytes, int, int, dict[int, int]]:
    with source.open("rb") as stream:
        prefix = stream.read(8)
        if prefix[:4] != b"II*\x00":
            raise ValueError("Only little-endian classic TIFF/DNG files are supported by this MVP.")
        ifd_offset = u32(prefix, 4)
        stream.seek(ifd_offset)
        entry_count = u16(stream.read(2), 0)
        entries = stream.read(entry_count * 12)
        if len(entries) != entry_count * 12:
            raise ValueError("The primary TIFF directory is truncated.")
        positions: dict[int, int] = {}
        values: dict[int, int] = {}
        for index in range(entry_count):
            pos = index * 12
            tag, kind, count, value = struct.unpack_from("<HHII", entries, pos)
            positions[tag] = ifd_offset + 2 + pos
            if count != 1:
                continue
            if kind == TIFF_SHORT:
                values[tag] = value & 0xFFFF
            elif kind == TIFF_LONG:
                values[tag] = value
        required = (TAG_IMAGE_WIDTH, TAG_IMAGE_LENGTH, TAG_BITS_PER_SAMPLE, TAG_COMPRESSION,
                    TAG_STRIP_OFFSETS, TAG_ROWS_PER_STRIP, TAG_STRIP_BYTE_COUNTS)
        missing = [str(tag) for tag in required if tag not in values or tag not in positions]
        if missing:
            raise ValueError(f"Unsupported DNG layout; required tags unavailable: {', '.join(missing)}")
        if values[TAG_COMPRESSION] != 1:
            raise ValueError("Input must be an uncompressed DNG frame.")
        _bits = int(values[TAG_BITS_PER_SAMPLE])
        if _bits == 8:
            pass
        elif _bits in (10, 12):
            raise ValueError(
                f"This legacy 8-bit-only reader saw {_bits}-bit input; "
                f"use the multi-bit source helpers (source_linear_table/read_source_stored) instead.")
        else:
            raise ValueError(f"Unsupported BitsPerSample {_bits}; expected 8, 10 or 12.")
        if values[TAG_ROWS_PER_STRIP] != values[TAG_IMAGE_LENGTH]:
            raise ValueError("This MVP supports one full-height strip only.")
        if values.get(TAG_SAMPLES_PER_PIXEL, 1) != 1:
            raise ValueError("This MVP supports one-channel Bayer frames only.")
        expected = values[TAG_IMAGE_WIDTH] * values[TAG_IMAGE_LENGTH]
        if values[TAG_STRIP_BYTE_COUNTS] < expected:
            raise ValueError("The source strip is shorter than its declared 8-bit Bayer raster.")
        stream.seek(0)
        header = bytearray(stream.read(values[TAG_STRIP_OFFSETS]))
        stream.seek(values[TAG_STRIP_OFFSETS])
        pixels = stream.read(values[TAG_STRIP_BYTE_COUNTS])
    if len(pixels) < expected:
        raise ValueError("The source pixel strip is truncated.")
    # Sigma fp test frames have a short trailing strip-alignment pad. It is not
    # raster data and therefore must not be handed to the JPEG scan encoder.
    return header, pixels[:expected], values[TAG_IMAGE_WIDTH], values[TAG_IMAGE_LENGTH], positions


def huffman_codes() -> dict[int, tuple[int, int]]:
    code = 0
    index = 0
    result: dict[int, tuple[int, int]] = {}
    for bit_length, count in enumerate(DC_BITS, start=1):
        for _ in range(count):
            result[DC_VALUES[index]] = (code, bit_length)
            code += 1
            index += 1
        code <<= 1
    return result


class BitWriter:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.bits = 0
        self.count = 0

    def put(self, value: int, width: int) -> None:
        self.bits = (self.bits << width) | value
        self.count += width
        while self.count >= 8:
            self.count -= 8
            byte = (self.bits >> self.count) & 0xFF
            self.buffer.append(byte)
            if byte == 0xFF:
                self.buffer.append(0)
        self.bits &= (1 << self.count) - 1

    def flush(self) -> bytes:
        if self.count:
            self.put((1 << (8 - self.count)) - 1, 8 - self.count)
        return bytes(self.buffer)


def magnitude_bits(value: int) -> tuple[int, int]:
    if value == 0:
        return 0, 0
    size = abs(value).bit_length()
    return size, value if value >= 0 else value + (1 << size) - 1


def lossless_jpeg(pixels: bytes, width: int, height: int) -> bytes:
    if len(pixels) != width * height:
        raise ValueError("Unexpected raster length.")
    codes = huffman_codes()
    output = bytearray(b"\xFF\xD8")
    dht = bytes(DC_BITS) + bytes(DC_VALUES)
    output += b"\xFF\xC4" + struct.pack(">H", len(dht) + 3) + b"\x00" + dht
    output += b"\xFF\xC3" + struct.pack(">H", 11) + bytes((8,)) + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    output += b"\xFF\xDA" + struct.pack(">H", 8) + b"\x01\x01\x00\x01\x00\x00"
    writer = BitWriter()
    midpoint = 128
    for y in range(height):
        row = y * width
        for x in range(width):
            sample = pixels[row + x]
            if x == 0 and y == 0:
                predictor = midpoint
            elif y == 0:
                predictor = pixels[row + x - 1]
            elif x == 0:
                predictor = pixels[row - width]
            else:
                predictor = pixels[row + x - 1]
            size, bits = magnitude_bits(sample - predictor)
            code, code_length = codes[size]
            writer.put(code, code_length)
            if size:
                writer.put(bits, size)
    output += writer.flush()
    return bytes(output + b"\xFF\xD9")


def tiled_dng(header: bytearray, jpeg: bytes, width: int, height: int) -> bytes:
    """Replace the primary strip layout with a one-tile TIFF/JPEG layout.

    The original metadata data remains at its original offsets in `header`; only
    the primary IFD is replaced and linked from the TIFF header.
    """
    old_ifd = u32(header, 4)
    old_count = u16(header, old_ifd)
    next_ifd = bytes(header[old_ifd + 2 + old_count * 12:old_ifd + 2 + old_count * 12 + 4])
    entries: list[bytearray] = []
    for index in range(old_count):
        entry = bytearray(header[old_ifd + 2 + index * 12:old_ifd + 2 + (index + 1) * 12])
        tag = u16(entry, 0)
        if tag in (TAG_STRIP_OFFSETS, TAG_ROWS_PER_STRIP, TAG_STRIP_BYTE_COUNTS):
            continue
        if tag == TAG_COMPRESSION:
            write_short(entry, 8, 7)
        entries.append(entry)

    # Full-frame tile. TIFF permits an edge tile to extend past ImageLength.
    new_ifd = len(header)
    final_count = len(entries) + 4
    jpeg_offset = new_ifd + 2 + final_count * 12 + 4
    entries.extend([
        bytearray(struct.pack("<HHII", 322, TIFF_LONG, 1, width)),
        bytearray(struct.pack("<HHII", 323, TIFF_LONG, 1, height)),
        bytearray(struct.pack("<HHII", 324, TIFF_LONG, 1, jpeg_offset)),
        bytearray(struct.pack("<HHII", 325, TIFF_LONG, 1, len(jpeg))),
    ])
    entries.sort(key=lambda item: u16(item, 0))
    struct.pack_into("<I", header, 4, new_ifd)
    directory = struct.pack("<H", len(entries)) + b"".join(entries) + next_ifd
    return bytes(header) + directory + jpeg


def log_linearization_table(numpy, base: float, black_protect: int) -> object:
    """Return the DNG decode LUT for a monotonic 12-bit log compander.

    Stored values are logarithmic.  The table maps them back to camera-linear
    12-bit codes as required by the DNG LinearizationTable tag.  This is an
    independently defined curve, not copied from a reference application's
    output.  The default strength (base 16, black_protect 256) was fitted
    against a reference 3:1 prioritize-quality curve (RMSE 6.5 codes
    vs 78 for the old base-64 default): identity to the black point, then a
    shadow-protecting log. Base 16 is the default: base 104 produced a measurable
    red/green bias in bright CFA samples at JPEG quality 90.
    """
    if base <= 1.0:
        raise ValueError("--log-base must be greater than 1.")
    if not 0 <= black_protect < 4095:
        raise ValueError("--black-protect must be from 0 to 4094.")
    codes = numpy.arange(4096, dtype=numpy.float64)
    protected = codes <= black_protect
    normalized = (codes - black_protect) / (4095.0 - black_protect)
    decoded = black_protect + ((numpy.power(base, numpy.maximum(normalized, 0.0)) - 1.0) / (base - 1.0)) * (4095.0 - black_protect)
    decoded[protected] = codes[protected]
    decoded = numpy.rint(decoded)
    table = numpy.clip(decoded, 0, 4095).astype(numpy.uint16)
    table[0] = 0
    table[-1] = 4095
    return table


def inverse_linearization_table(numpy, decode_table: object) -> object:
    """Map every 12-bit linear code to its nearest stored companded code."""
    linear = numpy.arange(4096, dtype=numpy.uint16)
    upper = numpy.searchsorted(decode_table, linear, side="left")
    upper = numpy.clip(upper, 0, len(decode_table) - 1)
    lower = numpy.maximum(upper - 1, 0)
    choose_lower = numpy.abs(decode_table[lower].astype(numpy.int32) - linear.astype(numpy.int32)) <= numpy.abs(decode_table[upper].astype(numpy.int32) - linear.astype(numpy.int32))
    return numpy.where(choose_lower, lower, upper).astype(numpy.uint16)


# ---------------------------------------------------------------------------
# Opt-in sensor-defect repair (4K red-pixel proof of concept).
#
# The repair runs on camera-linear raw values BEFORE log companding and JPEG
# coding, then maps the corrected value back to the nearest valid source
# 8-bit stored code so the existing compression path is used unchanged.
# Without --defect-map none of this code runs and output is unaffected.
# ---------------------------------------------------------------------------

# Directional same-color pairs at adjacent (±2 px, CFA-phase-preserving)
# offsets. A pair is used only if both endpoints are valid and agree within
# PAIR_AGREE_TOL linear codes. The tolerance comes from a read-only scan of
# the 4K test clip: clean neighboring red-site pairs agreed within 0-20
# linear codes while defect-contaminated pairs differed by 38+.
PAIR_OFFSETS = {
    "horizontal": ((-2, 0), (2, 0)),
    "vertical": ((0, -2), (0, 2)),
    "diagonal_main": ((-2, -2), (2, 2)),
    "diagonal_anti": ((2, -2), (-2, 2)),
}
PAIR_AGREE_TOL = 32
REPAIR_MIN_NEIGHBORS = 3
# Spill-guard ring for the median fallback: same-color sites at ±6 px
# (13x13 raw window edge). Hot pixels only add signal, and demosaic/JPEG
# spill around a cluster inflates the inner 9x9 median, so the fallback
# takes the lower of the inner and outer medians. It can only darken toward
# the wider background estimate, never brighten, and stays same-color only
# with mapped sites excluded like every other pool.
OUTER_RING_STEP = 6
REPAIR_PROFILE = "v2 spill-guarded wide-ring median fallback"


def load_defect_map(path) -> dict:
    """Load and schema-check a versioned JSON or canonical CSV defect map. Fails closed."""
    import json as json_module
    import csv as csv_module
    from pathlib import Path

    path_obj = Path(path)
    if path_obj.is_dir() and (path_obj / "defect_map.csv").is_file():
        path_obj = path_obj / "defect_map.csv"

    if path_obj.suffix.lower() == ".csv":
        with open(path_obj, "r", newline="", encoding="utf-8") as fh:
            rows = list(csv_module.DictReader(fh))
        if not rows:
            raise ValueError(f"Canonical defect map has no rows: {path_obj}")
        first = rows[0]
        group = first.get("group", "")
        bits = int(first.get("bits", 12))
        is_4k = "4k" in group.lower()
        width = 3856 if is_4k else 1936
        height = 2170 if is_4k else 1090
        map_paths = [(path_obj, 1)]
        second_path = path_obj.parent / "defect_map_pass2.csv"
        if path_obj.name == "defect_map.csv" and second_path.is_file():
            map_paths.append((second_path, 2))
        candidates = []
        seen = set()
        for map_path, correction_pass in map_paths:
            if map_path == path_obj:
                current_rows = rows
            else:
                with map_path.open("r", newline="", encoding="utf-8") as fh:
                    current_rows = list(csv_module.DictReader(fh))
            if not current_rows:
                raise ValueError(f"Canonical map has no rows: {map_path}")
            for entry in current_rows:
                if entry.get("group") != group or int(entry.get("bits", -1)) != bits:
                    raise ValueError(f"Map metadata does not match its canonical slot: {map_path}")
                x, y, gain = int(entry["x"]), int(entry["y"]), float(entry["gain"])
                if not (0 <= x < width and 0 <= y < height) or x % 2 or y % 2 or gain <= 1.0:
                    raise ValueError(f"Invalid red photosite or gain in canonical map: {map_path}: {x},{y}")
                if (x, y) in seen:
                    raise ValueError(f"Duplicate site across correction passes: {map_path}: {x},{y}")
                raw_cluster = entry.get("cluster")
                candidates.append({
                    "x": x,
                    "y": y,
                    "gain": gain,
                    "offset": float(entry["offset"]) if entry.get("offset") not in (None, "") else 0.0,
                    "cluster": int(raw_cluster) if raw_cluster not in (None, "") else len(candidates),
                    "correction_pass": correction_pass,
                })
                seen.add((x, y))
        return {
            "version": 2,
            "format": "canonical_csv",
            "group": group,
            "camera_model": "Sigma fp",
            "raw_width": width,
            "raw_height": height,
            "cfa": "RGGB",
            "bits": bits,
            "coordinate_space": "raw_sensor_coordinates",
            "candidates": candidates,
            "test_only": False,
            "requires_visual_approval": False,
        }

    with open(path_obj, "r", encoding="utf-8") as stream:
        data = json_module.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"Defect map is not a JSON object: {path_obj}")
    required = ("version", "camera_model", "raw_width", "raw_height", "cfa",
                "coordinate_space", "candidates", "test_only",
                "requires_visual_approval")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"Defect map {path_obj} is missing required keys: {', '.join(missing)}")
    if not isinstance(data["version"], int) or data["version"] < 1:
        raise ValueError(f"Defect map {path_obj} has an unsupported version.")
    if not isinstance(data["candidates"], list) or not data["candidates"]:
        raise ValueError(f"Defect map {path_obj} must list at least one candidate coordinate.")
    for entry in data["candidates"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("x"), int) or not isinstance(entry.get("y"), int):
            raise ValueError(f"Defect map {path_obj} has a malformed candidate entry: {entry!r}")
    return data


def cfa_pattern_name(pattern: bytes) -> str:
    """Decode a 2x2 CFARepeatPattern to a name such as 'RGGB'."""
    names = {0: "R", 1: "G", 2: "B", 3: "Cb", 4: "Cr", 5: "Y"}
    try:
        return "".join(names[byte] for byte in bytes(pattern[:4]))
    except KeyError as error:
        raise ValueError(f"Unsupported CFA pattern bytes: {list(bytes(pattern[:4]))}") from error


def cfa_color_at(pattern_name: str, x: int, y: int) -> str:
    """Return the color ('R', 'G', or 'B') of a raw site for an RGGB layout."""
    if pattern_name != "RGGB":
        raise ValueError(f"This repair path supports RGGB only, not {pattern_name}.")
    if x % 2 == 0 and y % 2 == 0:
        return "R"
    if x % 2 == 1 and y % 2 == 1:
        return "B"
    return "G"


def normalise_camera_model(value) -> str:
    """One canonical form of a DNG camera-model string.

    UniqueCameraModel (50708) and Model (272) come back from tifffile as either
    str or NUL-padded bytes depending on the writer. The profile selector and the
    converter must agree exactly, or the selector can accept a frame that the
    converter then refuses.

    Comparison is case-insensitive: 'Sigma fp', 'SIGMA fp', and 'sigma fp'
    are all treated as the same model.
    """
    if isinstance(value, bytes):
        return value.decode("ascii", "replace").strip().strip("\0").strip().lower()
    return str(value).strip().strip("\0").strip().lower()


SUPPORTED_SOURCE_BITS = (8, 10, 12)

# The public compression contract has exactly three modes.  ``lossy`` is kept
# as a compatibility alias for older scripts, but it is never reported as a
# user-facing mode and is normalized before any encoding decision is made.
COMPRESSION_MODES = ("lossless", "high_quality", "compact")
_COMPRESSION_ALIASES = {
    "lossless": "lossless",
    "high_quality": "high_quality",
    "high-quality": "high_quality",
    "quality": "high_quality",
    "lossy": "high_quality",
    "compact": "compact",
}
MODE_TARGETS = {
    "lossless": {"lossy": False, "ratio_min": None, "ratio_max": None},
    "high_quality": {"lossy": True, "ratio_min": 3.0, "ratio_max": 4.0},
    "compact": {"lossy": True, "ratio_min": 4.0, "ratio_max": 6.0},
}


def canonical_mode(mode: str) -> str:
    """Return one of the three public modes, rejecting unknown aliases."""
    key = str(mode).strip().lower().replace(" ", "_")
    try:
        return _COMPRESSION_ALIASES[key]
    except KeyError as error:
        raise ValueError(
            f"compression mode must be one of: {', '.join(COMPRESSION_MODES)}"
        ) from error


def mode_is_lossy(mode: str) -> bool:
    return MODE_TARGETS[canonical_mode(mode)]["lossy"]


def mode_target_status(mode: str, ratio: float) -> str:
    """Describe whether a lossy target range was achieved without changing mode."""
    mode = canonical_mode(mode)
    target = MODE_TARGETS[mode]
    if not target["lossy"]:
        return "not_applicable"
    if ratio < target["ratio_min"]:
        return f"not_achieved_below_{target['ratio_min']:.0f}:1"
    if ratio > target["ratio_max"]:
        return f"above_target_{target['ratio_max']:.0f}:1"
    return "achieved"


def source_bits_from_page(page) -> int:
    """Source BitsPerSample as int; raises on anything but 8/10/12."""
    raw = page.tags[TAG_BITS_PER_SAMPLE].value
    try:
        bits = int(raw[0]) if hasattr(raw, "__len__") and not isinstance(raw, (bytes, str)) else int(raw)
    except Exception as error:
        raise ValueError(f"Unreadable BitsPerSample {raw!r}.") from error
    if bits not in SUPPORTED_SOURCE_BITS:
        raise ValueError(f"Unsupported BitsPerSample {bits}; expected 8, 10 or 12.")
    return bits


def resolve_output_bits(requested: int | None, source_bits: int, mode: str) -> int:
    """Resolve storage depth, defaulting every public mode to 12-bit DNG."""
    mode = canonical_mode(mode)
    if mode_is_lossy(mode):
        return 12
    if requested is None:
        # Sigma fp's 8-bit CinemaDNG uses a LinearizationTable whose values
        # are 12-bit camera-linear codes.  Keeping its 8-bit storage while
        # rewriting the DNG can make applications disagree about the range.
        # A 12-bit linear output is semantically stable across RAW decoders;
        # it does not invent scene precision because it contains the exact
        # values supplied by the source table.
        return 12
    if requested not in SUPPORTED_SOURCE_BITS:
        raise ValueError(f"Unsupported output depth {requested}; expected 8, 10 or 12.")
    if requested < source_bits:
        raise ValueError(
            f"Lossless --output-bits {requested} cannot hold {source_bits}-bit source; "
            f"use --output-bits {source_bits} or 12.")
    if requested > source_bits and requested != 12:
        raise ValueError(
            f"Lossless promotion from {source_bits}-bit supports only --output-bits 12; "
            "10-bit promotion would require a different LinearizationTable.")
    return requested


def synthetic_linear_table(numpy, bits: int):
    """Map stored codes 0..2**bits-1 to 12-bit linear 0..4095 (white-preserving)."""
    if bits not in SUPPORTED_SOURCE_BITS:
        raise ValueError(f"Unsupported BitsPerSample {bits}; expected 8, 10 or 12.")
    if bits == 12:
        return numpy.arange(4096, dtype=numpy.uint16)
    n = 1 << bits
    codes = numpy.arange(n, dtype=numpy.float64)
    table = numpy.rint(codes * (4095.0 / (n - 1))).astype(numpy.uint16)
    table[0] = 0
    table[-1] = 4095
    return table


def source_linear_table(numpy, page):
    """Return (bits, table) mapping stored codes to 12-bit camera-linear.

    Uses the file LinearizationTable when present and correctly sized
    (256/1024/4096 for 8/10/12-bit); 10/12-bit Sigma fp frames ship without
    one, in which case a white-preserving synthetic table is used.
    """
    bits = source_bits_from_page(page)
    if TAG_LINEARIZATION_TABLE in page.tags:
        lut = numpy.asarray(page.tags[TAG_LINEARIZATION_TABLE].value, dtype=numpy.uint16)
        if len(lut) == (1 << bits):
            if bool((lut[:-1] > lut[1:]).any()):
                raise ValueError("Source LinearizationTable is not monotonic; refusing unsorted LUT.")
            if int(lut.min()) < 0 or int(lut.max()) > 4095:
                raise ValueError("Source LinearizationTable values outside 0..4095.")
            return bits, lut
        raise ValueError(
            f"Source LinearizationTable has {len(lut)} entries for {bits}-bit input "
            f"(expected {1 << bits}).")
    return bits, synthetic_linear_table(numpy, bits)


def read_source_stored(numpy, page):
    """Stored raw codes as a uint16 (H, W) array for 8/10/12-bit input."""
    arr = numpy.asarray(page.asarray())
    h, w = int(page.tags[TAG_IMAGE_LENGTH].value), int(page.tags[TAG_IMAGE_WIDTH].value)
    stored = numpy.asarray(arr, dtype=numpy.uint16).reshape(h, w)
    bits = source_bits_from_page(page)
    if int(stored.min()) < 0 or int(stored.max()) >= (1 << bits):
        raise ValueError(f"Stored raw values outside {bits}-bit range.")
    return stored


def stored_black_level(numpy, page, bits: int, linearization) -> float:
    """Black level in stored-code units for gain corrections."""
    if bits == 8:
        return 0.0
    black_values = _range_values(page.tags.get(TAG_BLACK_LEVEL), 4)
    if not black_values:
        return 0.0
    white_values = _range_values(page.tags.get(TAG_WHITE_LEVEL), 1)
    black = float(numpy.median(numpy.asarray(black_values, dtype=numpy.float64)))
    if white_values and white_values[0] > (1 << bits) - 1:
        table = numpy.asarray(linearization, dtype=numpy.int32)
        return float(int(numpy.argmin(numpy.abs(table - round(black)))))
    return black


def stored_white_level(page, bits: int) -> float:
    """Clipping threshold for original stored RAW codes."""
    values = _range_values(page.tags.get(TAG_WHITE_LEVEL), 1)
    return min(float(values[0]), (1 << bits) - 1) if values else float((1 << bits) - 1)


REGION_GAIN_BLEND = 0.5


def estimate_region_repair_gains(numpy, defect_map: dict, median_red, black: float,
                                 radius: int = 8) -> dict:
    """Blend canonical gains halfway toward stronger temporal/local estimates."""
    updated = {**defect_map, "candidates": [dict(site) for site in defect_map["candidates"]]}
    clusters = {}
    for index, site in enumerate(updated["candidates"]):
        cluster = site.get("cluster", index)
        clusters.setdefault(cluster, []).append((int(site["x"]) // 2, int(site["y"]) // 2))
    masked = numpy.zeros(median_red.shape, dtype=bool)
    for points in clusters.values():
        xs, ys = zip(*points)
        masked[min(ys):max(ys) + 1, min(xs):max(xs) + 1] = True
    height, width = median_red.shape
    for site in updated["candidates"]:
        x, y = int(site["x"]) // 2, int(site["y"]) // 2
        y0, y1 = max(0, y - radius), min(height, y + radius + 1)
        x0, x1 = max(0, x - radius), min(width, x + radius + 1)
        neighbors = median_red[y0:y1, x0:x1][~masked[y0:y1, x0:x1]]
        reference = float(numpy.median(neighbors)) if neighbors.size else 0.0
        target = float(median_red[y, x])
        region_gain = (target - black) / (reference - black) if reference - black > 1 else 1.0
        if not numpy.isfinite(region_gain) or region_gain < 1.03:
            region_gain = 1.0
        calibrated_gain = float(site["gain"])
        site["calibrated_gain"] = calibrated_gain
        site["region_gain"] = round(region_gain, 6)
        target_gain = max(calibrated_gain, min(region_gain, 4.0))
        site["gain"] = round(1.0 / ((1.0 - REGION_GAIN_BLEND) / calibrated_gain
                                    + REGION_GAIN_BLEND / target_gain), 6)
    updated["black_level_stored"] = float(black)
    updated["repair_model"] = "blended_region_gain"
    updated["repair_profile"] = "canonical map + 50% regional correction v1"
    return updated


def prepare_region_repair_map(files, defect_map: dict | None) -> dict | None:
    """Keep canonical calibration gains unchanged during conversion."""
    return defect_map

def validate_defect_map(defect_map: dict, page, width: int, height: int, map_path) -> None:
    """Fail closed unless the frame matches the map's camera/geometry/CFA."""
    models = []
    for tag_code in (50708, 272):
        if tag_code in page.tags:
            models.append(normalise_camera_model(page.tags[tag_code].value))
    if normalise_camera_model(defect_map["camera_model"]) not in models:
        raise ValueError(
            f"Defect map camera {defect_map['camera_model']!r} does not match frame models {models}; refusing {map_path}."
        )
    map_bits = defect_map.get("bits")
    if map_bits is not None and source_bits_from_page(page) != int(map_bits):
        raise ValueError(
            f"Defect map is {map_bits}-bit but the frame is {source_bits_from_page(page)}-bit; "
            f"refusing {map_path}."
        )
    if width != defect_map["raw_width"] or height != defect_map["raw_height"]:
        raise ValueError(
            f"Defect map covers {defect_map['raw_width']}x{defect_map['raw_height']} "
            f"but the frame is {width}x{height}; refusing {map_path}."
        )
    pattern_tag = page.tags.get(33422)
    if pattern_tag is None:
        raise ValueError(f"Defect map {map_path}: source frame is missing CFA tag 33422; refusing.")
    pattern = cfa_pattern_name(pattern_tag.value)
    if pattern != defect_map["cfa"]:
        raise ValueError(
            f"Defect map CFA {defect_map['cfa']!r} does not match frame CFA {pattern!r}; refusing {map_path}."
        )
    # Every mapped site must sit on the red phase of the declared CFA. A mistyped
    # coordinate would otherwise be repaired on the wrong colour channel, or be
    # silently skipped by the per-site evidence rule.
    expected_color = defect_map.get("site_color", "R")
    for entry in defect_map["candidates"]:
        if cfa_color_at(pattern, entry["x"], entry["y"]) != expected_color:
            raise ValueError(
                f"Defect map coordinate ({entry['x']}, {entry['y']}) is on the "
                f"{cfa_color_at(pattern, entry['x'], entry['y'])} phase of {pattern}, "
                f"not {expected_color}; refusing {map_path}."
            )
    for entry in defect_map["candidates"]:
        if not (0 <= entry["x"] < width and 0 <= entry["y"] < height):
            raise ValueError(
                f"Defect map coordinate ({entry['x']}, {entry['y']}) is outside "
                f"the {width}x{height} frame; refusing {map_path}."
            )


def edge_repair_records(numpy, stored, linear_table, defect_map: dict):
    """Reconstruct mapped red codes from this frame's untouched RAW mosaic."""
    from red_edge_reconstruction import edge_aware_red_sites

    if defect_map["cfa"] != "RGGB":
        raise ValueError("Edge-aware red repair requires RGGB CFA.")
    bits = int(defect_map.get("bits", 12))
    item = {"raw": stored, "bits": bits,
            "black": float(defect_map.get("black_level_stored", 0)),
            "white": float(defect_map.get("white_level_stored", (1 << bits) - 1))}
    replacements, _ = edge_aware_red_sites(
        item, defect_map["candidates"], defect_map.get("_frame_name", "frame"),
        int(defect_map.get("edge_debug_limit", 0)))
    records = []
    for site in replacements:
        x, y, new = int(site["x"]), int(site["y"]), int(site["replacement"])
        old = int(stored[y, x])
        if new != old:
            records.append({"x": x, "y": y, "color": "R", "original_stored": old,
                            "replacement_stored": new,
                            "original_linear": int(linear_table[old]),
                            "replacement_linear": int(linear_table[new]),
                            "method": f"red_edge_aware:{site['edge_direction']}",
                            "fallback": site["edge_fallback"],
                            "expanded_cluster_ring": bool(site.get("residual_halo")),
                            "correction_pass": int(site.get("correction_pass", 1))})
    estimated = {(int(s["x"]), int(s["y"])) for s in replacements}
    skipped = [{"x": int(s["x"]), "y": int(s["y"]), "color": "R",
                "reason": "no safe healthy red estimate"}
               for s in defect_map["candidates"] if (int(s["x"]), int(s["y"])) not in estimated]
    return records, skipped


def repair_defects(numpy, pixels: bytes, width: int, height: int, linear_table, defect_map: dict):
    """Repair mapped sites in camera-linear space; return patched pixels + records.

    Only mapped sites may change. Same-color neighbors only (CFA phase kept
    with multiples-of-two offsets), mapped sites excluded from every neighbor
    pool, corrected linear value mapped back to the nearest source 8-bit code.
    """
    linear_table = numpy.asarray(linear_table, dtype=numpy.uint16)
    table_min, table_max = int(linear_table.min()), int(linear_table.max())
    inverse = inverse_linearization_table(numpy, linear_table)
    stored = numpy.frombuffer(pixels, dtype=numpy.uint8)
    linear = linear_table[stored].astype(numpy.int32).reshape(height, width)
    patched = bytearray(pixels)
    defect_set = {(entry["x"], entry["y"]) for entry in defect_map["candidates"]}
    pattern_name = defect_map["cfa"]
    records: list[dict] = []
    skipped: list[dict] = []
    has_gain = any("gain" in entry for entry in defect_map.get("candidates", []))
    if has_gain:
        bits = int(defect_map.get("bits", 8))
        limit = (1 << bits) - 1
        black = float(defect_map.get("black_level_stored", 0.0 if bits == 8 else (float(linear_table[0]) if linear_table is not None and len(linear_table) > 0 else 0.0)))
        for entry in defect_map["candidates"]:
            x, y = entry["x"], entry["y"]
            gain = float(entry.get("gain", 1.0))
            if 0 <= y < height and 0 <= x < width:
                idx = y * width + x
                old = float(patched[idx])
                new = int(numpy.clip(numpy.rint(black + (old - black) / gain), 0, limit))
                if new != int(old):
                    patched[idx] = new
                    records.append({
                        "x": x, "y": y, "color": "R",
                        "original_stored": int(old), "replacement_stored": new,
                        "gain": gain, "method": defect_map.get("repair_model", "calibrated_gain"),
                    })
        return bytes(patched), records, skipped
    for entry in defect_map["candidates"]:
        x, y = entry["x"], entry["y"]
        color = cfa_color_at(pattern_name, x, y)
        neighbors: list[tuple[int, int]] = []
        for dy in range(-4, 5, 2):
            for dx in range(-4, 5, 2):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in defect_set:
                    neighbors.append((nx, ny))
        outer: list[tuple[int, int]] = []
        step = OUTER_RING_STEP
        for dy in range(-step, step + 1, 2):
            for dx in range(-step, step + 1, 2):
                if max(abs(dx), abs(dy)) != step:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in defect_set:
                    outer.append((nx, ny))
        if len(neighbors) < REPAIR_MIN_NEIGHBORS:
            skipped.append({"x": x, "y": y, "color": color, "reason": "too few valid same-color neighbors"})
            continue
        best_pair = None
        for direction, (first, second) in PAIR_OFFSETS.items():
            first_pos = (x + first[0], y + first[1])
            second_pos = (x + second[0], y + second[1])
            if first_pos not in neighbors or second_pos not in neighbors:
                continue
            first_val = int(linear[y + first[1], x + first[0]])
            second_val = int(linear[y + second[1], x + second[0]])
            gap = abs(first_val - second_val)
            if best_pair is None or gap < best_pair[0]:
                best_pair = (gap, direction, first_val, second_val)
        outer_median = None
        if len(outer) >= REPAIR_MIN_NEIGHBORS:
            outer_pool = sorted(int(linear[ny, nx]) for nx, ny in outer)
            outer_median = outer_pool[len(outer_pool) // 2]
        if best_pair is not None and best_pair[0] <= PAIR_AGREE_TOL:
            _, direction, first_val, second_val = best_pair
            corrected = (first_val + second_val + 1) // 2
            method = f"directional_pair:{direction}"
            agree_gap = best_pair[0]
            median_inner = None
            median_outer = outer_median
            if outer_median is not None and outer_median < corrected:
                # Same spill guard as the median fallback: an agreeing pair
                # can still sit inside the cluster halo with both endpoints
                # elevated, so cap toward the wider background estimate.
                corrected = outer_median
                method += "+spillcap"
            median_inner = None
        else:
            pool = sorted(int(linear[ny, nx]) for nx, ny in neighbors)
            median_inner = pool[len(pool) // 2]
            median_outer = outer_median
            if outer_median is not None:
                # Spill guard: the outer ring sees past the cluster halo, so
                # when it estimates a darker background, trust it over the
                # inflated inner median. min() only ever darkens toward the
                # wider background, never brightens.
                corrected = min(median_inner, outer_median)
                method = "median_spillguard"
            else:
                corrected = median_inner
                method = "median9x9"
            agree_gap = None
        if not table_min <= corrected <= table_max:
            skipped.append({"x": x, "y": y, "color": color, "reason": "corrected value outside LinearizationTable range"})
            continue
        original_stored = int(stored[y * width + x])
        original_linear = int(linear[y, x])
        replacement_stored = int(inverse[corrected])
        replacement_linear = int(linear_table[replacement_stored])
        patched[y * width + x] = replacement_stored
        records.append({
            "x": x, "y": y, "color": color,
            "original_stored": original_stored, "original_linear": original_linear,
            "replacement_stored": replacement_stored, "replacement_linear": replacement_linear,
            "method": method, "neighbor_count": len(neighbors), "pair_agreement_gap": agree_gap,
            "neighbor_count_outer": len(outer), "median_inner": median_inner, "median_outer": median_outer,
        })
    return bytes(patched), records, skipped


def repair_stored_array(numpy, stored, width: int, height: int, linear_table, defect_map: dict):
    """Generalized repair on a uint16 stored array (8/10/12-bit).

    Same directional-pair/spill-guard logic, method names and nearest-code rule
    as repair_defects. Returns (patched_stored, records, skipped); only mapped
    sites may change. For 8-bit input results are identical to repair_defects.
    """
    if (any("gain" in entry for entry in defect_map.get("candidates", []))
            and defect_map.get("red_correction", "edge-aware") == "edge-aware"):
        original = numpy.asarray(stored, dtype=numpy.uint16).reshape(height, width)
        patched = numpy.array(original, copy=True)
        records, skipped = edge_repair_records(numpy, original, linear_table, defect_map)
        for record in records:
            patched[record["y"], record["x"]] = record["replacement_stored"]
        return patched, records, skipped
    linear_table = numpy.asarray(linear_table, dtype=numpy.uint16)
    table_min, table_max = int(linear_table.min()), int(linear_table.max())
    inverse = inverse_linearization_table(numpy, linear_table)
    stored = numpy.asarray(stored, dtype=numpy.uint16).reshape(height, width)
    linear = linear_table[stored].astype(numpy.int32)
    patched = numpy.array(stored, copy=True)
    candidates = sorted(defect_map["candidates"], key=lambda site: int(site.get("correction_pass", 1)))
    defect_set = {(entry["x"], entry["y"]) for entry in candidates}
    pattern_name = defect_map["cfa"]
    records: list[dict] = []
    skipped: list[dict] = []
    has_gain = any("gain" in entry for entry in defect_map.get("candidates", []))
    if has_gain:
        bits = int(defect_map.get("bits", 12))
        limit = (1 << bits) - 1
        black = float(defect_map.get("black_level_stored", 0.0 if bits == 8 else (float(linear_table[0]) if linear_table is not None and len(linear_table) > 0 else 0.0)))
        for entry in candidates:
            x, y = entry["x"], entry["y"]
            gain = float(entry.get("gain", 1.0))
            if 0 <= y < height and 0 <= x < width:
                old = float(stored[y, x])
                new = int(numpy.clip(numpy.rint(black + (old - black) / gain), 0, limit))
                if new != int(old):
                    patched[y, x] = new
                    records.append({
                        "x": x, "y": y, "color": "R",
                        "original_stored": int(old), "replacement_stored": new,
                        "gain": gain, "method": defect_map.get("repair_model", "calibrated_gain"),
                        "correction_pass": int(entry.get("correction_pass", 1)),
                    })
        return patched, records, skipped
    for entry in candidates:
        x, y = entry["x"], entry["y"]
        color = cfa_color_at(pattern_name, x, y)
        neighbors: list[tuple[int, int]] = []
        for dy in range(-4, 5, 2):
            for dx in range(-4, 5, 2):
                if dx == 0 and dy == 0:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in defect_set:
                    neighbors.append((nx, ny))
        outer: list[tuple[int, int]] = []
        step = OUTER_RING_STEP
        for dy in range(-step, step + 1, 2):
            for dx in range(-step, step + 1, 2):
                if max(abs(dx), abs(dy)) != step:
                    continue
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in defect_set:
                    outer.append((nx, ny))
        if len(neighbors) < REPAIR_MIN_NEIGHBORS:
            skipped.append({"x": x, "y": y, "color": color, "reason": "too few valid same-color neighbors"})
            continue
        best_pair = None
        for direction, (first, second) in PAIR_OFFSETS.items():
            first_pos = (x + first[0], y + first[1])
            second_pos = (x + second[0], y + second[1])
            if first_pos not in neighbors or second_pos not in neighbors:
                continue
            first_val = int(linear[y + first[1], x + first[0]])
            second_val = int(linear[y + second[1], x + second[0]])
            gap = abs(first_val - second_val)
            if best_pair is None or gap < best_pair[0]:
                best_pair = (gap, direction, first_val, second_val)
        outer_median = None
        if len(outer) >= REPAIR_MIN_NEIGHBORS:
            outer_pool = sorted(int(linear[ny, nx]) for nx, ny in outer)
            outer_median = outer_pool[len(outer_pool) // 2]
        if best_pair is not None and best_pair[0] <= PAIR_AGREE_TOL:
            _, direction, first_val, second_val = best_pair
            corrected = (first_val + second_val + 1) // 2
            method = f"directional_pair:{direction}"
            agree_gap = best_pair[0]
            median_inner = None
            median_outer = outer_median
            if outer_median is not None and outer_median < corrected:
                corrected = outer_median
                method += "+spillcap"
            median_inner = None
        else:
            pool = sorted(int(linear[ny, nx]) for nx, ny in neighbors)
            median_inner = pool[len(pool) // 2]
            median_outer = outer_median
            if outer_median is not None:
                corrected = min(median_inner, outer_median)
                method = "median_spillguard"
            else:
                corrected = median_inner
                method = "median9x9"
            agree_gap = None
        if not table_min <= corrected <= table_max:
            skipped.append({"x": x, "y": y, "color": color, "reason": "corrected value outside LinearizationTable range"})
            continue
        original_stored = int(stored[y, x])
        original_linear = int(linear[y, x])
        replacement_stored = int(inverse[corrected])
        replacement_linear = int(linear_table[replacement_stored])
        patched[y, x] = replacement_stored
        records.append({
            "x": x, "y": y, "color": color,
            "original_stored": original_stored, "original_linear": original_linear,
            "replacement_stored": replacement_stored, "replacement_linear": replacement_linear,
            "method": method, "neighbor_count": len(neighbors), "pair_agreement_gap": agree_gap,
            "neighbor_count_outer": len(outer), "median_inner": median_inner, "median_outer": median_outer,
        })
    return patched, records, skipped


STAGE_NAMES = ("read_parse", "linearize_repair", "compand", "encode_write",
               "reopen_decode", "error_calc", "preview", "companion_copy",
               "pool_startup")


def _stage_begin(stage_times):
    import time as time_module

    return time_module.perf_counter() if stage_times is not None else 0.0


def _stage_end(stage_times, stage, start):
    import time as time_module

    if stage_times is not None:
        stage_times[stage] = stage_times.get(stage, 0.0) + (time_module.perf_counter() - start) * 1000.0


def _cfa_components(data):
    """Keep neighboring Bayer columns in separate JPEG components."""
    import numpy

    height, width, samples = data.shape
    if samples != 1 or width % 2:
        raise ValueError("CFA JPEG tiles require one sample and an even width.")
    return numpy.ascontiguousarray(data.reshape(height, width // 2, 2))


def _encode_lossless_cfa(data, *, bitspersample=None):
    import imagecodecs

    return imagecodecs.jpeg_encode(
        _cfa_components(data), lossless=True, predictor=1,
        bitspersample=bitspersample)


def _encode_lossy_cfa(data, *, level=None, bitspersample=None):
    import imagecodecs

    return imagecodecs.jpeg_encode(
        _cfa_components(data), level=level, bitspersample=bitspersample)


def _set_codec7(tifffile, mode: str) -> None:
    """Select the TIFF compression-7 encoder for `mode` (lossless vs lossy)."""
    tifffile.TIFF.COMPRESSORS._codecs[7] = (
        _encode_lossless_cfa if mode == "lossless" else _encode_lossy_cfa)


def collect_extra_tags(page, mode: str, output_bits: int, image_tags_written_by_tifffile: set) -> list:
    """Copy DNG/CFA metadata tags for the output frame (shared primitive)."""
    mode = canonical_mode(mode)
    extra_tags = []
    for tag in page.tags.values():
        # DNG/CFA metadata starts at 33421. Exclude IFD-pointer tags whose
        # targets are not copied into the new file.
        if ((tag.code < 33421 and tag.code not in PRESERVED_METADATA_TAGS)
                or tag.code in image_tags_written_by_tifffile
                or tag.code in (TAG_BLACK_LEVEL_REPEAT, TAG_BLACK_LEVEL,
                                 TAG_BLACK_DELTA_H, TAG_BLACK_DELTA_V, TAG_WHITE_LEVEL)
                or ((mode_is_lossy(mode) or output_bits == 12) and tag.code == TAG_LINEARIZATION_TABLE)):
            continue
        extra_tags.append((tag.code, tag.dtype, tag.count, tag.value, False))
    return extra_tags


def canonical_map_mode(defect_map: dict | None) -> str | None:
    if not defect_map:
        return None
    explicit = defect_map.get("mode")
    if explicit in ("crop", "full"):
        return explicit
    if not defect_map.get("group"):
        return None
    from fixed_map_selector import canonical_map_mode as map_mode
    return map_mode(defect_map["group"])


def validate_recording_mode(recording_mode: str | None, defect_map: dict | None = None) -> str | None:
    if recording_mode not in (None, "crop", "full"):
        raise ValueError("recording_mode must be crop or full.")
    map_mode = canonical_map_mode(defect_map)
    if recording_mode is not None and map_mode is not None and recording_mode != map_mode:
        raise ValueError("recording_mode does not match the selected defect map.")
    return recording_mode or map_mode


def add_recording_mode_metadata(extra_tags: list, defect_map: dict | None,
                                recording_mode: str | None = None) -> list:
    """Append the selected canonical Full/Crop mode to TIFF ImageDescription."""
    mode = validate_recording_mode(recording_mode, defect_map)
    if mode is None:
        return extra_tags
    previous = next((tag[3] for tag in extra_tags if tag[0] == 270), "")
    if isinstance(previous, bytes):
        previous = previous.decode("ascii", "replace")
    elif isinstance(previous, (tuple, list)):
        previous = "".join(str(value) for value in previous)
    previous = str(previous or "").strip("\0 \t\r\n")
    description = f"{previous}; Sigma fp {mode.title()}" if previous else f"Sigma fp {mode.title()}"
    encoded = description.encode("ascii", "replace") + b"\0"
    return [tag for tag in extra_tags if tag[0] != 270] + [(270, 2, len(encoded), encoded, False)]


def write_resolve_metadata_csv(csv_path: Path, clips, *, allow_existing: bool = False) -> Path | None:
    """Write a Resolve-importable Comments column for clips with a known mode."""
    import csv as csv_module

    rows = sorted((Path(path).name, mode.title()) for path, mode in clips
                  if mode in ("crop", "full"))
    if not rows:
        return None
    csv_path = Path(csv_path)
    if csv_path.exists():
        if not allow_existing:
            raise FileExistsError(f"Refusing to overwrite Resolve metadata file: {csv_path}")
        with csv_path.open("r", newline="", encoding="utf-8") as stream:
            existing = list(csv_module.DictReader(stream))
        if (list(existing[0].keys()) if existing else []) != ["File Name", "Comments"] or [
                (row.get("File Name"), row.get("Comments")) for row in existing] != rows:
            raise FileExistsError(f"Existing Resolve metadata file does not match this conversion: {csv_path}")
        return csv_path
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv_module.writer(stream)
        writer.writerow(("File Name", "Comments"))
        writer.writerows(rows)
    return csv_path


def _range_values(value, count: int, dtype=None) -> list[float]:
    """Decode a range tag, using TIFF dtype rather than tuple length."""
    if value is None:
        return []
    if hasattr(value, "value"):
        dtype = getattr(value, "dtype", dtype)
        value = value.value
    elif isinstance(value, tuple) and len(value) == 2:
        try:
            dtype_code = int(value[0])
        except (TypeError, ValueError):
            dtype_code = None
        if dtype_code is not None and 1 <= dtype_code <= 18:
            dtype, value = value
    raw = list(value) if isinstance(value, (tuple, list)) else [value]
    if dtype in (5, 10):
        values = [raw[i] / raw[i + 1] if raw[i + 1] else 0.0
                  for i in range(0, len(raw) - 1, 2)]
    else:
        values = [float(v) for v in raw]
    if len(values) == 1 and count > 1:
        values *= count
    return values[:count]


def normalise_range_tags(source_ranges: dict, source_bits: int, source_linearization,
                         output_bits: int, output_linearization, numpy) -> list:
    """Write BlackLevel/WhiteLevel in the DNG linearisation domain.

    DNG readers apply a LinearizationTable before black subtraction and white
    normalisation.  An 8-bit Sigma frame therefore legitimately has an 8-bit
    stored raster but 12-bit range tags (WhiteLevel 4095).  Re-scaling those
    tags to 255/16 while retaining the source LUT changes the rendered image,
    despite lossless stored pixels.  Preserve such already-linear tags exactly.
    Sources whose tags are in their stored domain (for example 10-bit frames
    without a LUT) are promoted through the source LUT to 12-bit linear codes.
    """
    output_max = (1 << output_bits) - 1
    white_source = source_ranges.get(TAG_WHITE_LEVEL)
    source_white_values = _range_values(white_source, 1)
    source_white = source_white_values[0] if source_white_values else float((1 << source_bits) - 1)
    source_lut = numpy.asarray(source_linearization, dtype=numpy.int32)
    repeat = source_ranges.get(TAG_BLACK_LEVEL_REPEAT, (2, 2))
    if hasattr(repeat, "value"):
        repeat = repeat.value
    elif (isinstance(repeat, tuple) and len(repeat) == 2
          and isinstance(repeat[1], (tuple, list))):
        repeat = repeat[1]
    repeat = tuple(int(v) for v in repeat[:2]) if isinstance(repeat, (tuple, list)) else (2, 2)
    count = max(1, repeat[0] * repeat[1])
    black_source = source_ranges.get(TAG_BLACK_LEVEL)
    black_raw = _range_values(black_source, count)
    if not black_raw:
        black_raw = [0.0] * count

    # A white point beyond the stored code range proves that these tags are
    # already in the LinearizationTable's output domain.  Retain their exact
    # rational representation so an 8-bit lossless re-wrap remains visually
    # identical in RAW applications as well as pixel-identical in TIFF.
    stored_max = (1 << source_bits) - 1
    if white_source is not None and source_white > stored_max:
        white_dtype, white_value = white_source
        black_dtype, black_value = black_source if black_source is not None else (5, (0, 1))
        black_values = list(black_value) if isinstance(black_value, (tuple, list)) else [black_value]
        black_count = (len(black_values) // 2 if black_dtype in (5, 10) else len(black_values))
        return [(TAG_BLACK_LEVEL_REPEAT, 3, 2, repeat, False),
                (TAG_BLACK_LEVEL, black_dtype, max(1, black_count), black_value, False),
                (TAG_WHITE_LEVEL, white_dtype, 1, white_value, False)]

    black_out = []
    for raw in black_raw:
        if 0 <= round(raw) < len(source_lut):
            linear = int(source_lut[round(raw)])
        else:
            linear = round(raw * 4095.0 / max(source_white, 1.0))
        black_out.append(max(0, min(4095, linear)))
    rational = tuple(value for black in black_out for value in (int(black), 1))
    return [(TAG_BLACK_LEVEL_REPEAT, 3, 2, repeat, False),
            (TAG_BLACK_LEVEL, 5, count, rational, False),
            (TAG_WHITE_LEVEL, 4, 1, 4095 if output_bits == 12 else output_max, False)]


def prepare_raster(numpy, pixels: bytes, width: int, height: int, linearization, mode: str, output_bits: int, companding: str, log_base: float, black_protect: int):
    """Build the output raster + DNG decode LUT (shared primitive).

    Returns (raster, effective_output_bits, output_linearization_or_None,
    expected_linear_raster). The expected raster is the camera-linear frame
    the output must decode back to; verification reuses it instead of
    reparsing the source.

    Legacy 8-bit bytes path is preserved byte-for-byte. For 10/12-bit use
    prepare_raster_from_stored with a uint16 stored array.
    """
    mode = canonical_mode(mode)
    effective_bits = 12 if mode_is_lossy(mode) else output_bits
    if effective_bits == 12:
        linear_table = numpy.asarray(linearization, dtype=numpy.uint16)
        if isinstance(pixels, bytes):
            linear_raster = linear_table[numpy.frombuffer(pixels, dtype=numpy.uint8)].reshape(height, width)
        else:
            stored = numpy.asarray(pixels, dtype=numpy.uint16).reshape(height, width)
            linear_raster = linear_table[stored]
        if mode_is_lossy(mode) and companding == "log":
            output_linearization = log_linearization_table(numpy, log_base, black_protect)
            inverse = inverse_linearization_table(numpy, output_linearization)
            raster = inverse[linear_raster]
        else:
            output_linearization = numpy.arange(4096, dtype=numpy.uint16)
            raster = linear_raster
        return raster, effective_bits, output_linearization, linear_raster
    if isinstance(pixels, bytes):
        raster = numpy.frombuffer(pixels, dtype=numpy.uint8).reshape(height, width)
        return raster, effective_bits, None, raster
    raster = numpy.asarray(pixels, dtype=numpy.uint16).reshape(height, width)
    return raster, effective_bits, None, raster


def prepare_raster_from_stored(numpy, stored, linear_table, mode: str, output_bits: int, companding: str, log_base: float, black_protect: int):
    """Generalized raster builder from a uint16 stored array (8/10/12-bit)."""
    mode = canonical_mode(mode)
    stored = numpy.asarray(stored, dtype=numpy.uint16)
    linear_table = numpy.asarray(linear_table, dtype=numpy.uint16)
    effective_bits = 12 if mode_is_lossy(mode) else output_bits
    if effective_bits == 12:
        linear_raster = linear_table[stored]
        if mode_is_lossy(mode) and companding == "log":
            output_linearization = log_linearization_table(numpy, log_base, black_protect)
            inverse = inverse_linearization_table(numpy, output_linearization)
            raster = inverse[linear_raster]
        else:
            output_linearization = numpy.arange(4096, dtype=numpy.uint16)
            raster = linear_raster
        return raster, effective_bits, output_linearization, linear_raster
    return stored, effective_bits, None, stored


def write_dng(tifffile, destination: Path, raster, output_bits: int, mode: str, quality: int, tile: tuple, extra_tags: list, output_linearization=None) -> None:
    """Write one tiled JPEG DNG frame, publishing it atomically on success."""
    mode = canonical_mode(mode)
    tags = list(extra_tags)
    resolution_tags = {tag[0]: tag[3] for tag in tags if tag[0] in (282, 283, 296)}
    tags = [tag for tag in tags if tag[0] not in (282, 283, 296)]
    if output_linearization is not None:
        tags.append((TAG_LINEARIZATION_TABLE, TIFF_SHORT, 4096, tuple(output_linearization), False))
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        raise FileExistsError(f"Refusing to reuse incomplete output: {partial}")
    with tifffile.TiffWriter(partial, byteorder="<") as writer:
        writer.write(
            raster,
            photometric=32803,  # CFA
            bitspersample=output_bits,
            compression=7,
            compressionargs={"level": quality, "bitspersample": output_bits} if mode_is_lossy(mode) else None,
            tile=tile,
            metadata=None,
            software=False,
            resolution=(resolution_tags.get(282, (1, 1)), resolution_tags.get(283, (1, 1))),
            resolutionunit=resolution_tags.get(296, 1),
            extratags=tags,
        )
    # Replace is atomic when both paths are on the same volume. A cancelled or
    # failed encode leaves a visibly incomplete .partial file, never a DNG that
    # looks complete to Resolve or to a later verification pass.
    partial.replace(destination)


def verify_raster(numpy, tifffile, expected, compressed: Path, output_bits: int, mode: str, stage_times=None, extended: bool = False) -> dict:
    """Native-decode one output and compare against the in-memory expected raster.

    Reopens only the completed output, never the source. Returns mean/max and,
    when extended, p99/p99.9 percentiles. Fails on decode/shape/dtype errors
    and non-finite calculations.
    """
    mode = canonical_mode(mode)
    start = _stage_begin(stage_times)
    actual = tifffile.imread(compressed)
    _stage_end(stage_times, "reopen_decode", start)
    if output_bits == 12 or mode_is_lossy(mode):
        with tifffile.TiffFile(compressed) as output_tiff:
            lut_tag = output_tiff.pages[0].tags.get(TAG_LINEARIZATION_TABLE)
            if lut_tag is None:
                raise RuntimeError(f"Native verification failed for {compressed.name}: missing LinearizationTable.")
            output_linearization = numpy.asarray(lut_tag.value, dtype=numpy.uint16)
        if len(output_linearization) == 0:
            raise RuntimeError(f"Native verification failed for {compressed.name}: missing LinearizationTable.")
        actual = output_linearization[actual]
    if actual.shape != expected.shape:
        raise RuntimeError(f"Decoded frame shape differs from source: {actual.shape} != {expected.shape}")
    if actual.dtype.kind not in "ui":
        raise RuntimeError(f"Native verification failed for {compressed.name}: unexpected dtype {actual.dtype}.")
    if mode == "lossless" and not numpy.array_equal(actual, expected):
        raise RuntimeError(
            "Decoded pixels differ from source: "
            f"source={hashlib.sha256(expected.tobytes()).hexdigest()} decoded={hashlib.sha256(actual.tobytes()).hexdigest()} "
        )
    start = _stage_begin(stage_times)
    difference = numpy.abs(actual.astype(numpy.int32) - expected.astype(numpy.int32))
    flat = difference.ravel().astype(numpy.float64)
    if not numpy.all(numpy.isfinite(flat)):
        raise RuntimeError(f"Native verification failed for {compressed.name}: non-finite error values.")
    result = {"mean": float(flat.mean()), "max": int(flat.max())}
    for label, mask in (("shadow", expected <= 512), ("highlight", expected >= 3584)):
        if bool(mask.any()):
            region = difference[mask].astype(numpy.float64)
            result[f"{label}_mean"] = float(region.mean())
            result[f"{label}_max"] = int(region.max())
    if extended:
        result["p99"] = float(numpy.percentile(flat, 99))
        result["p999"] = float(numpy.percentile(flat, 99.9))
    _stage_end(stage_times, "error_calc", start)
    return result


TILE_LAYOUTS = {"small": (256, 256), "four": (1088, 1936)}


def resolve_tile_layout(layout: str, width: int, height: int) -> tuple:
    """Map --tile-layout to tifffile (length, width); four is 3856x2170-only.

    Four 1936x1088 tiles (2x2 grid) instead of the reference 1928x1088:
    tifffile requires tile dimensions in multiples of 16 and 1928 is not.
    Same hypothesis under test (144 codec calls down to four).
    """
    if layout == "small":
        return TILE_LAYOUTS["small"]
    if layout == "four":
        if width != 3856 or height != 2170:
            raise ValueError(f"--tile-layout four supports 3856x2170 frames only, not {width}x{height}.")
        return TILE_LAYOUTS["four"]
    raise ValueError("--tile-layout must be small or four.")


# ---------------------------------------------------------------------------
# One-pass lossy fast path (Phase 2 of the fast-path-v2 plan).
#
# The legacy path builds full-frame arrays repeatedly: linearize for repair,
# linearize again in prepare_raster, then index the inverse companding LUT.
# The fast path composes source_linear_lut and output_encode_lut once into
# direct_lut[256], repairs only mapped neighborhoods from the source byte
# view, and allocates the expected linear raster only when verification
# needs it. Pixel values are identical to the legacy path by construction
# (same composed indexing, same nearest-code rule); regression tests prove it.
# ---------------------------------------------------------------------------

FAST_CACHE: dict = {}


def get_fast_luts(numpy, source_lut, log_base: float, black_protect: int):
    """Cached (decode_lut, encode_lut, direct_lut, source_inverse) bundle.

    Keyed by source LUT bytes + companding settings; still validated per
    frame by the caller, so a cache entry can never mask a mismatch.
    """
    key = (bytes(numpy.asarray(source_lut, dtype=numpy.uint16)), float(log_base), int(black_protect))
    cached = FAST_CACHE.get(key)
    if cached is None:
        decode_lut = log_linearization_table(numpy, log_base, black_protect)
        encode_lut = inverse_linearization_table(numpy, decode_lut)
        source_table = numpy.asarray(source_lut, dtype=numpy.uint16)
        direct_lut = encode_lut[source_table]
        source_inverse = inverse_linearization_table(numpy, source_table)
        cached = {"decode": decode_lut, "encode": encode_lut, "direct": direct_lut,
                  "source_table": source_table, "source_inverse": source_inverse}
        FAST_CACHE[key] = cached
    return cached


def precompute_repair_plan(defect_map: dict, width: int, height: int):
    """Precompute valid neighbor/pair coordinates per mapped site (once per geometry).

    Same pools as repair_defects: 9x9 same-color inner pool, ±6 outer ring,
    adjacent directional pairs; mapped sites excluded everywhere. Raises on
    out-of-bounds coordinates (fail closed).
    """
    candidates = sorted(defect_map["candidates"], key=lambda site: int(site.get("correction_pass", 1)))
    defect_set = {(entry["x"], entry["y"]) for entry in candidates}
    pattern_name = defect_map["cfa"]
    plans = []
    for entry in candidates:
        x, y = entry["x"], entry["y"]
        if not (0 <= x < width and 0 <= y < height):
            raise ValueError(f"Repair plan coordinate ({x}, {y}) outside {width}x{height}.")
        color = cfa_color_at(pattern_name, x, y)
        inner = [(x + dx, y + dy) for dy in range(-4, 5, 2) for dx in range(-4, 5, 2)
                 if not (dx == 0 and dy == 0)
                 and 0 <= x + dx < width and 0 <= y + dy < height
                 and (x + dx, y + dy) not in defect_set]
        step = OUTER_RING_STEP
        outer = [(x + dx, y + dy) for dy in range(-step, step + 1, 2) for dx in range(-step, step + 1, 2)
                 if max(abs(dx), abs(dy)) == step
                 and 0 <= x + dx < width and 0 <= y + dy < height
                 and (x + dx, y + dy) not in defect_set]
        pairs = {}
        for direction, (first, second) in PAIR_OFFSETS.items():
            first_pos = (x + first[0], y + first[1])
            second_pos = (x + second[0], y + second[1])
            if first_pos[0] < 0 or first_pos[0] >= width or first_pos[1] < 0 or first_pos[1] >= height:
                continue
            if second_pos[0] < 0 or second_pos[0] >= width or second_pos[1] < 0 or second_pos[1] >= height:
                continue
            pairs[direction] = (first_pos, second_pos)
        plan_item = {"x": x, "y": y, "color": color, "inner": inner, "outer": outer, "pairs": pairs, "correction_pass": int(entry.get("correction_pass", 1))}
        if "gain" in entry:
            plan_item["gain"] = float(entry["gain"])
            plan_item["method"] = defect_map.get("repair_model", "calibrated_gain")
            plan_item["black_level_stored"] = float(defect_map.get("black_level_stored", 0.0))
            plan_item["stored_limit"] = int((1 << int(defect_map.get("bits", 8))) - 1)
        plans.append(plan_item)
    return plans


def fast_repair_frame(numpy, stored_view, width: int, height: int, source_table, source_inverse, plans: list):
    """Repair mapped sites reading only neighborhoods from the source byte view.

    Same directional-pair/spill-guard logic, method names, and nearest-code
    rule as repair_defects. Returns (patched_stored_dict, records, skipped)
    where patched maps flat pixel offsets to replacement stored codes; only
    mapped sites may appear. Gain-path repairs honour the caller-supplied
    black level / stored-code limit (8-bit default preserves legacy
    behavior) and skip no-ops like the slow path.
    """
    linear_at = source_table
    records: list[dict] = []
    skipped: list[dict] = []
    patched: dict = {}
    for plan in plans:
        x, y = plan["x"], plan["y"]
        color = plan["color"]
        if "gain" in plan:
            gain = plan["gain"]
            original_stored = int(stored_view[y, x])
            black = float(plan.get("black_level_stored", 0.0))
            limit = int(plan.get("stored_limit", 255))
            replacement_stored = int(numpy.clip(numpy.rint(black + (original_stored - black) / gain), 0, limit))
            if replacement_stored == original_stored:
                continue
            patched[y * width + x] = replacement_stored
            records.append({
                "x": x, "y": y, "color": color,
                "original_stored": original_stored, "original_linear": int(linear_at[original_stored]),
                "replacement_stored": replacement_stored, "replacement_linear": int(source_table[replacement_stored]),
                "method": plan.get("method", "calibrated_gain"), "gain": gain,
                "correction_pass": plan.get("correction_pass", 1),
            })
            continue
        neighbors = [(nx, ny) for nx, ny in plan["inner"]]
        if len(neighbors) < REPAIR_MIN_NEIGHBORS:
            skipped.append({"x": x, "y": y, "color": color, "reason": "too few valid same-color neighbors"})
            continue
        get = lambda nx, ny: int(linear_at[int(stored_view[ny, nx])])
        best_pair = None
        for direction, (first_pos, second_pos) in plan["pairs"].items():
            if first_pos not in neighbors or second_pos not in neighbors:
                continue
            first_val = get(*first_pos)
            second_val = get(*second_pos)
            gap = abs(first_val - second_val)
            if best_pair is None or gap < best_pair[0]:
                best_pair = (gap, direction, first_val, second_val)
        outer_vals = None
        if len(plan["outer"]) >= REPAIR_MIN_NEIGHBORS:
            outer_pool = sorted(get(nx, ny) for nx, ny in plan["outer"])
            outer_vals = outer_pool[len(outer_pool) // 2]
        if best_pair is not None and best_pair[0] <= PAIR_AGREE_TOL:
            _, direction, first_val, second_val = best_pair
            corrected = (first_val + second_val + 1) // 2
            method = f"directional_pair:{direction}"
            agree_gap = best_pair[0]
            median_inner = None
            median_outer = outer_vals
            if outer_vals is not None and outer_vals < corrected:
                corrected = outer_vals
                method += "+spillcap"
        else:
            pool = sorted(get(nx, ny) for nx, ny in neighbors)
            median_inner = pool[len(pool) // 2]
            median_outer = outer_vals
            if outer_vals is not None:
                corrected = min(median_inner, outer_vals)
                method = "median_spillguard"
            else:
                corrected = median_inner
                method = "median9x9"
            agree_gap = None
        table_min, table_max = int(source_table.min()), int(source_table.max())
        if not table_min <= corrected <= table_max:
            skipped.append({"x": x, "y": y, "color": color, "reason": "corrected value outside LinearizationTable range"})
            continue
        original_stored = int(stored_view[y, x])
        original_linear = int(linear_at[original_stored])
        replacement_stored = int(source_inverse[corrected])
        replacement_linear = int(source_table[replacement_stored])
        patched[y * width + x] = replacement_stored
        records.append({
            "x": x, "y": y, "color": color,
            "original_stored": original_stored, "original_linear": original_linear,
            "replacement_stored": replacement_stored, "replacement_linear": replacement_linear,
            "method": method, "neighbor_count": len(neighbors), "pair_agreement_gap": agree_gap,
            "neighbor_count_outer": len(plan["outer"]), "median_inner": median_inner, "median_outer": median_outer,
        })
    return patched, records, skipped


def compress_frame(source: Path, destination: Path, output_bits: int | None = None, mode: str = "lossless", quality: int = 85, companding: str = "log", log_base: float = DEFAULT_LOG_BASE, black_protect: int = 256, defect_map: dict | None = None, defect_map_name: str = "defect map", defect_records: list | None = None, tile: tuple = (256, 256), stage_times: dict | None = None, tile_layout: str = "small", recording_mode: str | None = None) -> tuple[int, int]:
    mode = canonical_mode(mode)
    start = _stage_begin(stage_times)
    ensure_vendor()
    import imagecodecs
    import numpy
    import tifffile

    if (defect_map is not None and defect_map.get("format") == "canonical_csv"
            and defect_map.get("repair_model") not in ("region_median_gain", "blended_region_gain")):
        defect_map = prepare_region_repair_map([source], defect_map)
    with tifffile.TiffFile(source) as input_tiff:
        page = input_tiff.pages[0]
        width = int(page.tags[TAG_IMAGE_WIDTH].value)
        height = int(page.tags[TAG_IMAGE_LENGTH].value)
        bits, linearization = source_linear_table(numpy, page)
        frame_map = ({**defect_map,
                      "black_level_stored": stored_black_level(numpy, page, bits, linearization),
                      "white_level_stored": stored_white_level(page, bits),
                      "_frame_name": source.name} if defect_map is not None else None)
        output_bits = resolve_output_bits(output_bits, bits, mode)
        stored = read_source_stored(numpy, page)
        input_bytes = sum(int(count) for count in page.databytecounts)
        source_ranges = {code: (page.tags[code].dtype, page.tags[code].value) for code in
                         (TAG_BLACK_LEVEL_REPEAT, TAG_BLACK_LEVEL, TAG_BLACK_DELTA_H,
                          TAG_BLACK_DELTA_V, TAG_WHITE_LEVEL) if code in page.tags}
        if int(page.tags[TAG_COMPRESSION].value) != 1:
            raise ValueError("Input must be an uncompressed DNG frame.")
        if page.tags.get(TAG_SAMPLES_PER_PIXEL, None) is not None:
            try:
                spp = int(page.tags[TAG_SAMPLES_PER_PIXEL].value)
            except Exception:
                spp = 1
            if spp != 1:
                raise ValueError("This app supports one-channel Bayer frames only.")
    _stage_end(stage_times, "read_parse", start)
    if tile_layout != "small":
        tile = resolve_tile_layout(tile_layout, width, height)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing file: {destination}")

    # TIFF compression code 7 is JPEG. SOF3/LJPEG only for the lossless path;
    # the lossy path must keep the native DCT JPEG encoder. The registry is
    # process-global, so set it explicitly on every call (a previous lossless
    # call in the same process must not leak into a later lossy one).
    _set_codec7(tifffile, mode)
    image_tags_written_by_tifffile = set(IMAGE_TAGS)
    with tifffile.TiffFile(source) as input_tiff:
        page = input_tiff.pages[0]
        if defect_map is not None:
            validate_defect_map(defect_map, page, width, height, defect_map_name)
        extra_tags = add_recording_mode_metadata(
            collect_extra_tags(page, mode, output_bits, image_tags_written_by_tifffile),
            defect_map, recording_mode)
    if defect_map is not None:
        if defect_map.get("test_only") and not getattr(compress_frame, "_test_only_noted", False):
            compress_frame._test_only_noted = True
            print("NOTE: test-only defect map - output requires visual approval before any production use.", flush=True)
        start = _stage_begin(stage_times)
        stored, frame_repairs, frame_skipped = repair_stored_array(numpy, stored, width, height, linearization, frame_map)
        _stage_end(stage_times, "linearize_repair", start)
        if defect_records is not None:
            defect_records.append({"source": source.name, "repairs": frame_repairs, "skipped": frame_skipped})
    start = _stage_begin(stage_times)
    raster, output_bits, output_linearization, _ = prepare_raster_from_stored(
        numpy, stored, linearization, mode, output_bits, companding, log_base, black_protect)
    extra_tags.extend(normalise_range_tags(source_ranges, bits, linearization,
                                           output_bits, output_linearization, numpy))
    _stage_end(stage_times, "compand", start)
    start = _stage_begin(stage_times)
    write_dng(tifffile, destination, raster, output_bits, mode, quality, tile, extra_tags, output_linearization)
    _stage_end(stage_times, "encode_write", start)
    return input_bytes, destination.stat().st_size


VERIFY_MODES = ("full", "sampled", "checksum", "none")


def resolve_verify_mode(explicit: str | None, legacy_flag: bool) -> tuple[str, bool]:
    """Effective verification mode. Returns (mode, explicit_none_warning).

    Default preserves legacy behavior: --verify-native means full, otherwise
    silent none. An explicit --verify-mode none prints a warning at the call
    site. A checksum proves storage consistency, never image semantics.
    """
    if explicit is not None:
        if explicit not in VERIFY_MODES:
            raise ValueError("--verify-mode must be full, sampled, checksum, or none.")
        return explicit, explicit == "none"
    if legacy_flag:
        return "full", False
    return "none", False


def sampled_frame_indices(total: int, every: int) -> set:
    """Frames getting native decode under sampled verification."""
    if every < 1:
        raise ValueError("--verify-every must be >= 1.")
    selected = {0, total // 2, total - 1}
    selected.update(range(0, total, every))
    return {index for index in selected if 0 <= index < total}


def file_sha256(path: Path) -> str:
    import hashlib as hashlib_module

    digest = hashlib_module.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums_manifest(destination_root: Path, entries: list) -> Path:
    """Write checksums.sha256 (DNG outputs only) sorted by relative path."""
    manifest = destination_root / "checksums.sha256"
    partial = manifest.with_name(manifest.name + ".partial")
    with partial.open("w", encoding="utf-8") as stream:
        for relpath, checksum in sorted(entries):
            # Always posix separators: the verifier keys on relative posix paths,
            # and OS-native separators made nested output unverifiable on Windows.
            rel = Path(relpath).as_posix() if not isinstance(relpath, str) else relpath.replace("\\", "/")
            stream.write(f"{checksum}  {rel}\n")
    partial.replace(manifest)
    return manifest


def structural_seed(rel_names_sizes: list) -> int:
    """Deterministic seed from stable clip information (sorted names + sizes)."""
    import hashlib as hashlib_module

    digest = hashlib_module.sha256("\n".join(rel_names_sizes).encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def select_verification_set(total: int, every: int, warned: set, seed: int | None) -> dict:
    """Sampled native-decode selection: fixed set + deterministic extras + warned.

    Returns {frame_index: reason}. Extras (3 pseudo-random frames) apply to
    clips with more than 16 frames; the same clip always selects the same
    extra frames for a given seed.
    """
    if every < 1:
        raise ValueError("--verify-every must be >= 1.")
    selected: dict[int, str] = {}
    for index in sorted({0, total // 2, total - 1} | set(range(0, total, every))):
        if 0 <= index < total:
            selected[index] = "fixed(first/middle/last/Nth)"
    if total > 16 and seed is not None:
        import random as random_module

        rng = random_module.Random(seed)
        pool = [i for i in range(total) if i not in selected]
        for index in sorted(rng.sample(pool, min(3, len(pool)))):
            selected[index] = "pseudo-random"
    for index in sorted(warned):
        if 0 <= index < total:
            selected[index] = "warning"
    return selected


def check_frame_structure(path: Path, expected_width: int, expected_height: int, expected_bits: int = 12) -> dict:
    """Cheap all-frame structural validation of one output DNG (no pixel decode).

    Raises RuntimeError describing the first defect found.
    """
    import tifffile as tifffile_module

    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError(f"Structural check failed for {path.name}: file is empty.")
    try:
        with tifffile_module.TiffFile(path) as document:
            if not document.pages:
                raise RuntimeError(f"Structural check failed for {path.name}: no TIFF pages.")
            page = document.pages[0]
            tags = page.tags
            try:
                width = int(tags[256].value)
                height = int(tags[257].value)
                bits = int(tags[258].value)
                compression = int(tags[259].value)
            except KeyError as error:
                raise RuntimeError(f"Structural check failed for {path.name}: missing required tag {error}.")
            if (width, height) != (expected_width, expected_height):
                raise RuntimeError(f"Structural check failed for {path.name}: "
                                   f"dimensions {width}x{height} != expected {expected_width}x{expected_height}.")
            if bits != expected_bits:
                raise RuntimeError(f"Structural check failed for {path.name}: BitsPerSample {bits} != {expected_bits}.")
            if compression != 7:
                raise RuntimeError(f"Structural check failed for {path.name}: compression {compression} != JPEG (7).")
            spp_tag = tags.get(277)
            if spp_tag is not None:
                try:
                    if int(spp_tag.value) != 1:
                        raise RuntimeError(f"Structural check failed for {path.name}: SamplesPerPixel != 1.")
                except (TypeError, ValueError):
                    raise RuntimeError(f"Structural check failed for {path.name}: unreadable SamplesPerPixel.")
            try:
                pattern = bytes(tags[33422].value)[:4]
            except KeyError:
                raise RuntimeError(f"Structural check failed for {path.name}: missing CFA tag 33422.")
            if pattern != b"\x00\x01\x01\x02":
                raise RuntimeError(f"Structural check failed for {path.name}: CFA is not RGGB.")
            if 322 not in tags or 323 not in tags or not page.dataoffsets:
                raise RuntimeError(f"Structural check failed for {path.name}: primary image is not tiled.")
            for offset, count in zip(page.dataoffsets, page.databytecounts):
                if offset + count > size:
                    raise RuntimeError(f"Structural check failed for {path.name}: tile data extends past end of file (truncated?).")
            lut_tag = tags.get(50712)
            if expected_bits == 12:
                if lut_tag is None:
                    raise RuntimeError(f"Structural check failed for {path.name}: missing LinearizationTable.")
                lut = lut_tag.value
                if len(lut) != (1 << expected_bits):
                    raise RuntimeError(f"Structural check failed for {path.name}: LinearizationTable has {len(lut)} entries.")
            elif lut_tag is not None and len(lut_tag.value) != (1 << expected_bits):
                raise RuntimeError(f"Structural check failed for {path.name}: LinearizationTable has {len(lut_tag.value)} entries.")
            white = _range_values(tags.get(TAG_WHITE_LEVEL), 1)
            if not white:
                raise RuntimeError(f"Structural check failed for {path.name}: missing WhiteLevel.")
            # BlackLevel/WhiteLevel belong to the camera-linear domain.  For
            # an 8-bit Sigma source that domain is defined by its 256-entry
            # LinearizationTable and reaches 4095, not 255 stored-code units.
            expected_white = (max(lut_tag.value) if lut_tag is not None
                              else (1 << expected_bits) - 1)
            if round(white[0]) != expected_white:
                raise RuntimeError(f"Structural check failed for {path.name}: WhiteLevel {white[0]} != output maximum {expected_white}.")
            black_tag = tags.get(TAG_BLACK_LEVEL)
            if black_tag is not None:
                repeat_tag = tags.get(TAG_BLACK_LEVEL_REPEAT)
                repeat = tuple(int(v) for v in repeat_tag.value[:2]) if repeat_tag is not None else (2, 2)
                black_count = max(1, repeat[0] * repeat[1])
                black = _range_values(black_tag, black_count)
                if len(black) != black_count or any(value < 0 or value >= white[0] for value in black):
                    raise RuntimeError(f"Structural check failed for {path.name}: BlackLevel is outside 0..WhiteLevel.")
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(f"Structural check failed for {path.name}: {type(error).__name__}: {error}")
    return {"name": path.name, "bytes": size, "width": width, "height": height}


def settings_fingerprint(output_bits: int, mode: str, quality: int, companding: str,
                         log_base: float, black_protect: int, repair_profile: str | None = None,
                         tile_layout: str = "small", recording_mode: str | None = None) -> str:
    """Stable hash of the encoding settings (B10).

    Stored in the source manifest and verification report; resume refuses
    changed settings before it reuses output.
    """
    import hashlib as _hash

    settings = ("cfa-paired-v1", output_bits, mode, quality, companding,
                float(log_base), int(black_protect), tile_layout, repair_profile, recording_mode)
    material = repr(settings)
    return _hash.sha256(material.encode("utf-8")).hexdigest()[:16]


def repair_fingerprint(defect_map: dict | None) -> str | None:
    if defect_map is None:
        return None
    from red_edge_reconstruction import RED_EDGE_VERSION
    return f"{defect_map.get('repair_profile', '')}|{defect_map.get('red_correction', 'edge-aware')}|{RED_EDGE_VERSION}"


def check_resume_settings(destination_root: Path, fingerprint: str,
                          source_root: Path, dng_files: list[Path]) -> None:
    """Record source identity before output; refuse unsafe interrupted resumes."""
    import json as _json

    manifest = destination_root / "source-manifest.json"
    current = {"settings_fingerprint": fingerprint,
               "sources": {p.relative_to(source_root).as_posix(): file_sha256(p) for p in dng_files}}
    if manifest.exists():
        try:
            prior = _json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError(f"Resume refused: source manifest cannot be read: {error}") from error
        if prior != current:
            raise ValueError("Resume refused: source frames or encoding settings changed; use a new output folder.")
        return
    if destination_root.is_dir() and any(p.suffix.lower() == ".dng" for p in destination_root.rglob("*")):
        raise ValueError("Resume refused: existing DNGs have no source manifest; use a new output folder.")
    destination_root.mkdir(parents=True, exist_ok=True)
    partial = manifest.with_name(manifest.name + ".partial")
    partial.write_text(_json.dumps(current, indent=2) + "\n", encoding="utf-8")
    partial.replace(manifest)


def should_resume_skip(path: Path, width: int, height: int, bits: int) -> bool:
    """True when an existing output is structurally valid and safe to skip (N2).

    Returns False when the file is missing, invalid, or incomplete — the caller
    must then refuse (never silently overwrite).
    """
    try:
        check_frame_structure(path, width, height, bits)
        return True
    except Exception:
        return False


def clear_stale_partials(root: Path) -> int:
    """Delete leftover *.partial files under root (never final artifacts).

    Returns the count removed. Only used in resume mode after the destination
    guard has passed.
    """
    if not root.is_dir():
        return 0
    removed = 0
    for partial in sorted(root.rglob("*.partial")):
        try:
            if partial.is_file():
                partial.unlink()
                removed += 1
        except OSError:
            pass
    return removed


def write_verification_report(report_path: Path, payload: dict) -> None:
    import json as json_module

    partial = report_path.with_name(report_path.name + ".partial")
    with partial.open("w", encoding="utf-8") as stream:
        json_module.dump(payload, stream, indent=2)
        stream.write("\n")
    partial.replace(report_path)


AUDIO_SUFFIXES = {".wav", ".wave", ".aif", ".aiff", ".flac", ".m4a", ".aac", ".mp3"}


def companion_files(source_root: Path, copy_non_dng: bool, direct_only: bool = False) -> list[Path]:
    """Return companions to copy, always including associated audio."""
    all_files = [p for p in source_root.iterdir() if p.is_file()] if direct_only else [
        p for p in source_root.rglob("*") if p.is_file()]
    audio = [p for p in all_files if p.suffix.lower() in AUDIO_SUFFIXES]
    if audio and not copy_non_dng:
        raise ValueError("Associated audio is present; audio copying cannot be disabled for a complete job.")
    if copy_non_dng:
        return [p for p in all_files if p.suffix.lower() != ".dng"]
    return audio


def source_raw_bytes(path: Path) -> int:
    """Return uncompressed stored RAW raster bytes, not DNG container bytes."""
    ensure_vendor()
    import tifffile as _tf
    with _tf.TiffFile(path) as document:
        page = document.pages[0]
        if int(page.compression) != 1:
            raise ValueError(f"Input must be an uncompressed DNG frame: {path}")
        return sum(int(count) for count in page.databytecounts)


def source_bit_depth(path: Path) -> int:
    ensure_vendor()
    import tifffile as _tf
    with _tf.TiffFile(path) as document:
        return source_bits_from_page(document.pages[0])


def _metadata_value(value):
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, bytes):
        return value.rstrip(b"\0")
    if isinstance(value, (list, tuple)):
        return tuple(_metadata_value(item) for item in value)
    return value


def validate_metadata(source: Path, output: Path) -> dict:
    """Validate the DNG metadata needed for Resolve/RAW interpretation."""
    ensure_vendor()
    import tifffile as _tf
    # Image layout and range tags are intentionally remapped by the encoder;
    # the rest of these tags must survive byte-for-byte in meaning.
    required = (TAG_IMAGE_WIDTH, TAG_IMAGE_LENGTH, 33422, 50708, 272, 274,
                50706, 50711, 50728, 50729, 50730)
    errors = []
    checked = []
    with _tf.TiffFile(source) as src_doc, _tf.TiffFile(output) as out_doc:
        src_tags, out_tags = src_doc.pages[0].tags, out_doc.pages[0].tags
        for code in required:
            if code not in src_tags:
                continue
            checked.append(code)
            if code not in out_tags:
                errors.append(f"missing tag {code}")
                continue
            left = _metadata_value(src_tags[code].value)
            right = _metadata_value(out_tags[code].value)
            if left != right:
                errors.append(f"tag {code} changed")
    return {"ok": not errors, "checked_tags": checked, "errors": errors}


def recording_configuration(width: int, height: int, defect_map: dict | None = None,
                            source_root: Path | None = None,
                            recording_mode: str | None = None) -> str:
    """Report a known configuration, using only explicit labels as a hint."""
    resolution = "4K" if width >= 3000 else "1080p" if width >= 1500 else f"{width}x{height}"
    selected_mode = validate_recording_mode(recording_mode, defect_map)
    if selected_mode:
        return f"{resolution} {selected_mode}"
    if defect_map is not None:
        sensor_mode = (defect_map.get("recording_configuration")
                       or defect_map.get("sensor_mode") or defect_map.get("mode"))
        if sensor_mode:
            return str(sensor_mode)
        map_mode = canonical_map_mode(defect_map)
        if map_mode:
            return f"{resolution} {map_mode}"
    label = source_root.name.lower() if source_root is not None else ""
    if resolution == "1080p" and label.endswith("1080c"):
        return "1080p crop (folder-label hint; metadata does not prove crop)"
    if resolution == "1080p" and label.endswith("1080"):
        return "1080p normal (folder-label hint; metadata does not prove normal)"
    if resolution == "4K" and label.endswith("4kc"):
        return "4K crop (folder-label hint; metadata does not prove crop)"
    if resolution == "4K" and label.endswith("4k"):
        return "4K normal (folder-label hint; metadata does not prove normal)"
    return f"{resolution} unknown (normal/crop not confidently determined)"


def update_report_statistics(report_path: Path, stats: dict) -> None:
    """Atomically add job-level statistics after companions are copied."""
    import json as _json
    try:
        payload = _json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    except Exception:
        payload = {}
    payload.update(stats)
    write_verification_report(report_path, payload)


def ensure_vendor() -> None:
    """Add the bundled vendor directory to sys.path once (no per-frame growth)."""
    vendor = str(Path(__file__).with_name("vendor"))
    if vendor not in sys.path:
        sys.path.insert(0, vendor)


def print_stage_summary(stage_times: dict) -> None:
    """Print mean/median/p90/total ms per stage (benchmark use)."""
    import statistics as statistics_module

    print("Stage timings (ms):", flush=True)
    for stage in STAGE_NAMES:
        values = stage_times.get(stage)
        if not values:
            continue
        print(f"  {stage:16s} n={len(values):4d} mean={statistics_module.mean(values):8.1f} "
              f"median={statistics_module.median(values):8.1f} "
              f"p90={statistics_module.quantiles(values, n=10)[-1] if len(values) > 10 else max(values):8.1f} "
              f"total={sum(values) / 1000.0:8.1f} s", flush=True)


PROPOSED_GUARDRAILS = {
    # Candidate per-frame guardrails derived from the frozen 32-frame
    # full-verification fixtures (quality mean<=1.24 p99<=9 p999<=22;
    # compact mean<=2.73 p99<=19 p999<=49; maxima are repair-driven).
    # Recorded as warnings only; not enforcement defaults.
    "quality": {"p99": 12.0, "p999": 30.0},
    "compact": {"p99": 25.0, "p999": 65.0},
}


def guardrail_for_quality(quality: int) -> dict:
    """Provisional guardrail set by JPEG quality setting."""
    return PROPOSED_GUARDRAILS["quality"] if quality >= 75 else PROPOSED_GUARDRAILS["compact"]


def guardrail_warnings(filename: str, stats: dict, guard: dict) -> list:
    """Compare extended error stats against provisional guardrails (warnings only)."""
    warnings = []
    for key in ("p99", "p999"):
        if key in stats and stats[key] > guard[key]:
            warnings.append(f"{filename}: {key} {stats[key]:.1f} above provisional guardrail {guard[key]:.1f}")
    return warnings


def check_guardrails(filename: str, stats: dict, guard: dict) -> list:
    """Shared enforceable guardrail check (B1/I1).

    Returns warning strings (same content as guardrail_warnings). Callers must
    surface them loudly (stdout ERROR + report breach flag) instead of
    discarding them. Strict fail-closed behavior is opt-in via
    --strict-guardrails; by default breaches are loud warnings so the existing
    Quality default keeps running while the spec mismatch is visible.
    """
    return guardrail_warnings(filename, stats, guard)


def summarize_quality_metrics(entries: list[dict]) -> dict:
    """Summarize verified-frame error, shadow, and highlight measurements."""
    stats = [entry.get("error", {}) for entry in entries if entry.get("error")]
    summary: dict = {"verified_frames": len(stats)}
    for key in ("mean", "max", "p99", "p999", "shadow_mean", "shadow_max",
                "highlight_mean", "highlight_max"):
        values = [float(item[key]) for item in stats if key in item]
        if not values:
            continue
        summary[key] = {
            "average": round(sum(values) / len(values), 4),
            "maximum": round(max(values), 4),
        }
    return summary


def validate_numeric_args(log_base: float, black_protect: int, verify_every: int,
                           preview_quality: int) -> None:
    """Eager validation for args argparse leaves unchecked (P1)."""
    if not log_base > 1.0:
        raise ValueError("--log-base must be greater than 1.")
    if not 0 <= black_protect <= 4094:
        raise ValueError("--black-protect must be from 0 to 4094.")
    if verify_every < 1:
        raise ValueError("--verify-every must be >= 1.")
    if not 1 <= preview_quality <= 100:
        raise ValueError("--preview-quality must be 1..100.")


def _apply_expected_repairs(numpy, expected, repairs, linearization=None):
    """Apply recorded stored-code replacements to the verification reference."""
    if not repairs:
        return expected
    expected = numpy.array(expected, copy=True)
    for repair in repairs:
        x, y = int(repair["x"]), int(repair["y"])
        replacement = int(repair["replacement_stored"])
        if not (0 <= x < expected.shape[1] and 0 <= y < expected.shape[0]):
            raise ValueError(f"Repair verification coordinate is outside the frame: {x},{y}")
        if linearization is not None:
            if not 0 <= replacement < len(linearization):
                raise ValueError(f"Repair verification code is outside the LinearizationTable: {replacement}")
            replacement = int(linearization[replacement])
        expected[y, x] = replacement
    return expected


def verify_native(source: Path, compressed: Path, output_bits: int = 8, mode: str = "lossless", stage_times: dict | None = None, expected=None, extended: bool = False, repairs: list[dict] | None = None):
    """Legacy native verification (reparses the source unless expected is given).

    Passing an already-computed expected linear raster skips the source
    re-read; the fused worker path uses that. Default behavior is unchanged.
    With extended=True returns a dict with p99/p99.9 percentiles.
    """
    mode = canonical_mode(mode)
    start = _stage_begin(stage_times)
    ensure_vendor()
    import numpy
    import tifffile
    if expected is None:
        with tifffile.TiffFile(source) as input_tiff:
            page = input_tiff.pages[0]
            width = int(page.tags[TAG_IMAGE_WIDTH].value)
            height = int(page.tags[TAG_IMAGE_LENGTH].value)
            _, table = source_linear_table(numpy, page)
            stored = read_source_stored(numpy, page)
        if output_bits == 12 or mode_is_lossy(mode):
            expected = table[stored]
        else:
            expected = stored
        expected = _apply_expected_repairs(
            numpy, expected, repairs, table if output_bits == 12 or mode_is_lossy(mode) else None)
    else:
        width, height = expected.shape[1], expected.shape[0]
    _stage_end(stage_times, "reopen_decode", start)
    start = _stage_begin(stage_times)
    actual = tifffile.imread(compressed)
    if output_bits == 12 or mode_is_lossy(mode):
        with tifffile.TiffFile(compressed) as output_tiff:
            lut_tag = output_tiff.pages[0].tags.get(TAG_LINEARIZATION_TABLE)
            if lut_tag is None:
                raise RuntimeError(f"Native verification failed for {compressed.name}: missing LinearizationTable.")
            output_linearization = numpy.asarray(lut_tag.value, dtype=numpy.uint16)
        if len(output_linearization) == 0:
            raise RuntimeError(f"Native verification failed for {compressed.name}: missing LinearizationTable.")
        actual = output_linearization[actual]
    _stage_end(stage_times, "reopen_decode", start)
    if actual.shape != expected.shape:
        raise RuntimeError(f"Decoded frame shape differs from source: {actual.shape} != {expected.shape}")
    if mode == "lossless" and not numpy.array_equal(actual, expected):
        raise RuntimeError(
            "Decoded pixels differ from source: "
            f"source={hashlib.sha256(expected.tobytes()).hexdigest()} decoded={hashlib.sha256(actual.tobytes()).hexdigest()} "
            f"size={width}x{height}"
        )
    start = _stage_begin(stage_times)
    difference = numpy.abs(actual.astype(numpy.int32) - expected.astype(numpy.int32))
    flat = difference.ravel().astype(numpy.float64)
    result = {"mean": float(flat.mean()), "max": int(flat.max()),
              "p99": float(numpy.percentile(flat, 99)),
              "p999": float(numpy.percentile(flat, 99.9))}
    for label, mask in (("shadow", expected <= 512), ("highlight", expected >= 3584)):
        if bool(mask.any()):
            region = difference[mask].astype(numpy.float64)
            result[f"{label}_mean"] = float(region.mean())
            result[f"{label}_max"] = int(region.max())
    _stage_end(stage_times, "error_calc", start)
    return result


PREVIEW_SIZES = {"720p": 1280, "480p": 854}
PREVIEW_JPEG_QUALITY = 60


def bayer_to_preview_rgb(numpy, linear: object, width: int, height: int, neutral_gains, max_width: int):
    """Fast preview-only Bayer conversion, downsampled as early as possible.

    Quarter-resolution R/G/B planes (G averaged), nearest-neighbor downsample
    to max_width, AsShotNeutral white balance, gamma 2.2. Identification
    only - never grading.
    """
    plane = linear.reshape(height, width)
    red = plane[0::2, 0::2]
    green = (plane[0::2, 1::2] + plane[1::2, 0::2]) * 0.5
    blue = plane[1::2, 1::2]
    step = max(1.0, red.shape[1] / max_width)
    target_w = min(max_width, red.shape[1])
    target_h = max(2, int(red.shape[0] / step) // 2 * 2)
    if target_w % 2:
        target_w -= 1
    xs = (numpy.arange(target_w) * step).astype(int)
    ys = (numpy.arange(target_h) * (red.shape[0] / target_h)).astype(int)
    rgb = numpy.stack([red[ys][:, xs], green[ys][:, xs], blue[ys][:, xs]], axis=-1)
    gains = numpy.asarray(neutral_gains, dtype=numpy.float32).reshape(3)
    rgb = numpy.clip(rgb / 4095.0, 0.0, 1.0) * gains
    rgb = numpy.clip(rgb, 0.0, 1.0) ** (1.0 / 2.2)
    return numpy.ascontiguousarray((rgb * 255.0 + 0.5).astype(numpy.uint8))


def generate_frame_preview(source: Path, previews_dir: Path, preview_mode: str, preview_quality: int = PREVIEW_JPEG_QUALITY, rel: str | None = None) -> int:
    """Write one sidecar preview JPEG named after the DNG frame. Returns bytes."""
    import sys as sys_module

    vendor = Path(__file__).with_name("vendor")
    if str(vendor) not in sys_module.path:
        sys_module.path.insert(0, str(vendor))
    import imagecodecs
    import numpy
    import tifffile

    max_width = PREVIEW_SIZES[preview_mode]
    with tifffile.TiffFile(source) as input_tiff:
        page = input_tiff.pages[0]
        _, table = source_linear_table(numpy, page)
        stored = read_source_stored(numpy, page).ravel()
        width, height = int(page.tags[TAG_IMAGE_WIDTH].value), int(page.tags[TAG_IMAGE_LENGTH].value)
        neutral_tag = page.tags.get(50728)
        if neutral_tag is None:
            raise RuntimeError(f"Preview failed for {source.name}: missing AsShotNeutral tag 50728.")
        raw_neutral = tuple(neutral_tag.value)
    linear = table[stored].astype(numpy.float32)
    gains = numpy.array([raw_neutral[0] / raw_neutral[1], raw_neutral[2] / raw_neutral[3],
                         raw_neutral[4] / raw_neutral[5]], dtype=numpy.float32)
    gains = gains / gains[1]
    rgb = bayer_to_preview_rgb(numpy, linear, width, height, gains, max_width)
    jpeg = imagecodecs.jpeg_encode(rgb, level=int(preview_quality), colorspace="RGB",
                                   outcolorspace="YCbCr", subsampling=(2, 2), optimize=False)
    if rel is not None:
        destination = previews_dir / Path(rel).with_suffix(".jpg")
    else:
        destination = previews_dir / (source.stem + ".jpg")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite existing preview: {destination}")
    destination.write_bytes(bytes(jpeg))
    return destination.stat().st_size


def write_defect_report(report_path: Path, defect_map: dict, defect_map_name: str, records: list) -> None:
    import json as json_module
    from red_edge_reconstruction import RED_EDGE_VERSION

    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool": "cdng_lossless_mvp defect repair",
        "repair_profile": defect_map.get("repair_profile", REPAIR_PROFILE),
        "red_correction": defect_map.get("red_correction", "edge-aware"),
        "red_repair_version": RED_EDGE_VERSION if defect_map.get("red_correction", "edge-aware") == "edge-aware" else None,
        "defect_map_source": defect_map_name,
        "defect_map": defect_map,
        "frames": records,
    }
    partial = report_path.with_name(report_path.name + ".partial")
    with partial.open("w", encoding="utf-8") as stream:
        json_module.dump(payload, stream, indent=2)
        stream.write("\n")
    partial.replace(report_path)


def disk_space_ok(free_bytes: int, needed_bytes: int, allow_low_space: bool = False) -> bool:
    return free_bytes >= int(needed_bytes * 1.2) or allow_low_space


def free_bytes_for(path: Path) -> int:
    """Free space on the volume that will hold `path`.

    `Path("out").anchor` is the empty string on a relative path, and
    `shutil.disk_usage("")` raises FileNotFoundError, which the serial preflight
    re-raised and aborted the run on. Walk up to the nearest existing ancestor
    instead so a relative destination still gets a real margin check.
    """
    probe = path if path.is_absolute() else Path.cwd() / path
    try:
        probe = probe.resolve()
    except (OSError, RuntimeError):
        pass
    for candidate in (probe, *probe.parents):
        try:
            if candidate.is_dir():
                return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return shutil.disk_usage(Path.cwd().anchor or Path.cwd()).free


def guard_destination(source_root: Path, destination_root: Path, allow_existing_root: bool = False) -> None:
    """Fail closed before any output is written.

    The source is strictly read-only, so the output may not be the source and may
    not sit anywhere inside it. Unless allow_existing_root (resume mode), the
    output may also not already exist: the caller owns creating it, which keeps
    a stale or partially-written folder from being silently merged into.
    Raises ValueError or FileExistsError with a clear reason.
    """
    try:
        source = source_root.resolve()
        destination = destination_root.resolve()
    except OSError:
        source, destination = source_root, destination_root
    if source == destination:
        raise ValueError("Source and output folders must differ; refusing to convert a folder onto itself.")
    if destination.is_relative_to(source):
        raise ValueError(
            f"Output folder {destination} is inside the source folder {source}; "
            f"the source is read-only, refusing.")
    if source.is_relative_to(destination):
        raise ValueError(
            f"Source folder {source} is inside the output folder {destination}; "
            f"refusing to avoid recursive output.")
    if destination.exists():
        if not destination.is_dir():
            raise FileExistsError(f"Refusing to write over an existing file: {destination}")
        if not allow_existing_root:
            raise FileExistsError(f"Refusing to reuse existing output folder: {destination}")


def plan_root_companions(source_root: Path | None, destination_root: Path | None) -> list[Path]:
    if source_root is None and destination_root is None:
        return []
    if source_root is None or destination_root is None:
        raise ValueError("Both root companion folders must be provided.")
    source_root, destination_root = Path(source_root), Path(destination_root)
    if not source_root.is_dir():
        raise NotADirectoryError(f"Root companion source is not a folder: {source_root}")
    guard_destination(source_root, destination_root, allow_existing_root=True)
    pending = []
    for source in companion_files(source_root, True, direct_only=True):
        target = destination_root / source.name
        if target.exists():
            if target.is_file() and file_sha256(target) == file_sha256(source):
                continue
            raise FileExistsError(f"Refusing to overwrite different root companion: {target}")
        pending.append(source)
    return pending


def copy_root_companions(sources: list[Path], destination_root: Path) -> None:
    destination_root = Path(destination_root)
    if not sources:
        return
    destination_root.mkdir(parents=True, exist_ok=True)
    for source in sources:
        target = destination_root / source.name
        if target.exists():
            if target.is_file() and file_sha256(target) == file_sha256(source):
                continue
            raise FileExistsError(f"Refusing to overwrite different root companion: {target}")
        shutil.copy2(source, target)


def convert_tree(source_root: Path, destination_root: Path, output_bits: int | None, mode: str, quality: int, companding: str, log_base: float, black_protect: int, verify: bool, copy_non_dng: bool, defect_map: dict | None = None, defect_map_name: str = "defect map", defect_report: Path | None = None, preview: str = "none", preview_quality: int = PREVIEW_JPEG_QUALITY, timings: dict | None = None, error_stats: list | None = None, progress: bool = True, verify_mode: str | None = None, verify_every: int = 32, tile_layout: str = "small", stage_times: dict | None = None, strict_guardrails: bool = False, resume: bool = False, repair_maps: bool = False, roundtrip: bool = False, allow_low_space: bool = False, direct_only: bool = False, recording_mode: str | None = None, root_companions_source: Path | None = None, root_companions_destination: Path | None = None) -> None:
    import time as time_module

    job_start = time_module.perf_counter()
    mode = canonical_mode(mode)
    recording_mode = validate_recording_mode(recording_mode, defect_map)

    if preview not in ("none", "720p", "480p"):
        raise ValueError("--preview must be none, 720p, or 480p.")
    validate_numeric_args(log_base, black_protect, verify_every, preview_quality)
    if defect_map is not None and defect_report is None:
        raise ValueError("--defect-report is required when --defect-map is supplied.")
    # The bundled readers live in vendor/; the serial path imports tifffile here,
    # before any per-frame call would have run ensure_vendor().
    ensure_vendor()
    effective_mode, explicit_none = resolve_verify_mode(verify_mode, verify)
    if mode == "lossless" and effective_mode != "full":
        print("NOTE: Lossless mode requires full RAW verification; using full validation.", flush=True)
        effective_mode = "full"
    if explicit_none:
        print("WARNING: --verify-mode none skips all output validation (benchmark/emergency use only).", flush=True)
    tile = TILE_LAYOUTS.get(tile_layout)
    if tile is None:
        raise ValueError("--tile-layout must be small or four.")
    files = sorted(path for path in (source_root.iterdir() if direct_only else source_root.rglob("*")) if path.is_file())
    dng_files = [path for path in files if path.suffix.lower() == ".dng"]
    if not dng_files:
        raise ValueError("No DNG files found in the selected input folder.")
    # ---- parent preflight (parity with the parallel path): refuse before converting ----
    guard_destination(source_root, destination_root, allow_existing_root=resume)
    if not resume:
        for source in dng_files:
            if (destination_root / source.relative_to(source_root)).exists():
                raise FileExistsError(f"Refusing to overwrite existing file: {destination_root / source.relative_to(source_root)}")
    companions = companion_files(source_root, copy_non_dng, direct_only)
    if not resume:
        for source in companions:
            if (destination_root / source.relative_to(source_root)).exists():
                raise FileExistsError(f"Refusing to overwrite existing file: {destination_root / source.relative_to(source_root)}")
    root_companions = plan_root_companions(root_companions_source, root_companions_destination)
    expected_bytes = sum(p.stat().st_size for p in dng_files + companions + root_companions)
    free_bytes = free_bytes_for(destination_root)
    print(f"Disk check: {free_bytes / 2 ** 30:.1f} GiB free; estimated need "
          f"~{expected_bytes * 1.2 / 2 ** 30:.1f} GiB incl. 20% margin.", flush=True)
    if not disk_space_ok(free_bytes, expected_bytes, allow_low_space):
        raise OSError("Insufficient free space for estimated output plus 20% safety margin; refusing to start.")
    if free_bytes < int(expected_bytes * 1.2):
        print("WARNING: continuing despite the conservative space estimate; the destination may run out of space and leave partial output.", flush=True)
    seed = structural_seed(sorted(f"{p.relative_to(source_root).as_posix()}:{p.stat().st_size}" for p in dng_files))
    rel_list = [p.relative_to(source_root).as_posix() for p in dng_files]
    # Resume audit preservation (B9): reload the prior defect report so skipped
    # frames keep their repairs/skipped flags instead of reporting zeroes.
    prior_repairs: dict[str, dict] = {}
    if resume and defect_map is not None and defect_report is not None and defect_report.is_file():
        try:
            import json as _json
            for record in _json.loads(defect_report.read_text(encoding="utf-8"))["frames"]:
                prior_repairs[str(record.get("source", ""))] = record
            if progress:
                print(f"Resume: loaded prior defect report ({len(prior_repairs)} frame(s)).", flush=True)
        except Exception as error:
            print(f"WARNING: could not read prior defect report ({error}); resumed frames report no repairs.", flush=True)
    preload_warned = {i for i, rel in enumerate(rel_list) if prior_repairs.get(rel, {}).get("skipped")}
    selected: dict[int, str] = {}
    if effective_mode == "sampled":
        selected = select_verification_set(len(dng_files), verify_every, preload_warned, seed)
    native_set = set(selected)
    import tifffile as tifffile_module

    with tifffile_module.TiffFile(dng_files[0]) as first_frame:
        try:
            expected_width = int(first_frame.pages[0].tags[256].value)
            expected_height = int(first_frame.pages[0].tags[257].value)
            first_source_bits = source_bits_from_page(first_frame.pages[0])
        except KeyError as error:
            raise RuntimeError(f"First source frame is missing required tag {error}.")
    if defect_map is not None:
        defect_map = prepare_region_repair_map(dng_files, defect_map)
    output_bits = resolve_output_bits(output_bits, first_source_bits, mode)
    if tile_layout == "four" and (expected_width != 3856 or expected_height != 2170):
        raise ValueError(f"--tile-layout four supports 3856x2170 frames only, not {expected_width}x{expected_height}.")
    guard = guardrail_for_quality(quality)
    guardrail_breaches: list[str] = []
    roundtrip_entries: list[dict] = []
    fingerprint = settings_fingerprint(output_bits, mode, quality, companding, log_base, black_protect,
                                       repair_fingerprint(defect_map), tile_layout, recording_mode)
    check_resume_settings(destination_root, fingerprint, source_root, dng_files)
    destination_root.mkdir(parents=True, exist_ok=True)
    if root_companions:
        copy_root_companions(root_companions, root_companions_destination)
    if resume:
        cleared = clear_stale_partials(destination_root)
        if cleared and progress:
            print(f"Resume: cleared {cleared} stale .partial file(s).", flush=True)
    effective_out_bits = 12 if mode_is_lossy(mode) else output_bits
    checksum_entries: list = []
    verification_entries: list = []
    metadata_entries: list = []
    warned_names: list = []
    total_input = 0
    total_output = 0
    defect_records: list = []
    previews_dir = destination_root / "previews"
    for position, source in enumerate(dng_files):
        index = position + 1
        rel = source.relative_to(source_root).as_posix()
        destination = destination_root / source.relative_to(source_root)
        if resume and destination.exists():
            if not should_resume_skip(destination, expected_width, expected_height, effective_out_bits):
                raise FileExistsError(
                    f"Resume cannot reuse {destination}: structurally invalid or incomplete; "
                    f"clear the output folder and rerun without --resume.")
            resumed_metadata = validate_metadata(source, destination)
            metadata_entries.append({"file": rel, **resumed_metadata})
            if not resumed_metadata["ok"]:
                raise RuntimeError(f"Metadata validation failed for resumed {rel}: {resumed_metadata['errors']}")
            if progress:
                print(f"[{index}/{len(dng_files)}] {source.relative_to(source_root)} (resumed, skipped)", flush=True)
            skipped_bytes = destination.stat().st_size
            prior = prior_repairs.get(rel)
            if prior is not None:
                defect_records.append({"source": rel, "repairs": prior.get("repairs", []),
                                       "skipped": prior.get("skipped", False), "resumed": True})
            else:
                defect_records.append({"source": rel, "repairs": [], "skipped": False, "resumed": True})
            do_native = effective_mode == "full" or (effective_mode == "sampled" and position in native_set)
            if do_native:
                error_ext = verify_native(source, destination, output_bits, mode, None, None, True, defect_records[-1].get("repairs", []) if defect_records else None)
                breaches = check_guardrails(rel, error_ext, guard)
                if breaches:
                    guardrail_breaches.extend(breaches)
                    print(f"GUARDRAIL BREACH: {'; '.join(breaches)}", flush=True)
                verification_entries.append({"file": rel, "error": error_ext, "guardrail_warnings": breaches})
                if error_stats is not None:
                    error_stats.append((error_ext["mean"], error_ext["max"]))
                if roundtrip:
                    try:
                        rt = roundtrip_check(source, destination, mode=mode)
                    except RuntimeError as error:
                        raise RuntimeError(f"Round-trip check failed for {rel}: {error}")
                    roundtrip_entries.append({"file": rel, "roundtrip": rt})
                    if mode == "lossless" and rt["pass"] is False:
                        raise RuntimeError(f"Round-trip byte-exact claim violated for {rel}: {rt['detail']}")
            checksum_entries.append((str(destination.relative_to(destination_root)), file_sha256(destination)))
            total_input += source_raw_bytes(source)
            total_output += skipped_bytes
            continue
        if progress:
            print(f"[{index}/{len(dng_files)}] {source.relative_to(source_root)}", flush=True)
        frame_stages = {} if stage_times is not None else None
        start = time_module.perf_counter()
        original, output = compress_frame(source, destination, output_bits, mode, quality, companding, log_base, black_protect, defect_map, defect_map_name, defect_records, tile, frame_stages, tile_layout, recording_mode)
        if defect_records:
            defect_records[-1]["source"] = rel
        warned = bool(defect_records and defect_records[-1]["skipped"])
        struct_start = time_module.perf_counter()
        check_frame_structure(destination, expected_width, expected_height, 12 if mode_is_lossy(mode) else output_bits)
        metadata_result = validate_metadata(source, destination)
        metadata_entries.append({"file": rel, **metadata_result})
        if not metadata_result["ok"]:
            raise RuntimeError(f"Metadata validation failed for {rel}: {metadata_result['errors']}")
        if frame_stages is not None:
            frame_stages["structural"] = frame_stages.get("structural", 0.0) + (time_module.perf_counter() - struct_start) * 1000.0
        do_native = effective_mode == "full" or (effective_mode == "sampled" and (position in native_set or warned))
        error_ext = {"mean": 0.0, "max": 0}
        if do_native:
            error_ext = verify_native(source, destination, output_bits, mode, frame_stages, None, True, defect_records[-1].get("repairs", []) if defect_records else None)
            breaches = check_guardrails(rel, error_ext, guard)
            if breaches:
                guardrail_breaches.extend(breaches)
                print(f"GUARDRAIL BREACH: {'; '.join(breaches)}", flush=True)
            verification_entries.append({"file": rel, "error": error_ext,
                                         "guardrail_warnings": breaches})
            if error_stats is not None:
                error_stats.append((error_ext["mean"], error_ext["max"]))
            if roundtrip:
                try:
                    rt = roundtrip_check(source, destination, mode=mode)
                except RuntimeError as error:
                    raise RuntimeError(f"Round-trip check failed for {rel}: {error}")
                roundtrip_entries.append({"file": rel, "roundtrip": rt})
                if mode == "lossless" and rt["pass"] is False:
                    raise RuntimeError(f"Round-trip byte-exact claim violated for {rel}: {rt['detail']}")
        if defect_map is not None and repair_maps and defect_records and defect_records[-1].get("repairs"):
            try:
                heatmap_path = (destination_root / "repair-maps" / rel).with_suffix(".heatmap.jpg")
                generate_heatmap_bytes = write_repair_heatmap(source, heatmap_path, defect_records[-1]["repairs"])
                if timings is not None:
                    timings["heatmap_bytes"] = timings.get("heatmap_bytes", 0) + generate_heatmap_bytes
            except Exception as error:
                print(f"WARNING: heatmap failed for {rel}: {error}", flush=True)
        if warned:
            warned_names.append(rel)
            selected[position] = "warning"
        if effective_mode in ("sampled", "checksum", "full"):
            checksum_entries.append((str(destination.relative_to(destination_root)), file_sha256(destination)))
        if stage_times is not None and frame_stages:
            for stage, value in frame_stages.items():
                stage_times.setdefault(stage, []).append(value)
        dng_ms = (time_module.perf_counter() - start) * 1000.0
        preview_ms, preview_bytes = 0.0, 0
        if preview != "none":
            preview_start = time_module.perf_counter()
            try:
                preview_bytes = generate_frame_preview(source, previews_dir, preview, preview_quality, rel)
            except Exception as error:
                # Preview failure is reported but never invalidates DNG output.
                print(f"WARNING: preview failed for {rel}: {error}", flush=True)
                warned = True
                if timings is not None:
                    timings.setdefault("preview_failures", []).append(rel)
            preview_ms = (time_module.perf_counter() - preview_start) * 1000.0
            if stage_times is not None:
                stage_times.setdefault("preview", []).append(preview_ms)
        if warned and effective_mode == "sampled" and not do_native:
            # Late warning (e.g. preview failure after the decode decision).
            error_ext = verify_native(source, destination, output_bits, mode, frame_stages, None, True, defect_records[-1].get("repairs", []) if defect_records else None)
            breaches = check_guardrails(rel, error_ext, guard)
            if breaches:
                guardrail_breaches.extend(breaches)
                print(f"GUARDRAIL BREACH: {'; '.join(breaches)}", flush=True)
            verification_entries.append({"file": rel, "error": error_ext,
                                         "guardrail_warnings": breaches})
            if error_stats is not None:
                error_stats.append((error_ext["mean"], error_ext["max"]))
        if timings is not None:
            timings.setdefault("dng_ms", []).append(dng_ms)
            timings.setdefault("preview_ms", []).append(preview_ms)
            timings["dng_bytes"] = timings.get("dng_bytes", 0) + output
            timings["preview_bytes"] = timings.get("preview_bytes", 0) + preview_bytes
            timings["preview_count"] = timings.get("preview_count", 0) + (1 if preview_bytes else 0)
        total_input += original
        total_output += output
    if effective_mode in ("sampled", "checksum", "full"):
        write_checksums_manifest(destination_root, checksum_entries)
    output_names = sorted((p.relative_to(destination_root).as_posix() for p in destination_root.rglob("*") if p.is_file() and p.suffix.lower() == ".dng"))
    source_names = sorted((p.relative_to(source_root).as_posix() for p in dng_files))
    if output_names != source_names:
        raise RuntimeError("Verification failed: output filename set does not match source (missing/duplicate frames).")
    if guardrail_breaches:
        print(f"GUARDRAIL SUMMARY: {len(guardrail_breaches)} breach(es); see verification-report.json (warnings only unless --strict-guardrails).", flush=True)
    write_verification_report(destination_root / "verification-report.json", {
        "tool": "cdng serial verification",
        "mode": effective_mode,
        "verify_every": verify_every,
        "seed": seed,
        "guardrails_proposed": guard,
        "guardrail_breaches": guardrail_breaches,
        "strict_guardrails": strict_guardrails,
        "quality": quality,
        "settings_fingerprint": fingerprint,
        "roundtrip": roundtrip_entries,
        "selected": {str(index): {"file": dng_files[index].relative_to(source_root).as_posix(), "reason": reason}
                     for index, reason in sorted(selected.items())},
        "verified": verification_entries,
        "quality_metrics": summarize_quality_metrics(verification_entries),
        "warned": sorted(set(warned_names)),
        "structural": {"checked": len(dng_files), "failed": 0},
    })
    if strict_guardrails and guardrail_breaches:
        raise RuntimeError(f"Guardrail breach: {guardrail_breaches[0]} (+{len(guardrail_breaches)-1} more).")
    if defect_map is not None:
        write_defect_report(defect_report, defect_map, defect_map_name, defect_records)
    if companions:
        copy_start = time_module.perf_counter()
        for source in companions:
            destination = destination_root / source.relative_to(source_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if resume and file_sha256(destination) == file_sha256(source):
                    continue
                raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
            shutil.copy2(source, destination)
        if timings is not None:
            timings["copy_ms"] = (time_module.perf_counter() - copy_start) * 1000.0
    local_audio = [p for p in companions if p.suffix.lower() in AUDIO_SUFFIXES]
    root_audio = [p for p in root_companions if p.suffix.lower() in AUDIO_SUFFIXES]
    audio_sources = local_audio + root_audio
    audio_missing = [p.relative_to(source_root).as_posix() for p in local_audio
                     if not (destination_root / p.relative_to(source_root)).is_file()]
    audio_missing += [f"parent/{p.name}" for p in root_audio
                      if not (Path(root_companions_destination) / p.name).is_file()]
    audio_status = {"present": bool(audio_sources),
                    "files": ([p.relative_to(source_root).as_posix() for p in local_audio]
                              + [f"parent/{p.name}" for p in root_audio]),
                    "copied": not audio_missing,
                    "verified": not audio_missing,
                    "missing": audio_missing}
    if audio_missing:
        raise RuntimeError(f"Associated audio was not copied: {audio_missing}")
    ratio = total_input / total_output if total_output else 0.0
    update_report_statistics(destination_root / "verification-report.json", {
        "input_size_bytes": total_input,
        "output_size_bytes": total_output,
        "input_dng_container_bytes": sum(p.stat().st_size for p in dng_files),
        "achieved_compression_ratio": round(ratio, 4),
        "storage_saved_percent": round((1.0 - total_output / total_input) * 100.0, 3) if total_input else 0.0,
        "input_bit_depth": sorted({source_bit_depth(p) for p in dng_files}),
        "resolution": {"width": expected_width, "height": expected_height},
        "recording_mode": recording_configuration(expected_width, expected_height, defect_map, source_root, recording_mode),
        "compression_mode": mode,
        "frames": len(dng_files),
        "processing_time_seconds": round(time_module.perf_counter() - job_start, 3),
        "validation_result": "passed" if not guardrail_breaches else "passed_with_guardrail_warnings",
        "audio": audio_status,
        "metadata_validation": {"ok": all(item["ok"] for item in metadata_entries),
                                 "frames_checked": len(metadata_entries),
                                 "failures": [item for item in metadata_entries if not item["ok"]]},
        "target_status": mode_target_status(mode, ratio),
        "lossy": mode_is_lossy(mode),
        "exact_raw_comparison": {"checked": mode == "lossless",
                                  "differing_samples": 0 if mode == "lossless" else None},
    })
    map_mode = recording_mode
    if map_mode:
        metadata_path = write_resolve_metadata_csv(
            destination_root / "resolve-metadata.csv",
            ((destination_root / source.relative_to(source_root), map_mode) for source in dng_files),
            allow_existing=resume)
        print(f"Resolve Comments metadata: {metadata_path}", flush=True)
    if mode_is_lossy(mode) and mode_target_status(mode, ratio) != "achieved":
        print(f"LIMITATION: {mode} target not achieved ({ratio:.2f}:1); selected mode was preserved.", flush=True)
    effective_bits = 12 if mode_is_lossy(mode) else output_bits
    print(
        f"COMPLETE {len(dng_files)} DNG frames: {total_input} bytes -> {total_output} bytes "
        f"({1 - total_output / total_input:.1%} smaller; ratio={ratio:.2f}:1; mode={mode}; output bits={effective_bits}; "
        f"verify-mode={effective_mode})"
    )


def preflight_report(source_root: Path, destination_root: Path, *, output_bits: int | None = None,
                     mode: str = "high_quality", quality: int = 85, companding: str = "log",
                     log_base: float = DEFAULT_LOG_BASE, black_protect: int = 256, verify: bool = False,
                     verify_mode: str | None = None, verify_every: int = 32,
                     tile_layout: str = "small", defect_map: dict | None = None,
                     defect_report: Path | None = None, copy_non_dng: bool = True,
                     preview: str = "none", preview_quality: int = PREVIEW_JPEG_QUALITY,
                     resume: bool = False, allow_low_space: bool = False) -> dict:
    """Read-only dry-run validation (N1). No output is written.

    Returns {"ok": bool, "checks": [{"name", "ok", "detail"}]}. Every check the
    folder paths enforce is covered: numeric args, DNG presence, destination
    guards, per-file + companion collisions, first-frame dims, tile dims,
    defect-report requirement, defect-map match, disk margin.
    """
    ensure_vendor()
    checks: list[dict] = []

    def _add(name: str, ok: bool, detail: str = "", warning: bool = False):
        check = {"name": name, "ok": bool(ok), "detail": detail}
        if warning:
            check["warning"] = True
        checks.append(check)

    try:
        validate_numeric_args(log_base, black_protect, verify_every, preview_quality)
        _add("numeric-args", True, "log-base/black-protect/verify-every/preview-quality in range")
    except ValueError as error:
        _add("numeric-args", False, str(error))
    if preview not in ("none", "720p", "480p"):
        _add("preview-arg", False, f"preview={preview!r} must be none/720p/480p")
    else:
        _add("preview-arg", True, f"preview={preview}")
    try:
        files = sorted(path for path in source_root.rglob("*") if path.is_file())
    except OSError as error:
        files = []
        _add("source-scan", False, f"cannot list source: {error}")
    else:
        _add("source-scan", True, f"{len(files)} files found")
    dng_files = [p for p in files if p.suffix.lower() == ".dng"]
    if not dng_files:
        _add("dng-present", False, "No DNG files found in the selected input folder.")
        return {"ok": False, "checks": checks}
    _add("dng-present", True, f"{len(dng_files)} DNG frames")
    try:
        guard_destination(source_root, destination_root, allow_existing_root=resume)
        _add("destination-guard", True, "destination is new and outside the source"
             if not resume else "resume into existing folder allowed; outputs validated per file")
    except (ValueError, FileExistsError) as error:
        _add("destination-guard", False, str(error))
    collisions = [str(destination_root / p.relative_to(source_root))
                  for p in dng_files if (destination_root / p.relative_to(source_root)).exists()]
    _add("dng-collisions", not collisions or resume,
         "no collisions" if not collisions
         else (f"{len(collisions)} existing, reused by --resume" if resume
               else f"{len(collisions)} existing, e.g. {collisions[0]}"))
    try:
        companions = companion_files(source_root, copy_non_dng)
        _add("audio-preservation", True,
             "associated audio will be copied" if any(p.suffix.lower() in AUDIO_SUFFIXES for p in companions)
             else "no associated audio found")
    except ValueError as error:
        companions = []
        _add("audio-preservation", False, str(error))
    ccoll = [str(destination_root / p.relative_to(source_root))
             for p in companions if (destination_root / p.relative_to(source_root)).exists()]
    _add("companion-collisions", not ccoll or resume,
         f"{len(companions)} companions, no collisions" if not ccoll
         else (f"{len(ccoll)} existing, reused by --resume" if resume
               else f"{len(ccoll)} existing, e.g. {ccoll[0]}"))
    import tifffile as _tf
    try:
        with _tf.TiffFile(dng_files[0]) as _first:
            _w = int(_first.pages[0].tags[TAG_IMAGE_WIDTH].value)
            _h = int(_first.pages[0].tags[TAG_IMAGE_LENGTH].value)
            _source_bits = source_bits_from_page(_first.pages[0])
        _add("first-frame", True, f"{_w}x{_h}")
        try:
            _resolved_bits = resolve_output_bits(output_bits, _source_bits, mode)
            detail = (f"{_resolved_bits}-bit storage (default camera-linear)"
                      if output_bits is None and not mode_is_lossy(mode)
                      else f"{_resolved_bits}-bit storage (requested/effective)")
            _add("output-depth", True, detail)
        except ValueError as error:
            _add("output-depth", False, str(error))
    except Exception as error:
        _w = _h = 0
        _add("first-frame", False, f"unreadable first frame: {error}")
    if tile_layout not in TILE_LAYOUTS:
        _add("tile-layout", False, "--tile-layout must be small or four")
    elif tile_layout == "four" and (_w != 3856 or _h != 2170):
        _add("tile-layout", False, f"four supports 3856x2170 only, first frame {_w}x{_h}")
    else:
        _add("tile-layout", True, f"tile={tile_layout}")
    if defect_map is not None and defect_report is None:
        _add("defect-report", False, "--defect-report is required when --defect-map is supplied")
    else:
        _add("defect-report", True, "defect-report requirement satisfied")
    if defect_map is not None and _w and _h:
        try:
            with _tf.TiffFile(dng_files[0]) as _doc:
                validate_defect_map(defect_map, _doc.pages[0], _w, _h, "defect map")
            _add("defect-map-match", True, "first frame matches map camera/geometry/CFA")
        except Exception as error:
            _add("defect-map-match", False, str(error))
    try:
        need = sum(p.stat().st_size for p in dng_files + companions)
        free = free_bytes_for(destination_root)
        fits = free >= int(need * 1.2)
        overridden = allow_low_space and not fits
        detail = f"{free / 2 ** 30:.1f} GiB free; need ~{need * 1.2 / 2 ** 30:.1f} GiB"
        if overridden:
            detail += "; override enabled — output may run out of space"
        _add("disk-space", fits or allow_low_space, detail, warning=overridden)
    except OSError as error:
        _add("disk-space", False, f"disk check failed: {error}")
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def plan_mixed_split(source_root: Path) -> dict:
    """Group DNGs by raw layout for the mixed-layout auto-splitter (N3).

    Returns {"groups": {key: {"layout": {...}, "files": [rel posix]}}, "companions": [rel]}.
    Read-only; execute_mixed_split() performs the copy.
    """
    ensure_vendor()
    import tifffile as _tf
    groups: dict[str, dict] = {}
    dng_files = sorted(p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() == ".dng")
    for path in dng_files:
        try:
            with _tf.TiffFile(path) as doc:
                page = doc.pages[0]
                model_tag = page.tags.get(50708, page.tags.get(272))
                model = normalise_camera_model(model_tag.value) if model_tag is not None else "unknown"
                w, h = int(page.tags[TAG_IMAGE_WIDTH].value), int(page.tags[TAG_IMAGE_LENGTH].value)
                cfa = cfa_pattern_name(page.tags[33422].value) if 33422 in page.tags else "unknown"
                bits = int(page.tags[TAG_BITS_PER_SAMPLE].value)
        except Exception as error:
            key = "unreadable"
            groups.setdefault(key, {"layout": {"error": str(error)}, "files": []})["files"].append(
                path.relative_to(source_root).as_posix())
            continue
        key = f"{model}_{w}x{h}_{cfa}_{bits}bit".replace(" ", "_").replace("/", "-")
        entry = groups.setdefault(key, {"layout": {"camera_model": model, "width": w, "height": h,
                                                   "cfa": cfa, "bits": bits}, "files": []})
        entry["files"].append(path.relative_to(source_root).as_posix())
    companions = sorted(p.relative_to(source_root).as_posix()
                        for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() != ".dng")
    return {"groups": groups, "companions": companions}


def execute_mixed_split(source_root: Path, dest_parent: Path, copy_non_dng: bool = True) -> dict:
    """Copy each raw-layout group into dest_parent/<group>/ (N3).

    The source tree is never modified (read-only principle); files are copied.
    dest_parent must not exist. Returns {"dest_parent": str, "groups": {key: dest}}.
    """
    guard_destination(source_root, dest_parent, allow_existing_root=False)
    plan = plan_mixed_split(source_root)
    if len(plan["groups"]) < 2:
        raise ValueError("Source has a single raw layout; nothing to split.")
    out: dict[str, str] = {}
    for key, group in sorted(plan["groups"].items()):
        safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._") or "unreadable"
        target = dest_parent / safe_key
        for rel in group["files"]:
            src, dst = source_root / rel, target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        out[key] = str(target)
    if copy_non_dng:
        for key in sorted(plan["groups"]):
            safe_key = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._") or "unreadable"
            for rel in plan["companions"]:
                dst = dest_parent / safe_key / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_root / rel, dst)
    return {"dest_parent": str(dest_parent), "groups": out}


def recommend_preset(source_root: Path, *, quality: int = 90, max_samples: int = 3,
                     tmp_parent: Path | None = None) -> dict:
    """Measure-then-recommend Lossless vs High Quality on sampled frames (F2).

    Encodes up to max_samples spread frames both ways in a temp dir, measures
    file-wise ratios + lossy error vs guardrails. Returns numbers + recommendation.
    """
    ensure_vendor()
    import tempfile as _tf_module
    files = sorted(p for p in source_root.rglob("*") if p.is_file() and p.suffix.lower() == ".dng")
    if not files:
        raise ValueError("No DNG files found in the selected input folder.")
    n = len(files)
    picks = sorted({0, n // 2, n - 1} | set(range(0, n, max(1, n // max_samples))))[:max_samples]
    guard = guardrail_for_quality(quality)
    tmpdir = Path(_tf_module.mkdtemp(prefix="cdng-recommend-", dir=str(tmp_parent) if tmp_parent else None))
    try:
        ratios_l: list[float] = []
        ratios_q: list[float] = []
        breaches: list[str] = []
        err_means: list[float] = []
        for idx in picks:
            src = files[idx]
            src_bytes = src.stat().st_size
            dl = tmpdir / f"l_{idx}.dng"
            compress_frame(src, dl, 12, "lossless", quality, "log", DEFAULT_LOG_BASE, 256, None, "defect map", [], (256, 256), None, "small")
            ratios_l.append(src_bytes / dl.stat().st_size)
            dq = tmpdir / f"q_{idx}.dng"
            compress_frame(src, dq, 12, "high_quality", quality, "log", DEFAULT_LOG_BASE, 256, None, "defect map", [], (256, 256), None, "small")
            ratios_q.append(src_bytes / dq.stat().st_size)
            stats = verify_native(src, dq, 12, "high_quality", None, None, True)
            err_means.append(stats["mean"])
            breaches.extend(check_guardrails(src.name, stats, guard))
        avg_l = sum(ratios_l) / len(ratios_l)
        avg_q = sum(ratios_q) / len(ratios_q)
        avg_mean = sum(err_means) / len(err_means)
        if breaches:
            rec, reason = "lossless", f"q{quality} breaches guardrails on samples ({breaches[0]}); prefer byte-exact."
        elif avg_l >= 1.5:
            rec, reason = "lossless", f"lossless reaches {avg_l:.2f}:1 with zero error; no reason to go lossy."
        elif avg_q >= 2.0:
            rec, reason = "high_quality", f"high quality reaches {avg_q:.2f}:1, mean error {avg_mean:.2f}, no breach."
        else:
            rec, reason = "high_quality", f"high quality reaches {avg_q:.2f}:1 (below 2:1); lossless only {avg_l:.2f}:1 — high quality still wins on size."
        return {"samples": [files[i].name for i in picks], "lossless_ratio": round(avg_l, 3),
                "quality_ratio": round(avg_q, 3), "quality_mean_error": round(avg_mean, 3),
                "breaches": breaches, "recommendation": rec, "reason": reason}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def export_support_bundle(output_dir: Path, app_root: Path | None = None) -> Path:
    """Zip logs + reports + environment into a support bundle (N5).

    Written beside (never inside) the output dir. Returns the zip path.
    """
    import platform as _platform
    import zipfile as _zip
    from datetime import datetime as _dt
    root = app_root or Path(__file__).resolve().parent
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    bundle = output_dir.parent / (output_dir.name + f".support-bundle-{stamp}.zip")
    try:
        import numpy as _np
        np_v = _np.__version__
    except Exception:
        np_v = "unknown"
    for mod, attr in (("tifffile", "__version__"), ("imagecodecs", "__version__"), ("rawpy", "__version__")):
        try:
            __import__(mod)
        except Exception:
            pass
    versions = {"python": sys.version.split()[0], "platform": _platform.platform(), "numpy": np_v}
    for mod in ("tifffile", "imagecodecs", "rawpy"):
        try:
            versions[mod] = getattr(sys.modules[mod], "__version__", "present")
        except KeyError:
            versions[mod] = "missing"
    with _zip.ZipFile(bundle, "w", _zip.ZIP_DEFLATED) as archive:
        logs = sorted((root / "logs").glob("profile-selection-*.json"))[-5:] if (root / "logs").is_dir() else []
        for path in logs:
            archive.write(path, f"logs/{path.name}")
        if output_dir.is_dir():
            for name in ("verification-report.json", "checksums.sha256", "defect-repair-report.json"):
                path = output_dir / name
                if path.is_file():
                    archive.write(path, f"output/{name}")
        archive.writestr("environment.json", __import__("json").dumps(versions, indent=2))
    return bundle


def roundtrip_check(source: Path, compressed: Path, *, mode: str = "lossless") -> dict:
    """Independent LibRaw decode check (F5): the 'byte-exact on import' proof.

    Decodes the output through RawPy/LibRaw — a different decoder than the
    tifffile verifier — and compares against source-linear raw. For lossless
    the codes must match exactly (pass/fail); for lossy, stats are reported.
    Returns {"pass": bool|None, "max_diff": int, "mean_diff": float, "detail": str}.
    Raises RuntimeError when LibRaw cannot decode the output at all.
    """
    mode = canonical_mode(mode)
    ensure_vendor()
    import numpy as _np
    import tifffile as _tf
    try:
        import rawpy as _rawpy
    except ImportError as error:
        raise RuntimeError("RawPy/LibRaw is required for the round-trip check.") from error
    with _tf.TiffFile(source) as _doc:
        _page = _doc.pages[0]
        _, _table = source_linear_table(_np, _page)
        _stored = read_source_stored(_np, _page)
        expected = _table[_stored].astype(_np.int32)
    try:
        with _rawpy.imread(str(compressed)) as _raw:
            actual = _raw.raw_image_visible.copy()
    except Exception as error:
        # A file the native verifier already decoded but LibRaw cannot open is
        # a decoder-compatibility finding, not proof of corruption: report it
        # instead of failing (the tifffile verification above already passed).
        return {"pass": None, "max_diff": -1, "mean_diff": -1.0,
                "detail": f"LibRaw cannot decode {compressed.name}: {error}"}
    # LibRaw returns stored codes without applying the DNG LinearizationTable,
    # so linearize through the OUTPUT table before comparing to source-linear.
    # 8-bit outputs legitimately have no table: compare stored codes directly.
    with _tf.TiffFile(compressed) as _out:
        _lut_tag = _out.pages[0].tags.get(TAG_LINEARIZATION_TABLE)
        _out_lut = (_np.asarray(_lut_tag.value, dtype=_np.uint16)
                    if _lut_tag is not None else None)
    if actual.shape != expected.shape:
        return {"pass": False, "max_diff": -1, "mean_diff": -1.0,
                "detail": f"shape {actual.shape} != source {expected.shape}"}
    if _out_lut is None:
        with _tf.TiffFile(source) as _src:
            _stored = read_source_stored(_np, _src.pages[0]).astype(_np.int32)
        _same = bool(_np.array_equal(_np.asarray(actual, dtype=_np.int32), _stored))
        return {"pass": _same if mode == "lossless" else None,
                "max_diff": 0 if _same else -1, "mean_diff": 0.0 if _same else -1.0,
                "detail": ("byte-exact (no LinearizationTable)" if _same
                           else "stored codes differ (no LinearizationTable)")}
    actual_codes = _np.asarray(actual, dtype=_np.int32)
    if mode_is_lossy(mode):
        output_max = len(_out_lut) - 1
        if bool(_np.any(actual_codes < 0)) or bool(_np.any(actual_codes > output_max)):
            return {"pass": None, "max_diff": -1, "mean_diff": -1.0,
                    "detail": "LibRaw returned codes outside the output range; lossy round-trip is not measurable"}
        edge_fraction = float(_np.mean((actual_codes <= 0) | (actual_codes >= output_max)))
        if edge_fraction >= 0.95:
            return {"pass": None, "max_diff": -1, "mean_diff": -1.0,
                    "detail": ("LibRaw decoded degenerate CFA-JPEG data "
                               f"({edge_fraction:.1%} at code-range edges); "
                               "lossy round-trip is not measurable")}
    actual_linear = _out_lut[actual_codes.astype(_np.uint16)].astype(_np.int32)
    diff = _np.abs(actual_linear - expected)
    max_diff, mean_diff = int(diff.max()), float(diff.mean())
    if mode == "lossless":
        ok = bool(_np.array_equal(actual_linear, expected))
        return {"pass": ok, "max_diff": max_diff, "mean_diff": mean_diff,
                "detail": "byte-exact" if ok else f"{int((diff != 0).sum())} codes differ"}
    return {"pass": None, "max_diff": max_diff, "mean_diff": mean_diff,
            "detail": ("lossy stats only; no pass/fail (LibRaw and tifffile disagree on CFA-JPEG "
                       "stored codes, so LibRaw is not a valid oracle for lossy outputs)")}


def write_repair_heatmap(source: Path, heatmap_path: Path, repairs: list[dict]) -> int:
    """Render a before/after visual-proof JPEG for defect repair (F4).

    Downscaled source-linear preview with repaired sites marked in red.
    Returns bytes written.
    """
    ensure_vendor()
    import imagecodecs as _ic
    import numpy as _np
    import tifffile as _tf
    with _tf.TiffFile(source) as _doc:
        _page = _doc.pages[0]
        _, _table = source_linear_table(_np, _page)
        _stored = read_source_stored(_np, _page).ravel()
        _w = int(_page.tags[TAG_IMAGE_WIDTH].value)
        _h = int(_page.tags[TAG_IMAGE_LENGTH].value)
        try:
            _neutral = tuple(_page.tags[50728].value)
            _gains = _np.array([_neutral[0] / _neutral[1], _neutral[2] / _neutral[3],
                                _neutral[4] / _neutral[5]], dtype=_np.float32)
            _gains = _gains / _gains[1]
        except (KeyError, IndexError, ZeroDivisionError):
            _gains = _np.array([1.0, 1.0, 1.0], dtype=_np.float32)
    _linear = _table[_stored].astype(_np.float32)
    rgb = bayer_to_preview_rgb(_np, _linear, _w, _h, _gains, 1280)
    sx, sy = rgb.shape[1] / _w, rgb.shape[0] / _h
    for site in repairs:
        px, py = int(int(site["x"]) * sx), int(int(site["y"]) * sy)
        rgb[max(0, py - 2):py + 3, max(0, px - 2):px + 3] = (255, 0, 0)
    jpeg = _ic.jpeg_encode(_np.ascontiguousarray(rgb), level=70, colorspace="RGB",
                           outcolorspace="YCbCr", subsampling=(2, 2), optimize=False)
    heatmap_path.parent.mkdir(parents=True, exist_ok=True)
    if heatmap_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing heatmap: {heatmap_path}")
    heatmap_path.write_bytes(bytes(jpeg))
    return heatmap_path.stat().st_size


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an edit-ready tiled CinemaDNG frame.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--mode", type=canonical_mode, default="high_quality",
                        help="Compression mode: lossless, high_quality, or compact (default: high_quality).")
    parser.add_argument("--output-bits", choices=(8, 10, 12), type=int, default=None,
                        help="Lossless storage depth; omitted defaults to 12-bit camera-linear output. It must not be below source depth.")
    parser.add_argument("--quality", choices=range(1, 101), type=int, default=None, metavar="1..100", help="Lossy VBR quality. Defaults to 90 for high_quality and 82 for compact.")
    parser.add_argument("--companding", choices=("log", "linear"), default="log", help="Lossy mode: log preserves more shadow code precision; linear is the old baseline.")
    parser.add_argument("--log-base", type=float, default=DEFAULT_LOG_BASE, help="Log curve strength for --companding log (must be > 1; default: 16; gentler than the legacy base 104 to reduce pink highlight casts).")
    parser.add_argument("--black-protect", type=int, default=256, help="Keep this many darkest 12-bit codes linear before log companding (default: 256).")
    parser.add_argument("--verify-native", action="store_true", help="Decode every output through the native TIFF/JPEG codec and validate frame dimensions and raw error.")
    parser.add_argument("--no-copy-non-dng", action="store_true", help="Do not copy WAV/XMP and other non-DNG companion files when converting a folder.")
    parser.add_argument("--direct-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--recording-mode", choices=("crop", "full"), help=argparse.SUPPRESS)
    parser.add_argument("--root-companions-source", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--root-companions-destination", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--preview", choices=("none", "720p", "480p"), default="none", help="Write lightweight sidecar JPEG previews per DNG frame into previews/ (identification only, never grading).")
    parser.add_argument("--preview-quality", type=int, default=PREVIEW_JPEG_QUALITY, help="Sidecar JPEG quality 1..100, baseline 4:2:0 (default: 60).")
    parser.add_argument("--workers", default="1", help="Frame workers: auto (=6 on this PC) or 1..8. Default 1 keeps exact serial behavior.")
    parser.add_argument("--benchmark-workers", default=None, metavar="1,2,4,6,8", help="Run the folder conversion once per worker count into <destination>-wN folders and print a timing table.")
    parser.add_argument("--verify-mode", choices=("full", "sampled", "checksum", "none"), default=None, help="Validation policy. Default keeps legacy behavior (--verify-native means full, otherwise silent none). Explicit none prints a warning; checksums prove storage consistency, not image semantics.")
    parser.add_argument("--verify-every", type=int, default=32, metavar="N", help="Sampled mode also native-decodes every Nth frame plus first/middle/last and any warned frame (default: 32).")
    parser.add_argument("--tile-layout", choices=("small", "four"), default="small", help="small keeps 256x256 tiles; four uses four 1936x1088 tiles on 3856x2170 frames (experimental; 1936 not 1928 because tifffile requires multiples of 16).")
    parser.add_argument("--time-stages", action="store_true", help="Record low-overhead per-stage timings and print a stage summary (benchmark use).")
    parser.add_argument("--task-chunk", choices=(1, 2, 4), type=int, default=1, help="Consecutive frames per worker task; reduces scheduling overhead (parallel path only).")
    parser.add_argument("--strict-guardrails", action="store_true", help="Fail the run on guardrail breach instead of warning only.")
    parser.add_argument("--allow-low-space", action="store_true", help="Folder conversions only: continue when the conservative free-space estimate is short; the disk may fill and leave partial output.")
    parser.add_argument("--dry-run", action="store_true", help="Read-only preflight validation only; print checks and exit without writing output (N1).")
    parser.add_argument("--recommend-preset", action="store_true", help="Encode a few sampled frames both ways and print a preset recommendation (F2).")
    parser.add_argument("--resume", action="store_true", help="Resume into an existing output folder: skip structurally valid frames, refuse invalid ones (N2).")
    parser.add_argument("--roundtrip", action="store_true", help="Independent LibRaw decode check on verified frames; lossless mismatch fails the run (F5).")
    args = parser.parse_args()
    if args.quality is None:
        args.quality = 82 if args.mode == "compact" else 90
    try:
        validate_numeric_args(args.log_base, args.black_protect, args.verify_every, args.preview_quality)
    except ValueError as error:
        parser.error(str(error))
    defect_map = None
    try:
        args.recording_mode = validate_recording_mode(args.recording_mode, defect_map)
        plan_root_companions(args.root_companions_source, args.root_companions_destination)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if ((args.root_companions_source is not None or args.root_companions_destination is not None)
            and not args.source.is_dir()):
        parser.error("Root companion folders can only be used with a folder source.")
    defect_map_name = "defect map"
    if args.benchmark_workers is not None:
        import cdng_parallel as parallel

        try:
            counts = parallel.parse_benchmark_workers(args.benchmark_workers)
        except ValueError as error:
            parser.error(str(error))
        if not args.source.is_dir():
            parser.error("--benchmark-workers requires a folder source.")
        print("Worker sweep (quality/companding settings unchanged):", flush=True)
        for count in counts:
            sweep_out = args.destination.parent / f"{args.destination.name}-w{count}"
            sweep_start = time.perf_counter()
            if count == 1:
                convert_tree(args.source, sweep_out, output_bits=args.output_bits, mode=args.mode,
                             quality=args.quality, companding=args.companding, log_base=args.log_base,
                             black_protect=args.black_protect, verify=args.verify_native,
                             copy_non_dng=not args.no_copy_non_dng, defect_map=defect_map,
                             defect_map_name=defect_map_name,
                             defect_report=None,
                             preview=args.preview, preview_quality=args.preview_quality,
                             progress=True, verify_mode=args.verify_mode, verify_every=args.verify_every,
                             tile_layout=args.tile_layout, stage_times={} if args.time_stages else None,
                             strict_guardrails=args.strict_guardrails, allow_low_space=args.allow_low_space,
                             recording_mode=args.recording_mode,
                             root_companions_source=args.root_companions_source,
                             root_companions_destination=sweep_out if args.root_companions_source else None)
            else:
                parallel.convert_tree_parallel(args.source, sweep_out, output_bits=args.output_bits, mode=args.mode, quality=args.quality, companding=args.companding, log_base=args.log_base, black_protect=args.black_protect, verify=args.verify_native, copy_non_dng=not args.no_copy_non_dng, defect_map=defect_map, defect_map_name=defect_map_name, defect_report=None, preview=args.preview, preview_quality=args.preview_quality, workers=count, verify_mode=args.verify_mode, verify_every=args.verify_every, tile_layout=args.tile_layout, time_stages=args.time_stages, task_chunk=args.task_chunk, strict_guardrails=args.strict_guardrails, resume=args.resume, repair_maps=False, roundtrip=args.roundtrip, allow_low_space=args.allow_low_space, direct_only=args.direct_only, recording_mode=args.recording_mode, root_companions_source=args.root_companions_source, root_companions_destination=sweep_out if args.root_companions_source else None)
            print(f"  workers={count}: {time.perf_counter() - sweep_start:.1f} s -> {sweep_out}", flush=True)
        return 0
    if args.source.is_dir():
        import cdng_parallel as parallel

        if args.dry_run:
            report = preflight_report(args.source, args.destination, output_bits=args.output_bits,
                                      mode=args.mode, quality=args.quality, companding=args.companding,
                                      log_base=args.log_base, black_protect=args.black_protect,
                                      verify=args.verify_native, verify_mode=args.verify_mode,
                                      verify_every=args.verify_every, tile_layout=args.tile_layout,
                                      defect_map=defect_map, defect_report=None,
                                      copy_non_dng=not args.no_copy_non_dng, preview=args.preview,
                                      preview_quality=args.preview_quality, resume=args.resume,
                                      allow_low_space=args.allow_low_space)
            for check in report["checks"]:
                status = "WARN" if check.get("warning") else "OK" if check["ok"] else "FAIL"
                print(f"[{status}] {check['name']}: {check['detail']}", flush=True)
            return 0 if report["ok"] else 1
        if args.recommend_preset:
            import json as _json
            rec = recommend_preset(args.source, quality=args.quality)
            print(_json.dumps(rec, indent=2), flush=True)
            return 0
        try:
            workers = parallel.resolve_workers(args.workers)
        except ValueError as error:
            parser.error(str(error))
        stage_times: dict = {} if args.time_stages else None
        if workers == 1:
            convert_tree(args.source, args.destination, output_bits=args.output_bits, mode=args.mode,
                         quality=args.quality, companding=args.companding, log_base=args.log_base,
                         black_protect=args.black_protect, verify=args.verify_native,
                         copy_non_dng=not args.no_copy_non_dng, defect_map=defect_map,
                         defect_map_name=defect_map_name, defect_report=None,
                         preview=args.preview, preview_quality=args.preview_quality,
                         progress=True, verify_mode=args.verify_mode, verify_every=args.verify_every,
                         tile_layout=args.tile_layout, stage_times=stage_times,
                         strict_guardrails=args.strict_guardrails, resume=args.resume,
                         repair_maps=False, roundtrip=args.roundtrip, allow_low_space=args.allow_low_space,
                         direct_only=args.direct_only, recording_mode=args.recording_mode,
                         root_companions_source=args.root_companions_source,
                         root_companions_destination=args.root_companions_destination)
        else:
            summary = parallel.convert_tree_parallel(args.source, args.destination, output_bits=args.output_bits, mode=args.mode, quality=args.quality, companding=args.companding, log_base=args.log_base, black_protect=args.black_protect, verify=args.verify_native, copy_non_dng=not args.no_copy_non_dng, defect_map=defect_map, defect_map_name=defect_map_name, defect_report=None, preview=args.preview, preview_quality=args.preview_quality, workers=workers, verify_mode=args.verify_mode, verify_every=args.verify_every, tile_layout=args.tile_layout, time_stages=args.time_stages, task_chunk=args.task_chunk, strict_guardrails=args.strict_guardrails, resume=args.resume, repair_maps=False, roundtrip=args.roundtrip,
                          allow_low_space=args.allow_low_space, direct_only=args.direct_only,
                          recording_mode=args.recording_mode,
                          root_companions_source=args.root_companions_source,
                          root_companions_destination=args.root_companions_destination)
            if stage_times is not None:
                for stage, values in summary.get("stage_times", {}).items():
                    stage_times.setdefault(stage, []).extend(values)
        if stage_times is not None:
            print_stage_summary(stage_times)
    else:
        import cdng_parallel as parallel

        if args.dry_run:
            parser.error("--dry-run requires a folder source.")
        if args.recommend_preset:
            parser.error("--recommend-preset requires a folder source.")
        if args.resume:
            parser.error("--resume requires a folder source.")
        if args.preview != "none":
            parser.error("--preview requires a folder source.")
        if args.output_bits is None:
            try:
                args.output_bits = resolve_output_bits(None, source_bit_depth(args.source), args.mode)
            except ValueError as error:
                parser.error(str(error))
        try:
            single_workers = parallel.resolve_workers(args.workers)
        except ValueError as error:
            parser.error(str(error))
        if single_workers > 1:
            print("NOTE: single-frame source runs serially; parallel workers apply to folders.", flush=True)
        records: list = []
        single_job_start = time.perf_counter()
        single_stages: dict | None = {} if args.time_stages else None
        original, output = compress_frame(args.source, args.destination, args.output_bits, args.mode, args.quality, args.companding, args.log_base, args.black_protect, defect_map, defect_map_name, records, (256, 256), single_stages, args.tile_layout, args.recording_mode)
        effective_single, explicit_single_none = resolve_verify_mode(args.verify_mode, args.verify_native)
        if args.mode == "lossless" and effective_single != "full":
            print("NOTE: Lossless mode requires full RAW verification; using full validation.", flush=True)
            effective_single = "full"
        if explicit_single_none:
            print("WARNING: --verify-mode none skips all output validation (benchmark/emergency use only).", flush=True)
        if effective_single != "none":
            ensure_vendor()
            import tifffile as _tf
            with _tf.TiffFile(args.source) as _src:
                _w = int(_src.pages[0].tags[TAG_IMAGE_WIDTH].value)
                _h = int(_src.pages[0].tags[TAG_IMAGE_LENGTH].value)
            check_frame_structure(args.destination, _w, _h, 12 if mode_is_lossy(args.mode) else args.output_bits)
            ext = verify_native(args.source, args.destination, args.output_bits, args.mode, single_stages, None, True, records[0].get("repairs", []) if records else None)
            error_mean, error_max = ext["mean"], ext["max"]
            breaches = check_guardrails(args.source.name, ext, guardrail_for_quality(args.quality))
            for breach in breaches:
                print(f"GUARDRAIL BREACH: {breach}", flush=True)
            if args.strict_guardrails and breaches:
                raise RuntimeError(f"Guardrail breach: {breaches[0]}")
            if args.roundtrip:
                rt = roundtrip_check(args.source, args.destination, mode=args.mode)
                print(f"Round-trip: {rt['detail']} (max {rt['max_diff']}, mean {rt['mean_diff']:.3f})", flush=True)
                if args.mode == "lossless" and rt["pass"] is False:
                    raise RuntimeError(f"Round-trip byte-exact claim violated: {rt['detail']}")
        else:
            error_mean, error_max = 0.0, 0
        if single_stages is not None:
            print_stage_summary(single_stages)
        ensure_vendor()
        import tifffile as _single_tf
        with _single_tf.TiffFile(args.source) as _single_doc:
            _w = int(_single_doc.pages[0].tags[TAG_IMAGE_WIDTH].value)
            _h = int(_single_doc.pages[0].tags[TAG_IMAGE_LENGTH].value)
        metadata_result = validate_metadata(args.source, args.destination)
        if not metadata_result["ok"]:
            raise RuntimeError(f"Metadata validation failed: {metadata_result['errors']}")
        single_error = ext if effective_single != "none" else {"mean": error_mean, "max": error_max}
        single_ratio = original / output if output else 0.0
        single_report = args.destination.with_name(args.destination.name + ".verification-report.json")
        write_verification_report(single_report, {
            "tool": "cdng single-frame verification",
            "compression_mode": args.mode,
            "lossy": mode_is_lossy(args.mode),
            "input_size_bytes": original,
            "output_size_bytes": output,
            "achieved_compression_ratio": round(single_ratio, 4),
            "storage_saved_percent": round((1.0 - output / original) * 100.0, 3) if original else 0.0,
            "input_bit_depth": source_bit_depth(args.source),
            "resolution": {"width": _w, "height": _h},
            "recording_mode": recording_configuration(_w, _h, defect_map, args.source.parent, args.recording_mode),
            "frames": 1,
            "processing_time_seconds": round(time.perf_counter() - single_job_start, 3),
            "validation_result": "passed",
            "audio": {"present": False, "copied": False, "verified": True, "files": [], "missing": []},
            "metadata_validation": metadata_result,
            "target_status": mode_target_status(args.mode, single_ratio),
            "exact_raw_comparison": {"checked": args.mode == "lossless", "differing_samples": 0 if args.mode == "lossless" else None},
            "verified": [{"file": args.source.name, "error": single_error}],
            "quality_metrics": summarize_quality_metrics([{"error": single_error}]),
        })
        map_mode = args.recording_mode
        if map_mode:
            metadata_path = write_resolve_metadata_csv(
                args.destination.with_name(args.destination.stem + ".resolve-metadata.csv"),
                ((args.destination, map_mode),))
            print(f"Resolve Comments metadata: {metadata_path}", flush=True)
        if mode_is_lossy(args.mode) and mode_target_status(args.mode, single_ratio) != "achieved":
            print(f"LIMITATION: {args.mode} target not achieved ({single_ratio:.2f}:1); selected mode was preserved.")
        print(f"OK {args.source.name}: {original} source-pixel bytes -> {output} DNG bytes; ratio={single_ratio:.2f}:1; mode={args.mode}; mean_abs_error={error_mean:.3f}; max_abs_error={error_max}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
