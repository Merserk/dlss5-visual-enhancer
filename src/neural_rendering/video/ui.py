from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
from ...core.batch_ui import (
    BATCH_HEADERS, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls,
)
from ...core.disk_paths import create_media_archive

from ...core.ffmpeg import hdr_mode_supported
from ...core.ffmpeg.preview import normalize_preview_encoding, resolve_final_preview
from ...core.naming import RENAME_MODES
from ...core.paths import OUTPUTS
from ...core.runtime import DLSS_MODEL_PRESETS, NR_PRESETS, NR_STYLES, UPSCALING_MODES
from ...settings.models import (
    AUTOMATIC_MASK_CHOICES, CODEC_CHOICES, CONTAINER_CHOICES, QUALITY_CHOICES, UISettings,
    automatic_mask_choice, coerce_hdr_mode, parse_automatic_mask,
)
from ...settings.storage import current_preview_encoding, processing_gpu_settings
from .batch import convert_videos
from .models import ConversionOptions
from .preview import (
    _process_video, normalize_video_paths, preview_one_frame, preview_video, update_video_preview_mode,
)


def hdr_mode_update(codec: str):
    allowed = hdr_mode_supported(codec)
    if not allowed:
        return gr.update(value=False, interactive=False)
    return gr.update(interactive=True)


def rename_suffix_update(mode: str):
    return gr.update(interactive=mode == "Custom")


UPSCALING_CHOICES = tuple((mode["label"], factor) for factor, mode in UPSCALING_MODES.items())


def build_neural_controls(settings: UISettings):
    nr_preset = gr.Dropdown(
        list(NR_PRESETS), value=settings.nr_preset, label="NR Preset",
        info="Experimental content-dependent neural-rendering model hint. Default is recommended."
    )
    nr_style = gr.Radio(
        list(NR_STYLES), value=settings.nr_style, label="NR Style",
        info="Selects the native neural-rendering style."
    )
    upscaling_factor = gr.Dropdown(
        choices=list(UPSCALING_CHOICES), value=settings.upscaling_factor,
        label="Upscaling factor", info="Uses NVIDIA's fixed DLSS modes. DLAA keeps source resolution."
    )
    with gr.Row():
        nr_intensity = gr.Slider(
            0.0, 2.0, value=settings.nr_intensity, step=0.05, precision=2,
            label="NR Intensity", info="Overall neural-rendering strength.", buttons=["reset"]
        )
        local_tone_strength = gr.Slider(
            0.0, 2.0, value=settings.local_tone_strength, step=0.05, precision=2,
            label="Local Tone Strength", info="Local tone and contrast enhancement.", buttons=["reset"]
        )
    with gr.Row():
        local_structure_strength = gr.Slider(
            0.0, 2.0, value=settings.local_structure_strength, step=0.05, precision=2,
            label="Local Structure Strength", info="Local detail and texture structure.", buttons=["reset"]
        )
        skin_structure_strength = gr.Slider(
            -1.0, 2.0, value=settings.skin_structure_strength, step=0.05, precision=2,
            label="Skin Structure Strength", info="Skin-specific structure; -1.00 is the native default.",
            buttons=["reset"]
        )
    automatic_mask = gr.Radio(
        choices=AUTOMATIC_MASK_CHOICES,
        value=automatic_mask_choice(settings.automatic_mask),
        label="Automatic Mask",
        info=(
            "Experimental runtime-generated mask that changes where Neural Rendering is "
            "applied; it may cause flicker or inconsistent results."
        ),
    )
    return [
        nr_preset, nr_style, nr_intensity, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask
    ]


def build_dlss_model_control(settings: UISettings):
    return gr.Dropdown(
        choices=list(DLSS_MODEL_PRESETS),
        value=settings.dlss_model_preset,
        label="DLSS Model Preset",
        info=(
            "Default lets NVIDIA select its normal mode-specific presets. "
            "J, K, L, or M forces that model preset for every DLSS scaling mode."
        ),
    )

def render_video(
    input_path: str,
    nr_preset: str,
    nr_style: str,
    nr_intensity: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    dlss_model_preset: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool = False,
    progress=gr.Progress(track_tqdm=False),
):
    return _process_video(
        input_path, nr_preset, nr_style, nr_intensity, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask, dlss_model_preset,
        codec, container, quality, hdr_mode,
        progress, None, None
    )

def render_video_batch(
    input_paths: list[str] | str | None,
    nr_preset: str,
    nr_style: str,
    nr_intensity: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    dlss_model_preset: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    rename_mode: str,
    custom_suffix: str,
    progress=gr.Progress(track_tqdm=False),
    *, output_dir=None, controller=None, on_item_update=None, direct_disk=False,
) -> tuple[object, str | None, list[list[str]], str]:
    paths = normalize_video_paths(input_paths)
    if not paths:
        raise gr.Error("Choose at least one video first.")
    effective_hdr = coerce_hdr_mode(codec, hdr_mode)
    options = ConversionOptions(
        ai_gpu_uuid=processing_gpu_settings()[0],
        video_gpu_uuid=processing_gpu_settings()[1],
        nr_preset=nr_preset,
        nr_style=nr_style,
        nr_intensity=nr_intensity,
        local_tone_strength=local_tone_strength,
        local_structure_strength=local_structure_strength,
        skin_structure_strength=skin_structure_strength,
        automatic_mask=parse_automatic_mask(automatic_mask),
        dlss_model_preset=dlss_model_preset,
        upscaling_factor=upscaling_factor,
        codec=codec,
        container=container,
        quality=quality,
        preserve_hdr=effective_hdr,
        rename_mode=rename_mode,
        custom_suffix=custom_suffix,
    )

    def report(value: float, message: str) -> None:
        progress(value, desc=message)

    try:
        result = convert_videos(
            paths, options, progress=report,
            output_dir=output_dir if direct_disk else (output_dir or (OUTPUTS / "videos")),
            controller=controller, on_item_update=on_item_update,
        )
    except Exception as exc:
        traceback.print_exc()
        if on_item_update is not None:
            raise
        return gr.update(value=None, visible=not direct_disk), None, [], f"Failed: {exc}"

    ordered_rows: list[tuple[int, list[str]]] = []
    for item in result.successes:
        conversion = item.result
        details = (
            f"{conversion.frames} frames in {conversion.elapsed_seconds:.1f}s; "
            f"DLSS {conversion.dlss_mode}: "
            f"{conversion.render_width}×{conversion.render_height} → "
            f"{conversion.output_width}×{conversion.output_height}; "
            f"report: {conversion.report_path}"
        )
        ordered_rows.append(
            (
                item.index,
                [
                    Path(item.input_path).name,
                    "Complete",
                    Path(conversion.output_path).name,
                    details,
                ],
            )
        )
    for item in result.failures:
        state = "Skipped" if item.error == "Cancelled before rendering." else (
            "Cancelled" if item.cancelled else "Failed"
        )
        ordered_rows.append(
            (item.index, [Path(item.input_path).name, state, "", item.error])
        )
    rows = [row for _index, row in sorted(ordered_rows, key=lambda entry: entry[0])]
    try:
        preview_mode = normalize_preview_encoding(current_preview_encoding())
    except Exception:
        preview_mode = "Auto"
    output_preview = None
    used_derivative = False
    if not direct_disk and len(paths) == 1 and result.successes and not result.cancelled:
        candidate = result.successes[0].result.output_path
        output_preview, used_derivative = resolve_final_preview(
            candidate, preview_mode, controller=controller
        )
    failed_count = sum(not item.cancelled for item in result.failures)
    cancelled_count = sum(
        item.cancelled and item.error != "Cancelled before rendering."
        for item in result.failures
    )
    skipped_count = sum(
        item.error == "Cancelled before rendering." for item in result.failures
    )
    state = "Cancelled" if result.cancelled else "Complete"
    status = (
        f"{state}: {len(result.successes)} completed, {failed_count} failed, "
        f"{cancelled_count} cancelled, {skipped_count} skipped. "
        f"Batch manifest: {result.manifest_path}"
    )
    if result.failures:
        status += f"\nFirst error: {result.failures[0].error}"
    if used_derivative:
        status += "\nBrowser preview transcoded to H.264; the original file is unchanged."
    archive_path = None
    if not direct_disk and not result.cancelled:
        archive_path = create_media_archive(
            (item.result.output_path for item in result.successes),
            output_dir,
            "DLSS5_VIDEO_BATCH",
            controller=controller,
        )
    return gr.update(value=output_preview, visible=not direct_disk), archive_path, rows, status

@dataclass(slots=True)
class VideoTab:
    sources: object
    input_preview: object
    input_actions: object
    select_source: object
    clear_source: object
    neural: list[object]
    model_preset: object
    quality: object
    codec: object
    container: object
    rename_mode: object
    custom_suffix: object
    hdr_mode: object
    preview_frame: object
    preview: object
    render: object
    stop: object
    reset: object
    output_video: object
    zip_download: object
    status: object
    results: object
    input_path: object = None
    output_path: object = None
    job_state: object = None

    @property
    def render_inputs(self) -> list[object]:
        return [
            self.sources, *self.neural, self.model_preset, self.codec, self.container,
            self.quality, self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]

    @property
    def preview_inputs(self) -> list[object]:
        return [
            self.sources, *self.neural, self.model_preset, self.codec, self.container,
            self.quality, self.hdr_mode,
        ]

    @property
    def settings_inputs(self) -> list[object]:
        return [
            *self.neural, self.model_preset, self.codec, self.container, self.quality,
            self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]


def build_video_tab(settings: UISettings) -> VideoTab:
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(
                label="Input video(s)", file_count="multiple", file_types=["video"],
                type="filepath", allow_reordering=True, elem_id="video-upload-list",
                elem_classes=["media-upload-surface"],
            )
            input_preview = gr.Video(label="Input video preview", interactive=False, visible=False)
            with gr.Row(
                visible=False, elem_id="video-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    "Choose Videos", ["video"], "video-select-input",
                )
                clear_source = build_media_clear_button("video-clear-input")
            input_path, output_path = build_path_controls()
            with gr.Accordion("DLSS 5 Neural Rendering Settings", open=True):
                neural = build_neural_controls(settings)
            with gr.Accordion("DLSS 5 Settings", open=True):
                model_preset = build_dlss_model_control(settings)
            quality = gr.Radio(
                QUALITY_CHOICES, value=settings.quality, label="Encoding quality",
                info="Auto uses output resolution, FPS, and codec. Good = Auto×2, Best = Auto×4, Max = CQ 0.",
            )
            with gr.Row():
                codec = gr.Dropdown(
                    CODEC_CHOICES, value=settings.codec, label="Video codec",
                    info="Plain = CPU (libx264 / libx265 / libsvtav1). Suffixed (NVIDIA NVENC) = GPU. ProRes Proxy uses 10-bit 4:2:2 and requires MOV or MKV.",
                )
                container = gr.Dropdown(CONTAINER_CHOICES, value=settings.container, label="Container")
            with gr.Row():
                rename_mode = gr.Radio(
                    RENAME_MODES, value=settings.video_rename_mode, label="Rename",
                    info="Auto adds the current DLSS5 timestamp; Copy keeps the original base name; Custom appends your suffix.",
                )
                custom_suffix = gr.Textbox(
                    value=settings.video_custom_suffix, label="Custom suffix", placeholder="_DLSS5",
                    interactive=settings.video_rename_mode == "Custom",
                )
            hdr_mode = gr.Checkbox(
                value=settings.hdr_mode and hdr_mode_supported(settings.codec),
                label="HDR Mode",
                info="When on: 10-bit output, copies input colorspace; keeps HDR if input is HDR. Only for H.265 / AV1 / ProRes.",
                interactive=hdr_mode_supported(settings.codec),
            )
            with gr.Row():
                preview_frame = gr.Button("Preview 1 frame", visible=False)
                preview = gr.Button("Preview 3 sec", visible=False)
                render = gr.Button("Render video(s)", variant="primary")
                stop = gr.Button("Stop", variant="stop")
                reset = gr.Button("Reset settings")
        with gr.Column(scale=3):
            output_video = gr.Video(
                label="Output video", interactive=False, visible=True, height=520,
            )
            zip_download = gr.DownloadButton("Save as ZIP", visible=False)
            status = gr.Textbox(label="Status", interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(
                headers=BATCH_HEADERS,
                datatype=["str"] * len(BATCH_HEADERS), interactive=False,
                label="Batch results", wrap=True,
            )
    tab = VideoTab(
        sources, input_preview, input_actions, select_source, clear_source, neural, model_preset, quality, codec, container, rename_mode,
        custom_suffix, hdr_mode, preview_frame, preview, render, stop, reset, output_video,
        zip_download, status, results
    )
    tab.input_path, tab.output_path = input_path, output_path
    bind_video_events(tab)
    return tab


def bind_video_events(tab: VideoTab) -> None:
    bind_batch_ui(tab, render_video_batch, kind="video", preview_mode=update_video_preview_mode,
                  preview_actions=[(tab.preview_frame, preview_one_frame), (tab.preview, preview_video)])
    tab.rename_mode.change(rename_suffix_update, inputs=tab.rename_mode, outputs=tab.custom_suffix, queue=False)
    tab.codec.change(hdr_mode_update, inputs=tab.codec, outputs=tab.hdr_mode, queue=False)
