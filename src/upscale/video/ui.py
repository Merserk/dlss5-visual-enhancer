from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from ...core.batch_ui import (
    batch_headers, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls, build_save_controls,
)
from ...core.disk_paths import resolve_inputs
from ...core.ffmpeg import CODEC_CHOICES, ENCODING_QUALITIES, hdr_mode_supported
from ...core.i18n import option_label, t, translator
from ...core.naming import RENAME_MODES
from ...settings.storage import processing_gpu_settings
from .batch import upscale_videos
from .components import ManagedVideo
from .media import inspect_video
from .models import (HDR_PRECISION_CHOICES, SCALE_FACTORS, SETTING_FIELDS, SIZE_MODES,
                     VSR_QUALITIES, UpscaleOptions, options_from_settings, output_size)
from .preview import display_result, preview_mode, preview_upscale


def options_from_values(values):
    ai, video = processing_gpu_settings()
    return UpscaleOptions(**dict(zip(SETTING_FIELDS, values)), ai_gpu_uuid=ai, video_gpu_uuid=video)


def render_upscale_batch(paths, *values, progress=None, output_dir=None, controller=None, on_item_update=None, direct_disk=False):
    options = options_from_values(values)
    result = upscale_videos(paths, options, progress, output_dir=output_dir, controller=controller, on_item_update=on_item_update)
    files = [item.result.output_path for item in result.successes]
    output, detail = None, ""
    if not direct_disk and len(paths) == 1 and result.successes and not result.cancelled:
        output, detail = display_result(result.successes[0].result, options, controller)
    status = t(
        "upscale.video.status.batch",
        state="Cancelled" if result.cancelled else "Complete",
        completed=len(files),
        failed=len(result.failures),
        detail=detail,
        manifest=result.manifest_path,
    )
    if result.failures:
        status += "\n" + result.failures[0].error
    return gr.update(value=output, visible=not direct_disk, label=t("upscale.video.label.output_sdr_preview") if options.hdr_enabled and detail.startswith("SDR") else t("common.label.output_video")), files, [], status


def preview_frame(paths, *values, progress=gr.Progress(track_tqdm=False)):
    return preview_upscale(paths, options_from_values(values), one_frame=True, progress=lambda v,m: progress(v, desc=m))


def preview_clip(paths, *values, progress=gr.Progress(track_tqdm=False)):
    return preview_upscale(paths, options_from_values(values), progress=lambda v,m: progress(v, desc=m))


def describe_size(paths, input_path, *values):
    if not paths and not str(input_path or "").strip():
        return ""
    try:
        options = options_from_values(values)
        sources = resolve_inputs(input_path, paths, "video")
        lines = []
        for path in sources[:8]:
            m = inspect_video(Path(path), reject_hdr=True)
            w, h, note = output_size(m["width"], m["height"], options)
            lines.append(f"**{Path(path).name}**: {m['source_width']}×{m['source_height']} → **{w}×{h}**{note}")
        if len(sources) > 8:
            lines.append(t("upscale.video.status.more_files", count=len(sources) - 8))
        return "\n\n".join(lines)
    except Exception as exc:
        return str(exc)


@dataclass
class UpscaleTab:
    sources: object
    input_preview: object
    input_actions: object
    select_source: object
    clear_source: object
    controls: dict
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
    input_path: object
    output_path: object
    job_state: object = None

    @property
    def settings_inputs(self):
        return [self.controls[n] for n in SETTING_FIELDS]

    @property
    def render_inputs(self):
        return [self.sources, *self.settings_inputs]

    @property
    def preview_inputs(self):
        return self.render_inputs


def build_upscale_tab(settings):
    ui_t = translator(settings.language).t
    opts = options_from_settings(settings)
    c = {}
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(label=ui_t("common.label.input_video_plural"), file_count="multiple", file_types=["video"], type="filepath",
                              allow_reordering=True, elem_id="upscale-upload-list",
                              elem_classes=["media-upload-surface"])
            input_preview = ManagedVideo(label=ui_t("common.label.input_video_preview"), interactive=False, visible="hidden")
            with gr.Row(
                visible=False, elem_id="upscale-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    ui_t("common.button.choose_videos"), ["video"], "upscale-select-input",
                )
                clear_source = build_media_clear_button("upscale-clear-input")
            with gr.Row():
                render = gr.Button(ui_t("upscale.video.button.render"), variant="primary")
                stop = gr.Button(ui_t("common.button.stop"), variant="stop")
                preview_frame_button = gr.Button(ui_t("common.button.preview_frame"), visible=False)
                preview_button = gr.Button(ui_t("common.button.preview_clip"), visible=False)
                reset = gr.Button(ui_t("common.button.reset_settings"))
            with gr.Column():
                c["vsr_enabled"] = gr.Checkbox(value=opts.vsr_enabled, label=ui_t("upscale.video.label.enable"))
                c["vsr_quality"] = gr.Dropdown(VSR_QUALITIES, value=opts.vsr_quality, label=ui_t("upscale.label.vsr_quality"))
                c["size_mode"] = gr.Radio([(option_label(choice, settings.language), choice) for choice in SIZE_MODES], value=opts.size_mode, label=ui_t("upscale.label.output_sizing"))
                c["scale_factor"] = gr.Dropdown(
                    SCALE_FACTORS, value=opts.scale_factor, label=ui_t("upscale.label.scale_factor"),
                )
                with gr.Row(
                    visible=opts.size_mode == "Custom dimensions",
                    elem_id="upscale-video-custom-dimensions",
                ) as custom_dimensions_row:
                    c["width"] = gr.Number(value=opts.width, minimum=2, maximum=16384, precision=0, label=ui_t("upscale.label.output_width"))
                    c["height"] = gr.Number(value=opts.height, minimum=2, maximum=16384, precision=0, label=ui_t("upscale.label.output_height"))
                c["aspect_lock"] = gr.Checkbox(value=opts.aspect_lock, label=ui_t("upscale.label.lock_aspect_ratio"))
                dimensions = gr.Markdown(visible=False, elem_id="upscale-video-dimensions")
            input_path, output_path = build_path_controls()
            with gr.Accordion(ui_t("upscale.video.section.hdr"), open=True):
                c["hdr_enabled"] = gr.Checkbox(value=opts.hdr_enabled, label=ui_t("upscale.video.label.convert_hdr"), interactive=hdr_mode_supported(opts.codec))
                with gr.Column(visible=opts.hdr_enabled) as hdr_controls:
                    with gr.Row():
                        c["hdr_contrast"] = gr.Slider(0, 200, value=opts.hdr_contrast, step=1, precision=0, label=ui_t("upscale.video.label.hdr_contrast"))
                        c["hdr_saturation"] = gr.Slider(0, 200, value=opts.hdr_saturation, step=1, precision=0, label=ui_t("upscale.video.label.hdr_saturation"))
                    with gr.Row():
                        c["hdr_middle_gray"] = gr.Slider(10, 100, value=opts.hdr_middle_gray, step=1, precision=0, label=ui_t("upscale.video.label.hdr_middle_gray"))
                        c["hdr_peak_luminance"] = gr.Slider(400, 2000, value=opts.hdr_peak_luminance, step=1, precision=0, label=ui_t("upscale.video.label.hdr_peak_luminance"))
                    c["hdr_precision"] = gr.Radio([(option_label(label, settings.language), value) for label, value in HDR_PRECISION_CHOICES], value=opts.hdr_precision, label=ui_t("upscale.video.label.hdr_precision"))
            c["quality"] = gr.Radio([(option_label(choice, settings.language), choice) for choice in ENCODING_QUALITIES], value=opts.quality, label=ui_t("common.label.encoding_quality"))
            with gr.Row():
                c["codec"] = gr.Dropdown(CODEC_CHOICES, value=opts.codec, label=ui_t("common.label.video_codec"))
                c["container"] = gr.Dropdown(("MP4", "MKV", "MOV"), value=opts.container, label=ui_t("common.label.container"))
            with gr.Row():
                c["rename_mode"] = gr.Radio([(option_label(choice, settings.language), choice) for choice in RENAME_MODES], value=opts.rename_mode, label=ui_t("common.label.rename"))
                c["custom_suffix"] = gr.Textbox(value=opts.custom_suffix, label=ui_t("common.label.custom_suffix"), interactive=opts.rename_mode == "Custom")
        with gr.Column(scale=3):
            output_video = ManagedVideo(
                label=ui_t("common.label.output_video"), interactive=False, visible=True, height=520,
            )
            save_download, zip_button, zip_download = build_save_controls("video", "upscale-video")
            status = gr.Textbox(label=ui_t("common.label.status"), interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(headers=batch_headers(settings.language), datatype=["str"]*len(batch_headers(settings.language)), interactive=False, label=ui_t("common.label.batch_results"), wrap=True)
    tab = UpscaleTab(sources, input_preview, input_actions, select_source, clear_source, c, preview_frame_button, preview_button, render, stop, reset,
                     output_video, save_download, zip_button, zip_download, status, results, input_path, output_path)
    bind_batch_ui(
        tab, render_upscale_batch, kind="video", preview_mode=preview_mode,
        archive_prefix="RTXVIDEO_VIDEO_BATCH",
        preview_actions=[(tab.preview_frame, preview_frame), (tab.preview, preview_clip)],
    )
    c["hdr_enabled"].change(lambda enabled: gr.update(visible=enabled), inputs=c["hdr_enabled"], outputs=hdr_controls, queue=False)
    c["codec"].change(lambda codec: gr.update(interactive=True) if hdr_mode_supported(codec) else gr.update(value=False, interactive=False),
                        inputs=c["codec"], outputs=c["hdr_enabled"], queue=False)
    c["rename_mode"].change(lambda mode: gr.update(interactive=mode == "Custom"), inputs=c["rename_mode"], outputs=c["custom_suffix"], queue=False)
    def sizing_controls(enabled, mode, lock):
        custom = mode == "Custom dimensions"
        return [gr.update(interactive=enabled), gr.update(interactive=enabled),
                gr.update(visible=not custom, interactive=enabled), gr.update(visible=custom),
                gr.update(visible=custom, interactive=enabled),
                gr.update(visible=custom, interactive=enabled and not lock),
                gr.update(visible=custom, interactive=enabled)]
    sizing_outputs = [c["vsr_quality"], c["size_mode"], c["scale_factor"], custom_dimensions_row,
                      c["width"], c["height"], c["aspect_lock"]]
    for name in ("vsr_enabled", "size_mode", "aspect_lock"):
        c[name].change(sizing_controls, inputs=[c["vsr_enabled"], c["size_mode"], c["aspect_lock"]], outputs=sizing_outputs, queue=False)
    def initialize_custom(paths, path, factor, mode):
        if mode != "Custom dimensions":
            return gr.skip(), gr.skip()
        try:
            src = resolve_inputs(path, paths, "video")[0]
            m = inspect_video(Path(src), reject_hdr=True)
            w,h,_ = output_size(m["width"], m["height"], UpscaleOptions(scale_factor=factor))
            return w,h
        except Exception:
            return gr.skip(), gr.skip()
    c["size_mode"].input(initialize_custom, inputs=[sources, input_path, c["scale_factor"], c["size_mode"]], outputs=[c["width"], c["height"]], queue=False)
    def locked_height(paths, path, width, lock):
        if not lock:
            return gr.skip()
        try:
            src = resolve_inputs(path, paths, "video")[0]
            m = inspect_video(Path(src), reject_hdr=True)
            _, h, _ = output_size(m["width"], m["height"], UpscaleOptions(size_mode="Custom dimensions", width=width))
            return h
        except Exception:
            return gr.skip()
    for name in ("width", "aspect_lock"):
        c[name].input(locked_height, inputs=[sources, input_path, c["width"], c["aspect_lock"]], outputs=c["height"], queue=False)
    for component in (sources, input_path, *tab.settings_inputs):
        component.change(
            lambda *args: gr.update(value=(text := describe_size(*args)), visible=bool(text)),
            inputs=[sources, input_path, *tab.settings_inputs], outputs=dimensions,
            queue=False, show_progress="hidden", trigger_mode="always_last",
        )
    # Initialize state directly; events handle subsequent preset/reset changes.
    initial = sizing_controls(opts.vsr_enabled, opts.size_mode, opts.aspect_lock)
    for component, update in zip(sizing_outputs, initial):
        for key in ("visible", "interactive"):
            if key in update:
                setattr(component, key, update[key])
    return tab
