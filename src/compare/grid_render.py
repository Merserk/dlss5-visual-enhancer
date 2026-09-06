from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image

from ..core.paths import OUTPUTS
from ..core.runtime import DLSS_MODEL_PRESETS, NR_PRESETS, NR_STYLES, UPSCALING_CHOICES
from ..neural_rendering.image.batch import convert_images
from ..neural_rendering.image.models import ImageConversionOptions
from ..settings.models import AUTOMATIC_MASK_CHOICES, parse_automatic_mask
from ..settings.storage import processing_gpu_settings
from .grid import GridCell, compose_grid
from .models import ComparisonItem
from .processor import compute_diff_from_images, load_rgb

DIFF_BASELINE_CHOICES = ("First rendered cell", "Input image")
GRID_OUTPUT_DIR = OUTPUTS / "grids"
GRID_CELLS_DIR = GRID_OUTPUT_DIR / "cells"


def _identity(value: str):
    return value


_UPSCALING_LABEL_TO_FACTOR = {label: factor for label, factor in UPSCALING_CHOICES}


@dataclass(slots=True)
class GridAxis:
    key: str
    label: str
    field_name: str  # the ImageConversionOptions field this axis drives
    kind: str  # "categorical" | "continuous"
    choices: tuple[str, ...] = ()
    value_min: float = 0.0
    value_max: float = 2.0
    # Converts the widget's string value to whatever ImageConversionOptions expects.
    # Identity for the plain string fields (nr_preset/nr_style/dlss_model_preset).
    to_option: Callable[[str], object] = _identity


GRID_AXES: dict[str, GridAxis] = {
    "nr_preset": GridAxis("nr_preset", "NR Preset", "nr_preset", "categorical", choices=tuple(NR_PRESETS)),
    "nr_style": GridAxis("nr_style", "NR Style", "nr_style", "categorical", choices=tuple(NR_STYLES)),
    "dlss_model_preset": GridAxis(
        "dlss_model_preset", "DLSS Model Preset", "dlss_model_preset", "categorical",
        choices=tuple(DLSS_MODEL_PRESETS),
    ),
    "automatic_mask": GridAxis(
        "automatic_mask", "Automatic Mask", "automatic_mask", "categorical",
        choices=AUTOMATIC_MASK_CHOICES, to_option=parse_automatic_mask,
    ),
    "upscaling_factor": GridAxis(
        "upscaling_factor", "Upscaling factor", "upscaling_factor", "categorical",
        choices=tuple(_UPSCALING_LABEL_TO_FACTOR), to_option=_UPSCALING_LABEL_TO_FACTOR.get,
    ),
    "nr_intensity": GridAxis(
        "nr_intensity", "NR Intensity", "nr_intensity", "continuous", value_min=0.0, value_max=2.0
    ),
    "local_tone_strength": GridAxis(
        "local_tone_strength", "Local Tone Strength", "local_tone_strength", "continuous",
        value_min=0.0, value_max=2.0,
    ),
    "local_structure_strength": GridAxis(
        "local_structure_strength", "Local Structure Strength", "local_structure_strength", "continuous",
        value_min=0.0, value_max=2.0,
    ),
    "skin_structure_strength": GridAxis(
        "skin_structure_strength", "Skin Structure Strength", "skin_structure_strength", "continuous",
        value_min=-1.0, value_max=2.0,
    ),
}

X_AXIS_CHOICES = [(axis.label, key) for key, axis in GRID_AXES.items()]
Y_AXIS_CHOICES = [("(none)", "none")] + X_AXIS_CHOICES


def default_continuous_text(axis: GridAxis) -> str:
    """5 evenly-spaced points across the axis's real, validated range — e.g.
    "0, 0.5, 1, 1.5, 2" for NR Intensity — instead of an arbitrary guess."""
    return ", ".join(f"{value:g}" for value in np.linspace(axis.value_min, axis.value_max, 5))


def _resolve_axis_values(axis_key: str, categorical_values, continuous_text) -> list[tuple[str, object]]:
    """(tile caption, ImageConversionOptions value) pairs for one axis.
    axis_key == "none" means "not swept" — a single pass-through entry."""
    if axis_key == "none":
        return [("", None)]
    axis = GRID_AXES[axis_key]
    if axis.kind == "categorical":
        return [(value, axis.to_option(value)) for value in (categorical_values or [])]
    values: list[tuple[str, object]] = []
    for raw in (continuous_text or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = float(raw)
        except ValueError:
            continue
        values.append((f"{parsed:g}", parsed))
    return values


def _save_composite(image: Image.Image, suffix: str) -> str:
    GRID_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = GRID_OUTPUT_DIR / f"grid_{stamp}_{suffix}.png"
    image.save(path)
    return str(path)


def render_grid(
    input_path: str | None,
    x_axis_key: str, x_categorical: list[str], x_continuous: str,
    y_axis_key: str, y_categorical: list[str], y_continuous: str,
    resolution_choice: str, show_diff_grid: bool, diff_baseline_source: str,
    nr_preset: str, nr_style: str, nr_intensity: float, local_tone_strength: float,
    local_structure_strength: float, skin_structure_strength: float, upscaling_factor: float,
    automatic_mask: str, dlss_model_preset: str,
    progress: Callable[[float], None] | None = None,
) -> tuple[Image.Image, Image.Image | None, str, list[ComparisonItem]]:
    """Render one input image across a sweep of one or two settings (using the
    baseline settings passed in for everything not swept) and tile the results.
    A cell that fails to render doesn't abort the grid — it shows up as a labeled
    error tile. Optionally also renders a second, same-shape grid of per-cell diffs
    against either the first rendered cell or the original input image.
    """
    if not input_path:
        raise ValueError("Choose an input image first.")
    if x_axis_key == "none":
        raise ValueError("Pick an X axis to sweep.")
    if y_axis_key != "none" and y_axis_key == x_axis_key:
        raise ValueError("X and Y axes must be different.")

    x_values = _resolve_axis_values(x_axis_key, x_categorical, x_continuous)
    y_values = _resolve_axis_values(y_axis_key, y_categorical, y_continuous)
    if not x_values:
        raise ValueError("No X values to render — add at least one.")

    base_kwargs = dict(
        ai_gpu_uuid=processing_gpu_settings()[0],
        nr_preset=nr_preset, nr_style=nr_style, nr_intensity=nr_intensity,
        local_tone_strength=local_tone_strength, local_structure_strength=local_structure_strength,
        skin_structure_strength=skin_structure_strength,
        automatic_mask=parse_automatic_mask(automatic_mask),
        dlss_model_preset=dlss_model_preset, upscaling_factor=upscaling_factor,
        output_format="PNG", quality=100, rename_mode="Auto", custom_suffix="_grid",
    )

    x_axis_label = GRID_AXES[x_axis_key].label
    y_axis_label = GRID_AXES[y_axis_key].label if y_axis_key != "none" else None
    col_labels = [f"{x_axis_label}: {caption}" for caption, _option in x_values]
    row_labels = [f"{y_axis_label}: {caption}" for caption, _option in y_values] if y_axis_label else [""]

    total_cells = len(y_values) * len(x_values)
    done = 0
    grid_cells: list[list[GridCell]] = []
    comparison_items: list[ComparisonItem] = [
        ComparisonItem(f"Input: {Path(input_path).name}", input_path)
    ]

    for row_index, (y_caption, y_option) in enumerate(y_values):
        row: list[GridCell] = []
        for col_index, (x_caption, x_option) in enumerate(x_values):
            if progress is not None:
                desc = " / ".join(part for part in (x_caption, y_caption) if part)
                progress(done / max(total_cells, 1), desc=f"Rendering {desc}")
            kwargs = dict(base_kwargs)
            if x_axis_key != "none":
                kwargs[GRID_AXES[x_axis_key].field_name] = x_option
            if y_axis_key != "none":
                kwargs[GRID_AXES[y_axis_key].field_name] = y_option
            label = col_labels[col_index] if y_axis_key == "none" else f"{col_labels[col_index]} / {row_labels[row_index]}"
            try:
                options = ImageConversionOptions(**kwargs)
                result = convert_images(
                    [input_path], options, output_dir=GRID_CELLS_DIR,
                    generate_previews=False, create_zip=False,
                )
                if result.successes:
                    output_path = result.successes[0].output_path
                    with Image.open(output_path) as handle:
                        image = handle.convert("RGB").copy()
                    row.append(GridCell(label, image))
                    comparison_items.append(ComparisonItem(f"Cell: {label}", output_path))
                else:
                    error = result.failures[0].error if result.failures else "No output produced."
                    row.append(GridCell(label, None, error))
            except Exception as exc:  # a bad axis value shouldn't take down the whole grid
                row.append(GridCell(label, None, str(exc)))
            done += 1
        grid_cells.append(row)

    target_long_edge = None if resolution_choice == "Full res" else int(resolution_choice)
    show_row_labels = y_axis_key != "none"
    composed = compose_grid(
        grid_cells, row_labels, col_labels, target_long_edge=target_long_edge,
        show_row_labels=show_row_labels, show_col_labels=True,
    )
    grid_path = _save_composite(composed, "grid")
    comparison_items.append(ComparisonItem("Grid: Full grid", grid_path))

    # A single input image is useless as a Comparison reference against a whole tiled
    # grid — wrong shape entirely, so the slider comparison told you nothing. Build a
    # same-shape grid of the (unmodified) input repeated in every cell instead, so
    # sliding actually reveals "all inputs" vs. "all rendered variants" side by side.
    input_image_full = load_rgb(input_path)
    input_grid_cells = [[GridCell(cell.label, input_image_full) for cell in row] for row in grid_cells]
    input_grid_composed = compose_grid(
        input_grid_cells, row_labels, col_labels, target_long_edge=target_long_edge,
        show_row_labels=show_row_labels, show_col_labels=True,
    )
    input_grid_path = _save_composite(input_grid_composed, "input")
    comparison_items.append(ComparisonItem("Grid: Input grid", input_grid_path))

    diff_composed: Image.Image | None = None
    if show_diff_grid:
        if diff_baseline_source == "Input image":
            baseline_image = input_image_full
            baseline_error = None
        else:
            first_cell = grid_cells[0][0]
            baseline_image = first_cell.image
            baseline_error = first_cell.error or "The baseline cell failed to render."
        if baseline_image is None:
            pass  # status message below explains why the diff grid was skipped
        else:
            diff_rows: list[list[GridCell]] = []
            for row in grid_cells:
                diff_row: list[GridCell] = []
                for cell in row:
                    if cell.image is None:
                        diff_row.append(GridCell(cell.label, None, cell.error))
                        continue
                    diff_image, _metrics = compute_diff_from_images(baseline_image, cell.image, amplify=4.0)
                    diff_row.append(GridCell(cell.label, diff_image.convert("RGB")))
                diff_rows.append(diff_row)
            diff_composed = compose_grid(
                diff_rows, row_labels, col_labels, target_long_edge=target_long_edge,
                show_row_labels=show_row_labels, show_col_labels=True,
            )
            diff_path = _save_composite(diff_composed, "diff")
            comparison_items.append(ComparisonItem("Grid: Diff grid", diff_path))

    successes = sum(1 for row in grid_cells for cell in row if cell.image is not None)
    status = f"Rendered {successes}/{total_cells} cell(s)."
    if successes < total_cells:
        status += f" {total_cells - successes} failed — see the red tile(s) for the error."
    if show_diff_grid and diff_composed is None:
        status += " Diff grid skipped: " + (baseline_error or "no baseline available.")
    return composed, diff_composed, status, comparison_items
