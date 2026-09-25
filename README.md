# Visual Enhancer

[![Downloads](https://img.shields.io/github/downloads/Merserk/dlss5-visual-enhancer/total.svg?style=flat-square&label=Downloads)](https://github.com/Merserk/dlss5-visual-enhancer/releases) ![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows11&logoColor=white) ![DLSS](https://img.shields.io/badge/DLSS%205-Neural%20Rendering-76B900?style=flat-square) ![DLSS Frame Generation](https://img.shields.io/badge/DLSS-Frame%20Generation-76B900?style=flat-square&logo=nvidia&logoColor=white) ![RTX Video](https://img.shields.io/badge/RTX-Video-76B900?style=flat-square&logo=nvidia&logoColor=white) [![Patreon](https://img.shields.io/badge/Patreon-Merserk-FF424D?style=flat-square&logo=patreon&logoColor=white)](https://www.patreon.com/Merserk)

**Visual Enhancer** is a portable Windows application for AI-assisted image and video enhancement on NVIDIA RTX GPUs. It uses **NVIDIA DLSS 5 Neural Rendering** through the **Neuroframe Engine** for image and video processing, **NVIDIA DLSS Frame Generation** for video frame interpolation, and **NVIDIA RTX Video Super Resolution** and **RTX Video HDR** for dedicated upscale workflows. Live mode brings DLSS 5 Neural Rendering to local videos and supported online streams during playback.

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

### DLSS 5 Neural Rendering

- **Neuroframe Engine:** brings DLSS 5 Neural Rendering to images and videos directly inside Visual Enhancer. No external graphics injector or game add-on is required.
- **Image and video processing:** enhance single files or full batches with previews, per-file progress, and diagnostic reports.
- **Neural Rendering controls:** NR Style, NR Intensity, Local Tone Strength, Local Structure Strength, Skin Structure Strength, Automatic Mask, and 1–4 Neural Rendering passes.
- **Composition controls:** NR Color Strength, Tone Preservation, Face/Skin Protection, Grain Preservation, and Mask Feather give additional control over how Neural Rendering is blended with the source.
- **Detail-Only:** one-click preset that keeps the source color and tone while retaining Neural Rendering detail changes.
- **Custom NR Mask:** use an image mask to control where Neural Rendering is applied. The selected mask is also available to Live during the current application session.
- **Shimmer Suppression:** helps stabilize fine detail between frames in Video and Live processing.
- **Processing scale:** process at Source, 125%, 150%, 175%, 200%, 75%, 50%, or 25% of the source dimensions. This is separate from RTX Video Super Resolution.
- **HDR preservation:** supported video workflows can preserve 10-bit HDR with H.265, AV1, ProRes Proxy, ProRes HQ, or FFV1 Lossless RGB 10-bit output.

### RTX Video Upscale

- **NVIDIA RTX Video Super Resolution:** enhance and upscale images or SDR video with VSR quality levels 1–4.
- **Flexible sizing:** use 1× native-resolution enhancement, 1.5×, 2×, 3×, 4×, or custom output dimensions up to 16384 pixels per dimension.
- **NVIDIA RTX Video HDR:** convert SDR video to HDR with adjustable contrast, saturation, middle gray, peak luminance, and processing precision.
- **Combined processing:** RTX Video Super Resolution and RTX Video HDR can be used together, or RTX Video HDR can be used by itself.
- **Image output:** PNG, JPEG, WebP, AVIF, and TIFF with quality, metadata, and naming controls.
- **Video output:** H.264, H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit, with NVIDIA NVENC options where available.

### DLSS Frame Generation

- **Frame Interpolation:** uses NVIDIA DLSS Frame Generation to create additional frames for smoother video playback and high-frame-rate output.
- **Target frame rates:** choose from 23.976 up to 480 FPS, including common cinema, broadcast, and high-refresh rates.
- **DLSSG modes:** **Auto**, **Native DLSSG**, and **Cascade** provide flexible interpolation for different source and target frame rates.
- **Preview clips:** render 3, 5, 10, 20, or 30 seconds from the current playhead before starting a full render.
- **HDR preservation:** 10-bit HDR output is supported with H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit.

### Live

- **Local and online playback:** apply DLSS 5 Neural Rendering while watching local videos, direct network streams, YouTube, and Twitch sources.
- **Integrated playback:** pause or resume playback, control volume and mute, expand the player, or enter fullscreen without leaving the Live workflow.
- **Live quality controls:** choose source quality, maximum input resolution from 480p to 2160p, 1/2/4-second segments, Auto/Source/60/30/24 FPS modes, a 2–30 second playback buffer, and an independent Live Scale with Source, 125%, 150%, 175%, 200%, 75%, 50%, and 25% options.
- **Independent Live tuning:** Live keeps its own Neural Rendering settings, so Live adjustments do not overwrite the main Neural Rendering settings. The custom NR mask remains shared for the current session.
- **Dynamic updates:** most Neural Rendering controls can be changed while Live is running. Source selection, Live Scale, source quality, maximum input resolution, target FPS, segment duration, and playback buffer changes apply the next time Live starts.
- **Performance information:** view processing and playback status while Live is running.

### Workflow and preview

- **Unified media viewer:** compare Input and Output with **Split**, **2-Up**, and **Output** views, fit images to the viewer, inspect them at 100%, and scrub through video from a shared timeline.
- **Realtime preview:** supported Neural Rendering and Upscale changes can refresh the current preview automatically. Video previews can follow the selected playhead position; Frame Interpolation preview clips remain manual.
- **Focus Preview:** hide the side panels to give the media viewer more space, using the viewer control, menu command, or `Ctrl+Shift+F`.
- **Full-resolution image previews:** processed Neural Rendering and RTX Video image previews are retained at full output resolution for detailed inspection.
- **Batch processing:** add multiple files or folders, drag and drop media, retry failed items, clear completed items, stop processing, and reveal completed files in Explorer.
- **Clipboard media:** paste clipboard bitmap images, copied local files, or supported browser image URLs into compatible batch workflows; remote web images are downloaded before being added to the queue.
- **Per-file progress:** each queue item shows its current state, progress, elapsed time, processing details, dimensions, and output path.
- **GPU selection:** choose the GPU used for AI processing separately from the GPU used for NVIDIA NVENC video encoding.
- **Automatic containers:** H.264 uses MP4; H.265, AV1, and FFV1 Lossless RGB 10-bit use MKV; ProRes Proxy and ProRes HQ use MOV.
- **Safe output handling:** completed files are kept, incomplete work is cleaned up when needed, and existing files are not silently overwritten.
- **Media preservation:** supported workflows preserve useful source information such as rotation, timestamps, metadata, chapters, audio, and subtitles where possible.

Completed media is saved to `outputs/`. Logs and diagnostic reports are available in `logs/` for troubleshooting.

## Supported media

| Type | Input | Output |
| --- | --- | --- |
| Images | Common image formats, HEIF/HEIC, SVG, TIFF, and many camera RAW formats | PNG, JPEG, WebP, AVIF, TIFF |
| Video | MP4, MKV, MOV, AVI, WebM, M4V, TS/MTS/M2TS, MXF, VOB, WMV, FLV, MPG/MPEG and other supported video formats | H.264, H.265, AV1, ProRes Proxy, ProRes HQ, FFV1 Lossless RGB 10-bit |
| Live | Local video, direct network streams, YouTube, Twitch | Processed playback inside Visual Enhancer |

Image processing applies EXIF orientation, handles supported color profiles, preserves supported metadata where possible, and keeps transparency except when saving to JPEG. Animated and multipage image sources use the first frame/page.

## Requirements

- **Windows:** 64-bit Windows 11 with Direct3D 12.
- **NVIDIA RTX GPU:** a compatible NVIDIA GeForce RTX GPU with a current NVIDIA driver.
- **DLSS 5 Neural Rendering:** availability depends on supported NVIDIA RTX hardware, the installed driver, and successful feature initialization on the selected AI Processing GPU.
- **DLSS Frame Generation:** requires compatible NVIDIA RTX hardware. Hardware-accelerated GPU scheduling (HAGS) should be enabled.
- **RTX Video:** RTX Video Super Resolution and RTX Video HDR require compatible NVIDIA RTX hardware and driver support.
- **NVIDIA NVENC:** NVIDIA NVENC output requires hardware encoding support for the selected codec and output settings. CPU encoding options are also available.

## Settings

### DLSS 5 Neural Rendering

| Setting | Values | Default |
| --- | --- | --- |
| NR Style | Default, Natural, Cinematic | Default |
| Scale | Source (Original), 125%, 150%, 175%, 200%, 75%, 50%, 25% | Source (Original) |
| NR Intensity | 0.00–2.00 | 1.00 |
| NR Passes | 1–4 | 1 |
| Local Tone Strength | 0.00–2.00 | 1.00 |
| Local Structure Strength | 0.00–2.00 | 1.00 |
| Skin Structure Strength | -1.00–2.00 | -1.00 |
| NR Color Strength | 0.00–1.00 | 1.00 |
| Tone Preservation | 0.00–1.00 | 0.00 |
| Face/Skin Protection | 0.00–1.00 | 0.00 |
| Grain Preservation | 0.00–1.00 | 0.00 |
| Mask Feather | 0–128 output pixels | 0 |
| Automatic Mask | Off, On | Off |
| Shimmer Suppression | 0.00–1.00; Video and Live only | 0.70 |

**NR Passes** controls how many Neural Rendering passes are applied. Additional passes increase processing and can produce a stronger cumulative result.

**Scale** controls the size used for Neural Rendering. Source keeps the original dimensions; percentage options process at the selected size. Neural Rendering dimensions must stay within the supported 64×64 minimum and 7680×4320 maximum boundary. This setting is separate from RTX Video Super Resolution.

**Detail-Only** sets NR Color Strength to `0.00` and Tone Preservation to `1.00`. All other Neural Rendering controls remain editable.

Setting Skin Structure Strength above `-1.00` automatically enables Automatic Mask so the skin adjustment can take effect. Automatic Mask can still be switched off manually afterward.

### Upscale: Image

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| VSR quality | 1 - Low, 2 - Medium, 3 - High, 4 - Ultra | 4 - Ultra |
| Output sizing | Scale factor or Custom dimensions | Scale factor |
| Scale factor | 1×, 1.5×, 2×, 3×, 4× | 2× |
| Custom dimensions | Up to 16384 pixels per dimension; cannot be smaller than the source | 3840×2160 |
| Lock aspect ratio | Off, On | On |
| Output format | PNG, JPEG, WebP, AVIF, TIFF | PNG |
| Image quality | 1–100 for lossy formats | 95 |
| Preserve EXIF Metadata | Off, On | On |
| Rename | Auto, Copy, Custom | Auto |

RTX Video Super Resolution can be used at **1×** to enhance an image without enlarging it. A selected image can be previewed before the final render.

### Upscale: Video

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| RTX Video Super Resolution | Off, On | On |
| VSR quality | 1 - Low, 2 - Medium, 3 - High, 4 - Ultra | 4 - Ultra |
| Output sizing | Scale factor or Custom dimensions | Scale factor |
| Scale factor | 1×, 1.5×, 2×, 3×, 4× | 2× |
| Custom dimensions | Up to 16384 pixels per dimension; cannot be smaller than the source while VSR is enabled | 3840×2160 |
| Lock aspect ratio | Off, On | On |
| RTX Video HDR | Off, On; converts SDR input to HDR | Off |
| HDR contrast | 0–200 | 100 |
| HDR saturation | 0–200 | 100 |
| HDR middle gray | 10–100 | 50 |
| HDR peak luminance | 400–2000 nits | 1000 nits |
| HDR processing precision | Packed 10-bit, Packed 10-bit (FP16) | Packed 10-bit |
| Video codec | H.264, H.265, AV1, ProRes Proxy, ProRes HQ, FFV1 Lossless RGB 10-bit; CPU/NVIDIA NVENC variants where available | H.265 (NVIDIA NVENC) |
| Container | Selected automatically from the codec | MKV |
| Encoding quality | Auto (Default), Good, Best, Max; ProRes HQ and FFV1 use codec-fixed quality | Auto (Default) |
| Rename | Auto, Copy, Custom | Auto |

RTX Video Super Resolution and RTX Video HDR can be used together. VSR can also be disabled for HDR-only processing, but at least one RTX Video effect must be enabled.

RTX Video HDR accepts SDR source video and creates HDR output. Existing HDR input is not accepted by this workflow. HDR output requires H.265, AV1, ProRes Proxy, ProRes HQ, or FFV1 Lossless RGB 10-bit.

### Frame Interpolation

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Output FPS | 23.976, 25, 29.97, 30, 50, 59.94, 60, 90, 119.88, 120, 144, 165, 180, 240, 360, 480 | 60 |
| DLSS engine | Auto, Native DLSSG, Cascade | Auto |
| Preview length | 3, 5, 10, 20, 30 seconds | 3 seconds |
| Video codec | H.264, H.265, AV1, ProRes Proxy, ProRes HQ, FFV1 Lossless RGB 10-bit; CPU/NVIDIA NVENC variants where available | H.264 (NVIDIA NVENC) |
| Container | Selected automatically from the codec | MP4 |
| Encoding quality | Auto (Default), Good, Best, Max; ProRes HQ and FFV1 use codec-fixed quality | Auto (Default) |
| Preserve 10-bit HDR | Off, On; H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit | Off |
| Rename | Auto, Copy, Custom | Auto |

**Auto** selects the appropriate DLSS Frame Generation path for the requested source and output frame rates. If the selected output FPS is equal to or below the source frame rate, frames are resampled without generating additional frames.

### Live

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Source | Online URL or Local video | Online |
| Source quality | Auto, 480p, 720p, 1080p, 1440p, 2160p | Auto |
| Max input resolution | 480p, 720p, 1080p, 1440p, 2160p | 720p |
| Segment duration | 1, 2, 4 seconds | 2 seconds |
| Target FPS | Auto, Source, 60, 30, 24 | Auto |
| Playback buffer | 2–30 seconds | 6 seconds |
| Live Scale | Source (Original), 125%, 150%, 175%, 200%, 75%, 50%, 25% | Source (Original) |
| Neural Rendering controls | Independent Live copy of the Neural Rendering controls | Neural Rendering defaults |

Most Live Neural Rendering controls can be changed while playback is active. Buffered frames keep their previous appearance until playback reaches newly processed frames. Live Scale, source selection, source quality, maximum input resolution, segment duration, target FPS, and playback buffer take effect on the next **Start Live**.

### Video output and encoding

| Output setting | Behavior |
| --- | --- |
| H.264 | MP4; CPU or NVIDIA NVENC; 8-bit SDR |
| H.265 | MKV; CPU or NVIDIA NVENC; supports 10-bit HDR mode |
| AV1 | MKV; CPU or NVIDIA NVENC; supports 10-bit HDR mode |
| ProRes Proxy | MOV; CPU-based 10-bit 4:2:2 output; supports HDR mode |
| ProRes HQ | MOV; CPU-based 10-bit 4:2:2 output; supports HDR mode |
| FFV1 Lossless RGB 10-bit | MKV; CPU-based lossless 10-bit RGB output; supports HDR mode |
| Encoding quality | Auto (Default), Good, Best, Max; ProRes HQ and FFV1 use codec-fixed quality |
| Rename | Auto creates a workflow-specific name; Copy keeps the source base name; Custom adds the selected suffix |

Compatible workflows preserve metadata and chapters where supported. MKV can keep compatible audio and subtitle streams, while MP4/MOV use AAC audio. Supported text subtitles can be retained where the selected workflow allows it.

### Application

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| AI Processing GPU | Automatic (Best Available) or a compatible detected NVIDIA RTX GPU | Automatic |
| Video Processing GPU | Automatic or a detected NVIDIA GPU used for NVIDIA NVENC/NVDEC work | Automatic |
| Preview Encoding Strategy | Auto, Always H.264, Disabled | Auto |
| Realtime Preview | Off, On; Neural Rendering and Upscale | On |
| Settings preset | Export or import adjustable application settings as versioned JSON; custom NR mask is not included | n/a |
| Factory reset | Restores processing, GPU, mask, and encoding settings to defaults; window geometry is kept | n/a |

Saved GPU selections follow the selected GPU identity. If that GPU is no longer available, the setting returns to Automatic. Compatible older settings presets can be imported and migrated automatically.

## License and third-party notices

Project-owned Visual Enhancer material is distributed under the **Merserk Source License 1.0**. The software may be used for personal, professional, and commercial work, and outputs created or processed with Visual Enhancer may be used commercially. Redistribution, mirroring, repackaging, rebranding, resale, sublicensing, publishing modified builds, and similar redistribution require prior written permission except where the license states otherwise. See [LICENSE](LICENSE) for the complete terms.

Third-party components remain subject to their own licenses and are not relicensed under the Merserk Source License. See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES) for the notices included with the application.

Visual Enhancer is an independent community project and is not affiliated with, sponsored by, or endorsed by NVIDIA. NVIDIA, GeForce RTX, DLSS, and RTX Video are trademarks and/or registered trademarks of NVIDIA Corporation.
