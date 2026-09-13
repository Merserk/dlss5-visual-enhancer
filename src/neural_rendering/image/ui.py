from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import gradio as gr
from ...core.batch_ui import (
    batch_headers, bind_batch_ui, build_media_clear_button, build_media_select_button,
    build_path_controls, build_save_controls,
)
from ...core.i18n import option_label, t, translator
from PIL import Image

from ...core.naming import RENAME_MODES
from ...core.runtime import NR_STYLES, UPSCALING_MODES
from ...settings.models import AUTOMATIC_MASK_CHOICES, UISettings, automatic_mask_choice, parse_automatic_mask
from ...settings.storage import full_size_image_previews_enabled, processing_gpu_settings
from .decoder import decode_image_preview, full_size_image_preview_path
from .encoder import take_image_preview
from .batch import convert_images
from .preview import render_image_preview
from .models import IMAGE_FORMATS, RAW_EXTENSIONS, ImageConversionOptions
from ..composition_ui import CompositionWidgets, build_composition_sliders, build_composition_widgets


def rename_suffix_update(mode: str):
    return gr.update(interactive=mode == "Custom")


UPSCALING_CHOICES = tuple((mode["label"], factor) for factor, mode in UPSCALING_MODES.items())


def build_neural_controls(settings: UISettings):
    ui_t = translator(settings.language).t
    nr_style = gr.Radio(
        list(NR_STYLES), value=settings.nr_style, label=ui_t("neural.label.nr_style"),
    )
    upscaling_factor = gr.Dropdown(
        choices=list(UPSCALING_CHOICES), value=settings.upscaling_factor,
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
        label=ui_t("neural.label.local_structure_strength"), buttons=["reset"]
    )
    skin_structure_strength = gr.Slider(
        -1.0, 2.0, value=settings.skin_structure_strength, step=0.05, precision=2,
        label=ui_t("neural.label.skin_structure_strength"),
        buttons=["reset"],
    )
    composition = build_composition_sliders(settings, ui_t)
    automatic_mask = gr.Radio(
        choices=[(option_label(choice, settings.language), choice) for choice in AUTOMATIC_MASK_CHOICES],
        value=automatic_mask_choice(settings.automatic_mask),
        label=ui_t("neural.label.automatic_mask"),
    )
    return [
        nr_style, nr_intensity, nr_passes, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask
    ] + composition


def preview_input_images(paths: list[str] | str | None):
    if not paths:
        return []
    if isinstance(paths, str):
        paths = [paths]
    full_size = full_size_image_previews_enabled()
    previews = []
    for raw_path in paths:
        try:
            image = (
                full_size_image_preview_path(raw_path)
                if full_size
                else decode_image_preview(raw_path, (1200, 900))
            )
            previews.append((image, Path(raw_path).name))
        except Exception:
            continue
    return previews


def _image_options(
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
    nr_mask: object | None,
    nr_gpu_mode: bool,
    image_format: str,
    image_quality: float,
    rename_mode: str,
    custom_suffix: str,
) -> ImageConversionOptions:
    return ImageConversionOptions(
        ai_gpu_uuid=processing_gpu_settings()[0],
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
        mask_feather=int(mask_feather),
        nr_mask=nr_mask,
        nr_gpu_mode=nr_gpu_mode,
        upscaling_factor=upscaling_factor,
        output_format=image_format,
        quality=int(image_quality),
        rename_mode=rename_mode,
        custom_suffix=custom_suffix,
    )


def render_image_batch(
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
    nr_mask: object | None,
    nr_gpu_mode: bool,
    image_format: str,
    image_quality: float,
    rename_mode: str,
    custom_suffix: str,
    progress=gr.Progress(track_tqdm=False),
    *, output_dir=None, controller=None, on_item_update=None, direct_disk=False,
):
    if not input_paths:
        raise gr.Error(t("neural.image.error.choose_image"))
    if isinstance(input_paths, str):
        input_paths = [input_paths]
    options = _image_options(
        nr_style, nr_intensity, nr_passes, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask,
        nr_color_strength, tone_preservation, face_skin_protection, grain_preservation,
        mask_feather, nr_mask, nr_gpu_mode,
        image_format, image_quality, rename_mode, custom_suffix,
    )
    full_size = full_size_image_previews_enabled()

    def report(value: float, message: str) -> None:
        progress(value, desc=message)

    try:
        result = convert_images(input_paths, options, progress=report, output_dir=output_dir,
                                controller=controller, on_item_update=on_item_update,
                                generate_previews=not direct_disk and not full_size, create_zip=False)
    except Exception as exc:
        traceback.print_exc()
        if on_item_update is not None:
            raise
        return [], None, [], f"{t('neural.video.status.failed', error=exc)}"

    gallery = []
    for item in ([] if direct_disk else result.successes):
        preview = None
        if full_size:
            try:
                preview = full_size_image_preview_path(item.output_path)
            except Exception:
                # Preview display must never invalidate a successful render.
                preview = None
        else:
            preview = take_image_preview(item.output_path)
        if preview is None:
            with Image.open(item.output_path) as output:
                preview = output.convert("RGBA")
                preview.thumbnail((1200, 900), Image.Resampling.BILINEAR)
                preview = preview.copy()
        gallery.append((preview, Path(item.output_path).name))
    rows = [
        [Path(item.input_path).name, "Complete", Path(item.output_path).name, "; ".join(item.warnings)]
        for item in result.successes
    ]
    rows.extend(
        [Path(item.input_path).name, "Failed", "", item.error] for item in result.failures
    )
    state = "Cancelled" if result.cancelled else "Complete"
    status = (
        f"{state}: {len(result.successes)} image(s) rendered, {len(result.failures)} failed. "
        "Every successful output returned feature-18 success and has a diagnostic report."
    )
    if result.failures:
        status += f"\nFirst error: {result.failures[0].error}"
    return gallery, [item.output_path for item in result.successes], rows, status


def preview_rendered_image(
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
    nr_mask: object | None,
    nr_gpu_mode: bool,
    image_format: str,
    image_quality: float,
    rename_mode: str,
    custom_suffix: str,
    progress=gr.Progress(track_tqdm=False),
    *, output_dir=None, controller=None, ephemeral_preview=False,
):
    del output_dir, ephemeral_preview
    paths = [input_paths] if isinstance(input_paths, str) else list(input_paths or [])
    if len(paths) != 1:
        raise gr.Error(t("neural.image.error.choose_exactly_one"))
    options = _image_options(
        nr_style, nr_intensity, nr_passes, local_tone_strength, local_structure_strength,
        skin_structure_strength, upscaling_factor, automatic_mask,
        nr_color_strength, tone_preservation, face_skin_protection, grain_preservation,
        mask_feather, nr_mask, nr_gpu_mode,
        image_format, image_quality, rename_mode, custom_suffix,
    )
    full_size = full_size_image_previews_enabled()
    image, status = render_image_preview(
        paths[0], options, progress=lambda value, message: progress(value, desc=message),
        controller=controller, full_size_preview=full_size,
    )
    return (
        gr.update(value=[(image, t("neural.image.status.preview_title", name=Path(paths[0]).name))], visible=True),
        status,
    )


@dataclass(slots=True)
class ImageTab:
    sources: object
    input_gallery: object
    input_actions: object
    select_source: object
    clear_source: object
    neural: list[object]
    composition: CompositionWidgets
    mask_state: object
    gpu_mode: object
    output_format: object
    quality: object
    rename_mode: object
    custom_suffix: object
    render: object
    stop: object
    preview: object
    reset: object
    output_gallery: object
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
        return [self.sources, *self.neural, self.mask_state, self.gpu_mode, self.output_format, self.quality, self.rename_mode, self.custom_suffix]

    @property
    def preview_inputs(self) -> list[object]:
        return self.render_inputs

    @property
    def settings_inputs(self) -> list[object]:
        return [*self.neural, self.output_format, self.quality, self.rename_mode, self.custom_suffix]


def build_image_tab(settings: UISettings, gpu_mode_state: object, mask_state: object) -> ImageTab:
    ui_t = translator(settings.language).t
    upload_types = ["image", ".svg", ".heic", ".heif", *sorted(RAW_EXTENSIONS)]
    with gr.Row():
        with gr.Column(scale=3):
            sources = gr.File(
                label=ui_t("common.label.input_image_plural"), file_count="multiple", file_types=upload_types,
                type="filepath", allow_reordering=True, elem_id="image-upload-list",
                elem_classes=["media-upload-surface"],
            )
            input_gallery = gr.Gallery(
                label=ui_t("common.label.input_preview"), columns=3, height=520, object_fit="contain",
                interactive=False, visible="hidden", buttons=["fullscreen"], elem_id="image-input-preview",
            )
            with gr.Row(
                visible=False, elem_id="image-input-actions",
                elem_classes=["media-input-actions"],
            ) as input_actions:
                select_source = build_media_select_button(
                    ui_t("common.button.choose_images"), upload_types, "image-select-input",
                )
                clear_source = build_media_clear_button("image-clear-input")
            with gr.Row():
                render = gr.Button(ui_t("neural.image.button.render"), variant="primary")
                stop = gr.Button(ui_t("common.button.stop"), variant="stop")
                preview = gr.Button(ui_t("common.button.preview"), visible=False)
                reset = gr.Button(ui_t("common.button.reset_settings"))
            with gr.Column(elem_classes=["neural-controls-unified"]):
                neural = build_neural_controls(settings)
                composition = build_composition_widgets(settings.language)
            input_path, output_path = build_path_controls()
            with gr.Row():
                output_format = gr.Dropdown(
                    list(IMAGE_FORMATS), value=settings.image_format, label=ui_t("neural.image.label.output_format"),
                )
                quality = gr.Slider(
                    1, 100, value=settings.image_quality, step=1, precision=0,
                    label=ui_t("common.label.lossy_quality"),
                )
            with gr.Row():
                rename_mode = gr.Radio(
                    [(option_label(choice, settings.language), choice) for choice in RENAME_MODES],
                    value=settings.image_rename_mode,
                    label=ui_t("common.label.rename"),
                )
                custom_suffix = gr.Textbox(
                    value=settings.image_custom_suffix,
                    label=ui_t("common.label.custom_suffix"),
                    placeholder=ui_t("neural.image.placeholder.custom_suffix"),
                    interactive=settings.image_rename_mode == "Custom",
                )
        with gr.Column(scale=3):
            output_gallery = gr.Gallery(
                label=ui_t("neural.image.label.output_gallery"), columns=2, height=520, object_fit="contain",
                interactive=False, buttons=["download", "download_all", "fullscreen"],
                elem_id="image-output-preview",
            )
            save_download, zip_button, zip_download = build_save_controls("image", "nr-image")
            status = gr.Textbox(label=ui_t("common.label.status"), interactive=False, lines=5, max_lines=12)
            results = gr.Dataframe(
                headers=batch_headers(settings.language),
                datatype=["str"] * len(batch_headers(settings.language)), interactive=False,
                label=ui_t("common.label.batch_results"), wrap=True,
            )
    tab = ImageTab(
        sources, input_gallery, input_actions, select_source, clear_source, neural, composition, mask_state, gpu_mode_state, output_format, quality, rename_mode,
        custom_suffix, render, stop, preview, reset, output_gallery, save_download, zip_button, zip_download, status, results
    )
    tab.input_path, tab.output_path = input_path, output_path
    bind_image_events(tab)
    return tab


def bind_image_events(tab: ImageTab) -> None:
    bind_batch_ui(
        tab, render_image_batch, kind="image", preview_mode=preview_input_images,
        archive_prefix="DLSS5_IMAGE_BATCH",
        preview_actions=[(tab.preview, preview_rendered_image)],
        realtime_preview=preview_rendered_image,
        realtime_components=tab.neural,
    )
    tab.rename_mode.change(
        rename_suffix_update, inputs=tab.rename_mode, outputs=tab.custom_suffix, queue=False,
    )
