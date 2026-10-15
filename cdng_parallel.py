"""Opt-in parallel CinemaDNG conversion (ProcessPoolExecutor).

Frames are independent: each worker parses, optionally repairs, compresses,
verifies, and previews exactly one frame with the same code paths as the
serial converter. The parent owns preflight, ordered aggregation, the defect
report, companion copies, and all user-visible output. Default serial
behavior in cdng_lossless_mvp is untouched; this module is only used when
workers > 1 is requested.
"""

from __future__ import annotations

import concurrent.futures
import shutil
import sys
import time
import traceback
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import cdng_lossless_mvp as serial

AUTO_WORKERS = 6
MAX_WORKERS = 8


def resolve_workers(value: str) -> int:
    """Parse --workers auto|N. auto chooses 6; manual range is 1..8."""
    if value == "auto":
        return AUTO_WORKERS
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError("--workers must be auto or an integer 1..8.")
    if not 1 <= count <= MAX_WORKERS:
        raise ValueError("--workers must be auto or an integer 1..8.")
    return count


def parse_benchmark_workers(value: str) -> list[int]:
    """Parse --benchmark-workers '1,2,4,6,8' into a validated count list."""
    try:
        counts = [int(part) for part in value.split(",") if part.strip()]
    except ValueError:
        raise ValueError("--benchmark-workers must be a comma list like 1,2,4,6,8.")
    if not counts or any(not 1 <= count <= MAX_WORKERS for count in counts):
        raise ValueError("--benchmark-workers counts must each be 1..8.")
    return counts


def _worker_init() -> None:
    vendor = APP_DIR / "vendor"
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))
    import imagecodecs  # noqa: F401 - warm per-process codec state
    import numpy  # noqa: F401
    import tifffile  # noqa: F401
    # NOTE: do NOT touch TIFF.COMPRESSORS here. The codec is selected per task
    # via serial_mod._set_codec7 (ljpeg leaks into lossy DCT writes otherwise).


def _process_one_frame(task: dict, frame: dict, serial_mod, numpy, tifffile, time_module) -> dict:
    """Fused single-parse frame pipeline: parse once, reuse expected raster.

    Uses the same shared primitives as the serial path (identical bytes),
    but the source is parsed and tagged exactly once and verification
    reopens only the completed output.
    """
    result: dict = {"index": frame["index"], "name": frame["name"], "rel": frame.get("rel", frame["name"])}
    source, destination = Path(frame["source"]), Path(frame["destination"])
    stage = {} if task["time_stages"] else None
    try:
        records: list = []
        start = time_module.perf_counter()
        read_start = serial_mod._stage_begin(stage)
        with tifffile.TiffFile(source) as input_tiff:
            page = input_tiff.pages[0]
            width = int(page.tags[serial_mod.TAG_IMAGE_WIDTH].value)
            height = int(page.tags[serial_mod.TAG_IMAGE_LENGTH].value)
            _bits, linearization = serial_mod.source_linear_table(numpy, page)
            stored = serial_mod.read_source_stored(numpy, page)
            frame_map = ({**task["defect_map"],
                          "black_level_stored": serial_mod.stored_black_level(numpy, page, _bits, linearization),
                          "white_level_stored": serial_mod.stored_white_level(page, _bits),
                          "_frame_name": source.name} if task["defect_map"] is not None else None)
            source_ranges = {code: (page.tags[code].dtype, page.tags[code].value) for code in
                             (serial_mod.TAG_BLACK_LEVEL_REPEAT, serial_mod.TAG_BLACK_LEVEL,
                              serial_mod.TAG_BLACK_DELTA_H, serial_mod.TAG_BLACK_DELTA_V,
                              serial_mod.TAG_WHITE_LEVEL) if code in page.tags}
            if int(page.tags[serial_mod.TAG_COMPRESSION].value) != 1:
                raise ValueError("Input must be an uncompressed DNG frame.")
        serial_mod._stage_end(stage, "read_parse", read_start)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
        with tifffile.TiffFile(source) as input_tiff:
            page = input_tiff.pages[0]
            if task["defect_map"] is not None:
                serial_mod.validate_defect_map(task["defect_map"], page, width, height, task["defect_map_name"])
            extra_tags = serial_mod.add_recording_mode_metadata(
                serial_mod.collect_extra_tags(page, task["mode"], task["output_bits"], serial_mod.IMAGE_TAGS),
                task["defect_map"], task["recording_mode"])
        if task["defect_map"] is not None:
            repair_start = serial_mod._stage_begin(stage)
            stored, frame_repairs, frame_skipped = serial_mod.repair_stored_array(
                numpy, stored, width, height, linearization, frame_map)
            serial_mod._stage_end(stage, "linearize_repair", repair_start)
            records.append({"source": frame.get("rel", frame["name"]), "repairs": frame_repairs, "skipped": frame_skipped})
        output_bits = 12 if serial_mod.mode_is_lossy(task["mode"]) else task["output_bits"]
        if task["mode"] == "lossless" and output_bits < _bits:
            raise ValueError(
                f"Lossless --output-bits {output_bits} cannot hold {_bits}-bit source; "
                f"use --output-bits {_bits} or 12.")
        compand_start = serial_mod._stage_begin(stage)
        raster, output_bits, output_linearization, expected = serial_mod.prepare_raster_from_stored(
            numpy, stored, linearization, task["mode"], output_bits,
            task["companding"], task["log_base"], task["black_protect"])
        extra_tags.extend(serial_mod.normalise_range_tags(
            source_ranges, _bits, linearization, output_bits, output_linearization, numpy))
        serial_mod._stage_end(stage, "compand", compand_start)
        tile = serial_mod.resolve_tile_layout(task["tile_layout"], width, height)
        encode_start = serial_mod._stage_begin(stage)
        serial_mod._set_codec7(tifffile, task["mode"])
        serial_mod.write_dng(tifffile, destination, raster, output_bits, task["mode"],
                             task["quality"], tile, extra_tags, output_linearization)
        serial_mod._stage_end(stage, "encode_write", encode_start)
        output_bytes = destination.stat().st_size
        warned = bool(records and records[0]["skipped"])
        struct_start = serial_mod._stage_begin(stage)
        serial_mod.check_frame_structure(destination, width, height, output_bits)
        serial_mod._stage_end(stage, "structural", struct_start)
        error_ext, verified = {"mean": 0.0, "max": 0}, False
        if task["verify_mode"] == "full" or (task["verify_mode"] == "sampled" and (
                frame["index"] in task["native_set"] or warned)):
            error_ext = serial_mod.verify_raster(
                numpy, tifffile, expected, destination, output_bits, task["mode"], stage, extended=True)
            verified = True
        checksum = None
        if task["verify_mode"] in ("sampled", "checksum", "full"):
            checksum = serial_mod.file_sha256(destination)
        dng_ms = (time_module.perf_counter() - start) * 1000.0
        preview_ms, preview_bytes = 0.0, 0
        if task["preview"] != "none":
            preview_start = time_module.perf_counter()
            try:
                preview_bytes = serial_mod.generate_frame_preview(
                    source, Path(task["previews_dir"]), task["preview"], task["preview_quality"],
                    frame.get("rel"))
            except Exception as error:
                result["preview_error"] = f"{type(error).__name__}: {error}"
                warned = True
            preview_ms = (time_module.perf_counter() - preview_start) * 1000.0
            if stage is not None:
                stage["preview"] = stage.get("preview", 0.0) + preview_ms
        if warned and task["verify_mode"] == "sampled" and not verified:
            # Late warning (e.g. preview failure after the decode decision):
            # verify now so warned frames are always covered.
            error_ext = serial_mod.verify_raster(
                numpy, tifffile, expected, destination, output_bits, task["mode"], stage, extended=True)
            verified = True
        heatmap_bytes = 0
        if task["defect_map"] is not None and task.get("repair_maps", True) and records and records[0]["repairs"]:
            try:
                heatmap_path = (Path(task["heatmaps_dir"]) / frame.get("rel", frame["name"])).with_suffix(".heatmap.jpg")
                heatmap_bytes = serial_mod.write_repair_heatmap(source, heatmap_path, records[0]["repairs"])
            except Exception as error:
                result["heatmap_error"] = f"{type(error).__name__}: {error}"
        result.update({
            "dng_ms": dng_ms, "output_bytes": output_bytes,
            "error_mean": error_ext["mean"], "error_max": error_ext["max"],
            "error_ext": error_ext,
            "verified": verified, "warned": warned,
            "repairs": records[0]["repairs"] if records else [],
            "skipped": bool(records[0]["skipped"]) if records else False,
            "checksum": checksum,
            "preview_ms": preview_ms, "preview_bytes": preview_bytes,
            "heatmap_bytes": heatmap_bytes,
            "stages": stage or {},
        })
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
    return result


# Promotion gate for the one-pass fast path (Phase 5 of the fast-path-v2
# plan). False keeps the legacy fused pipeline; True routes workers through
# _process_one_frame_fast once Phase 3 parity proves byte identity.
FAST_PATH_DEFAULT = False


def _process_one_frame_fast(task: dict, frame: dict, serial_mod, numpy, tifffile, time_module) -> dict:
    """One-pass lossy fast path: composed LUT, neighborhood-only repair.

    Byte-identical to the legacy fused pipeline by construction (same
    composed indexing, same nearest-code rule, same methods/reports), with
    far fewer full-frame numpy passes and no expected raster unless
    verification needs it.
    """
    result: dict = {"index": frame["index"], "name": frame["name"], "rel": frame.get("rel", frame["name"])}
    source, destination = Path(frame["source"]), Path(frame["destination"])
    stage = {} if task["time_stages"] else None
    try:
        records: list = []
        start = time_module.perf_counter()
        read_start = serial_mod._stage_begin(stage)
        with tifffile.TiffFile(source) as _probe:
            _pg = _probe.pages[0]
            try:
                _bits = serial_mod.source_bits_from_page(_pg)
            except ValueError as error:
                raise ValueError(f"Unreadable BitsPerSample; failing closed: {error}") from error
        if _bits != 8:
            # Fast path is 8-bit only (composed 256-entry LUT); fall back to
            # the generalized fused pipeline for 10/12-bit input.
            return _process_one_frame(task, frame, serial_mod, numpy, tifffile, time_module)
        header, pixels, width, height, _ = serial_mod.parse_uncompressed_dng(source)
        serial_mod._stage_end(stage, "read_parse", read_start)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
        with tifffile.TiffFile(source) as input_tiff:
            page = input_tiff.pages[0]
            _, linearization = serial_mod.source_linear_table(numpy, page)
            frame_map = ({**task["defect_map"],
                          "black_level_stored": serial_mod.stored_black_level(numpy, page, _bits, linearization),
                          "white_level_stored": serial_mod.stored_white_level(page, _bits),
                          "_frame_name": source.name} if task["defect_map"] is not None else None)
            source_ranges = {code: (page.tags[code].dtype, page.tags[code].value) for code in
                             (serial_mod.TAG_BLACK_LEVEL_REPEAT, serial_mod.TAG_BLACK_LEVEL,
                              serial_mod.TAG_BLACK_DELTA_H, serial_mod.TAG_BLACK_DELTA_V,
                              serial_mod.TAG_WHITE_LEVEL) if code in page.tags}
            if len(numpy.asarray(linearization)) != 256:
                raise ValueError("Fast path requires a 256-entry 8-bit source table.")
            if task["defect_map"] is not None:
                serial_mod.validate_defect_map(task["defect_map"], page, width, height, task["defect_map_name"])
            extra_tags = serial_mod.add_recording_mode_metadata(
                serial_mod.collect_extra_tags(page, task["mode"], task["output_bits"], serial_mod.IMAGE_TAGS),
                task["defect_map"], task["recording_mode"])
        bundle = serial_mod.get_fast_luts(numpy, linearization, task["log_base"], task["black_protect"])
        if bytes(bundle["source_table"]) != bytes(numpy.asarray(linearization, dtype=numpy.uint16)):
            raise RuntimeError("Source LinearizationTable differs from the cached fast-path bundle; failing closed.")
        stored_view = numpy.frombuffer(pixels, dtype=numpy.uint8).reshape(height, width)
        output_bits = 12 if serial_mod.mode_is_lossy(task["mode"]) else task["output_bits"]
        warned = False
        patched: dict = {}
        if task["defect_map"] is not None:
            repair_start = serial_mod._stage_begin(stage)
            if frame_map.get("red_correction", "edge-aware") == "edge-aware" and any(
                    "gain" in site for site in frame_map["candidates"]):
                frame_repairs, frame_skipped = serial_mod.edge_repair_records(
                    numpy, stored_view, bundle["source_table"], frame_map)
                patched = {record["y"] * width + record["x"]: record["replacement_stored"]
                           for record in frame_repairs}
            else:
                plans = serial_mod.precompute_repair_plan(frame_map, width, height)
                patched, frame_repairs, frame_skipped = serial_mod.fast_repair_frame(
                    numpy, stored_view, width, height, bundle["source_table"],
                    bundle["source_inverse"], plans)
            serial_mod._stage_end(stage, "linearize_repair", repair_start)
            records.append({"source": frame.get("rel", frame["name"]), "repairs": frame_repairs, "skipped": frame_skipped})
            warned = bool(frame_skipped)
        compand_start = serial_mod._stage_begin(stage)
        raster = bundle["direct"][numpy.frombuffer(pixels, dtype=numpy.uint8).reshape(height, width)]
        if patched:
            # Repaired sites only; unmapped pixels are untouched by construction.
            flat_out = raster.ravel()
            for flat, code in patched.items():
                flat_out[flat] = bundle["direct"][code]
        serial_mod._stage_end(stage, "compand", compand_start)
        output_linearization = bundle["decode"] if output_bits == 12 else None
        extra_tags.extend(serial_mod.normalise_range_tags(
            source_ranges, _bits, linearization, output_bits, output_linearization, numpy))
        tile = serial_mod.resolve_tile_layout(task["tile_layout"], width, height)
        encode_start = serial_mod._stage_begin(stage)
        serial_mod._set_codec7(tifffile, task["mode"])
        serial_mod.write_dng(tifffile, destination, raster, output_bits, task["mode"],
                             task["quality"], tile, extra_tags, output_linearization)
        serial_mod._stage_end(stage, "encode_write", encode_start)
        del raster
        output_bytes = destination.stat().st_size
        struct_start = serial_mod._stage_begin(stage)
        serial_mod.check_frame_structure(destination, width, height, output_bits)
        serial_mod._stage_end(stage, "structural", struct_start)
        need_expected = task["verify_mode"] == "full" or (
            task["verify_mode"] == "sampled" and (frame["index"] in task["native_set"] or warned))
        expected = None
        if need_expected:
            if patched:
                base = numpy.frombuffer(bytearray(pixels), dtype=numpy.uint8)
                for flat, code in patched.items():
                    base[flat] = code
            else:
                base = numpy.frombuffer(bytearray(stored_view), dtype=numpy.uint8)
            expected = bundle["source_table"][base].reshape(height, width)
            del base
        error_ext, verified = {"mean": 0.0, "max": 0}, False
        if need_expected:
            error_ext = serial_mod.verify_raster(
                numpy, tifffile, expected, destination, output_bits, task["mode"], stage, extended=True)
            verified = True
        checksum = None
        if task["verify_mode"] in ("sampled", "checksum", "full"):
            checksum = serial_mod.file_sha256(destination)
        dng_ms = (time_module.perf_counter() - start) * 1000.0
        preview_ms, preview_bytes = 0.0, 0
        if task["preview"] != "none":
            preview_start = time_module.perf_counter()
            try:
                preview_bytes = serial_mod.generate_frame_preview(
                    source, Path(task["previews_dir"]), task["preview"], task["preview_quality"],
                    frame.get("rel"))
            except Exception as error:
                result["preview_error"] = f"{type(error).__name__}: {error}"
                warned = True
            preview_ms = (time_module.perf_counter() - preview_start) * 1000.0
            if stage is not None:
                stage["preview"] = stage.get("preview", 0.0) + preview_ms
        if warned and task["verify_mode"] == "sampled" and not verified:
            if expected is None:
                # Late preview warning: reconstruct from the retained source
                # view (kept alive until end of frame for exactly this case).
                if patched:
                    base = numpy.frombuffer(bytearray(pixels), dtype=numpy.uint8)
                    for flat, code in patched.items():
                        base[flat] = code
                else:
                    base = numpy.frombuffer(bytearray(stored_view), dtype=numpy.uint8)
                expected = bundle["source_table"][base].reshape(height, width)
                del base
            error_ext = serial_mod.verify_raster(
                numpy, tifffile, expected, destination, output_bits, task["mode"], stage, extended=True)
            verified = True
        heatmap_bytes = 0
        if task["defect_map"] is not None and task.get("repair_maps", True) and records and records[0]["repairs"]:
            try:
                heatmap_path = (Path(task["heatmaps_dir"]) / frame.get("rel", frame["name"])).with_suffix(".heatmap.jpg")
                heatmap_bytes = serial_mod.write_repair_heatmap(source, heatmap_path, records[0]["repairs"])
            except Exception as error:
                result["heatmap_error"] = f"{type(error).__name__}: {error}"
        result.update({
            "dng_ms": dng_ms, "output_bytes": output_bytes,
            "error_mean": error_ext["mean"], "error_max": error_ext["max"],
            "error_ext": error_ext,
            "verified": verified, "warned": warned,
            "repairs": records[0]["repairs"] if records else [],
            "skipped": bool(records[0]["skipped"]) if records else False,
            "checksum": checksum,
            "preview_ms": preview_ms, "preview_bytes": preview_bytes,
            "heatmap_bytes": heatmap_bytes,
            "stages": stage or {},
        })
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}\n{traceback.format_exc()}"
    return result


def _worker_task(task: dict) -> list:
    import time as time_module

    import numpy
    import tifffile

    import cdng_lossless_mvp as serial_mod

    use_fast = task.get("fast_path", FAST_PATH_DEFAULT) and serial_mod.mode_is_lossy(task.get("mode")) \
        and task.get("companding") == "log"
    process = _process_one_frame_fast if use_fast else _process_one_frame
    return [process(task, frame, serial_mod, numpy, tifffile, time_module)
            for frame in task["frames"]]


def check_disk_space(destination_root: Path, needed_bytes: int) -> tuple[int, bool]:
    """Return (free bytes, fits flag) for a 20% safety margin estimate."""
    free = serial.free_bytes_for(destination_root)
    return free, free >= int(needed_bytes * 1.2)


def convert_tree_parallel(source_root: Path, destination_root: Path, *, output_bits: int | None,
                          mode: str, quality: int, companding: str, log_base: float,
                          black_protect: int, verify: bool, copy_non_dng: bool,
                          defect_map: dict | None, defect_map_name: str,
                          defect_report: Path | None, preview: str = "none",
                          preview_quality: int = 60, workers: int = 6,
                          progress: bool = True, verify_mode: str | None = None,
                          verify_every: int = 32, tile_layout: str = "small",
                          time_stages: bool = False, task_chunk: int = 1,
                          fast_path: bool = FAST_PATH_DEFAULT,
                          strict_guardrails: bool = False,
                          resume: bool = False,
                          repair_maps: bool = False,
                          roundtrip: bool = False,
                          allow_low_space: bool = False,
                          direct_only: bool = False,
                          recording_mode: str | None = None,
                          root_companions_source: Path | None = None,
                          root_companions_destination: Path | None = None) -> dict:
    """Parallel folder conversion. Raises RuntimeError on worker failure."""
    job_start = time.perf_counter()
    mode = serial.canonical_mode(mode)
    recording_mode = serial.validate_recording_mode(recording_mode, defect_map)
    if preview not in ("none", "720p", "480p"):
        raise ValueError("--preview must be none, 720p, or 480p.")
    if not 1 <= workers <= MAX_WORKERS:
        raise ValueError("workers must be 1..8.")
    if task_chunk not in (1, 2, 4):
        raise ValueError("task chunk size must be 1, 2, or 4.")
    serial.ensure_vendor()
    serial.validate_numeric_args(log_base, black_protect, verify_every, preview_quality)
    effective_mode, explicit_none = serial.resolve_verify_mode(verify_mode, verify)
    if mode == "lossless" and effective_mode != "full":
        print("NOTE: Lossless mode requires full RAW verification; using full validation.", flush=True)
        effective_mode = "full"
    if explicit_none:
        print("WARNING: --verify-mode none skips all output validation (benchmark/emergency use only).", flush=True)
    files = sorted(path for path in (source_root.iterdir() if direct_only else source_root.rglob("*")) if path.is_file())
    dng_files = [path for path in files if path.suffix.lower() == ".dng"]
    if not dng_files:
        raise ValueError("No DNG files found in the selected input folder.")

    # ---- parent preflight: refuse before any worker starts ----
    serial.guard_destination(source_root, destination_root, allow_existing_root=resume)
    import tifffile as _tf
    with _tf.TiffFile(dng_files[0]) as _first:
        try:
            _fw = int(_first.pages[0].tags[256].value)
            _fh = int(_first.pages[0].tags[257].value)
            _source_bits = serial.source_bits_from_page(_first.pages[0])
        except KeyError as error:
            raise RuntimeError(f"First source frame is missing required tag {error}.")
    output_bits = serial.resolve_output_bits(output_bits, _source_bits, mode)
    out_bits = output_bits
    if tile_layout == "four" and (_fw != 3856 or _fh != 2170):
        raise ValueError(f"--tile-layout four supports 3856x2170 frames only, not {_fw}x{_fh}.")
    frames = []
    skipped: dict[int, dict] = {}
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
    for index, source in enumerate(dng_files):
        destination = destination_root / source.relative_to(source_root)
        if destination.exists():
            if not resume:
                raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
            if not serial.should_resume_skip(destination, _fw, _fh, out_bits):
                raise FileExistsError(
                    f"Resume cannot reuse {destination}: structurally invalid or incomplete; "
                    f"clear the output folder and rerun without --resume.")
            if progress:
                print(f"[{index + 1}/{len(dng_files)}] {source.relative_to(source_root)} (resumed, skipped)", flush=True)
            rel = source.relative_to(source_root).as_posix()
            prior = prior_repairs.get(rel, {})
            skipped[index] = {"output_bytes": destination.stat().st_size,
                              "checksum": serial.file_sha256(destination),
                              "repairs": prior.get("repairs", []),
                              "skipped": prior.get("skipped", False)}
            continue
        frames.append({"index": index, "name": source.name,
                       "rel": source.relative_to(source_root).as_posix(),
                       "source": str(source), "destination": str(destination)})
    companions = serial.companion_files(source_root, copy_non_dng, direct_only)
    if not resume:
        for source in companions:
            if (destination_root / source.relative_to(source_root)).exists():
                raise FileExistsError(f"Refusing to overwrite existing file: {destination_root / source.relative_to(source_root)}")
    root_companions = serial.plan_root_companions(root_companions_source, root_companions_destination)
    expected_bytes = sum(p.stat().st_size for p in dng_files + companions + root_companions)
    free_bytes, fits = check_disk_space(destination_root, expected_bytes)
    print(f"Disk check: {free_bytes / 2 ** 30:.1f} GiB free; estimated need "
          f"~{expected_bytes * 1.2 / 2 ** 30:.1f} GiB incl. 20% margin.", flush=True)
    if not serial.disk_space_ok(free_bytes, expected_bytes, allow_low_space):
        raise OSError("Insufficient free space for estimated output plus 20% safety margin; refusing to start.")
    if not fits:
        print("WARNING: continuing despite the conservative space estimate; the destination may run out of space and leave partial output.", flush=True)
    if defect_map is not None and defect_report is None:
        raise ValueError("--defect-report is required when --defect-map is supplied.")
    if defect_map is not None:
        defect_map = serial.prepare_region_repair_map(dng_files, defect_map)

    fingerprint = serial.settings_fingerprint(output_bits, mode, quality, companding, log_base, black_protect,
                                               serial.repair_fingerprint(defect_map), tile_layout, recording_mode)
    serial.check_resume_settings(destination_root, fingerprint, source_root, dng_files)
    if resume:
        cleared = serial.clear_stale_partials(destination_root)
        if cleared and progress:
            print(f"Resume: cleared {cleared} stale .partial file(s).", flush=True)

    destination_root.mkdir(parents=True, exist_ok=True)
    root_copy_start = time.perf_counter()
    if root_companions:
        serial.copy_root_companions(root_companions, root_companions_destination)
    root_copy_ms = (time.perf_counter() - root_copy_start) * 1000.0
    previews_dir = destination_root / "previews"
    if preview != "none":
        previews_dir.mkdir(exist_ok=True)

    seed_material = [f"{source.relative_to(source_root).as_posix()}:{source.stat().st_size}" for source in dng_files]
    seed = serial.structural_seed(sorted(seed_material))
    selected: dict[int, str] = {}
    if effective_mode == "sampled":
        preload = {i for i, p in enumerate(dng_files)
                   if prior_repairs.get(p.relative_to(source_root).as_posix(), {}).get("skipped")}
        selected = serial.select_verification_set(len(dng_files), verify_every, preload, seed)
    native_set = set(selected)
    settings = {"output_bits": output_bits, "mode": mode, "quality": quality,
                "companding": companding, "log_base": log_base,
                "black_protect": black_protect, "verify_mode": effective_mode,
                "native_set": native_set,
                "defect_map": defect_map, "defect_map_name": defect_map_name,
                "preview": preview, "preview_quality": preview_quality,
                "previews_dir": str(previews_dir), "tile_layout": tile_layout,
                "recording_mode": recording_mode,
                "time_stages": time_stages, "fast_path": fast_path,
                "repair_maps": repair_maps,
                "heatmaps_dir": str(destination_root / "repair-maps")}
    tasks = []
    for start in range(0, len(frames), task_chunk):
        task = dict(settings)
        task["frames"] = frames[start:start + task_chunk]
        tasks.append(task)

    # ---- bounded parallel execution (at most 2 x workers outstanding) ----
    results: dict[int, dict] = {}
    failures: list[str] = []
    pending: dict[concurrent.futures.Future, int] = {}
    queue = list(tasks)
    done_count = 0
    pool_start = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,
                                                initializer=_worker_init) as pool:
        pool_ready_s = time.perf_counter() - pool_start
        try:
            wall_start = time.perf_counter()
            while queue or pending:
                while queue and len(pending) < 2 * workers and not failures:
                    task = queue.pop(0)
                    pending[pool.submit(_worker_task, task)] = len(pending)
                if not pending:
                    break
                finished, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in finished:
                    pending.pop(future)
                    try:
                        frame_results = future.result()
                    except Exception as error:  # pragma: no cover - defensive
                        frame_results = [{"index": -1, "name": "?",
                                          "error": f"{type(error).__name__}: {error}"}]
                    for result in frame_results:
                        results[result["index"]] = result
                        done_count += 1
                        if "error" in result:
                            failures.append(f"{result.get('name')}: {result['error'].splitlines()[0]}")
                        elif progress:
                            print(f"[{done_count}/{len(frames)}] {result.get('name')} "
                                  f"{result.get('dng_ms', 0.0):.0f} ms", flush=True)
        finally:
            # On failure: queued work is cancelled, running workers finish
            # safely, partial output is left in place (never auto-deleted).
            for future in list(pending):
                future.cancel()
    wall_s = time.perf_counter() - wall_start
    if failures:
        raise RuntimeError(f"Parallel run failed on {len(failures)} frame(s): "
                           + "; ".join(failures[:5]))

    # ---- resumed frames rejoin here (validated structurally in preflight) ----
    for index, skip in skipped.items():
        results[index] = {"index": index, "name": dng_files[index].name,
                          "rel": dng_files[index].relative_to(source_root).as_posix(),
                          "dng_ms": 0.0, "output_bytes": skip["output_bytes"],
                          "error_mean": 0.0, "error_max": 0,
                          "error_ext": {"mean": 0.0, "max": 0},
                          "verified": False, "warned": bool(skip.get("skipped")),
                          "repairs": skip.get("repairs", []), "skipped": skip.get("skipped", False),
                          "resumed": True,
                          "checksum": skip["checksum"],
                          "preview_ms": 0.0, "preview_bytes": 0, "stages": {}}
    if skipped and (effective_mode == "full" or effective_mode == "sampled"):
        import numpy as _np
        import tifffile as _tf2
        for index in sorted(skipped):
            if effective_mode == "sampled" and index not in native_set:
                continue
            src, dst = dng_files[index], destination_root / dng_files[index].relative_to(source_root)
            with _tf2.TiffFile(src) as _doc:
                _page = _doc.pages[0]
                _, _table = serial.source_linear_table(_np, _page)
                _stored = serial.read_source_stored(_np, _page)
            _repairs = skipped[index].get("repairs", []) if isinstance(skipped[index], dict) else []
            _expected = _table[_stored] if (out_bits == 12 or serial.mode_is_lossy(mode)) else _stored
            if _repairs:
                _expected = serial._apply_expected_repairs(
                    _np, _expected, _repairs,
                    _table if (out_bits == 12 or serial.mode_is_lossy(mode)) else None)
            _err = serial.verify_raster(_np, _tf2, _expected, dst, out_bits, mode, None, extended=True)
            results[index]["error_ext"] = _err
            results[index]["error_mean"] = _err["mean"]
            results[index]["error_max"] = _err["max"]
            results[index]["verified"] = True

    # ---- ordered aggregation in original input order ----
    rel_of = {i: p.relative_to(source_root).as_posix() for i, p in enumerate(dng_files)}
    ordered = [results[index] for index in range(len(dng_files))]
    output_rels = sorted(rel_of[r["index"]] for r in ordered)
    expected_rels = sorted(f.relative_to(source_root).as_posix() for f in dng_files)
    if output_rels != expected_rels or len(set(output_rels)) != len(dng_files):
        raise RuntimeError("Verification failed: output filename set does not match source (missing/duplicate frames).")
    for r in ordered:
        if r.get("warned"):
            selected[r["index"]] = "warning"
    guard = serial.guardrail_for_quality(quality)
    guardrail_breaches: list[str] = []
    verified_entries = []
    for r in ordered:
        if not r.get("verified"):
            continue
        rel = rel_of[r["index"]]
        breaches = serial.check_guardrails(rel, r.get("error_ext", {}), guard)
        if breaches:
            guardrail_breaches.extend(breaches)
            print(f"GUARDRAIL BREACH: {'; '.join(breaches)}", flush=True)
        verified_entries.append({"file": rel, "error": r.get("error_ext", {}),
                                 "guardrail_warnings": breaches})
    if guardrail_breaches:
        print(f"GUARDRAIL SUMMARY: {len(guardrail_breaches)} breach(es); see verification-report.json (warnings only unless --strict-guardrails).", flush=True)
    metadata_entries = []
    for source in dng_files:
        rel = source.relative_to(source_root).as_posix()
        result = serial.validate_metadata(source, destination_root / source.relative_to(source_root))
        metadata_entries.append({"file": rel, **result})
        if not result["ok"]:
            raise RuntimeError(f"Metadata validation failed for {rel}: {result['errors']}")
    roundtrip_entries: list[dict] = []
    if roundtrip:
        for r in ordered:
            if not r.get("verified"):
                continue
            src = dng_files[r["index"]]
            dst = destination_root / dng_files[r["index"]].relative_to(source_root)
            try:
                rt = serial.roundtrip_check(src, dst, mode=mode)
            except RuntimeError as error:
                raise RuntimeError(f"Round-trip check failed for {rel_of[r['index']]}: {error}")
            roundtrip_entries.append({"file": rel_of[r["index"]], "roundtrip": rt})
            if mode == "lossless" and rt["pass"] is False:
                raise RuntimeError(f"Round-trip byte-exact claim violated for {rel_of[r['index']]}: {rt['detail']}")
    verification_payload = {
        "tool": "cdng parallel verification",
        "mode": effective_mode,
        "verify_every": verify_every,
        "seed": seed,
        "guardrails_proposed": guard,
        "guardrail_breaches": guardrail_breaches,
        "strict_guardrails": strict_guardrails,
        "quality": quality,
        "settings_fingerprint": fingerprint,
        "roundtrip": roundtrip_entries,
        "selected": {str(index): {"file": rel_of[index], "reason": reason}
                     for index, reason in sorted(selected.items())},
        "verified": verified_entries,
        "quality_metrics": serial.summarize_quality_metrics(verified_entries),
        "warned": [rel_of[r["index"]] for r in ordered if r.get("warned")],
        "structural": {"checked": len(ordered), "failed": 0},
    }
    serial.write_verification_report(destination_root / "verification-report.json", verification_payload)
    if strict_guardrails and guardrail_breaches:
        raise RuntimeError(f"Guardrail breach: {guardrail_breaches[0]} (+{len(guardrail_breaches)-1} more).")
    defect_records = [{"source": rel_of[r["index"]],
                       "repairs": r["repairs"], "skipped": r["skipped"],
                       "resumed": r.get("resumed", False)} for r in ordered]
    if defect_map is not None:
        serial.write_defect_report(defect_report, defect_map, defect_map_name, defect_records)
    checksum_entries = []
    if effective_mode in ("sampled", "checksum", "full"):
        for r in ordered:
            destination = destination_root / dng_files[r["index"]].relative_to(source_root)
            checksum_entries.append((str(destination.relative_to(destination_root)), r["checksum"]))
        serial.write_checksums_manifest(destination_root, checksum_entries)
    copy_start = time.perf_counter()
    for source in companions:
        destination = destination_root / source.relative_to(source_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if resume and serial.file_sha256(destination) == serial.file_sha256(source):
                continue
            raise FileExistsError(f"Refusing to overwrite existing file: {destination}")
        shutil.copy2(source, destination)
    copy_ms = root_copy_ms + (time.perf_counter() - copy_start) * 1000.0
    local_audio = [p for p in companions if p.suffix.lower() in serial.AUDIO_SUFFIXES]
    root_audio = [p for p in root_companions if p.suffix.lower() in serial.AUDIO_SUFFIXES]
    audio_sources = local_audio + root_audio
    audio_missing = [p.relative_to(source_root).as_posix() for p in local_audio
                     if not (destination_root / p.relative_to(source_root)).is_file()]
    audio_missing += [f"parent/{p.name}" for p in root_audio
                      if not (Path(root_companions_destination) / p.name).is_file()]
    if audio_missing:
        raise RuntimeError(f"Associated audio was not copied: {audio_missing}")
    total_input = sum(serial.source_raw_bytes(p) for p in dng_files)
    total_output = sum(r["output_bytes"] for r in ordered)
    ratio = total_input / total_output if total_output else 0.0
    target_status = serial.mode_target_status(mode, ratio)
    serial.update_report_statistics(destination_root / "verification-report.json", {
        "input_size_bytes": total_input,
        "output_size_bytes": total_output,
        "input_dng_container_bytes": sum(p.stat().st_size for p in dng_files),
        "achieved_compression_ratio": round(ratio, 4),
        "storage_saved_percent": round((1.0 - total_output / total_input) * 100.0, 3) if total_input else 0.0,
        "input_bit_depth": sorted({serial.source_bit_depth(p) for p in dng_files}),
        "resolution": {"width": _fw, "height": _fh},
        "recording_mode": serial.recording_configuration(_fw, _fh, defect_map, source_root, recording_mode),
        "compression_mode": mode,
        "frames": len(dng_files),
        "processing_time_seconds": round(time.perf_counter() - job_start, 3),
        "validation_result": "passed" if not guardrail_breaches else "passed_with_guardrail_warnings",
        "audio": {"present": bool(audio_sources), "files": ([p.relative_to(source_root).as_posix() for p in local_audio] + [f"parent/{p.name}" for p in root_audio]), "copied": not audio_missing, "verified": not audio_missing, "missing": audio_missing},
        "metadata_validation": {"ok": all(item["ok"] for item in metadata_entries), "frames_checked": len(metadata_entries), "failures": [item for item in metadata_entries if not item["ok"]]},
        "target_status": target_status,
        "lossy": serial.mode_is_lossy(mode),
        "exact_raw_comparison": {"checked": mode == "lossless", "differing_samples": 0 if mode == "lossless" else None},
    })
    map_mode = recording_mode
    if map_mode:
        metadata_path = serial.write_resolve_metadata_csv(
            destination_root / "resolve-metadata.csv",
            ((destination_root / source.relative_to(source_root), map_mode) for source in dng_files),
            allow_existing=resume)
        print(f"Resolve Comments metadata: {metadata_path}", flush=True)
    preview_failures = [rel_of[r["index"]] for r in ordered if r.get("preview_error")]
    for name in [rel_of[r["index"]] for r in ordered if r.get("preview_error")]:
        print(f"WARNING: preview failed for {name} (DNG output stands).", flush=True)
    heatmap_failures = [rel_of[r["index"]] for r in ordered if r.get("heatmap_error")]
    for name in heatmap_failures:
        print(f"WARNING: heatmap failed for {name} (DNG output stands).", flush=True)
    stage_times: dict = {}
    for r in ordered:
        for stage, value in r.get("stages", {}).items():
            stage_times.setdefault(stage, []).append(value)
    stage_times["pool_startup"] = [pool_ready_s * 1000.0]
    stage_times["companion_copy"] = [copy_ms]
    print(
        f"COMPLETE {len(dng_files)} DNG frames ({len(frames)} converted, {len(skipped)} resumed): "
        f"{total_input} bytes -> {total_output} bytes "
        f"({1 - total_output / total_input:.1%} smaller; ratio={ratio:.2f}:1; mode={mode}; "
        f"output bits={12 if serial.mode_is_lossy(mode) else output_bits}; verify-mode={effective_mode}; "
        f"workers={workers})",
        flush=True,
    )
    if serial.mode_is_lossy(mode) and target_status != "achieved":
        print(f"LIMITATION: {mode} target not achieved ({ratio:.2f}:1); selected mode was preserved.", flush=True)
    return {
        "frames": len(frames), "wall_s": wall_s,
        "dng_ms": [r["dng_ms"] for r in ordered],
        "preview_ms": [r.get("preview_ms", 0.0) for r in ordered],
        "errors": [(r["error_mean"], r["error_max"]) for r in ordered],
        "error_ext": [r.get("error_ext", {}) for r in ordered if r.get("verified")],
        "verified": [r.get("verified", False) for r in ordered],
        "verification": verification_payload,
        "dng_bytes": total_output, "input_bytes": total_input,
        "preview_bytes": sum(r.get("preview_bytes", 0) for r in ordered),
        "preview_count": sum(1 for r in ordered if r.get("preview_bytes")),
        "preview_failures": preview_failures,
        "defect_records": defect_records,
        "stage_times": stage_times,
        "pool_startup_s": pool_ready_s,
        "job_statistics": {
            "input_size_bytes": total_input,
            "output_size_bytes": total_output,
            "achieved_compression_ratio": round(ratio, 4),
            "storage_saved_percent": round((1.0 - total_output / total_input) * 100.0, 3) if total_input else 0.0,
            "input_bit_depth": sorted({serial.source_bit_depth(p) for p in dng_files}),
            "resolution": {"width": _fw, "height": _fh},
            "recording_mode": serial.recording_configuration(_fw, _fh, defect_map, source_root, recording_mode),
            "compression_mode": mode,
            "frames": len(dng_files),
            "processing_time_seconds": round(time.perf_counter() - job_start, 3),
            "validation_result": "passed" if not guardrail_breaches else "passed_with_guardrail_warnings",
            "audio": {"present": bool(audio_sources), "files": ([p.relative_to(source_root).as_posix() for p in local_audio] + [f"parent/{p.name}" for p in root_audio]), "copied": not audio_missing, "verified": not audio_missing, "missing": audio_missing},
            "metadata_validation": {"ok": all(item["ok"] for item in metadata_entries), "frames_checked": len(metadata_entries), "failures": [item for item in metadata_entries if not item["ok"]]},
            "target_status": target_status,
            "lossy": serial.mode_is_lossy(mode),
            "exact_raw_comparison": {"checked": mode == "lossless", "differing_samples": 0 if mode == "lossless" else None},
        },
    }
