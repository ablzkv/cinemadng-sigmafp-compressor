# Sigma fp CinemaDNG Compressor

A Windows desktop utility for converting Sigma fp CinemaDNG footage into tiled, compressed CinemaDNG. The app includes its Python runtime and libraries; it does not need a separate Python installation or an internet connection to run.

## Support

If you find this useful, you can support continued development on [Ko-fi](https://ko-fi.com/blatzkrov).

## Start the app

1. Keep this whole folder together, including `runtime`, `vendor`, and `assets`.
2. Double-click **Run CinemaDNG Compressor.cmd**.
3. Choose the source footage folder and a separate output folder.
4. Choose a preset and verification level, then use **Dry-run** to check the job before starting.

The source folder is read-only to the conversion workflow. The output folder must not be the source, inside the source, or contain the source. Subfolders are always preserved.

## Presets

| Preset | Output | Intended use |
| --- | --- | --- |
| Lossless | 12-bit, RAW-exact values | Archival or when pixel values must be preserved |
| High Quality | 12-bit, lossy quality 90 | General editing |
| Compact | 12-bit, lossy quality 82 | Smaller editing files |

High Quality and Compact use log companding before JPEG encoding. Compression ratios vary with footage; the estimates shown in the app are targets, not guarantees.

## Verification and safety

- **Sampled** is the default. It checks selected frames, including first, middle, last, periodic frames, and flagged frames.
- **Full** decodes and checks every frame. Use it when the additional time is acceptable.
- **Checksum** checks that files match their recorded hashes; it does not prove image values are correct.
- **Dry-run** performs preflight checks and writes no conversion output.
- **Resume** reuses structurally valid frames from an interrupted job. Keep the output folder and its source manifest intact.
- **Stop after current work** keeps completed output.
- To reuse a non-empty destination, enable **Allow existing output** and type `OVERWRITE` exactly. The old destination is moved to a dated backup folder; it is not deleted.
- The low-space override disables only the estimate-based stop. The destination disk can still fill during conversion.

Input must be an uncompressed, little-endian classic TIFF/DNG containing one-channel Bayer RAW data at 8, 10, or 12 bits. Compressed DNG input is rejected. The four-tile layout is experimental and only supports 3856 × 2170 frames; the small-tile layout is the normal choice.

## Other app controls

- **Analyze & Recommend** samples footage and suggests a preset. It does not change the preset until you apply the recommendation.
- **Batch Queue** runs queued folder jobs in order. “Continue on error” controls whether later jobs run after a failure.
- **Strict guardrails** turns image-error threshold warnings into failures.
- **Keep reports in output** leaves reports beside the converted footage. By default, reports are moved to the app's `logs/sessions` folder; the resume manifest stays with output when Resume is enabled.
- **Support bundle** packages conversion logs, reports, and environment details for troubleshooting.

## What the app writes

Converted DNG frames retain the input folder structure. Depending on selected options, output can also include previews, `checksums.sha256`, `verification-report.json`, a Resolve comments CSV, and a resume manifest. Session logs and archived reports are stored under `logs/sessions` beside the app when that folder is writable. If it is not writable, the app uses a session folder beside the output.

## Project map

- `sigma_fp_app.py` — Tk/CustomTkinter window, folder validation, queue, progress, and launching the converter.
- `gui_theme.py` — shared colors, fonts, and GUI sizing.
- `cdng_lossless_mvp.py` — DNG reading/writing, serial conversion, metadata, verification, reports, preflight checks, and the command-line entry point.
- `cdng_parallel.py` — parallel folder conversion.
- `assets/mountain_header.png` — window header artwork.
- `runtime/` — portable Python interpreter and standard library.
- `vendor/` — bundled Python packages, including NumPy, tifffile, imagecodecs, CustomTkinter, and Pillow.
- `Run CinemaDNG Compressor.cmd` — Windows launcher; it starts the Python GUI and sets the app folder as the working directory.

This is a Python app launched from source files, not a compiled standalone executable. Keep the runtime and vendor folders beside the Python files.

## Notes for future AI / maintainers

- Treat this copy as compression-only. Do not reintroduce red-pixel repair or defect-map UI, command-line flags, or conversion behavior.
- The GUI and public command-line options no longer expose repair. Some dormant defect-map and repair helpers remain in the shared converter modules and reference modules that are not included in this copy. Do not call those paths; remove them only as a separate, carefully tested cleanup.
- Preserve fail-closed checks around input format, source/output overlap, destination reuse, disk-space estimates, resume fingerprints, and frame verification.
- Keep the portable runtime and vendor packages together with the app. `ensure_vendor()` adds the local `vendor` folder to Python's import path; do not replace it with network installs as part of ordinary startup.
- The app's backend is also a CLI. Use `runtime\python.exe cdng_lossless_mvp.py --help` to inspect supported options. Red-pixel repair options must remain absent.
- The original development folder contains Python tests; this cleaned release copy does not. The older full suite includes tests for removed repair and GUI workflows, so select and update compression-only tests when changing this copy. Keep generated fixtures and conversion outputs outside the source footage.
- For conversion changes, cover lossless pixel equality, lossy verification, 8/10/12-bit inputs, both tested Sigma fp frame sizes, resume behavior, companions, disk-space checks, and serial/parallel output consistency.
- Before release, run a real Sigma fp clip through the GUI on a clean Windows account or machine, inspect the output in the intended editor, and confirm the launcher works from a path containing spaces.

### Known validation limit

Synthetic DNG fixtures in the original development test folder covered all three presets and the 8/10/12-bit paths. No real camera clip or third-party editor was available for this release-readiness pass.