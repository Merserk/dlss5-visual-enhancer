from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
from ..core.batch_ui import (
    batch_headers, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls, build_save_controls,
)
from ..core.i18n import batch_state_label, option_label, t, translator

from ..core.ffmpeg import container_for_codec, hdr_mode_supported
from ..core.ffmpeg.preview import normalize_preview_encoding, resolve_final_preview
from ..core.naming import RENAME_MODES
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


def codec_settings_update(codec: str):
    return gr.update(value=container_for_codec(codec)), hdr_mode_update(codec)


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
        raise gr.Error(t("frame_interpolation.error.choose_video"))
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
        result = interpolate_videos(paths, options, report, output_dir=output_dir,
                                    controller=controller, on_item_update=on_item_update)
    except Exception as exc:
        traceback.print_exc()
        if on_item_update is not None:
            raise
        return gr.update(value=None, visible=not direct_disk), None, [], t("neural.video.status.failed", error=exc)
    ordered: list[tuple[int, list[str]]] = []
    for item in result.successes:
        value = item.result
        details = (
            t(
                "frame_interpolation.status.row_details",
                output_frames=value.output_frames,
                selected_path=value.selected_path,
                elapsed=value.elapsed_seconds,
                fps=value.output_frames / max(value.elapsed_seconds, 0.001),
                native_multiplier=value.native_multiplier,
                cascade_stages=value.cascade_stages,
                copied_frames=value.copied_frames,
                generated_frames=value.generated_frames,
                scene_cuts=value.scene_cuts,
                decode_backend=value.decode_backend,
                encode_backend=value.encode_backend,
                memory_path=value.memory_path,
                upload_bytes=value.upload_bytes,
                download_bytes=value.download_bytes,
                pool_allocated=value.surface_pool_pressure.get("allocated", 0),
                pool_capacity=value.surface_pool_pressure.get("capacity", 0),
                pool_waits=value.surface_pool_pressure.get("waits", 0),
                bridge_version=value.bridge_version,
                bridge_abi_version=value.bridge_abi_version,
                report_path=value.report_path,
            )
        )
        ordered.append(
            (item.index, [Path(item.input_path).name, batch_state_label("Complete"), Path(value.output_path).name, details])
        )
    for item in result.failures:
        state = "Skipped" if item.error == "Cancelled before rendering." else (
            "Cancelled" if item.cancelled else "Failed"
        )
        ordered.append((item.index, [Path(item.input_path).name, batch_state_label(state), "", item.error]))
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
    status = t(
        "frame_interpolation.status.batch",
        state=batch_state_label("Cancelled" if result.cancelled else "Complete"),
        successes=len(result.successes),
        failures=len(result.failures),
        manifest=result.manifest_path,
    )
    if result.failures:
        status += "\n" + t("frame_interpolation.status.first_error", error=result.failures[0].error)
    if used_derivative:
        status += "\n" + t("frame_interpolation.status.proxy_created")
    return gr.update(value=preview, visible=not direct_disk), files, rows, status

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
    save_download: object
    zip_button: object
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
    ui_t = translator(settings.language).t
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(
                label=ui_t("common.label.input_video_plural"), file_count="multiple", file_types=["video"],
                type="filepath", allow_reordering=True, elem_id="frame-interpolation-upload-list",
                elem_classes=["media-upload-surface"],
            )
            input_preview = gr.Video(label=ui_t("common.label.input_video_preview"), interactive=False, visible="hidden")
            with gr.Row(
                visible=False, elem_id="frame-interpolation-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    ui_t("common.button.choose_videos"), ["video"], "frame-interpolation-select-input",
                )
                clear_source = build_media_clear_button("frame-interpolation-clear-input")
            with gr.Row():
                render = gr.Button(ui_t("frame_interpolation.button.render"), variant="primary")
                preview = gr.Button(ui_t("common.button.preview_clip"), visible=False)
                stop = gr.Button(ui_t("common.button.stop"), variant="stop")
                reset = gr.Button(ui_t("common.button.reset_settings"))
            with gr.Column():
                with gr.Row():
                    target_fps = gr.Dropdown(
                        FPS_CHOICES, value=settings.frame_interpolation_target_fps,
                        label=ui_t("frame_interpolation.label.output_fps"),
                    )
                    engine = gr.Radio(
                        [(option_label(choice, settings.language), choice) for choice in ENGINE_CHOICES],
                        value=settings.frame_interpolation_engine,
                        label=ui_t("frame_interpolation.label.engine"),
                    )
            input_path, output_path = build_path_controls()
            quality = gr.Radio(
                [(option_label(choice, settings.language), choice) for choice in QUALITY_CHOICES],
                value=settings.frame_interpolation_quality,
                label=ui_t("common.label.encoding_quality"),
            )
            with gr.Row():
                codec = gr.Dropdown(
                    CODEC_CHOICES, value=settings.frame_interpolation_codec,
                    label=ui_t("common.label.video_codec"),
                )
                container = gr.Dropdown(
                    CONTAINER_CHOICES,
                    value=container_for_codec(settings.frame_interpolation_codec),
                    label=ui_t("common.label.container"), interactive=False,
                )
            with gr.Row():
                rename_mode = gr.Radio(
                    [(option_label(choice, settings.language), choice) for choice in RENAME_MODES],
                    value=settings.frame_interpolation_rename_mode,
                    label=ui_t("common.label.rename"),
                )
                custom_suffix = gr.Textbox(
                    value=settings.frame_interpolation_custom_suffix,
                    label=ui_t("common.label.custom_suffix"),
                    placeholder=ui_t("frame_interpolation.placeholder.custom_suffix"),
                    interactive=settings.frame_interpolation_rename_mode == "Custom",
                )
            hdr_mode = gr.Checkbox(
                value=settings.frame_interpolation_hdr_mode and hdr_mode_supported(settings.frame_interpolation_codec),
                label=ui_t("common.label.hdr_mode"),
                interactive=hdr_mode_supported(settings.frame_interpolation_codec),
            )
        with gr.Column(scale=3):
            output_video = gr.Video(
                label=ui_t("frame_interpolation.label.output_video"), interactive=False, visible=True, height=520,
            )
            save_download, zip_button, zip_download = build_save_controls("video", "frame-interpolation")
            status = gr.Textbox(label=ui_t("common.label.status"), interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(
                headers=batch_headers(settings.language),
                datatype=["str"] * len(batch_headers(settings.language)), interactive=False,
                label=ui_t("common.label.batch_results"), wrap=True,
            )
    tab = FrameInterpolationTab(
        sources=sources, input_preview=input_preview, input_actions=input_actions,
        select_source=select_source, clear_source=clear_source, target_fps=target_fps,
        engine=engine, quality=quality, codec=codec,
        container=container, rename_mode=rename_mode, custom_suffix=custom_suffix,
        hdr_mode=hdr_mode, preview=preview, render=render, stop=stop, reset=reset,
        output_video=output_video, save_download=save_download, zip_button=zip_button,
        zip_download=zip_download, status=status, results=results)
    tab.input_path, tab.output_path = input_path, output_path
    bind_frame_interpolation_events(tab)
    return tab


def bind_frame_interpolation_events(tab: FrameInterpolationTab) -> None:
    bind_batch_ui(
        tab, render_frame_interpolation_batch, kind="video",
        preview_mode=update_frame_interpolation_preview_mode, archive_prefix="DLSSFG_VIDEO_BATCH",
        preview_actions=[(tab.preview, preview_frame_interpolation)],
    )
    tab.codec.change(
        codec_settings_update, inputs=tab.codec,
        outputs=[tab.container, tab.hdr_mode], queue=False,
    )
    tab.rename_mode.change(rename_suffix_update, inputs=tab.rename_mode, outputs=tab.custom_suffix, queue=False)
