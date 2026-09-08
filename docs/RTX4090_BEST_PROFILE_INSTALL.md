# RTX 4090 Best Profile - Clean Portable Install

The RTX 4090 branch includes a one-click clean installer for the matching DLSS 5 Visual Enhancer v7.0 portable runtime.

## Recommended install

1. Close DLSS 5 Visual Enhancer if it is running.
2. Download or update this branch on your Windows PC.
3. Double-click `install_clean_4090.bat` from the repository root.
4. The installer downloads the verified v7.0 portable archive, checks its SHA-256, overlays this repository's modified source, verifies the required runtime files, applies the RTX 4090 profile, and launches the installed copy.

The default destination is a sibling folder named `DLSS5_4090_PORTABLE`. If that destination already exists it is moved to a timestamped backup first rather than deleted.

### Windows path-quoting fix

The installer now forwards its repository path to Windows PowerShell as `%~dp0.` instead of a quoted `%~dp0` value ending in a backslash. This avoids the Windows command-line parsing edge case that can leave an embedded quote in `SourceRoot` and cause:

```text
Exception calling "IsPathRooted" with "1" argument(s): "Illegal characters in path."
```

The PowerShell helper also normalizes forwarded path arguments defensively and reports the offending path name if it is malformed. If you saw that error with an earlier copy, update the branch and rerun `install_clean_4090.bat`.

## Optional commands

```powershell
# Install to another folder
.\install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090"

# Install to another folder and launch it
.\install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090" -Launch

# Keep the downloaded runtime archive in the temporary staging directory while the installer runs
.\install_clean_4090.bat -KeepDownload
```

## RTX 4090 profile

The profile identifies an RTX 4090 by stable NVIDIA UUID and verified PCI-to-CUDA mapping. It selects that UUID for AI/video processing and uses NVIDIA NVENC variants for supported H.264/H.265/AV1 selections. The dedicated launcher defaults to NVENC preset p5 for a performance/quality balance; set `DLSS5_NVENC_PRESET=p6` before launch if you prefer a slower higher-quality encode preset.

Your monitor can remain attached to the second GPU. The profile does not change Windows display assignments, registry display preferences, CUDA visibility, clock settings, power limits, or VRAM allocation.

## Fast Neural Rendering preview path

The 4090 launcher now enables two performance paths that are deliberately limited to work FFmpeg can actually accelerate:

1. **3-second browser-compatible previews use H.264 NVENC.** The old compatibility path forced plain `H.264`, which selected `libx264` on the CPU. At outputs such as 2112x3696 this can throttle the rendered-frame queue and make the whole preview wait for CPU encoding. The 4090 launcher sets `DLSS5_FAST_PREVIEW_NVENC=1`, causing forced H.264/MP4 previews to use `H.264 (NVIDIA NVENC)` and the selected Video Processing GPU.
2. **Input video decode can use FFmpeg CUDA/NVDEC.** `DLSS5_CUDA_DECODE=auto` installs a frame-stream wrapper around the Neural Rendering video processor. It tries hardware decode on the selected AI GPU, prefers CUDA-side conversion to RGBA when the bundled FFmpeg filter supports it, preserves frame PTS through a NUT pipe, and falls back to the original PyAV/software decoder when the source or filter path is unsupported.

The original processing path remains available:

```powershell
set DLSS5_FAST_PREVIEW_NVENC=0
set DLSS5_CUDA_DECODE=off
.\start_4090.bat
```

For testing CUDA decode strictly, use:

```powershell
set DLSS5_CUDA_DECODE=on
.\start_4090.bat
```

`on` fails instead of falling back if FFmpeg CUDA cannot initialize. `auto` is recommended.

Every successful Neural Rendering video report now adds `cuda_decode` and `performance_analysis` sections. `performance_analysis.dlss_feature18_ms_per_frame` shows the synchronous native feature-18 time, while `rgba_output_readback_gib` shows how much full-resolution RGBA data was returned by the native worker. This distinguishes FFmpeg decode/encode bottlenecks from the DLSS feature evaluation/readback stage.

### What CUDA cannot replace

Neural Rendering still executes signed NVIDIA feature 18 in the native D3D12/NGX worker one frame at a time. FFmpeg CUDA can accelerate source decode, compatible pixel conversion, and NVENC output, but it cannot replace feature-18 evaluation or its RGBA readback. If `dlss_feature18_seconds` accounts for most of the elapsed time after the new FFmpeg paths are enabled, the remaining optimization belongs in the native worker: GPU-resident texture exchange, asynchronous/fenced queues, fewer CPU copies, or a native encoder handoff.

## Important native-worker limitation

RTX Video can receive the selected DirectX adapter through the worker's existing GPU LUID interface, and NVENC receives a verified CUDA ordinal. The current prebuilt Neural Rendering and DLSS Frame Generation workers do not expose a verified adapter-selection argument through their wrappers in this source tree. Selecting the RTX 4090 in Python therefore does not by itself prove that those opaque native workers are hard-bound to GPU 0.

The installer does not bypass NVIDIA feature/driver checks, invent unsupported worker flags, force 24 GB of VRAM to be allocated, pool the VRAM of both GPUs, or install a separate CUDA toolkit.

## Restore profile settings

Inside the installed portable folder, close the application and run:

```powershell
.\start_4090.bat --restore
```

Only profile keys that still equal the values applied by the profile are restored; later user changes are preserved.
