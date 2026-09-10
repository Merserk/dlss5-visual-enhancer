from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from ..neural_rendering.image.ui import build_dlss_model_control, build_neural_controls
from ..settings.models import UISettings
from .grid_render import (
    DIFF_BASELINE_CHOICES, GRID_AXES, X_AXIS_CHOICES, Y_AXIS_CHOICES,
    default_continuous_text, render_grid,
)

RESOLUTION_CHOICES = ("512", "768", "1024", "Full res")
DEFAULT_X_AXIS = "nr_intensity"


def _axis_widget_update(axis_key: str):
    """Show the right input widget (checkbox group vs. free-typed numbers) for
    whichever axis was just picked, seeded with sensible defaults either way."""
    if axis_key == "none":
        return gr.update(visible=False), gr.update(visible=False)
    axis = GRID_AXES[axis_key]
    if axis.kind == "categorical":
        choices = list(axis.choices)
        return gr.update(choices=choices, value=choices, visible=True), gr.update(visible=False)
    return gr.update(visible=False), gr.update(value=default_continuous_text(axis), visible=True)


def _x_axis_change(axis_key: str):
    return _axis_widget_update(axis_key)


def _y_axis_change(axis_key: str):
    return _axis_widget_update(axis_key)


def _mirror_first_input(paths):
    if not paths:
        return None
    return paths[0] if isinstance(paths, list) else paths


def _render_grid_or_report_error(*args, progress=gr.Progress(track_tqdm=False)):
    try:
        return render_grid(*args, progress=progress)
    except Exception as exc:
        return None, None, f"Couldn't render the grid: {exc}", []


@dataclass(slots=True)
class GridTab:
    input_image: object
    neural: tuple
    model_preset: object
    x_axis: object
    x_categorical: object
    x_continuous: object
    y_axis: object
    y_categorical: object
    y_continuous: object
    resolution: object
    show_diff_grid: object
    diff_baseline_source: object
    render: object
    output_image: object
    diff_output_image: object
    status: object
    comparison_items: object
    send_to_compare: object


def build_grid_tab(settings: UISettings) -> GridTab:
    gr.Markdown(
        "Sweeps one (or two) settings across a fixed input image and tiles the results into "
        "a single grid. Set your baseline below first — anything not chosen as an X or Y axis "
        "renders using these values, so a swept axis just overrides its own field per cell."
    )
    input_image = gr.File(
        label="Input image (auto-filled from the Image tab's first upload — override if you want a different one)",
        file_count="single", type="filepath",
    )
    with gr.Accordion("Baseline settings", open=True):
        neural = build_neural_controls(settings)
        model_preset = build_dlss_model_control(settings)
        gr.Markdown(
            "_If a setting above is also picked as an X or Y axis below, its axis values "
            "override this baseline for that setting, cell by cell._"
        )
    with gr.Row():
        with gr.Column():
            x_axis = gr.Dropdown(X_AXIS_CHOICES, value=DEFAULT_X_AXIS, label="X axis")
            x_categorical = gr.CheckboxGroup(visible=False, label="X values")
            x_continuous = gr.Textbox(
                value=default_continuous_text(GRID_AXES[DEFAULT_X_AXIS]),
                label="X values (comma-separated)", visible=True,
            )
        with gr.Column():
            y_axis = gr.Dropdown(Y_AXIS_CHOICES, value="none", label="Y axis (optional)")
            y_categorical = gr.CheckboxGroup(visible=False, label="Y values")
            y_continuous = gr.Textbox(label="Y values (comma-separated)", visible=False)
    with gr.Row():
        resolution = gr.Radio(
            list(RESOLUTION_CHOICES), value="1024", label="Tile resolution",
            info="Longest edge of each tile; never upscales a tile past its own native resolution.",
        )
        show_diff_grid = gr.Checkbox(label="Also render a diff grid (same shape, each cell vs. a baseline)")
        diff_baseline_source = gr.Radio(
            list(DIFF_BASELINE_CHOICES), value="First rendered cell", label="Diff baseline",
        )
    render = gr.Button("Render grid", variant="primary")
    output_image = gr.Image(type="pil", interactive=False, label="Grid result")
    diff_output_image = gr.Image(type="pil", interactive=False, label="Diff grid", visible=False)
    comparison_items = gr.State([])
    send_to_compare = gr.Button("Send to Comparison")
    status = gr.Textbox(label="Status", interactive=False)

    x_axis.change(_x_axis_change, inputs=x_axis, outputs=[x_categorical, x_continuous], queue=False)
    y_axis.change(_y_axis_change, inputs=y_axis, outputs=[y_categorical, y_continuous], queue=False)

    return GridTab(
        input_image, neural, model_preset, x_axis, x_categorical, x_continuous, y_axis,
        y_categorical, y_continuous, resolution, show_diff_grid, diff_baseline_source, render,
        output_image, diff_output_image, status, comparison_items, send_to_compare,
    )


def _render_and_toggle_diff_visibility(*args, progress=gr.Progress(track_tqdm=False)):
    grid_image, diff_image, status, items = _render_grid_or_report_error(*args, progress=progress)
    return grid_image, gr.update(value=diff_image, visible=diff_image is not None), status, items


def bind_grid_events(grid_tab: GridTab, image_tab) -> None:
    # Quality-of-life only: whatever's first in the Image tab's uploader shows up here too,
    # so the common case (upload once, try it in both tabs) doesn't need a re-upload. Still
    # freely overridable — this only fires when the Image tab's own uploader changes.
    image_tab.sources.change(
        _mirror_first_input, inputs=image_tab.sources, outputs=grid_tab.input_image, queue=False
    )

    render_inputs = [
        grid_tab.input_image,
        grid_tab.x_axis, grid_tab.x_categorical, grid_tab.x_continuous,
        grid_tab.y_axis, grid_tab.y_categorical, grid_tab.y_continuous,
        grid_tab.resolution, grid_tab.show_diff_grid, grid_tab.diff_baseline_source,
        *grid_tab.neural, grid_tab.model_preset,
    ]
    grid_tab.render.click(
        _render_and_toggle_diff_visibility, inputs=render_inputs,
        outputs=[grid_tab.output_image, grid_tab.diff_output_image, grid_tab.status, grid_tab.comparison_items],
        concurrency_limit=1, show_progress="full", show_progress_on=grid_tab.output_image,
    )
