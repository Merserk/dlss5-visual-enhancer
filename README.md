# Visual Enhancer

[![Downloads](https://img.shields.io/github/downloads/Merserk/dlss5-visual-enhancer/total.svg?style=flat-square&label=Downloads)](https://github.com/Merserk/dlss5-visual-enhancer/releases) ![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows11&logoColor=white) ![DLSS](https://img.shields.io/badge/DLSS%205-Neural%20Rendering-76B900?style=flat-square) ![DLSS Super Resolution](https://img.shields.io/badge/DLSS-Super%20Resolution-76B900?style=flat-square&logo=nvidia&logoColor=white) ![DLSS Ray Reconstruction](https://img.shields.io/badge/DLSS-Ray%20Reconstruction-76B900?style=flat-square&logo=nvidia&logoColor=white) ![DLSS Frame Generation](https://img.shields.io/badge/DLSS-Frame%20Generation-76B900?style=flat-square&logo=nvidia&logoColor=white) ![FSR Frame Generation](https://img.shields.io/badge/AMD-FSR%20Frame%20Generation-ED1C24?style=flat-square&logo=amd&logoColor=white) ![RTX Video](https://img.shields.io/badge/RTX-Video-76B900?style=flat-square&logo=nvidia&logoColor=white) ![NVIDIA Video Effects (VFX)](https://img.shields.io/badge/NVIDIA-Video%20Effects%20%28VFX%29-76B900?style=flat-square&logo=nvidia&logoColor=white) ![ReShade](https://img.shields.io/badge/ReShade-Effects-6C5CE7?style=flat-square) ![NVIDIA NIS](https://img.shields.io/badge/NVIDIA-NIS-76B900?style=flat-square&logo=nvidia&logoColor=white) ![AMD CAS](https://img.shields.io/badge/AMD-CAS-ED1C24?style=flat-square&logo=amd&logoColor=white) ![3D LUT](https://img.shields.io/badge/3D-LUT-6C5CE7?style=flat-square) [![Patreon](https://img.shields.io/badge/Patreon-Merserk-FF424D?style=flat-square&logo=patreon&logoColor=white)](https://www.patreon.com/Merserk)

**Visual Enhancer** is a portable Windows application for AI-assisted image and video enhancement. Its configurable pipelines combine **NVIDIA DLSS 5 Neural Rendering** through the **Neuroframe Engine**, scaling, **DLSS Super Resolution**, **DLSS Ray Reconstruction**, **RTX Video Super Resolution**, **ReShade**, coloring, sharpening, and film grain. Video processing also offers **RTX Video HDR**, **DLSS Frame Generation**, **FSR Frame Generation**, **Video Super Resolution**, **Video Frame Generation**, and **Upscale (Lightweight)** as separate stages. DLSS, RTX Video, and NVIDIA Video Effects require a supported RTX GPU; FSR Frame Generation uses compatible DirectX 12 hardware with AMD FidelityFX Optical Flow. Arrange the effects, compare previews, and export single files or batches.

<img width="1920" height="1080" alt="Main Cover_v2" src="https://github.com/user-attachments/assets/9248c946-391e-408c-b146-f7d8a6e9c695" />

<img width="1920" height="1080" alt="Second Cover" src="https://github.com/user-attachments/assets/b1cb9d91-7b0b-4962-bd61-8ef9184d31e5" />

## Installation

1. Download the [latest release](https://github.com/Merserk/dlss5-visual-enhancer/releases/latest).
2. Extract the complete ZIP archive to a folder.
3. Run **`Visual Enhancer.exe`**.

## Examples

### Original

https://github.com/user-attachments/assets/8df8bd4c-01b4-47dd-9705-3614a0b0ff75

### DLSS 5 Neural Rendering

https://github.com/user-attachments/assets/cff68783-4ee9-4c99-8b36-4eee2a6437ec

### DLSS Frame Generation

https://github.com/user-attachments/assets/81c29005-e4f0-4acf-b9f7-d58850bb055f

## Main features

The **Neural Rendering** workspace has separate **Image** and **Video** pipelines. Each keeps its own stage order and enabled effects. Drag cards to reorder them and enable only the stages you need; all cards are off by default.

| Feature | Processing |
| --- | --- |
| **DLSS Neural Rendering** | Enhance images and videos with selectable styles, intensity, local tone and structure, skin controls, automatic masking, custom control masks, and multiple passes. |
| **Scaling** | Resize images and videos with selectable resampling filters. |
| **DLSS Super Resolution** | DLAA or AI upscaling with selectable quality modes and presets. Video includes optical-flow quality controls. |
| **DLSS Ray Reconstruction** | Reconstruct images and videos at native **1× resolution**. Video uses NVIDIA Optical Flow (NVOFA), with High, Medium, or Low optical-flow quality. |
| **RTX Super Resolution** | NVIDIA RTX Video enhancement and upscaling, with quality levels, scale factors, or custom dimensions. |
| **RTX Video HDR** | Convert SDR video to HDR with contrast, saturation, middle gray, peak luminance, and precision controls. |
| **DLSS Frame Generation** | Increase video frame rate with a 2×–8× multiplier or target FPS, automatic engine selection, and High, Medium, or Low optical-flow quality. |
| **FSR Frame Generation** | Generate intermediate video frames using DirectX 12 and AMD FidelityFX Optical Flow, with a 2×–8× multiplier or target FPS. FSR processing runs on the GPU without CUDA or NVIDIA Optical Flow. |
| **ReShade** | Apply `.ini` presets to images and videos, enable individual effects, edit their parameters, and blend the result. Optional AI depth and AMD FidelityFX Optical Flow stabilize depth-dependent effects in video. |
| **Coloring** | Adjust light and color, apply `.cube` LUTs, match image references, and export graded LUTs. |
| **Sharpening** | NVIDIA NIS or AMD FidelityFX CAS with adjustable strength. |
| **Grain** | Adjustable film grain with size, color, tonal response, and seed controls; animated grain for video. |
| **Video Super Resolution** | Standard Upscaling, Denoise, Deblur, and High-Bitrate modes with quality and strength controls. Denoise and Deblur retain the input dimensions. |
| **Upscale (Lightweight)** | Fast video upscaling with scale-factor and sharpening controls. |
| **Video Frame Generation** | Generate intermediate video frames using a multiplier or target FPS, with model quality and automatic scene-cut detection. |

Ray Reconstruction adapts a model designed for ray-traced rendering to imported media using estimated scene guides. It preserves the input dimensions and frame rate. Choose either **DLSS Super Resolution** or **DLSS Ray Reconstruction** in a pipeline; enabling one disables the other. RR has a compact enable-only card in Image mode and an expandable **Optical Flow Quality** control in Video mode, without DLSS mode, preset, or strength controls.

Video-only effects appear as individual cards in the Video pipeline. Stages run from top to bottom; the application checks hardware support, dimensions, frame rates, and HDR compatibility before processing. Choose one frame-generation provider per pipeline: **DLSS**, **FSR**, or **Video Frame Generation**. DLSS and FSR offer **Multiplier** and **Target FPS** modes, with fixed targets from **23.976 to 480 FPS**; multiplier mode retains the source's fractional frame rate. The **Frame Interpolation** workspace also lets you choose between DLSS and FSR. ReShade includes a bundled preset, shader and texture search paths, precision and HDR controls, and optional AI depth generation; RTGI shaders are not bundled.

### Workflow and preview

- **Comparison:** Split, 2-Up, and Output views, zoom, fit-to-view, a shared video timeline, and Focus Preview (`Ctrl+Shift+F`).
- **Previews:** realtime updates for supported changes, full-resolution image previews, and short video previews from the selected position. Cached results reduce repeated processing.
- **Batches:** add files or folders, drag and drop, paste clipboard media, track per-file progress, pause or cancel exports, and retry failed items. **Show input in Explorer** and **Show output in Explorer** select the corresponding file in File Explorer.
- **Export:** choose the format, codec, container, quality, output folder, and naming mode. Existing files are not silently overwritten.
- **HDR:** preserve or export 10-bit HDR with a compatible pipeline and codec. Place SDR-only effects before HDR conversion.
- **Presets:** save and restore processing settings, stage order, and enabled effects as JSON.

## Supported media

| Type | Input | Output |
| --- | --- | --- |
| Images | PNG, JPEG, WebP, AVIF, TIFF, BMP, GIF, SVG, HEIF/HEIC, and many camera RAW formats | PNG, JPEG, WebP, AVIF, TIFF; 16-bit export for PNG/TIFF |
| Video | MP4, MKV, MOV, AVI, WebM, M4V, TS/MTS/M2TS, WMV, FLV | H.264, H.265, AV1, ProRes Proxy/HQ, FFV1 Lossless RGB 10-bit; compatible MP4, MKV, or MOV containers |

Image processing applies supported color profiles and EXIF orientation, retains supported metadata, and preserves transparency where the output format allows it. Animated or multipage images use the first frame/page. Video exports retain audio, metadata, and chapters where supported; subtitle preservation depends on the processing path and container.

The application accepts image dimensions up to **32,768 pixels per side**, subject to available memory and the limits of the selected effects and file format.

## Requirements

- **Windows:** Windows 11, 64-bit.
- **GPU and driver:** NVIDIA GeForce RTX with a compatible NVIDIA driver for DLSS, RTX Video, and NVIDIA Video Effects. Super Resolution and Lightweight Upscale support RTX 20, 30, 40, and 50 series; Neural Rendering availability is checked on the selected GPU. FSR Frame Generation has its own DirectX GPU selection and compatibility checks.
- **Ray Reconstruction video:** NVIDIA Optical Flow support must be available through the installed NVIDIA driver. Images do not require optical flow.
- **Frame generation:** DLSS Frame Generation and NVIDIA Video Frame Generation require supported RTX 40 or 50 series hardware. Enable **Hardware-accelerated GPU scheduling** in Windows for DLSS Frame Generation. FSR Frame Generation uses compatible DirectX 12 hardware and AMD FidelityFX Optical Flow; adapter support is checked before processing. [NVIDIA DLSS](https://developer.nvidia.com/rtx/dlss), [Video Frame Generation support](https://docs.nvidia.com/maxine/vfx/latest/Filters/VideoFrameGeneration.html).
- **Video filtering:** a working Vulkan-capable GPU and driver for Vulkan filter stages. NVIDIA, AMD, or Intel can handle this path. FSR Frame Generation uses DirectX 12 for its own processing.
- **Storage:** extract the complete package to a writable folder, with free space for output files and temporary video data.

NVIDIA NVENC export requires an encoder supported by the selected RTX GPU. AV1 hardware encoding on NVIDIA requires RTX 40 or 50 series. FSR exports require GPU encoding with **H.264**, **H.265**, or **AV1** on the selected DirectX adapter; codec support is checked on that GPU. ProRes and FFV1 are not FSR export codecs.

## Settings

**Settings** contains interface language, **AI Processing GPU (Tensor Cores / NGX)** for NVIDIA processing, **AI Processing GPU (DirectX 12 / FidelityFX)** for FSR Frame Generation, **Decoding / Encoding Device**, **Realtime Preview**, and **Processing Cache**. The processing GPU selections are independent of the general video device setting. FSR's **Auto** selection prioritizes a compatible discrete GPU using Windows' high-performance adapter preference. **Clean Cache** releases reclaimable preview, shader, and processing cache data. Preset import and export are available from the header menu.

Use the processing cards for individual effects and **Export Settings** for image format, bit depth, video codec, container, quality, and **Preserve / Export 10-bit HDR**. The Export dialog selects the destination and filename. Saved settings and caches use Windows per-user storage. The default output folder is `outputs/` beside the executables, and session logs are stored in `logs/`; these folders are created when the application starts. Temporary processing files use application-managed temporary storage.

## Supported languages

- English (US)
- &lrm;العربية
- Deutsch
- Español
- Français
- हिन्दी
- 日本語
- 한국어
- Polski
- Українська
- 中文 (简体)
- 中文 (繁體)

## Command line

**`VE_CLI.exe`** uses the same processing engines as the desktop application. Keep **`Visual Enhancer.exe`** and **`VE_CLI.exe`** at the release folder's root, together with the bundled Qt DLLs, `bin/`, `resources/`, and plugin folders.

| Command | Purpose |
| --- | --- |
| `render` | Neural Rendering, or the complete enabled pipeline from a JSON preset. |
| `reconstruct` | DLSS Ray Reconstruction for images and videos at native 1× resolution. |
| `reshade` | Process images or videos with a ReShade `.ini` preset. |
| `upscale` | RTX or DLSS Super Resolution. |
| `interpolate` | DLSS or FSR Frame Generation with a multiplier or target FPS. |
| `devices` | List NVIDIA processing GPUs and DirectX adapters for FSR. |

```powershell
.\VE_CLI.exe render "photo.png" -o "results\photo.png"
.\VE_CLI.exe reconstruct "photo.png" -o "results\photo-rr.png"
.\VE_CLI.exe reshade "photo.png" --reshade-preset "look.ini" -o "results\photo-reshade.png"
.\VE_CLI.exe upscale "clip.mp4" --scale 2 -o "results"
.\VE_CLI.exe interpolate "clip.mp4" --fps 60 -o "results"
.\VE_CLI.exe interpolate "clip.mp4" --interpolator fsr3 --fsr-gpu auto --fps 60 --codec hevc -o "results\clip-fsr.mp4"
.\VE_CLI.exe interpolate "clip.mp4" --interpolator fsr3 --multiplier 3 -o "results\clip-fsr-3x.mp4"
.\VE_CLI.exe render "clip.mp4" --preset "pipeline.json" -o "results"
```

Use `--help` for options, `--recursive` for subfolders, `--dry-run` to inspect a job, and `--json --quiet` for scripting. Use `reconstruct --help` for RR options; RR always processes at 1× and does not accept DLSS mode or RR preset options. `interpolate` defaults to DLSS; select FSR with `--interpolator fsr3`, then choose either `--fps` or an integer `--multiplier` from 2 through 8. `--fsr-gpu` accepts `auto`, a DirectX adapter index, or a saved DXGI ID from `devices --json`, independently of the NVIDIA `--gpu` option. ReShade accepts `--reshade-preset`, shader and texture search paths, and depth controls; use `reshade --help` for details. Presets exported from the application include advanced effect and export settings. Without `-o`, output goes to `outputs/` beside the executables.

## License and third-party notices

Project-owned Visual Enhancer material is distributed under the **Merserk Source License**. The software may be used for personal, professional, and commercial work, and outputs created or processed with Visual Enhancer may be used commercially. Redistribution, mirroring, repackaging, rebranding, resale, sublicensing, publishing modified builds, and similar redistribution require prior written permission except where the license states otherwise. See [LICENSE](LICENSE) for the complete terms.

Third-party components remain subject to their own licenses and are not relicensed under the Merserk Source License. See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES) for the notices included with the application.

Visual Enhancer is an independent community project and is not affiliated with, sponsored by, or endorsed by NVIDIA or AMD. NVIDIA, GeForce RTX, DLSS, and RTX Video are trademarks and/or registered trademarks of NVIDIA Corporation. AMD and FidelityFX are trademarks of Advanced Micro Devices, Inc.
