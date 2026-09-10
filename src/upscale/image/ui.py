from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import gradio as gr

from ...core.batch_ui import (BATCH_HEADERS, bind_batch_ui, build_media_clear_button,
                             build_media_select_button, build_path_controls)
from ...core.disk_paths import create_media_archive, resolve_inputs
from ...core.naming import RENAME_MODES
from ...neural_rendering.image.decoder import decode_image
from ...neural_rendering.image.encoder import take_image_preview
from ...neural_rendering.image.models import RAW_EXTENSIONS
from ...neural_rendering.image.ui import preview_input_images
from ...settings.storage import processing_gpu_settings
from .batch import upscale_images
from .models import (IMAGE_FORMATS, SCALE_FACTORS, SETTING_FIELDS, SIZE_MODES, VSR_QUALITIES,
                     ImageUpscaleOptions, options_from_settings, output_size)


def options_from_values(values):
    options = ImageUpscaleOptions(**dict(zip(SETTING_FIELDS, values)), ai_gpu_uuid=processing_gpu_settings()[0])
    options.validate()
    return options


def render_image_batch(paths, *values, progress=None, output_dir=None, controller=None,
                       on_item_update=None, direct_disk=False):
    result = upscale_images(paths, options_from_values(values), progress, output_dir=output_dir,
                            controller=controller, on_item_update=on_item_update, generate_previews=not direct_disk)
    gallery = []
    if not direct_disk:
        for item in result.successes:
            preview = take_image_preview(item.output_path)
            if preview is not None:
                gallery.append((preview, Path(item.output_path).name))
    archive_path = None
    if not direct_disk and not result.cancelled:
        archive_path = create_media_archive(
            (item.output_path for item in result.successes),
            output_dir,
            "RTXVIDEO_IMAGE_BATCH",
            controller=controller,
        )
    return gallery, archive_path, [], str(result.manifest_path)


def describe_size(paths, input_path, *values):
    if not paths and not str(input_path or "").strip():
        return ""
    try:
        options = options_from_values(values)
        sources = resolve_inputs(input_path, paths, "image")
        lines = []
        for path in sources[:8]:
            decoded = decode_image(path)
            h, w = decoded.rgba.shape[:2]
            ow, oh = output_size(w, h, options)
            lines.append(f"**{Path(path).name}**: {w}×{h} → **{ow}×{oh}**")
        if len(sources) > 8:
            lines.append(f"{len(sources)-8} more images; dimensions are calculated for each source.")
        return "\n\n".join(lines)
    except Exception as exc:
        return str(exc)


def build_image_tab(settings):
    opts = options_from_settings(settings)
    upload_types = ["image", ".svg", ".heic", ".heif", *sorted(RAW_EXTENSIONS)]
    c = {}
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(label="Input image(s)", file_count="multiple", file_types=upload_types,
                              type="filepath",
                              allow_reordering=True, elem_id="upscale-image-upload-list",
                              elem_classes=["media-upload-surface"])
            input_gallery = gr.Gallery(
                label="Input images", columns=3, height=520, object_fit="contain",
                visible=False, interactive=False, type="pil", buttons=["fullscreen"],
                elem_id="upscale-image-input-preview",
            )
            with gr.Row(visible=False, elem_classes=["media-input-actions"]) as input_actions:
                select_source = build_media_select_button(
                    "Choose Images", upload_types, "upscale-image-select-input",
                )
                clear_source = build_media_clear_button("upscale-image-clear-input")
            input_path, output_path = build_path_controls()
            with gr.Accordion("RTX Video Super Resolution", open=True, elem_id="upscale-image-vsr-box"):
                c["vsr_quality"] = gr.Dropdown(VSR_QUALITIES, value=opts.vsr_quality, label="VSR quality")
                c["size_mode"] = gr.Radio(SIZE_MODES, value=opts.size_mode, label="Output sizing")
                c["scale_factor"] = gr.Dropdown(
                    SCALE_FACTORS, value=opts.scale_factor, label="Scale factor",
                )
                with gr.Row(
                    visible=opts.size_mode == "Custom dimensions",
                    elem_id="upscale-image-custom-dimensions",
                ) as custom_dimensions_row:
                    c["width"] = gr.Number(value=opts.width, minimum=1, maximum=16384, precision=0, label="Output width")
                    c["height"] = gr.Number(value=opts.height, minimum=1, maximum=16384, precision=0, label="Output height")
                c["aspect_lock"] = gr.Checkbox(value=opts.aspect_lock, label="Lock aspect ratio", info="Custom width determines height for each image.")
                dimensions = gr.Markdown(visible=False, elem_id="upscale-image-dimensions")
            c["output_format"] = gr.Dropdown(IMAGE_FORMATS, value=opts.output_format, label="Output format")
            c["quality"] = gr.Slider(1, 100, value=opts.quality, step=1, precision=0, label="Image quality", info="Used for JPEG, WebP, and AVIF.")
            c["preserve_metadata"] = gr.Checkbox(value=opts.preserve_metadata, label="Preserve metadata")
            with gr.Row():
                c["rename_mode"] = gr.Radio(RENAME_MODES, value=opts.rename_mode, label="Rename")
                c["custom_suffix"] = gr.Textbox(value=opts.custom_suffix, label="Custom suffix", interactive=opts.rename_mode == "Custom")
            with gr.Row():
                render = gr.Button("Upscale image(s)", variant="primary")
                stop = gr.Button("Stop", variant="stop")
                reset = gr.Button("Reset settings")
        with gr.Column(scale=3):
            output_gallery = gr.Gallery(
                label="Upscaled images", columns=2, height=520, object_fit="contain",
                interactive=False, type="pil",
                buttons=["download", "download_all", "fullscreen"],
                elem_id="upscale-image-output-preview",
            )
            zip_download = gr.DownloadButton("Save as ZIP", visible=False)
            send_to_compare = gr.Button("Send to Comparison")
            status = gr.Textbox(label="Status", interactive=False, lines=5)
            results = gr.Dataframe(headers=BATCH_HEADERS, datatype=["str"] * len(BATCH_HEADERS), interactive=False,
                                   label="Batch results", wrap=True)
    tab = SimpleNamespace(sources=sources, input_gallery=input_gallery, input_actions=input_actions,
                          select_source=select_source, clear_source=clear_source, input_path=input_path,
                          output_path=output_path, render=render, stop=stop, reset=reset, output_gallery=output_gallery,
                          zip_download=zip_download, send_to_compare=send_to_compare, status=status, results=results,
                          controls=c, settings_inputs=[c[n] for n in SETTING_FIELDS])
    tab.render_inputs = [sources, *tab.settings_inputs]
    bind_batch_ui(tab, render_image_batch, kind="image", preview_mode=preview_input_images)
    c["rename_mode"].change(lambda mode: gr.update(interactive=mode == "Custom"), inputs=c["rename_mode"], outputs=c["custom_suffix"], queue=False)

    def sizing_controls(mode, lock):
        custom = mode == "Custom dimensions"
        return (gr.update(visible=not custom), gr.update(visible=custom),
                gr.update(visible=custom), gr.update(visible=custom, interactive=not lock),
                gr.update(visible=custom))

    sizing_outputs = [c["scale_factor"], custom_dimensions_row, c["width"], c["height"], c["aspect_lock"]]
    for name in ("size_mode", "aspect_lock"):
        c[name].change(sizing_controls, inputs=[c["size_mode"], c["aspect_lock"]], outputs=sizing_outputs, queue=False)
    for component, update in zip(sizing_outputs, sizing_controls(opts.size_mode, opts.aspect_lock)):
        for key in ("visible", "interactive"):
            if key in update:
                setattr(component, key, update[key])
    def custom_dimensions(paths, path, mode, factor, width, lock, initialize=False):
        if mode != "Custom dimensions":
            return gr.skip(), gr.skip()
        try:
            decoded = decode_image(resolve_inputs(path, paths, "image")[0])
            h, w = decoded.rgba.shape[:2]
            options = (ImageUpscaleOptions(scale_factor=factor) if initialize else
                       ImageUpscaleOptions(size_mode=mode, width=width, aspect_lock=True))
            ow, oh = output_size(w, h, options)
            return (ow if initialize else gr.skip()), (oh if lock or initialize else gr.skip())
        except Exception:
            return gr.skip(), gr.skip()

    size_inputs = [sources, input_path, c["size_mode"], c["scale_factor"], c["width"], c["aspect_lock"]]
    c["size_mode"].input(lambda *args: custom_dimensions(*args, initialize=True), inputs=size_inputs,
                          outputs=[c["width"], c["height"]], queue=False)
    for component in (c["width"], c["aspect_lock"]):
        component.input(custom_dimensions, inputs=size_inputs, outputs=[c["width"], c["height"]], queue=False)
    for component in (sources, input_path, *[c[n] for n in ("size_mode", "scale_factor", "width", "height", "aspect_lock")]):
        component.change(lambda *args: gr.update(value=(text := describe_size(*args)), visible=bool(text)),
                         inputs=[sources, input_path, *tab.settings_inputs], outputs=dimensions,
                         queue=False, show_progress="hidden", trigger_mode="always_last")
    return tab
