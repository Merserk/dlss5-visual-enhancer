from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import gradio as gr
from PIL import Image

from ...core.batch_ui import (bind_batch_ui, build_media_clear_button,
                             build_media_select_button, build_path_controls, build_save_controls, batch_headers)
from ...core.i18n import option_label, t, translator
from ...core.disk_paths import resolve_inputs
from ...core.naming import RENAME_MODES
from ...neural_rendering.image.decoder import decode_image, full_size_image_preview_path
from ...neural_rendering.image.encoder import take_image_preview
from ...neural_rendering.image.models import RAW_EXTENSIONS
from ...neural_rendering.image.ui import preview_input_images
from ...settings.storage import full_size_image_previews_enabled, processing_gpu_settings
from .batch import upscale_images
from .processor import preview_upscale_image
from .models import (IMAGE_FORMATS, SCALE_FACTORS, SETTING_FIELDS, SIZE_MODES, VSR_QUALITIES,
                     ImageUpscaleOptions, options_from_settings, output_size)


def options_from_values(values):
    options = ImageUpscaleOptions(**dict(zip(SETTING_FIELDS, values)), ai_gpu_uuid=processing_gpu_settings()[0])
    options.validate()
    return options


def render_image_batch(paths, *values, progress=None, output_dir=None, controller=None,
                       on_item_update=None, direct_disk=False):
    full_size = full_size_image_previews_enabled()
    result = upscale_images(
        paths, options_from_values(values), progress, output_dir=output_dir,
        controller=controller, on_item_update=on_item_update,
        generate_previews=not direct_disk and not full_size,
    )
    gallery = []
    if not direct_disk:
        for item in result.successes:
            preview = None
            if full_size:
                try:
                    preview = full_size_image_preview_path(item.output_path)
                except Exception:
                    preview = None
            else:
                preview = take_image_preview(item.output_path)
            if preview is None:
                try:
                    with Image.open(item.output_path) as output:
                        preview = output.convert("RGBA")
                        preview.thumbnail((1200, 900), Image.Resampling.BILINEAR)
                        preview = preview.copy()
                except Exception:
                    continue
            gallery.append((preview, Path(item.output_path).name))
    files = [item.output_path for item in result.successes]
    return gallery, files, [], str(result.manifest_path)


def preview_image(paths, *values, progress=gr.Progress(track_tqdm=False)):
    selected = [paths] if isinstance(paths, str) else list(paths or [])
    if len(selected) != 1:
        raise gr.Error(t("upscale.image.error.choose_exactly_one"))
    full_size = full_size_image_previews_enabled()
    image, status = preview_upscale_image(
        selected[0], options_from_values(values),
        progress=lambda value, message: progress(value, desc=message),
        full_size_preview=full_size,
    )
    return (
        gr.update(value=[(image, t("upscale.image.status.preview_title", name=Path(selected[0]).name))], visible=True),
        status,
    )


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
            lines.append(t("upscale.image.status.more_images", count=len(sources) - 8))
        return "\n\n".join(lines)
    except Exception as exc:
        return str(exc)


def build_image_tab(settings):
    ui_t = translator(settings.language).t
    opts = options_from_settings(settings)
    upload_types = ["image", ".svg", ".heic", ".heif", *sorted(RAW_EXTENSIONS)]
    c = {}
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(label=ui_t("common.label.input_image_plural"), file_count="multiple", file_types=upload_types,
                              type="filepath",
                              allow_reordering=True, elem_id="upscale-image-upload-list",
                              elem_classes=["media-upload-surface"])
            input_gallery = gr.Gallery(
                label=ui_t("common.label.input_images"), columns=3, height=520, object_fit="contain",
                visible="hidden", interactive=False, type="pil", buttons=["fullscreen"],
                elem_id="upscale-image-input-preview",
            )
            with gr.Row(visible=False, elem_classes=["media-input-actions"]) as input_actions:
                select_source = build_media_select_button(
                    ui_t("common.button.choose_images"), upload_types, "upscale-image-select-input",
                )
                clear_source = build_media_clear_button("upscale-image-clear-input")
            with gr.Row():
                render = gr.Button(ui_t("upscale.image.button.render"), variant="primary")
                stop = gr.Button(ui_t("common.button.stop"), variant="stop")
                preview = gr.Button(ui_t("common.button.preview"), visible=False)
                reset = gr.Button(ui_t("common.button.reset_settings"))
            with gr.Column(elem_id="upscale-image-vsr-box"):
                c["vsr_quality"] = gr.Dropdown(
                    [(option_label(label, settings.language), value) for label, value in VSR_QUALITIES],
                    value=opts.vsr_quality,
                    label=ui_t("upscale.label.vsr_quality"),
                )
                c["size_mode"] = gr.Radio([(option_label(choice, settings.language), choice) for choice in SIZE_MODES], value=opts.size_mode, label=ui_t("upscale.label.output_sizing"))
                c["scale_factor"] = gr.Dropdown(
                    SCALE_FACTORS, value=opts.scale_factor, label=ui_t("upscale.label.scale_factor"),
                )
                with gr.Row(
                    visible=opts.size_mode == "Custom dimensions",
                    elem_id="upscale-image-custom-dimensions",
                ) as custom_dimensions_row:
                    c["width"] = gr.Number(value=opts.width, minimum=1, maximum=16384, precision=0, label=ui_t("upscale.label.output_width"))
                    c["height"] = gr.Number(value=opts.height, minimum=1, maximum=16384, precision=0, label=ui_t("upscale.label.output_height"))
                c["aspect_lock"] = gr.Checkbox(value=opts.aspect_lock, label=ui_t("upscale.label.lock_aspect_ratio"))
                dimensions = gr.Markdown(visible=False, elem_id="upscale-image-dimensions")
            input_path, output_path = build_path_controls()
            c["output_format"] = gr.Dropdown(IMAGE_FORMATS, value=opts.output_format, label=ui_t("common.label.output_format"))
            c["quality"] = gr.Slider(1, 100, value=opts.quality, step=1, precision=0, label=ui_t("common.label.image_quality"))
            c["preserve_metadata"] = gr.Checkbox(value=opts.preserve_metadata, label=ui_t("upscale.label.preserve_metadata"))
            with gr.Row():
                c["rename_mode"] = gr.Radio([(option_label(choice, settings.language), choice) for choice in RENAME_MODES], value=opts.rename_mode, label=ui_t("common.label.rename"))
                c["custom_suffix"] = gr.Textbox(value=opts.custom_suffix, label=ui_t("common.label.custom_suffix"), interactive=opts.rename_mode == "Custom")
        with gr.Column(scale=3):
            output_gallery = gr.Gallery(
                label=ui_t("upscale.image.label.output_gallery"), columns=2, height=520, object_fit="contain",
                interactive=False, type="pil",
                buttons=["download", "download_all", "fullscreen"],
                elem_id="upscale-image-output-preview",
            )
            save_download, zip_button, zip_download = build_save_controls("image", "upscale-image")
            status = gr.Textbox(label=ui_t("common.label.status"), interactive=False, lines=5)
            results = gr.Dataframe(headers=batch_headers(settings.language), datatype=["str"] * len(batch_headers(settings.language)), interactive=False,
                                   label=ui_t("common.label.batch_results"), wrap=True)
    tab = SimpleNamespace(sources=sources, input_gallery=input_gallery, input_actions=input_actions,
                          select_source=select_source, clear_source=clear_source, input_path=input_path,
                          output_path=output_path, render=render, stop=stop, preview=preview, reset=reset, output_gallery=output_gallery,
                          save_download=save_download, zip_button=zip_button, zip_download=zip_download, status=status, results=results,
                          controls=c, settings_inputs=[c[n] for n in SETTING_FIELDS])
    tab.render_inputs = [sources, *tab.settings_inputs]
    tab.preview_inputs = list(tab.render_inputs)
    bind_batch_ui(
        tab, render_image_batch, kind="image", preview_mode=preview_input_images,
        archive_prefix="RTXVIDEO_IMAGE_BATCH",
        preview_actions=[(tab.preview, preview_image)],
    )
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
