# Visual Enhancer

[![Downloads](https://img.shields.io/github/downloads/Merserk/dlss5-visual-enhancer/total.svg?style=flat-square&label=Downloads)](https://github.com/Merserk/dlss5-visual-enhancer/releases) ![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?style=flat-square&logo=windows11&logoColor=white) ![DLSS](https://img.shields.io/badge/DLSS%205-Neural%20Rendering-76B900?style=flat-square) ![DLSS Frame Generation](https://img.shields.io/badge/DLSS-Frame%20Generation-76B900?style=flat-square&logo=nvidia&logoColor=white) ![RTX Video](https://img.shields.io/badge/RTX-Video-76B900?style=flat-square&logo=nvidia&logoColor=white) [![Patreon](https://img.shields.io/badge/Patreon-Merserk-FF424D?style=flat-square&logo=patreon&logoColor=white)](https://www.patreon.com/Merserk)

**Visual Enhancer** is a portable Windows application for AI-assisted image and video enhancement on NVIDIA RTX GPUs. Its configurable image and video processing pipeline combines **NVIDIA DLSS 5 Neural Rendering** through the **Neuroframe Engine**, conventional scaling, **DLSS Super Resolution**, **NVIDIA RTX Video Super Resolution**, coloring, and sharpening. Video processing also supports **NVIDIA RTX Video HDR** and **NVIDIA DLSS Frame Generation** as independent pipeline stages. Arrange and enable individual processing stages, preview the result, and export single files or batches. Live mode brings DLSS 5 Neural Rendering to local videos and supported online streams during playback.

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

The **Neural Rendering** workspace provides separate **Image** and **Video** pipelines. Each has its own stage order and enabled stages, so effects can be combined, rearranged by dragging their cards, or switched off individually. The image pipeline offers **DLSS Neural Rendering → Scaling → DLSS Super Resolution → RTX Super Resolution → Coloring → Sharpening** in its initial order. The video pipeline initially uses **DLSS Neural Rendering → Scaling → DLSS Super Resolution → RTX Super Resolution → RTX Video HDR → DLSS Frame Generation → Coloring → Sharpening**. These are available stages, not effects that must all be enabled; all processing cards are off by default. Rendering validates the selected order, dimensions, frame rates, hardware capabilities, and HDR compatibility before processing.

### DLSS 5 Neural Rendering

- **Neuroframe Engine:** brings DLSS 5 Neural Rendering to images and videos directly inside Visual Enhancer. No external graphics injector or game add-on is required.
- **Image and video processing:** use Neural Rendering as one stage in a larger workflow, on single files or batches.
- **Neural Rendering controls:** NR Style, NR Intensity, Local Tone Strength, Local Structure Strength, Skin Structure Strength, Automatic Mask, and 1–4 Neural Rendering passes.
- **Composition controls:** NR Color Strength, Tone Preservation, Face/Skin Protection, Grain Preservation, and Mask Feather control how Neural Rendering is blended with the source.
- **Detail-Only:** one-click preset that sets NR Color Strength to `0.00` and Tone Preservation to `1.00` while keeping the other controls editable.
- **Custom NR Mask:** use an image mask to control where Neural Rendering is applied. The selected mask is also available to Live during the current application session.
- **Shimmer Suppression:** helps stabilize fine detail between frames in Video and Live processing.
- **Independent scaling:** a separate Scaling stage supports Source, 200%, 175%, 150%, 125%, 75%, 50%, and 25% with image- or video-specific resampling filters.
- **HDR handling:** compatible video pipelines can retain 10-bit HDR with a supported HDR codec and the export HDR option enabled.

### RTX Video Upscale

- **NVIDIA RTX Video Super Resolution:** enhance and upscale images or SDR video with VSR quality levels 1–4. This is a separate pipeline stage from DLSS Super Resolution and conventional Scaling.
- **Flexible sizing:** use 1× native-resolution enhancement, 1.5×, 2×, 3×, 4×, or custom dimensions, subject to the 16384-pixel-per-dimension texture limit and source-size restrictions.
- **NVIDIA RTX Video HDR:** an independent Video pipeline stage converts SDR video to HDR with adjustable contrast, saturation, middle gray, peak luminance, and processing precision.
- **Combined processing:** RTX Video Super Resolution and RTX Video HDR can both be enabled in one workflow. HDR receives the dimensions produced by earlier stages and does not require VSR to be enabled.
- **DLSS Super Resolution:** a separate image/video stage offers DLAA (1×), Quality (1.5×), Balanced (~1.72×), Performance (2×), and Ultra Performance (3×), with selectable DLSS presets.
- **Image output:** PNG, JPEG, WebP, AVIF, or TIFF, including 16-bit PNG/TIFF export and quality settings for lossy formats.
- **Video output:** H.264, H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit, with hardware encoding through the selected decoding/encoding device where supported and software fallback where available.

### DLSS Frame Generation

- **Video pipeline stage:** uses NVIDIA DLSS Frame Generation to create intermediate frames at a selected output frame rate; it can be positioned among the other video stages.
- **Target frame rates:** choose from 23.976 to 480 FPS, including common cinema, broadcast, and high-refresh rates. The selected target must exceed the frame rate entering the stage.
- **DLSSG modes:** **Auto**, **Native DLSSG**, and **Cascade** provide different interpolation paths depending on the input and requested output.
- **Preview clips:** render 1, 3, 5, 10, 20, or 30 seconds from the selected video position before a full export.
- **HDR preservation:** 10-bit HDR output is supported with H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit when HDR export is enabled.

### Live

- **Local and online playback:** apply DLSS 5 Neural Rendering while watching local videos, direct network streams, YouTube, and Twitch sources.
- **Integrated playback:** pause or resume playback, control volume and mute, expand the player, or enter fullscreen without leaving the Live workflow.
- **Live quality controls:** choose source quality, maximum input resolution from 480p to 2160p, 1/2/4-second segments, Auto/Source/60/30/24 FPS modes, a 2–30 second playback buffer, and an independent Live Scale with Source, 200%, 175%, 150%, 125%, 75%, 50%, and 25% options.
- **Independent Live tuning:** Live keeps its own Neural Rendering settings, so Live adjustments do not overwrite the main Neural Rendering settings. The custom NR mask remains shared for the current session.
- **Dynamic updates:** most Neural Rendering controls can be changed while Live is running. Source selection, Live Scale, source quality, maximum input resolution, target FPS, segment duration, and playback buffer changes apply the next time Live starts.
- **Performance information:** view processing and playback status while Live is running.

### Workflow and preview

- **Configurable pipeline:** Image and Video have independent, reorderable stage lists. Enable only the stages needed for the current task; stage-order checks prevent incompatible processing sequences.
- **Coloring:** match an image's colors to its original input or a selected reference image, or apply and adjust a 3D `.cube` LUT. Video uses the LUT workflow. Manual light/color adjustments, automatic analysis, reference-image matching, and graded LUT export are available; video can analyze the current frame or a fixed set of frames.
- **Sharpening:** choose NVIDIA NIS or AMD FidelityFX CAS, with adjustable sharpening strength for image or video output.
- **Unified media viewer:** compare Input and Output with **Split**, **2-Up**, and **Output** views, fit images to the viewer, inspect them at 100%, and scrub through video from a shared timeline.
- **Smart preview cache:** preview stages reuse unchanged intermediate results, reducing repeat work when adjusting later stages. Realtime Preview can refresh supported changes automatically; video previews follow the selected playhead position, while Frame Generation clips can also be rendered manually.
- **Video cache modes:** **Stage by Stage** writes video intermediates between processing cards, while **Rolling Cache (5 GB)** uses bounded disposable working storage and streams later stages where possible. Cache intermediates can use **FFV1** or **ProRes Proxy**.
- **Focus Preview:** hide the side panels to give the media viewer more space, using the viewer control, menu command, or `Ctrl+Shift+F`.
- **Full-resolution image previews:** inspect generated image previews at their output resolution.
- **Batch processing:** add multiple files or folders, drag and drop media, retry failed items, clear completed items, stop processing, and reveal completed files in Explorer.
- **Clipboard media:** paste clipboard bitmap images, copied local files, or supported browser image URLs into compatible batch workflows; remote web images are downloaded before being added to the queue.
- **Per-file progress:** each queue item shows its current state, progress, elapsed time, processing details, dimensions, and output path.
- **Export destinations:** save next to the source (**Same as Input**), to the application **Output** folder, or to a selected folder. **Auto**, **Copy**, and **Custom** naming modes are available.
- **Processing devices:** choose the NVIDIA RTX GPU used by AI/NGX stages separately from the **Decoding / Encoding Device**, which can use Automatic selection, CPU software codecs, or a detected Vulkan-capable device.
- **Codec and container selection:** choose the video codec and a compatible MP4, MKV, or MOV container separately. The application keeps the selected container when it is compatible and uses a supported default when a codec change requires one.
- **Safe output handling:** incomplete output is cleaned up when necessary, and existing files are not silently overwritten.
- **Media preservation:** the Neural Rendering pipeline exporter carries source audio, metadata, and chapters where compatible. Subtitle streams are not mapped by this exporter; original image metadata is not embedded in its exported images.

By default, completed media is saved to `outputs/`; a different destination can be selected in the Export dialog. Logs are available in `logs/` for troubleshooting.

## Supported media

| Type | Input | Output |
| --- | --- | --- |
| Images | Common image formats, HEIF/HEIC, SVG, TIFF, and many camera RAW formats | PNG, JPEG, WebP, AVIF, TIFF; 8-bit, or 16-bit for PNG/TIFF |
| Video | MP4, MKV, MOV, AVI, WebM, M4V, TS/MTS/M2TS, MXF, VOB, WMV, FLV, MPG/MPEG, and other supported video formats | H.264, H.265, AV1, ProRes Proxy, ProRes HQ, FFV1 Lossless RGB 10-bit |
| Live | Local video, direct network streams, YouTube, Twitch | Processed playback inside Visual Enhancer |

Image decoding applies EXIF orientation, handles supported color profiles, and retains transparency in formats that support it (not JPEG). Animated and multipage image sources use the first frame/page. **Unified pipeline image exports do not preserve original EXIF or other source metadata.** The Neural Rendering video exporter maps the processed picture together with original audio, metadata, and chapters where the container and codec allow it, but does not map subtitle streams.

## Requirements

- **Windows:** 64-bit Windows 11 with Direct3D 12.
- **NVIDIA RTX GPU:** a compatible NVIDIA GeForce RTX GPU with a current NVIDIA driver for NVIDIA-powered stages.
- **DLSS 5 Neural Rendering:** availability depends on supported NVIDIA RTX hardware, the installed driver, and successful feature initialization on the selected AI Processing GPU.
- **DLSS Super Resolution:** requires a compatible NVIDIA GPU, driver, and available DLSS Super Resolution runtime.
- **DLSS Frame Generation:** requires compatible NVIDIA RTX hardware. Hardware-accelerated GPU scheduling (HAGS) should be enabled; the runtime may reject Frame Generation when HAGS is disabled.
- **RTX Video:** RTX Video Super Resolution and RTX Video HDR require compatible NVIDIA RTX hardware, driver support, and their respective runtimes.
- **Vulkan filtering:** video scaling and other GPU filter operations require a working Vulkan device and driver. AMD, Intel, or NVIDIA Vulkan devices can be used for this path; CPU decoding/encoding mode still uses a Vulkan device for GPU filters.
- **Video decoding/encoding:** Automatic mode uses Vulkan hardware codecs where supported and falls back to software codecs when necessary. CPU mode uses software decoding/encoding; hardware support depends on the selected device, codec, driver, and bundled FFmpeg capabilities.

## Settings

The following processing controls are available through the **Neural Rendering** workspace's Image and Video stage cards. Export options are in **Export Settings** and the Export dialog; language, processing devices, cache, preview, and preset controls are in **Settings**. Image and Video retain independent stage arrangements, while individual processing parameters can be shared between their corresponding cards.

### Processing pipeline

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Mode | Image, Video | Image |
| Image stage order | DLSS Neural Rendering, Scaling, DLSS Super Resolution, RTX Super Resolution, Coloring, Sharpening; drag to reorder | Listed order |
| Video stage order | DLSS Neural Rendering, Scaling, DLSS Super Resolution, RTX Super Resolution, RTX Video HDR, DLSS Frame Generation, Coloring, Sharpening; drag to reorder | Listed order |
| Enabled stages | Each stage can be enabled or disabled independently | All off |
| Preview length | 1, 3, 5, 10, 20, 30 seconds for video clip previews | 3 seconds |

Stages execute **top to bottom** in their displayed order. Preflight checks dimensions and capabilities after each enabled stage. Video Scaling, DLSS Super Resolution, and RTX Super Resolution cannot process HDR video at their position; place them before HDR conversion. Sharpening with nonzero strength also must precede HDR conversion. **RTX Video HDR is an independent stage** that requires SDR video at its position and marks subsequent video as HDR. DLSS Frame Generation can work with supported HDR video. A Frame Generation target must be higher than the frame rate entering that stage. An export that finishes at an odd video dimension requires adjustment to even dimensions, except with FFV1 Lossless RGB 10-bit.

### DLSS 5 Neural Rendering

| Setting | Values | Default |
| --- | --- | --- |
| NR Style | Default, Natural, Cinematic | Default |
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
| Custom NR Mask | Load or clear a mask image; shared with Live for the current session | None |

**NR Passes** controls how many Neural Rendering passes are applied. Additional passes increase processing and can produce a stronger cumulative result. The Neural Rendering stage operates at the dimensions supplied by the preceding stages; use the separate **Scaling** stage to change those dimensions. Its input must be at least 64×64, and Neural Rendering supports dimensions up to 16384 pixels per side.

**Detail-Only** sets NR Color Strength to `0.00` and Tone Preservation to `1.00`. All other Neural Rendering controls remain editable. Setting Skin Structure Strength above `-1.00` automatically enables Automatic Mask so the skin adjustment can take effect; Automatic Mask can still be switched off manually afterward.

### Scaling and DLSS Super Resolution

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Scaling: scale | Source (Original), 200%, 175%, 150%, 125%, 75%, 50%, 25% | Source (Original) |
| Scaling: image filter | Lanczos4, Area, Bicubic, Bilinear, Nearest | Lanczos4 |
| Scaling: video filter | Spline36, Lanczos, Bicubic, Area, Bilinear | Spline36 |
| DLSS Super Resolution: mode | DLAA (1×), Quality (1.5×), Balanced (~1.72×), Performance (2×), Ultra Performance (3×) | Quality |
| DLSS Super Resolution: preset | Default, J, K, L, M | Default |

**Scaling** performs conventional resizing independently of Neural Rendering and RTX Video Super Resolution. It must produce at least 64×64 pixels; Neural Rendering supports up to 16384 pixels per dimension. **DLSS Super Resolution** is a separate neural upscaling stage: DLAA retains the input size, while the other modes increase it. DLSS Super Resolution requires at least 32×32 input and supports a maximum output texture dimension of 16384 pixels. Video output dimensions for DLSS Super Resolution are rounded up to even values.

### Upscale: Image

The **RTX Super Resolution** stage has these image controls; **DLSS Super Resolution** uses its separate mode and preset controls above.

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| VSR quality | 1 - Low, 2 - Medium, 3 - High, 4 - Ultra | 4 - Ultra |
| Output sizing | Scale factor or Custom dimensions | Scale factor |
| Scale factor | 1×, 1.5×, 2×, 3×, 4× | 2× |
| Custom dimensions | Up to 16384 pixels per dimension; cannot be smaller than the incoming image | 3840×2160 |
| Lock aspect ratio | Off, On | On |
| Output format | PNG, JPEG, WebP, AVIF, TIFF; in Export Settings | PNG |
| Image bit depth | 8-bit for all formats; 16-bit for PNG or TIFF | 8-bit |
| Image quality | 1–100 for lossy formats | 95 |
| Rename | Auto, Copy, Custom; in the Export dialog | Auto |

RTX Video Super Resolution can be used at **1×** to enhance an image without enlarging it. Sizing applies to the dimensions entering this stage, which may already have been changed by previous stages. Unified pipeline image exports do not preserve source EXIF metadata, even if the source image contains it.

### Upscale: Video

The Video pipeline exposes **RTX Super Resolution** and **RTX Video HDR** as separate processing cards. RTX Super Resolution controls enhancement and sizing; RTX Video HDR has its own HDR controls and does not depend on VSR.

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| RTX Super Resolution stage | Enable or disable the pipeline card | Off |
| VSR quality | 1 - Low, 2 - Medium, 3 - High, 4 - Ultra | 4 - Ultra |
| Output sizing | Scale factor or Custom dimensions | Scale factor |
| Scale factor | 1×, 1.5×, 2×, 3×, 4× | 2× |
| Custom dimensions | Up to 16384 pixels per dimension; cannot be smaller than the incoming video | 3840×2160 |
| Lock aspect ratio | Off, On | On |
| RTX Video HDR stage | Enable or disable the pipeline card; converts SDR input to HDR | Off |
| HDR contrast | 0–200 | 100 |
| HDR saturation | 0–200 | 100 |
| HDR middle gray | 10–100 | 50 |
| HDR peak luminance | 400–2000 nits | 1000 nits |
| HDR processing precision | Packed 10-bit, Packed 10-bit (FP16) | Packed 10-bit |

RTX Video HDR accepts SDR video at its position in the pipeline, not existing HDR input. It retains the dimensions produced by preceding stages rather than using the RTX Super Resolution sizing controls. RTX Super Resolution and RTX Video HDR can both be enabled in the same workflow. To export an HDR result, enable **Preserve / Export 10-bit HDR** in Export Settings and choose an HDR-capable codec.

### Frame Interpolation

**DLSS Frame Generation** is a stage in the Video pipeline rather than a separate workspace.

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Output FPS | 23.976, 25, 29.97, 30, 50, 59.94, 60, 90, 119.88, 120, 144, 165, 180, 240, 360, 480 | 60 |
| DLSS engine | Auto, Native DLSSG, Cascade | Auto |
| Preview length | 1, 3, 5, 10, 20, 30 seconds; shared video preview selector | 3 seconds |

**Auto** chooses a suitable Frame Generation path for the source and selected target FPS. The target must exceed the frame rate entering this stage; otherwise, preflight rejects the pipeline. A source already at or above the selected target is not silently resampled by this stage. Configure the final codec and HDR output in the shared video Export Settings.

### Coloring and sharpening

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Image Coloring mode | Color Match, LUT | Color Match |
| Color Match source | Input Image, Selected Image | Input Image |
| Video Coloring | Apply a 3D `.cube` LUT and adjustments | LUT |
| LUT file | Optional 3D `.cube` LUT | None |
| LUT reference image | Optional image used to derive color adjustments from the current input/reference pair | None |
| LUT resolution | 33, 65, 129 | 33 |
| LUT strength | 0–200% | 100% |
| Exposure | -3.00–3.00 EV | 0.00 EV |
| Contrast, Highlights, Shadows, Whites, Blacks, Midtones | -100–100 each | 0 |
| Temperature, Tint, Vibrance, Saturation | -100–100 each | 0 |
| Hue Shift | -180°–180° | 0° |
| Sharpening method | NVIDIA NIS, AMD CAS | AMD CAS |
| Sharpening strength | 0–100% | 50% |

**Color Match** adjusts a processed image using either its original input or a user-selected reference image; it is not a video option. **LUT** mode applies a supplied `.cube` file or uses the adjustment controls to generate a grade. For LUT adjustments, **Auto** analyzes an image, **Auto 1 Frame** analyzes the current video frame, and **Auto Fixed Frames** analyzes a fixed set of video frames. Selecting a LUT reference image can derive adjustments by comparing the source with that reference. **Reset** clears adjustment values, and **Save LUT** exports the graded 3D LUT. The Coloring and Sharpening cards can each be enabled, disabled, and repositioned like other pipeline stages. Video Sharpening with nonzero strength must run before HDR conversion.

### Live

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Source | Online URL or Local video | Online |
| Source quality | Auto, 480p, 720p, 1080p, 1440p, 2160p | Auto |
| Max input resolution | 480p, 720p, 1080p, 1440p, 2160p | 720p |
| Segment duration | 1, 2, 4 seconds | 2 seconds |
| Target FPS | Auto, Source, 60, 30, 24 | Auto |
| Playback buffer | 2–30 seconds | 6 seconds |
| Live Scale | Source (Original), 200%, 175%, 150%, 125%, 75%, 50%, 25% | Source (Original) |
| Neural Rendering controls | Independent Live copy of the Neural Rendering controls | Neural Rendering defaults |

Most Live Neural Rendering controls can be changed while playback is active. Buffered frames keep their previous appearance until playback reaches newly processed frames. Live Scale, source selection, source quality, maximum input resolution, segment duration, target FPS, and playback buffer take effect on the next **Start Live**. The Live tab uses Neural Rendering independently of the Image/Video stage pipelines.

### Video output and encoding

| Output setting | Behavior |
| --- | --- |
| H.264 | 8-bit SDR; MP4 is the default container |
| H.265 | SDR or 10-bit HDR; MKV is the default container |
| AV1 | SDR or 10-bit HDR; MP4 or MKV; MKV by default |
| ProRes Proxy | 10-bit 4:2:2; MOV or MKV; supports HDR; MOV by default |
| ProRes HQ | 10-bit 4:2:2; MOV or MKV; supports HDR; MOV by default |
| FFV1 Lossless RGB 10-bit | Lossless 10-bit RGB; supports HDR; MKV by default |
| Video container | MP4, MKV, MOV; only combinations accepted by the application are offered for the selected codec |
| Default video codec | H.264 |
| Encoding quality | Auto (Default), Good, Best, Max; ProRes HQ and FFV1 use codec-fixed quality |
| Hardware/software encoding | H.264, H.265, and AV1 use the selected Decoding / Encoding Device when a supported Vulkan encoder is available; software fallback is used otherwise |
| Preserve / Export 10-bit HDR | Off by default; available with H.265, AV1, ProRes Proxy, ProRes HQ, and FFV1 Lossless RGB 10-bit |
| Export destination | Same as Input, Output (`outputs/`), Select Folder; Output by default |
| Rename | Auto creates a workflow-specific name; Copy keeps the source base name; Custom adds the selected suffix |

The Neural Rendering video exporter maps the processed video alongside the original audio, metadata, and chapters where supported. Audio is copied or encoded according to container compatibility; subtitle streams are **not** mapped by this exporter. HDR results require the corresponding export setting and a supported codec. The final video codec, container, encoding quality, HDR mode, destination, and filename settings apply to the complete pipeline, not to each individual stage.

### Application

| Setting | Choices and behavior | Default |
| --- | --- | --- |
| Interface Language | Available application languages | English (US) |
| AI Processing GPU | Automatic (Best Available) or a compatible detected NVIDIA RTX GPU | Automatic |
| Decoding / Encoding Device | Automatic (Best Available), CPU software decoding/encoding, or a detected Vulkan-capable device | Automatic |
| Cache Memory | Stage by Stage, Rolling Cache (5 GB) | Stage by Stage |
| Cache codec | FFV1, ProRes Proxy | FFV1 |
| Realtime Preview | Off, On; supported Image and Video pipeline changes | On |
| Settings preset | Export or import adjustable application settings as JSON; custom NR mask is not included | n/a |
| Factory reset | Restores processing, device, cache, and export settings to defaults and clears the custom NR mask; interface language and window layout are separate | n/a |

**Stage by Stage** caches complete video intermediates between enabled processing cards. **Rolling Cache (5 GB)** uses bounded disposable working storage and streams later stages where possible instead of retaining another complete intermediate for every card. **FFV1** provides lossless RGB 10-bit cache storage; **ProRes Proxy** uses 10-bit 4:2:2 intermediates.

Saved GPU and decoding/encoding device selections are validated when the application starts; unavailable selections fall back to Automatic. Compatible older settings presets can be imported and migrated automatically. Pipeline stage order and enabled-state settings are included in saved presets; the custom NR mask is not.

## Command line

**`VE_CLI.exe`** runs the same image and video processing engines as the desktop application without opening a window. Keep it in the application folder beside `cli.py`, `src`, and `bin`; no separate Python installation is required. Run it from PowerShell, Command Prompt, or another program.

### Commands

| Command | Default behavior | Common options |
| --- | --- | --- |
| `render` | Neural Rendering at the original dimensions; PNG for images, H.264/MP4 for videos | `--style natural`, `--strength 0.8`, `--preset pipeline.json` |
| `upscale` | RTX Super Resolution at 2×; PNG for images, H.265/MKV for videos | `--scale 2`, `--size 3840x2160`, `--engine dlss --mode quality` |
| `interpolate` | DLSS Frame Generation to 60 FPS; H.264/MP4 | `--fps 120`, `--preset settings.json` |
| `devices` | List NVIDIA GPU indices, UUIDs, driver versions, and VRAM | `--json` |

Use `VE_CLI.exe --help` for the command list and `VE_CLI.exe render --help`, `VE_CLI.exe upscale --help`, or `VE_CLI.exe interpolate --help` for command-specific options. Running without arguments also shows help.

### Quick examples

```powershell
# Enhance an image or video with Neural Rendering.
.\VE_CLI.exe render "C:\Media\photo.png" -o "C:\Results\photo.png"
.\VE_CLI.exe render "C:\Media\clip.mp4" -o "C:\Results"

# Upscale 2x using RTX Super Resolution.
.\VE_CLI.exe upscale "C:\Media\clip.mp4" --scale 2 -o "C:\Results"

# Upscale with DLSS Super Resolution; Performance is approximately 2x.
.\VE_CLI.exe upscale "C:\Media\photo.png" --engine dlss --mode performance -o "C:\Results"

# Increase video frame rate to 60 FPS.
.\VE_CLI.exe interpolate "C:\Media\clip.mp4" --fps 60 -o "C:\Results"

# Run every enabled stage from a preset exported by the desktop app.
.\VE_CLI.exe render "C:\Media\clip.mp4" --preset "C:\Presets\cinematic.json" -o "C:\Results"
```

DLSS output sizing follows the selected mode: DLAA 1×, Quality 1.5×, Balanced approximately 1.724×, Performance 2×, and Ultra Performance 3×. `--scale` and `--size` apply to RTX Super Resolution; `--size` uses the exact requested dimensions. Frame Generation accepts the same target rates as the application, from 23.976 through 480 FPS, and the selected target must be higher than the input frame rate.

### Files and batches

The CLI accepts individual files, multiple inputs, folders, and quoted wildcards. Add `--recursive` to include subfolders. Jobs run one file at a time in the supplied order, duplicate inputs are processed once, and unsupported files discovered through folder or wildcard scans are skipped.

```powershell
# Process several files.
.\VE_CLI.exe render "photo.png" "clip.mp4" -o ".\results"

# Process supported files directly inside a folder.
.\VE_CLI.exe upscale "C:\Photos" -o "C:\Results"

# Include subfolders or use a wildcard.
.\VE_CLI.exe render "C:\Media" --recursive -o "C:\Results"
.\VE_CLI.exe render "C:\Media\*.mp4" -o "C:\Results"
```

Without `-o`, output is written to the application's `outputs` folder. For a single input, `-o` can also be a supported output filename; its extension selects the image format or video container. Existing files are not overwritten. Folder outputs keep the source base name and add `_2`, `_3`, and so on when required. Use **Ctrl+C** or **Ctrl+Break** to request cancellation; completed files are kept and unfinished output from the active job is cleaned up.

### Presets and output options

The CLI starts from factory defaults rather than the desktop application's currently loaded settings. Export a JSON settings preset from the desktop app and pass it with `--preset` to reuse stage order, enabled stages, and advanced processing/output settings. Command-line flags override corresponding preset values. Reference images and LUT files stored in a preset must remain accessible.

```powershell
# Select an image output format.
.\VE_CLI.exe render "photo.png" --format webp -o "C:\Results"

# Select video codec, container, and quality.
.\VE_CLI.exe render "clip.mp4" --codec hevc --container mp4 --quality best -o "C:\Results"

# Select the RTX processing/video GPU by NVIDIA index or UUID.
.\VE_CLI.exe render "clip.mp4" --gpu 0 -o "C:\Results"

# Inspect available NVIDIA devices.
.\VE_CLI.exe devices
```

Supported CLI image formats are PNG, JPEG, WebP, AVIF, and TIFF. Video codecs are `h264`, `hevc`, `av1`, `prores` (HQ), and `ffv1` (lossless); quality modes are `auto`, `good`, `best`, and `max`. `--container mp4|mkv|mov` selects the video container, and incompatible codec/container combinations are rejected. ProRes supports MOV and MKV; AV1 supports MP4 and MKV. HDR, sharpening, coloring, image bit depth, image compression quality, and other advanced controls can be supplied through presets.

### Dry run and JSON output

Use `--dry-run` to inspect media metadata, stage order, planned output paths, dimensions, and frame rates without starting the enhancement engines or creating output folders. `--quiet` suppresses progress while retaining errors and the final result. `--json` writes a single machine-readable result object to stdout while progress and errors use stderr.

```powershell
# Validate and inspect a job without rendering.
.\VE_CLI.exe upscale "photo.png" --scale 2 --dry-run

# Produce one JSON result object for scripting.
.\VE_CLI.exe render "C:\Media" --json --quiet -o "C:\Results"

# PowerShell: parse the result and inspect the process exit status.
$resultText = .\VE_CLI.exe render "clip.mp4" --json --quiet -o "C:\Results"
$exitCode = $LASTEXITCODE
$result = $resultText | ConvertFrom-Json
$result.items
```

| Exit code | Meaning |
| --- | --- |
| `0` | All files succeeded, or help/devices/dry-run succeeded |
| `1` | Processing, runtime, hardware, or unexpected failure |
| `2` | Invalid arguments, paths, or settings |
| `130` | Cancelled |

## License and third-party notices

Project-owned Visual Enhancer material is distributed under the **Merserk Source License 1.0**. The software may be used for personal, professional, and commercial work, and outputs created or processed with Visual Enhancer may be used commercially. Redistribution, mirroring, repackaging, rebranding, resale, sublicensing, publishing modified builds, and similar redistribution require prior written permission except where the license states otherwise. See [LICENSE](LICENSE) for the complete terms.

Third-party components remain subject to their own licenses and are not relicensed under the Merserk Source License. See [THIRD-PARTY-NOTICES](THIRD-PARTY-NOTICES) for the notices included with the application.

Visual Enhancer is an independent community project and is not affiliated with, sponsored by, or endorsed by NVIDIA. NVIDIA, GeForce RTX, DLSS, and RTX Video are trademarks and/or registered trademarks of NVIDIA Corporation.
