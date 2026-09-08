# RTX 4090 / Windows 10 performance update

## What is and is not fixed

This is a **source update for the matching v7.0 portable application**, not a
standalone release. Keep the original `bin/` payload and third-party notices.
The upstream README lists Windows 11. These Python changes target Windows 10,
but **Windows 10 native DLSS runtime compatibility has not been validated**.
No OS, signature, feature-support, or driver checks have been bypassed.

| Path | GPU selection after this update |
| --- | --- |
| RTX Video Super Resolution / RTX Video HDR | Existing worker receives an explicit DirectX LUID derived from the selected GPU's verified CUDA identity. |
| H.264 / H.265 / AV1 NVIDIA NVENC | Explicit verified CUDA ordinal; support is tested by the actual encoder. |
| DLSS Neural Rendering, including Live | The requested UUID is selected in Python, but the existing `nvngx.dll --video` wrapper does **not** pass an adapter selector. Native adapter remains **unverified**. |
| DLSS Frame Generation | Same limitation: existing `dlssg-worker.exe --serve` and `--probe` wrappers do not pass the selected GPU. |
| Plain H.264 / H.265 / AV1 and ProRes Proxy | Remain CPU encoders when explicitly selected. |

**This update does not yet guarantee that DLSS Neural Rendering or Frame
Generation uses GPU 0.** Their worker source is absent from this repository.
Adding unsupported CLI switches or `CUDA_VISIBLE_DEVICES=0` would not be an
honest fix for a Direct3D adapter-selection problem. RTX Video's documented-in-
source `--gpu-luid` switch must not be assumed to exist in those other binaries.

The DisplayPort cable may remain on GPU 1. Nothing here changes monitors,
primary display, browser/MPV preferences, registry settings, HAGS, clocks, power
limits, process priority, or CUDA visibility. The profile identifies the RTX
4090 by **model + stable UUID**, not by an assumption that Task Manager, DXGI,
nvidia-smi and CUDA all assign it the same index.

## Use the update

Close the app before applying source changes or changing/restoring the profile.
Apply the changed files to the matching portable installation (or use this
branch with that installation's existing `bin/`). Do not delete your media,
runtime, or other settings.

Double-click **`start_4090.bat`**. On its first run it:

1. Finds exactly one RTX 4090 (multiple cards require `--gpu-uuid GPU-...`).
2. Matches its PCI identity to the CUDA driver ordinal, refusing to guess or
   silently fall back to the display GPU.
3. Backs up affected settings to `config/rtx4090-profile.json`, then selects that
   UUID for AI and video processing. Existing plain H.264/H.265/AV1 choices are
   switched to their NVENC equivalents; ProRes and other controls are retained.
4. Launches with NVENC preset **p4**, unless `DLSS5_NVENC_PRESET` is already set.
   p4 trades some compression efficiency/quality for throughput relative to p6.
   Neural effect strength, DLSS model preset, resolution, HDR controls, bitrate
   quality selection and frame-generation rate are not changed.

Later launches preserve subsequent codec/quality edits. An explicit selection
of another GPU in Settings causes this launcher to stop rather than override it
silently. Use the normal launcher for that selection. The normal `start.bat`
remains unchanged and defaults to NVENC p6, although saved codec/GPU settings
remain applied until restored.

PowerShell, from the application folder:

```powershell
# Read-only report, including the native-affinity limitation:
.\start_4090.bat --diagnose

# Apply settings without starting the app:
.\start_4090.bat --apply

# Use original NVENC quality preset with the 4090 profile:
$env:DLSS5_NVENC_PRESET = 'p6'
.\start_4090.bat

# Restore the original affected settings (close the app first):
.\start_4090.bat --restore
```

Restore only reverses profile keys that still equal the applied values;
subsequent user edits are preserved. Other settings/sections are left intact.
The backup is created before the config write and never overwritten by a repeat
apply. Restored history is retained in `config/rtx4090-profile.restored.json`.
ConfigParser preserves values/sections, not original comments/whitespace.

## Performance changes

- Lazy top-level package exports avoid importing the complete optional media
  stack merely to import `src` or show the initial loading screen.
- Capacity-first Automatic GPU selection prefers the 24 GB 4090 over an 11 GB
  2080 Ti independently of enumeration/display order. It is a target-system
  heuristic, not a general GPU performance benchmark. Explicit choices win.
- CUDA ordinals are matched by PCI identity. The unsafe fallback from an unknown
  CUDA ordinal to an nvidia-smi index has been removed.
- Successful encoder probes are reused for 60 seconds in a bounded 128-entry
  cache. Keys include encoder, dimensions, CUDA device/visibility/order and
  executable identity. Failures and timeouts are retried, not cached. Probes
  have a 20-second timeout; actual encoder initialization still validates support.
- Motion vectors are scaled on their small guide grid before enlargement,
  avoiding two full-resolution strided multiplication passes. A reusable CPU
  float32 scratch buffer reduces allocation churn.
- OpenCV's CPU-dispatched FP16 conversion replaces expensive NumPy conversion
  where available, with a NumPy fallback for custom/older OpenCV builds. The
  returned half-precision buffer owns an independent backing allocation so
  queued frames never alias the reusable float32 scratch.
- Exactly unchanged NR grayscale guides skip optical flow without resetting
  temporal history. Scene-cut resets are retained; invalid flow resets safely.

## Measured validation, not an FPS promise

48 CPU unit tests passed on Linux, Python 3.13.5, NumPy 2.3.5 and OpenCV 4.13.0.
Coverage includes reversed GPU ordering, PCI/CUDA mismatches, explicit selection,
missing-device failure, probe caching, preserved CPU codec semantics, NVENC
command construction, HDR output arguments, motion equivalence/ownership,
FP16 bit representation, scene resets, lazy imports, and profile rollback.
Process-controller dependencies are substituted in encoder unit tests; codec
logic is loaded from the actual module. Hardware probes are mocked.

The included microbenchmark uses a 640x360 synthetic motion field, one OpenCV
CPU thread, 12 samples per path, warmup, alternating order, and median timings.
It covers **motion resizing + vector scaling + FP16 conversion only**:

| Motion output | Previous | Updated | Stage speedup |
| --- | ---: | ---: | ---: |
| 1920x1080 | 13.62 ms | 3.32 ms | 4.11x |
| 3840x2160 | 56.69 ms | 11.73 ms | 4.83x |
| 7680x4320 | 236.10 ms | 56.63 ms | 4.17x |

These are not Windows/4090 measurements, whole-guide timings, DLSS FPS, encoding
throughput or end-to-end render speedups. Floating-point order changes are
validated within tolerance; FP16 conversion itself is tested against NumPy.
Raw measurements are in `benchmark_motion_linux.json` next to this document.

```powershell
# Run with the portable Python (already contains the media dependencies):
.\bin\python-3.13.15-embed-amd64\python.exe -m unittest discover -s tests -v
.\bin\python-3.13.15-embed-amd64\python.exe tools\benchmark_motion.py --iterations 12
```

Full Gradio startup, packaged Windows native workers, actual 4090 VRAM usage,
NVENC output and visual/video correctness still require local hardware testing.
Compare the same clip/options before and after, using render elapsed time and
existing diagnostic timing fields. Check the actual worker process GPU engine
and dedicated memory rather than treating a Settings dropdown as proof.

## CUDA and the 24 GB VRAM question

The DLSS path in this repository uses D3D12/native NGX workers, not a trainable
PyTorch model. NVENC uses dedicated video-encoding hardware and CUDA device
identity; that does not turn DLSS into a CUDA/PyTorch pipeline. No separate CUDA
toolkit installation is introduced by this patch.

There is no exposed "use all VRAM", batch-size, TensorRT, or allocator control
for the bundled DLSS model. This update adds no artificial VRAM cap, but cannot
force opaque native runtimes to allocate a particular amount. Filling 24 GB
with dummy buffers would consume headroom rather than accelerate processing.
The 4090 and display GPU memories are not pooled. WDDM/runtime budgets and other
applications still limit actually available device memory.

The new scratch buffers are **system RAM**, not VRAM: roughly 16/63/253 MiB at
1080p/4K/8K. FP16 output buffers remain independently owned for queue safety.

To finish hard binding for NR/FG, the native worker source/build is needed:
accept an adapter LUID, create D3D12 on that adapter, report the selected LUID in
a versioned setup/probe response, and reject mismatches. GPU-resident frame
exchange, fenced upload/readback rings and explicit DXGI memory-budget handling
also belong in that native layer. None of these is claimed as implemented here.

References: [DXGI adapter preference](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_6/nf-dxgi1_6-idxgifactory6-enumadapterbygpupreference),
[NVENC presets](https://docs.nvidia.com/video-technologies/video-codec-sdk/13.0/nvenc-video-encoder-api-prog-guide/index.html),
[OpenCV FP16 conversion](https://docs.opencv.org/4.13.0/d2/de8/group__core__array.html).
