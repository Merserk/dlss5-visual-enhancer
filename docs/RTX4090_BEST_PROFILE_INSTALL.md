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

The profile identifies an RTX 4090 by stable NVIDIA UUID and verified PCI-to-CUDA mapping. It selects that UUID for AI/video processing and uses NVIDIA NVENC variants for supported H.264/H.265/AV1 selections. The dedicated launcher defaults to NVENC preset p4 for a performance-oriented balance; set `DLSS5_NVENC_PRESET=p6` before launch if you prefer the normal higher-quality preset.

Your monitor can remain attached to the second GPU. The profile does not change Windows display assignments, registry display preferences, CUDA visibility, clock settings, power limits, or VRAM allocation.

## Important native-worker limitation

RTX Video can receive the selected DirectX adapter through the worker's existing GPU LUID interface, and NVENC receives a verified CUDA ordinal. The current prebuilt Neural Rendering and DLSS Frame Generation workers do not expose a verified adapter-selection argument through their wrappers in this source tree. Selecting the RTX 4090 in Python therefore does not by itself prove that those opaque native workers are hard-bound to GPU 0.

The installer does not bypass NVIDIA feature/driver checks, invent unsupported worker flags, force 24 GB of VRAM to be allocated, pool the VRAM of both GPUs, or install a separate CUDA toolkit.

## Restore profile settings

Inside the installed portable folder, close the application and run:

```powershell
.\start_4090.bat --restore
```

Only profile keys that still equal the values applied by the profile are restored; later user changes are preserved.
