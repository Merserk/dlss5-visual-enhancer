"""Shared Image/Video/Live controls for the post-DLSS compositor."""
from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from ..core.nr_composition import inspect_nr_mask, mask_status
from ..core.i18n import t as current_t, translator
from ..settings.models import UISettings


@dataclass(slots=True)
class CompositionWidgets:
    mask: object
    mask_status: object
    detail_only: object


def build_composition_sliders(settings: UISettings, t) -> list[object]:
    # Created directly in the parent Column (no gr.Row): each slider spans the
    # full width in one vertical stack. Return order is unchanged.
    color = gr.Slider(
        0.0, 1.0, value=settings.nr_color_strength, step=0.05, precision=2,
        label=t("neural.label.nr_color_strength"), buttons=["reset"],
    )
    tone = gr.Slider(
        0.0, 1.0, value=settings.tone_preservation, step=0.05, precision=2,
        label=t("neural.label.tone_preservation"), buttons=["reset"],
    )
    face_skin = gr.Slider(
        0.0, 1.0, value=settings.face_skin_protection, step=0.05, precision=2,
        label=t("neural.label.face_skin_protection"), buttons=["reset"],
    )
    grain = gr.Slider(
        0.0, 1.0, value=settings.grain_preservation, step=0.05, precision=2,
        label=t("neural.label.grain_preservation"), buttons=["reset"],
    )
    feather = gr.Slider(
        0, 128, value=settings.mask_feather, step=1, precision=0,
        label=t("neural.label.mask_feather"), buttons=["reset"],
    )
    return [color, tone, face_skin, grain, feather]


def build_composition_widgets(language: str | None = None) -> CompositionWidgets:
    t = translator(language).t
    detail_only = gr.Button(
        t("neural.button.detail_only"),
        size="sm",
        variant="secondary",
    )
    mask = gr.Image(
        label=t("neural.label.custom_mask"),
        type="filepath",
        # Preserve the uploaded bytes and original filename for EXIF/animation
        # handling plus report hashing; decoding/conversion belongs to our mask
        # preparation step, not Gradio's image preprocessor.
        image_mode=None,
        sources=["upload"],
        height=180,
    )
    status = gr.Markdown(mask_status(None))
    return CompositionWidgets(mask, status, detail_only)


def _select_mask(path: str | None):
    try:
        selection = inspect_nr_mask(path)
    except ValueError as exc:
        raise gr.Error(str(exc)) from exc
    from ..settings.ui import persist_nr_mask

    persist_nr_mask(selection)
    value = selection.state() if selection is not None else None
    display_path = selection.path if selection is not None else None
    status = mask_status(selection)
    return value, display_path, display_path, display_path, status, status, status


def _clear_mask():
    return _select_mask(None)


def bind_composition_mask_events(
    mask_state: object,
    image_widgets: CompositionWidgets,
    video_widgets: CompositionWidgets,
    live_widgets: CompositionWidgets,
) -> None:
    widgets = (image_widgets, video_widgets, live_widgets)
    masks = [item.mask for item in widgets]
    statuses = [item.mask_status for item in widgets]
    outputs = [mask_state, *masks, *statuses]
    for item in widgets:
        item.mask.upload(
            _select_mask,
            inputs=item.mask,
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )
        item.mask.clear(
            _clear_mask,
            outputs=outputs,
            queue=False,
            show_progress="hidden",
        )
