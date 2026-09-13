from dataclasses import dataclass

import gradio as gr

from ..core.batch_ui import bind_input_surface_reactivation
from ..core.i18n import option_label
from ..settings.models import UPSCALE_MODE_CHOICES
from .image.ui import build_image_tab
from .video.ui import build_upscale_tab as build_video_tab


def _stable_visible(show: bool):
    return True if show else "hidden"


def mode_visibility(mode):
    return (
        gr.update(visible=_stable_visible(mode == "Image")),
        gr.update(visible=_stable_visible(mode == "Video")),
    )


_MODE_GUARD_JS = """(mode) => {
    document.documentElement.dataset.upscaleMode = mode;
    const hiddenPanel = document.getElementById(
        mode === 'Video' ? 'upscale-image' : 'upscale-video'
    );
    if (hiddenPanel) {
        hiddenPanel.querySelectorAll('video').forEach((node) => {
            try { node.pause(); } catch (_) {}
        });
    }
}"""


@dataclass
class UpscaleTab:
    mode: object
    image: object
    video: object
    image_panel: object
    video_panel: object


def build_upscale_tab(settings):
    selected_mode = settings.upscale_mode
    mode = gr.Radio(
        [(option_label(choice, settings.language), choice) for choice in UPSCALE_MODE_CHOICES],
        value=selected_mode,
        label=None,
        show_label=False,
        container=False,
        elem_id="upscale-mode",
    )
    with gr.Column(
        visible=_stable_visible(selected_mode == "Image"),
        elem_id="upscale-image",
        elem_classes=["workflow-mode-panel"],
    ) as image_panel:
        image = build_image_tab(settings)
    with gr.Column(
        visible=_stable_visible(selected_mode == "Video"),
        elem_id="upscale-video",
        elem_classes=["workflow-mode-panel"],
    ) as video_panel:
        video = build_video_tab(settings)

    transition = mode.change(
        None, inputs=mode, js=_MODE_GUARD_JS, queue=False, show_progress="hidden",
        trigger_mode="always_last",
    )
    visibility = transition.then(
        mode_visibility, inputs=mode, outputs=[image_panel, video_panel],
        queue=False, show_progress="hidden", trigger_mode="always_last",
    )
    bind_input_surface_reactivation(visibility, image, kind="image")
    bind_input_surface_reactivation(visibility, video, kind="video")
    return UpscaleTab(mode, image, video, image_panel, video_panel)
