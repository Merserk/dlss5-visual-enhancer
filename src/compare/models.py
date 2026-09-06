from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..core.batch_ui import BATCH_HEADERS


@dataclass(slots=True)
class ComparisonItem:
    """One image that can be picked as a reference or candidate in the Comparison tab.

    `label` is what shows up in the dropdowns (e.g. "Input: cat.png" or
    "Output: cat_DLSS5.png"); `path` is the real file on disk. For sent-from-Image-tab
    items this is always the full-resolution file, never a UI thumbnail.
    """

    label: str
    path: str


def build_comparison_items_from_batch_results(
    input_paths: list[str] | str | None, rows: list[list[str]] | None
) -> list[ComparisonItem]:
    """Turn a tab's current input list + live batch-results table into ComparisonItems.

    Used at "Send to Comparison" click time. Reads real output paths from the
    "Output path" column of the batch results table (populated live during
    rendering by BatchProgress) rather than depending on a dedicated file-list
    component — that component (output_files) doesn't exist on every tab and was
    removed from the Image tab entirely, since the gallery's own download buttons
    already cover browsing/downloading outputs.
    """
    state_col = BATCH_HEADERS.index("State")
    path_col = BATCH_HEADERS.index("Output path")

    if isinstance(input_paths, str):
        input_paths = [input_paths]
    items = [ComparisonItem(f"Input: {Path(path).name}", path) for path in (input_paths or [])]

    # tab.results is a gr.Dataframe (type="pandas" by default), so its value here is a
    # pandas.DataFrame, not a plain list of lists — normalize before indexing into it.
    if rows is None:
        record_rows: list[list] = []
    elif hasattr(rows, "values"):  # pandas.DataFrame
        record_rows = rows.values.tolist()
    else:
        record_rows = list(rows)

    for row in record_rows:
        if len(row) > path_col and row[state_col] == "Complete" and row[path_col]:
            output_path = row[path_col]
            items.append(ComparisonItem(f"Output: {Path(output_path).name}", output_path))
    return items


@dataclass(slots=True)
class DiffMetrics:
    """Numeric results of comparing two images. Purely computational — no display
    formatting or labels here; the UI layer turns this into rows for the user.
    """

    reference_size: tuple[int, int]
    candidate_size: tuple[int, int]
    compared_size: tuple[int, int]
    resampled: bool
    mean_abs_error: float
    root_mean_square_error: float
    changed_pixels_pct: float
    max_channel_delta: int
