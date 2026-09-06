from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
from ..core.batch_ui import (
    BATCH_HEADERS, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls,
)
from ..core.disk_paths import create_media_archive

from ..core.ffmpeg import hdr_mode_supported
from ..core.ffmpeg.preview import normalize_preview_encoding, resolve_final_preview
from ..core.naming import RENAME_MODES
from ..core.paths import OUTPUTS
from ..settings.models import CODEC_CHOICES, CONTAINER_CHOICES, QUALITY_CHOICES, UISettings, coerce_hdr_mode
from ..settings.storage import current_preview_encoding, processing_gpu_settings
from .batch import interpolate_videos
from .models import ENGINE_CHOICES, FPS_CHOICES, FrameInterpolationOptions
from .preview import normalize_video_paths, preview_frame_interpolation, update_frame_interpolation_preview_mode


def hdr_mode_update(codec: str):
    allowed = hdr_mode_supported(codec)
    if not allowed:
        return gr.update(value=False, interactive=False)
    return gr.update(interactive=True)


def rename_suffix_update(mode: str):
    return gr.update(interactive=mode == "Custom")

def render_frame_interpolation_batch(
    input_paths: list[str] | str | None,
    target_fps: str,
    engine: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    rename_mode: str,
    custom_suffix: str,
    progress=gr.Progress(track_tqdm=False),
    *, output_dir=None, controller=None, on_item_update=None, direct_disk=False,
):
    paths = normalize_video_paths(input_paths)
    if not paths:
        raise gr.Error("Choose at least one video first.")
    effective_hdr = coerce_hdr_mode(codec, hdr_mode)
    options = FrameInterpolationOptions(
        ai_gpu_uuid=processing_gpu_settings()[0],
        video_gpu_uuid=processing_gpu_settings()[1],
        target_fps=target_fps,
        engine=engine,
        codec=codec,
        container=container,
        quality=quality,
        hdr_mode=effective_hdr,
        rename_mode=rename_mode,
        custom_suffix=custom_suffix,
    )

    def report(value: float, message: str) -> None:
        progress(value, desc=message)

    try:
        result = interpolate_videos(
            paths, options, report,
            output_dir=output_dir if direct_disk else (output_dir or (OUTPUTS / "frame_interpolation")),
            controller=controller, on_item_update=on_item_update,
        )
    except Exception as exc:
        traceback.print_exc()
        if on_item_update is not None:
            raise
        return gr.update(value=None, visible=not direct_disk), None, [], f"Failed: {exc}"
    ordered: list[tuple[int, list[str]]] = []
    for item in result.successes:
        value = item.result
        details = (
            f"{value.output_frames} frames; {value.selected_path}; "
            f"{value.elapsed_seconds:.1f}s; processing {value.output_frames / max(value.elapsed_seconds, 0.001):.1f} frames/s; "
            f"native {value.native_multiplier}×; cascade stages {value.cascade_stages}; "
            f"copied {value.copied_frames}, DLSSG {value.generated_frames}, "
            f"cuts {value.scene_cuts}; report: {value.report_path}"
        )
        ordered.append(
            (item.index, [Path(item.input_path).name, "Complete", Path(value.output_path).name, details])
        )
    for item in result.failures:
        state = "Skipped" if item.error == "Cancelled before rendering." else (
            "Cancelled" if item.cancelled else "Failed"
        )
        ordered.append((item.index, [Path(item.input_path).name, state, "", item.error]))
    rows = [row for _index, row in sorted(ordered, key=lambda entry: entry[0])]
    files = [item.result.output_path for item in result.successes]
    try:
        preview_mode = normalize_preview_encoding(current_preview_encoding())
    except Exception:
        preview_mode = "Auto"
    preview = None
    used_derivative = False
    if not direct_disk and len(paths) == 1 and result.successes and not result.cancelled:
        preview, used_derivative = resolve_final_preview(files[0], preview_mode, controller=controller)
    status = (
        f"{'Cancelled' if result.cancelled else 'Complete'}: "
        f"{len(result.successes)} completed, {len(result.failures)} failed/cancelled. "
        f"Batch manifest: {result.manifest_path}"
    )
    if result.failures:
        status += f"\nFirst error: {result.failures[0].error}"
    if used_derivative:
        status += "\nBrowser preview transcoded to H.264; the original file is unchanged."
    archive_path = None
    if not direct_disk and not result.cancelled:
        archive_path = create_media_archive(
            files, output_dir, "DLSSFG_VIDEO_BATCH", controller=controller,
        )
    return gr.update(value=preview, visible=not direct_disk), archive_path, rows, status

@dataclass(slots=True)
class FrameInterpolationTab:
    sources: object
    input_preview: object
    input_actions: object
    select_source: object
    clear_source: object
    target_fps: object
    engine: object
    quality: object
    codec: object
    container: object
    rename_mode: object
    custom_suffix: object
    hdr_mode: object
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
            self.sources, self.target_fps, self.engine, self.codec, self.container, self.quality,
            self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]

    @property
    def preview_inputs(self) -> list[object]:
        return [self.sources, self.target_fps, self.engine, self.codec, self.container, self.quality]

    @property
    def settings_inputs(self) -> list[object]:
        return [
            self.target_fps, self.engine, self.codec, self.container, self.quality,
            self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]


def build_frame_interpolation_tab(settings: UISettings) -> FrameInterpolationTab:
    # The uploader lives in its own full-width row, above the input/output columns,
    # so both preview panes are the first element in their column and line up
    # vertically regardless of how many files are queued in the uploader.
    sources = gr.File(
        label="Input video(s)", file_count="multiple", file_types=["video"],
        type="filepath", allow_reordering=True, elem_id="frame-interpolation-upload-list",
        elem_classes=["media-upload-surface"],
    )
    input_path, output_path = build_path_controls()
    with gr.Row():
        with gr.Column(scale=3):
            input_preview = gr.Video(label="Input video preview", interactive=False, visible=False)
            with gr.Row(
                visible=False, elem_id="frame-interpolation-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    "Choose Videos", ["video"], "frame-interpolation-select-input",
                )
                clear_source = build_media_clear_button("frame-interpolation-clear-input")
            input_path, output_path = build_path_controls()
            with gr.Accordion("DLSS Frame Generation Settings", open=True):
                with gr.Row():
                    target_fps = gr.Dropdown(
                        FPS_CHOICES, value=settings.frame_interpolation_target_fps,
                        label="Output FPS", info="Fractional choices use exact 1001-based rates.",
                    )
                    engine = gr.Radio(
                        ENGINE_CHOICES, value=settings.frame_interpolation_engine, label="DLSS engine",
                        info="Auto uses a supported exact native grid, then the 2× cascade when required.",
                    )
            quality = gr.Radio(
                QUALITY_CHOICES, value=settings.frame_interpolation_quality,
                label="Encoding quality", info="Auto uses output resolution, selected FPS, and codec.",
            )
            with gr.Row():
                codec = gr.Dropdown(
                    CODEC_CHOICES, value=settings.frame_interpolation_codec,
                    label="Video codec", info="Plain = CPU, Suffixed = NVIDIA NVENC.",
                )
                container = gr.Dropdown(
                    CONTAINER_CHOICES, value=settings.frame_interpolation_container, label="Container"
                )
            with gr.Row():
                rename_mode = gr.Radio(
                    RENAME_MODES, value=settings.frame_interpolation_rename_mode, label="Rename",
                    info="Auto adds a DLSSFG timestamp; Copy keeps the original base name; Custom appends your suffix.",
                )
                custom_suffix = gr.Textbox(
                    value=settings.frame_interpolation_custom_suffix, label="Custom suffix",
                    placeholder="_DLSSFG", interactive=settings.frame_interpolation_rename_mode == "Custom",
                )
            hdr_mode = gr.Checkbox(
                value=settings.frame_interpolation_hdr_mode and hdr_mode_supported(settings.frame_interpolation_codec),
                label="HDR Mode",
                info="When on: 10-bit output, copies input colorspace; keeps HDR if input is HDR. Only for H.265 / AV1 / ProRes.",
                interactive=hdr_mode_supported(settings.frame_interpolation_codec),
            )
            with gr.Row():
                preview = gr.Button("Preview 3 sec", visible=False)
                render = gr.Button("Interpolate video(s)", variant="primary")
                stop = gr.Button("Stop", variant="stop")
                reset = gr.Button("Reset settings")
        with gr.Column(scale=3):
            output_video = gr.Video(
                label="Interpolated output", interactive=False, visible=True, height=520,
            )
            zip_download = gr.DownloadButton("Save as ZIP", visible=False)
            status = gr.Textbox(label="Status", interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(
                headers=BATCH_HEADERS,
                datatype=["str"] * len(BATCH_HEADERS), interactive=False,
                label="Batch results", wrap=True,
            )
    tab = FrameInterpolationTab(
        sources, input_preview, input_actions, select_source, clear_source, target_fps, engine, quality, codec, container, rename_mode,
        custom_suffix, hdr_mode, preview, render, stop, reset, output_video, zip_download, status, results
    )
    tab.input_path, tab.output_path = input_path, output_path
    bind_frame_interpolation_events(tab)
    return tab


def bind_frame_interpolation_events(tab: FrameInterpolationTab) -> None:
    bind_batch_ui(tab, render_frame_interpolation_batch, kind="video",
                  preview_mode=update_frame_interpolation_preview_mode,
                  preview_actions=[(tab.preview, preview_frame_interpolation)])
    tab.codec.change(hdr_mode_update, inputs=tab.codec, outputs=tab.hdr_mode, queue=False)
    tab.rename_mode.change(rename_suffix_update, inputs=tab.rename_mode, outputs=tab.custom_suffix, queue=False)
