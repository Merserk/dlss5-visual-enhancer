from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
from ...core.batch_ui import (
    batch_headers, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls, build_save_controls,
)
from ...core.i18n import batch_state_label, option_label, t, translator
from ...core.ffmpeg import container_for_codec, hdr_mode_supported
from ...core.ffmpeg.preview import normalize_preview_encoding, resolve_final_preview
from ...core.naming import RENAME_MODES
from ...core.runtime import NR_STYLES, UPSCALING_MODES
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
from ..composition_ui import CompositionWidgets, build_composition_sliders, build_composition_widgets


def hdr_mode_update(codec: str):
    allowed = hdr_mode_supported(codec)
    if not allowed:
        return gr.update(value=False, interactive=False)
    return gr.update(interactive=True)


def rename_suffix_update(mode: str):
    return gr.update(interactive=mode == "Custom")


UPSCALING_CHOICES = tuple((mode["label"], factor) for factor, mode in UPSCALING_MODES.items())


def build_neural_controls(settings: UISettings):
    ui_t = translator(settings.language).t
    nr_style = gr.Radio(
        choices=[(option_label(choice, settings.language), choice) for choice in NR_STYLES],
        value=settings.nr_style,
        label=ui_t("neural.label.nr_style"),
    )
    upscaling_factor = gr.Dropdown(
        choices=[(option_label(label, settings.language), value) for label, value in UPSCALING_CHOICES],
        value=settings.upscaling_factor,
        label=ui_t("neural.label.scale"),
    )
    # Each control is created directly in the parent Column (no gr.Row), so
    # every slider spans the full width in one vertical stack. Creation order
    # sets the visual order; the returned list keeps the canonical positional
    # order consumed by render/persist/settings-mirror code.
    nr_intensity = gr.Slider(
        0.0, 2.0, value=settings.nr_intensity, step=0.05, precision=2,
        label=ui_t("neural.label.nr_intensity"), buttons=["reset"],
    )
    nr_passes = gr.Slider(
        1, 4, value=settings.nr_passes, step=1, precision=0,
        label=ui_t("neural.label.nr_passes"), buttons=["reset"],
    )
    local_tone_strength = gr.Slider(
        0.0, 2.0, value=settings.local_tone_strength, step=0.05, precision=2,
        label=ui_t("neural.label.local_tone_strength"), buttons=["reset"]
    )
    local_structure_strength = gr.Slider(
        0.0, 2.0, value=settings.local_structure_strength, step=0.05, precision=2,
        label=ui_t("neural.label.local_structure_strength"), buttons=["reset"],
    )
    skin_structure_strength = gr.Slider(
        -1.0, 2.0, value=settings.skin_structure_strength, step=0.05, precision=2,
        label=ui_t("neural.label.skin_structure_strength"),
        buttons=["reset"],
    )
    composition = build_composition_sliders(settings, ui_t)
    shimmer_suppression = gr.Slider(
        0.0, 1.0, value=settings.shimmer_suppression, step=0.05, precision=2,
        label=ui_t("neural.label.shimmer_suppression"), buttons=["reset"],
    )
    automatic_mask = gr.Radio(
        choices=[(option_label(choice, settings.language), choice) for choice in AUTOMATIC_MASK_CHOICES],
        value=automatic_mask_choice(settings.automatic_mask),
        label=ui_t("neural.label.automatic_mask"),
    )
    controls = [
        nr_style, nr_intensity, nr_passes, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask
    ] + composition
    return [*controls, shimmer_suppression]


def render_video(
    input_path: str,
    nr_style: str,
    nr_intensity: float,
    nr_passes: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    nr_color_strength: float,
    tone_preservation: float,
    face_skin_protection: float,
    grain_preservation: float,
    mask_feather: float,
    shimmer_suppression: float,
    nr_mask: object | None,
    nr_gpu_mode: bool,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool = False,
    progress=gr.Progress(track_tqdm=False),
):
    return _process_video(
        input_path, nr_style, nr_intensity, nr_passes, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask,
        nr_color_strength, tone_preservation, face_skin_protection, grain_preservation,
        mask_feather, shimmer_suppression, nr_mask, nr_gpu_mode,
        codec, container, quality, hdr_mode,
        progress, None, None
    )

def render_video_batch(
    input_paths: list[str] | str | None,
    nr_style: str,
    nr_intensity: float,
    nr_passes: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    nr_color_strength: float,
    tone_preservation: float,
    face_skin_protection: float,
    grain_preservation: float,
    mask_feather: float,
    shimmer_suppression: float,
    nr_mask: object | None,
    nr_gpu_mode: bool,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    rename_mode: str,
    custom_suffix: str,
    progress=gr.Progress(track_tqdm=False),
    *, output_dir=None, controller=None, on_item_update=None, direct_disk=False,
) -> tuple[object, list[str], list[list[str]], str]:
    paths = normalize_video_paths(input_paths)
    if not paths:
        raise gr.Error(t("neural.video.error.choose_video"))
    effective_hdr = coerce_hdr_mode(codec, hdr_mode)
    options = ConversionOptions(
        ai_gpu_uuid=processing_gpu_settings()[0],
        video_gpu_uuid=processing_gpu_settings()[1],
        nr_style=nr_style,
        nr_intensity=nr_intensity,
        nr_passes=int(nr_passes),
        local_tone_strength=local_tone_strength,
        local_structure_strength=local_structure_strength,
        skin_structure_strength=skin_structure_strength,
        automatic_mask=parse_automatic_mask(automatic_mask),
        nr_color_strength=float(nr_color_strength),
        tone_preservation=float(tone_preservation),
        face_skin_protection=float(face_skin_protection),
        grain_preservation=float(grain_preservation),
        shimmer_suppression=float(shimmer_suppression),
        mask_feather=int(mask_feather),
        nr_mask=nr_mask,
        nr_gpu_mode=nr_gpu_mode,
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
            paths, options, progress=report, output_dir=output_dir, controller=controller,
            on_item_update=on_item_update, create_archive=False,
        )
    except Exception as exc:
        traceback.print_exc()
        if on_item_update is not None:
            raise
        return gr.update(value=None, visible=not direct_disk), None, [], t("neural.video.status.failed", error=exc)

    ordered_rows: list[tuple[int, list[str]]] = []
    for item in result.successes:
        conversion = item.result
        details = (
            t(
                "neural.video.status.row_details",
                frames=conversion.frames,
                elapsed=conversion.elapsed_seconds,
                render_width=conversion.render_width,
                render_height=conversion.render_height,
                resize_method=conversion.resize_method,
                memory_path=conversion.memory_path,
                report_path=conversion.report_path,
            )
        )
        ordered_rows.append(
            (
                item.index,
                [
                    Path(item.input_path).name,
                    batch_state_label("Complete"),
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
            (item.index, [Path(item.input_path).name, batch_state_label(state), "", item.error])
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
            candidate, preview_mode, controller=controller, bounded_proxy=True
        )
    failed_count = sum(not item.cancelled for item in result.failures)
    cancelled_count = sum(
        item.cancelled and item.error != "Cancelled before rendering."
        for item in result.failures
    )
    skipped_count = sum(
        item.error == "Cancelled before rendering." for item in result.failures
    )
    status = t(
        "neural.video.status.batch",
        state=batch_state_label("Cancelled" if result.cancelled else "Complete"),
        successes=len(result.successes),
        failed=failed_count,
        cancelled=cancelled_count,
        skipped=skipped_count,
        manifest=result.manifest_path,
    )
    if result.failures:
        status += "\n" + t("neural.video.status.first_error", error=result.failures[0].error)
    if used_derivative:
        status += "\n" + t("neural.video.status.output_proxy")
    files = [item.result.output_path for item in result.successes]
    return gr.update(value=output_preview, visible=not direct_disk), files, rows, status

@dataclass(slots=True)
class VideoTab:
    sources: object
    input_preview: object
    input_actions: object
    select_source: object
    clear_source: object
    neural: list[object]
    composition: CompositionWidgets
    mask_state: object
    gpu_mode: object
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
    save_download: object
    zip_button: object
    zip_download: object
    status: object
    results: object
    input_path: object = None
    output_path: object = None
    job_state: object = None
    refresh_realtime_preview_after: object = None

    @property
    def render_inputs(self) -> list[object]:
        return [
            self.sources, *self.neural, self.mask_state, self.gpu_mode, self.codec, self.container,
            self.quality, self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]

    @property
    def preview_inputs(self) -> list[object]:
        return [
            self.sources, *self.neural, self.mask_state, self.gpu_mode, self.codec, self.container,
            self.quality, self.hdr_mode,
        ]

    @property
    def settings_inputs(self) -> list[object]:
        return [
            *self.neural, self.codec, self.container, self.quality,
            self.hdr_mode, self.rename_mode, self.custom_suffix,
        ]


def build_video_tab(settings: UISettings, gpu_mode_state: object, mask_state: object) -> VideoTab:
    ui_t = translator(settings.language).t
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(
                label=ui_t("common.label.input_video_plural"), file_count="multiple", file_types=["video"],
                type="filepath", allow_reordering=True, elem_id="video-upload-list",
                elem_classes=["media-upload-surface"],
            )
            input_preview = gr.Video(label=ui_t("common.label.input_video_preview"), interactive=False, visible="hidden")
            with gr.Row(
                visible=False, elem_id="video-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    ui_t("common.button.choose_videos"), ["video"], "video-select-input",
                )
                clear_source = build_media_clear_button("video-clear-input")
            with gr.Row():
                render = gr.Button(ui_t("neural.video.button.render"), variant="primary")
                stop = gr.Button(ui_t("common.button.stop"), variant="stop")
                preview_frame = gr.Button(ui_t("common.button.preview_frame"), visible=False)
                preview = gr.Button(ui_t("common.button.preview_clip"), visible=False)
                reset = gr.Button(ui_t("common.button.reset_settings"))
            with gr.Column(elem_classes=["neural-controls-unified"]):
                neural = build_neural_controls(settings)
                composition = build_composition_widgets(settings.language)
            input_path, output_path = build_path_controls()
            quality = gr.Radio(
                [(option_label(choice, settings.language), choice) for choice in QUALITY_CHOICES],
                value=settings.quality,
                label=ui_t("common.label.encoding_quality"),
            )
            with gr.Row():
                codec = gr.Dropdown(
                    CODEC_CHOICES, value=settings.codec, label=ui_t("common.label.video_codec"),
                )
                container = gr.Dropdown(
                    CONTAINER_CHOICES, value=container_for_codec(settings.codec),
                    label=ui_t("common.label.container"), interactive=False,
                )
            with gr.Row():
                rename_mode = gr.Radio(
                    [(option_label(choice, settings.language), choice) for choice in RENAME_MODES],
                    value=settings.video_rename_mode,
                    label=ui_t("common.label.rename"),
                )
                custom_suffix = gr.Textbox(
                    value=settings.video_custom_suffix,
                    label=ui_t("common.label.custom_suffix"),
                    placeholder=ui_t("neural.image.placeholder.custom_suffix"),
                    interactive=settings.video_rename_mode == "Custom",
                )
            hdr_mode = gr.Checkbox(
                value=settings.hdr_mode and hdr_mode_supported(settings.codec),
                label=ui_t("common.label.hdr_mode"),
                interactive=hdr_mode_supported(settings.codec),
            )
        with gr.Column(scale=3):
            output_video = gr.Video(
                label=ui_t("neural.video.label.output_video"), interactive=False, visible=True, height=520,
            )
            save_download, zip_button, zip_download = build_save_controls("video", "nr-video")
            status = gr.Textbox(label=ui_t("common.label.status"), interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(
                headers=batch_headers(settings.language),
                datatype=["str"] * len(batch_headers(settings.language)), interactive=False,
                label=ui_t("common.label.batch_results"), wrap=True,
            )
    tab = VideoTab(
        sources, input_preview, input_actions, select_source, clear_source, neural, composition, mask_state, gpu_mode_state, quality, codec, container, rename_mode,
        custom_suffix, hdr_mode, preview_frame, preview, render, stop, reset, output_video,
        save_download, zip_button, zip_download, status, results
    )
    tab.input_path, tab.output_path = input_path, output_path
    bind_video_events(tab)
    return tab


def bind_video_events(tab: VideoTab) -> None:
    tab.refresh_realtime_preview_after = bind_batch_ui(
        tab, render_video_batch, kind="video", preview_mode=update_video_preview_mode,
        archive_prefix="DLSS5_VIDEO_BATCH",
        preview_actions=[(tab.preview_frame, preview_one_frame), (tab.preview, preview_video)],
        realtime_preview=preview_one_frame,
        realtime_components=tab.neural,
    )
    tab.rename_mode.change(rename_suffix_update, inputs=tab.rename_mode, outputs=tab.custom_suffix, queue=False)
    tab.codec.change(
        lambda selected: (
            gr.update(value=container_for_codec(selected)),
            hdr_mode_update(selected),
        ),
        inputs=tab.codec, outputs=[tab.container, tab.hdr_mode], queue=False,
    )
