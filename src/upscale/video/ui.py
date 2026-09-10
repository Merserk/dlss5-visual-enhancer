from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import gradio as gr

from ...core.batch_ui import (
    BATCH_HEADERS, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls,
)
from ...core.disk_paths import create_media_archive, resolve_inputs
from ...core.ffmpeg import CODEC_CHOICES, ENCODING_QUALITIES, hdr_mode_supported
from ...core.naming import RENAME_MODES
from ...core.preview_duration import build_duration_control
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
    status = f"{'Cancelled' if result.cancelled else 'Complete'}: {len(files)} completed; {len(result.failures)} failed/skipped.\n{detail}\nReport: {result.manifest_path}"
    if result.failures:
        status += "\n" + result.failures[0].error
    archive_path = None
    if not direct_disk and not result.cancelled:
        archive_path = create_media_archive(
            files, output_dir, "RTXVIDEO_VIDEO_BATCH", controller=controller,
        )
    return gr.update(value=output, visible=not direct_disk, label="SDR tone-mapped preview (download original for HDR)" if options.hdr_enabled and detail.startswith("SDR") else "Output video"), archive_path, [], status


def preview_with_duration(paths, *values, progress=gr.Progress(track_tqdm=False)):
    *settings_values, duration = values
    return preview_upscale(
        paths, options_from_values(settings_values), duration=duration,
        progress=lambda v, m: progress(v, desc=m),
    )


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
            lines.append(f"{len(sources)-8} more files; dimensions are calculated independently for each source.")
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
    preview_duration: object
    preview: object
    render: object
    stop: object
    reset: object
    output_video: object
    send_to_compare: object
    zip_download: object
    status: object
    results: object
    input_path: object
    output_path: object
    job_state: object = None
    last_preview_path: object = None

    @property
    def settings_inputs(self):
        return [self.controls[n] for n in SETTING_FIELDS]

    @property
    def render_inputs(self):
        return [self.sources, *self.settings_inputs]

    @property
    def preview_inputs(self):
        return [*self.render_inputs, self.preview_duration]


def build_upscale_tab(settings):
    opts = options_from_settings(settings)
    c = {}
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(label="Input video(s)", file_count="multiple", file_types=["video"], type="filepath",
                              allow_reordering=True, elem_id="upscale-upload-list",
                              elem_classes=["media-upload-surface"])
            input_preview = ManagedVideo(label="Input video preview", interactive=False, visible=False)
            with gr.Row(
                visible=False, elem_id="upscale-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    "Choose Videos", ["video"], "upscale-select-input",
                )
                clear_source = build_media_clear_button("upscale-clear-input")
            input_path, output_path = build_path_controls()
            with gr.Accordion("RTX Video Super Resolution", open=True):
                c["vsr_enabled"] = gr.Checkbox(value=opts.vsr_enabled, label="RTX Video Super Resolution")
                c["vsr_quality"] = gr.Dropdown(VSR_QUALITIES, value=opts.vsr_quality, label="VSR quality", info="Ultra gives the highest quality.")
                c["size_mode"] = gr.Radio(SIZE_MODES, value=opts.size_mode, label="Output sizing")
                c["scale_factor"] = gr.Dropdown(
                    SCALE_FACTORS, value=opts.scale_factor, label="Scale factor",
                    info="1× enhances at original resolution. Other factors increase width and height.",
                )
                with gr.Row(
                    visible=opts.size_mode == "Custom dimensions",
                    elem_id="upscale-video-custom-dimensions",
                ) as custom_dimensions_row:
                    c["width"] = gr.Number(value=opts.width, minimum=2, maximum=16384, precision=0, label="Output width")
                    c["height"] = gr.Number(value=opts.height, minimum=2, maximum=16384, precision=0, label="Output height")
                c["aspect_lock"] = gr.Checkbox(value=opts.aspect_lock, label="Lock aspect ratio", info="Custom width determines height for each source.")
                dimensions = gr.Markdown(visible=False, elem_id="upscale-video-dimensions")
            with gr.Accordion("RTX Video HDR", open=True):
                c["hdr_enabled"] = gr.Checkbox(value=opts.hdr_enabled, label="Convert SDR to HDR", interactive=hdr_mode_supported(opts.codec),
                                              info="Creates HDR highlights and colors. Requires H.265, AV1, or ProRes output.")
                with gr.Column(visible=opts.hdr_enabled) as hdr_controls:
                    with gr.Row():
                        c["hdr_contrast"] = gr.Slider(0, 200, value=opts.hdr_contrast, step=1, precision=0, label="HDR contrast")
                        c["hdr_saturation"] = gr.Slider(0, 200, value=opts.hdr_saturation, step=1, precision=0, label="HDR saturation")
                    with gr.Row():
                        c["hdr_middle_gray"] = gr.Slider(10, 100, value=opts.hdr_middle_gray, step=1, precision=0, label="HDR middle gray")
                        c["hdr_peak_luminance"] = gr.Slider(400, 2000, value=opts.hdr_peak_luminance, step=1, precision=0, label="HDR peak luminance (nits)")
                    c["hdr_precision"] = gr.Radio(HDR_PRECISION_CHOICES, value=opts.hdr_precision, label="HDR processing precision",
                                                  info="FP16 uses more memory. HDR video exports use 10-bit color in both modes.")
            c["quality"] = gr.Radio(ENCODING_QUALITIES, value=opts.quality, label="Encoding quality")
            with gr.Row():
                c["codec"] = gr.Dropdown(CODEC_CHOICES, value=opts.codec, label="Video codec", info="Plain = CPU; NVIDIA NVENC = GPU. ProRes requires MOV or MKV.")
                c["container"] = gr.Dropdown(("MP4", "MKV", "MOV"), value=opts.container, label="Container")
            with gr.Row():
                c["rename_mode"] = gr.Radio(RENAME_MODES, value=opts.rename_mode, label="Rename")
                c["custom_suffix"] = gr.Textbox(value=opts.custom_suffix, label="Custom suffix", interactive=opts.rename_mode == "Custom")
            with gr.Row():
                preview_duration = build_duration_control(allow_single_frame=True)
                preview_button = gr.Button("Preview", visible=False)
                render = gr.Button("Upscale video(s)", variant="primary")
                stop = gr.Button("Stop", variant="stop")
                reset = gr.Button("Reset settings")
        with gr.Column(scale=3):
            output_video = ManagedVideo(
                label="Output video", interactive=False, visible=True, height=520,
            )
            zip_download = gr.DownloadButton("Save as ZIP", visible=False)
            send_to_compare = gr.Button("Send to Comparison")
            status = gr.Textbox(label="Status", interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(headers=BATCH_HEADERS, datatype=["str"]*len(BATCH_HEADERS), interactive=False, label="Batch results", wrap=True)
    tab = UpscaleTab(sources, input_preview, input_actions, select_source, clear_source, c, preview_duration, preview_button, render, stop, reset,
                     output_video, send_to_compare, zip_download, status, results, input_path, output_path)
    tab.last_preview_path = gr.State(None)
    bind_batch_ui(tab, render_upscale_batch, kind="video", preview_mode=preview_mode,
                  preview_actions=[(tab.preview, preview_with_duration)],
                  preview_controls=[tab.preview_duration],
                  preview_result_state=tab.last_preview_path)
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
