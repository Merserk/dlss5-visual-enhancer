# RTX 4090 best profile and clean installer

This branch targets the dual-GPU Windows setup where the RTX 4090 is the
compute/processing card and a second GPU may drive the monitors.

## Normal launch

Double-click `start_4090.bat`.

It runs:

```text
tools\rtx4090_profile.py --launch --best-settings
```

The launcher defaults `DLSS5_NVENC_PRESET=p5`. Set the environment variable
before launch if you intentionally want another NVENC preset.

The best profile controls only hardware-oriented defaults:

- AI Processing GPU: verified RTX 4090 UUID.
- Video Processing GPU: verified RTX 4090 UUID.
- Neural Rendering video: H.265 (NVIDIA NVENC), MP4, Auto bitrate.
- Frame Interpolation: Auto engine, H.265 (NVIDIA NVENC), MP4, Auto bitrate.
- RTX Video: VSR enabled at quality 4 / Ultra for video and image modes.
- RTX Video video output: H.265 (NVIDIA NVENC), MP4, Auto bitrate.
- Preview Encoding: Auto.
- DLSS model preset: Default, so NVIDIA/runtime selection is not replaced with
  an unverified forced model hint.

It intentionally does **not** force the upscaling factor, Frame Interpolation
target FPS, HDR enablement, or Neural Rendering style/effect strengths. Those
are content/output decisions rather than RTX 4090 hardware tuning.

The profile state is reversible. A profile created by the previous PR (schema
v1) is upgraded to schema v2 on the next best-profile launch without replacing
its original rollback values.

```powershell
.\start_4090.bat --diagnose
.\start_4090.bat --restore
```

`--restore` only reverts profile-controlled settings that still equal the
profile-applied values, preserving later unrelated/user changes.

## Clean portable install

A source checkout intentionally does not include the large portable runtime.
Double-click:

```text
install_clean_4090.bat
```

With no arguments it creates a sibling directory named
`DLSS5_4090_PORTABLE`, applies the best profile, and launches it.

The installer:

1. Requires native 64-bit Windows and Windows PowerShell.
2. Downloads the official `Merserk/dlss5-visual-enhancer` v7.0 portable ZIP.
3. Verifies SHA-256
   `995a3c8ec73cac1dce8b329279b2ec66f4b9f52048b7d119dcb46c6b7e0914cb`
   before extraction.
4. Builds a staged install from that verified runtime.
5. Overlays application/source files from this repository. Runtime binaries
   always come from the verified upstream archive, not an arbitrary local
   `bin/` directory.
6. Checks the required portable Python, FFmpeg/ffprobe and DLSS runtime files.
7. If the requested destination already exists, renames it to a timestamped
   backup instead of deleting it.
8. Moves the staged install into place, writes `install_manifest_4090.json`,
   and applies `--best-settings`.
9. Deletes the temporary staging/download data unless `-KeepDownload` is used.

Custom path examples:

```powershell
.\install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090"
.\install_clean_4090.bat -InstallDir "E:\Apps\DLSS5_4090" -Launch
.\install_clean_4090.bat -KeepDownload
```

The install destination must be outside the Git repository. This prevents a
recursive overlay and keeps the 500+ MB runtime payload out of source control.

## Why P5 and HEVC NVENC?

NVIDIA's modern NVENC presets range from faster P1 toward higher-quality,
lower-throughput P7. P5 is a strong middle/default-quality point and leaves
substantial encoding headroom on Ada while avoiding P6/P7 when encoding is not
the part of the pipeline worth slowing down. H.265 NVENC is used as the
portable default because the RTX 4090 supports it, the app supports 10-bit HDR
with it, and it is generally easier to consume in existing video workflows
than AV1. AV1 remains available in the UI.

`Auto (Default)` bitrate is retained because forcing the app's `Best` or `Max`
delivery settings mostly increases bitrate/file size; it does not make DLSS
Neural Rendering itself higher quality.

## GPU/VRAM limitation that remains

The existing RTX Video worker has an explicit `--gpu-luid` path and NVENC has
an explicit CUDA ordinal path. The bundled DLSS Neural Rendering and DLSS Frame
Generation worker wrappers still do not expose verified adapter binding in
this source repository.

The launcher therefore does not set `CUDA_VISIBLE_DEVICES` as a fake fix for
Direct3D adapter selection, and it does not allocate dummy VRAM to make Task
Manager show 24 GB in use. Native DLSS runtime memory allocation remains
runtime/WDDM-managed.

The monitor cable can remain on the second GPU; these scripts do not change
Windows display configuration, primary adapter settings, registry keys, HAGS,
GPU clocks, power limits, or process priority.
