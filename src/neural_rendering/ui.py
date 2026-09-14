from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from ..core.batch_ui import bind_input_surface_reactivation
from ..core.i18n import option_label
from ..settings.models import UISettings
from .image.ui import ImageTab, build_image_tab
from .video.ui import VideoTab, build_video_tab


MODE_CHOICES = ("Image", "Video")


def _stable_visible(show: bool):
    # ``hidden`` keeps the component tree mounted while taking no layout space.
    return True if show else "hidden"


def mode_visibility(mode: str) -> tuple[dict, dict]:
    """Show one workflow while keeping both stateful component trees mounted."""
    show_video = mode == "Video"
    return (
        gr.update(visible=_stable_visible(not show_video)),
        gr.update(visible=_stable_visible(show_video)),
    )


_MODE_GUARD_JS = """(mode) => {
    document.documentElement.dataset.neuralRenderingMode = mode;
    const hiddenPanel = document.getElementById(
        mode === 'Video' ? 'neural-rendering-image' : 'neural-rendering-video'
    );
    if (hiddenPanel) {
        hiddenPanel.querySelectorAll('video').forEach((node) => {
            try { node.pause(); } catch (_) {}
        });
    }
}"""


@dataclass(slots=True)
class NeuralRenderingTab:
    mode: object
    image: ImageTab
    video: VideoTab
    image_panel: object
    video_panel: object


def build_neural_rendering_tab(
    settings: UISettings, gpu_mode_state: object, mask_state: object,
) -> NeuralRenderingTab:
    mode = gr.Radio(
        choices=[(option_label(choice, settings.language), choice) for choice in MODE_CHOICES],
        value="Image",
        label=None,
        show_label=False,
        container=False,
        interactive=True,
        elem_id="neural-rendering-mode",
    )
    with gr.Column(
        visible=True, elem_id="neural-rendering-image", elem_classes=["workflow-mode-panel"],
    ) as image_panel:
        image_tab = build_image_tab(settings, gpu_mode_state, mask_state)
    with gr.Column(
        visible="hidden", elem_id="neural-rendering-video", elem_classes=["workflow-mode-panel"],
    ) as video_panel:
        video_tab = build_video_tab(settings, gpu_mode_state, mask_state)

    # First hide the old panel synchronously in the browser.  The backend update
    # then makes the requested panel authoritative.  If requests overlap, the
    # latest client guard still prevents an older response from exposing the
    # wrong panel; at worst the correct panel stays blank until the latest reply.
    transition = mode.change(
        None, inputs=mode, js=_MODE_GUARD_JS, queue=False, show_progress="hidden",
        trigger_mode="always_last",
    )
    visibility = transition.then(
        mode_visibility,
        inputs=mode,
        outputs=[image_panel, video_panel],
        queue=False,
        show_progress="hidden",
        trigger_mode="always_last",
    )
    # Re-assert the upload/preview invariant after the mode panel is restored.
    # This is non-destructive: it only corrects visibility and never regenerates
    # media or clears output/download state.
    bind_input_surface_reactivation(visibility, image_tab, kind="image")
    bind_input_surface_reactivation(visibility, video_tab, kind="video")
    return NeuralRenderingTab(mode, image_tab, video_tab, image_panel, video_panel)
