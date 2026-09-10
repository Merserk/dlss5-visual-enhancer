from __future__ import annotations

from dataclasses import dataclass

import gradio as gr

from ..frame_interpolation.models import FrameInterpolationOptions
from .models import ComparisonItem, DiffMetrics, build_comparison_items_from_batch_results
from .processor import compute_diff
from .video_ui import VideoCompareTab, bind_video_compare_events, build_video_compare_panel, receive_video_items

NO_SELECTION = "(none yet — send an image here from another tab)"


def _choices(pool: list[ComparisonItem]) -> list[str]:
    labels = [item.label for item in pool]
    return labels or [NO_SELECTION]


def _path_for(pool: list[ComparisonItem], label: str) -> str | None:
    for item in pool:
        if item.label == label:
            return item.path
    return None


def _metrics_rows(
    metrics: DiffMetrics, reference_label: str, candidate_label: str
) -> list[list[str]]:
    rows = [
        ["Reference", f"{reference_label}  ({metrics.reference_size[0]}×{metrics.reference_size[1]})"],
        ["Candidate", f"{candidate_label}  ({metrics.candidate_size[0]}×{metrics.candidate_size[1]})"],
        ["Mean abs error", f"{metrics.mean_abs_error:.3f} / 255"],
        ["RMSE", f"{metrics.root_mean_square_error:.3f} / 255"],
        ["Changed pixels", f"{metrics.changed_pixels_pct:.2f}%"],
        ["Max channel delta", f"{metrics.max_channel_delta} / 255"],
    ]
    if metrics.resampled:
        rows.append(
            [
                "Note",
                f"Candidate resampled to {metrics.compared_size[0]}×{metrics.compared_size[1]} to "
                "match the reference before comparing — resolutions differed, so this is a "
                "like-for-like check at that size, not a native-detail comparison.",
            ]
        )
    return rows


def refresh_comparison(
    pool: list[ComparisonItem], reference_label: str, candidate_label: str, amplify: float
):
    reference_path = _path_for(pool, reference_label)
    candidate_path = _path_for(pool, candidate_label)
    if not reference_path or not candidate_path:
        return None, [["Status", "Pick a reference and a candidate above to compare."]], None
    try:
        diff_image, metrics = compute_diff(reference_path, candidate_path, amplify=amplify)
    except Exception as exc:
        return None, [["Status", f"Couldn't compare those two: {exc}"]], None
    return (
        (reference_path, candidate_path),
        _metrics_rows(metrics, reference_label, candidate_label),
        diff_image,
    )


def swap_selection(reference_label: str, candidate_label: str):
    return candidate_label, reference_label


PREFERRED_CANDIDATE_LABELS = ("Grid: Full grid",)
PREFERRED_REFERENCE_LABELS = ("Grid: Input grid",)


def switch_to_compare_tab():
    """Selects the Comparison tab, as its own event -- deliberately NOT bundled
    into the same click as the mode/panel-visibility updates below. See the
    long comment on bind_comparison_events for why: a Tabs selection change
    and a Column visibility change landing in the *same* output batch race
    against the target TabItem's (re)mount, and the panel-visibility half of
    that race is not reliably won on every send."""
    return gr.Tabs(selected="compare")


def receive_items(new_items: list[ComparisonItem]):
    labels = [item.label for item in new_items]
    default_reference = (
        next((label for label in PREFERRED_REFERENCE_LABELS if label in labels), None)
        or next((label for label in labels if label.startswith("Input: ")), None)
        or (labels[0] if labels else NO_SELECTION)
    )
    default_candidate = (
        next((label for label in labels if label.startswith("Output: ")), None)
        or next((label for label in PREFERRED_CANDIDATE_LABELS if label in labels), None)
        or (labels[-1] if labels else NO_SELECTION)
    )
    return (
        new_items,
        gr.update(choices=labels or [NO_SELECTION], value=default_reference),
        gr.update(choices=labels or [NO_SELECTION], value=default_candidate),
        gr.update(value="Image"),
        gr.update(visible=True),  # image panel
        gr.update(visible=False),  # video panel
    )


def send_batch_results_to_compare(input_paths, rows):
    """Send-to-Comparison handler for tabs (Image) whose render pipeline doesn't
    hand back a ready-made ComparisonItem list — reads the tab's current sources
    and its live batch-results table directly instead."""
    return receive_items(build_comparison_items_from_batch_results(input_paths, rows))


def send_batch_results_to_video_compare(input_paths, rows, preview_path):
    """Same idea as send_batch_results_to_compare, but for Video/Upscale Video —
    both already carry real output paths in their results table via the same
    bind_batch_ui/BatchProgress mechanism as Image, so this is the exact same
    helper feeding the video pool + mode switch instead.

    preview_path is the tab's most recent rendered preview (tracked via
    bind_batch_ui's preview_result_state) -- included so a preview can be sent
    even when no full batch render has completed yet, since previews never
    populate `rows`."""
    return receive_video_items(build_comparison_items_from_batch_results(input_paths, rows, preview_path))


def send_frame_interpolation_results_to_video_compare(input_paths, rows, target_fps, preview_path):
    """Frame Interpolation's own "Send to Comparison" handler. Everything else
    about this is identical to send_batch_results_to_video_compare above, but
    Frame Interpolation is the one tab that already knows its output's exact
    frame rate (the target_fps the user picked) -- so unlike Video/Upscale,
    which leave the sync player's frame-step guess alone, this defaults it to
    1/target_fps instead of the generic ~30fps guess."""
    items = build_comparison_items_from_batch_results(input_paths, rows, preview_path)
    try:
        target_rate = FrameInterpolationOptions(target_fps=target_fps).target_rate
        # Round for display -- an unrounded float (e.g. 0.016666666666666666)
        # was overflowing the frame-step box, which only made the missing
        # spinner-arrow room look worse than it already was.
        frame_step_seconds = round(1.0 / float(target_rate), 6) if target_rate else None
    except Exception:
        # An unparseable/unexpected target_fps value shouldn't block the send
        # itself -- just fall back to leaving the frame-step field as-is.
        frame_step_seconds = None
    return receive_video_items(items, frame_step_seconds=frame_step_seconds)


@dataclass(slots=True)
class CompareTab:
    mode: object
    image_panel: object
    video_panel: object
    pool: object
    reference: object
    candidate: object
    swap: object
    diff_amplify: object
    slider: object
    metrics: object
    diff_image: object
    video: VideoCompareTab


def build_compare_tab() -> CompareTab:
    mode = gr.Radio(["Image", "Video"], value="Image", label="Mode")
    with gr.Column(visible=True) as image_panel:
        gr.Markdown(
            "Send an image here with the **Send to Comparison** button under a render's output "
            "(Image or Grid tab). Pick any two of the images that came along with it — an input, "
            "or any rendered output — to diff them against each other."
        )
        pool = gr.State([])
        with gr.Row():
            reference = gr.Dropdown(choices=[NO_SELECTION], value=NO_SELECTION, label="Reference (baseline)")
            candidate = gr.Dropdown(choices=[NO_SELECTION], value=NO_SELECTION, label="Candidate")
            swap = gr.Button("⇄ Swap", scale=0)
        slider = gr.ImageSlider(type="filepath", label="Before / after", height=520)
        with gr.Row():
            with gr.Column(scale=1):
                metrics = gr.Dataframe(
                    headers=["Metric", "Value"], datatype=["str", "str"], interactive=False,
                    label="Comparison metrics", wrap=True,
                )
            with gr.Column(scale=1):
                diff_amplify = gr.Slider(
                    1, 16, value=4, step=1, label="Diff visualization amplify",
                    info="Brightens the difference view below so subtle changes are easier to see. Doesn't affect the metrics.",
                )
                diff_image = gr.Image(
                    type="pil", interactive=False, label="Difference (amplified, grayscale)", height=360,
                )
    with gr.Column(visible=False) as video_panel:
        video = build_video_compare_panel()

    mode.change(
        lambda selected: (gr.update(visible=selected == "Image"), gr.update(visible=selected == "Video")),
        inputs=mode, outputs=[image_panel, video_panel], queue=False,
    )
    return CompareTab(
        mode, image_panel, video_panel, pool, reference, candidate, swap, diff_amplify, slider,
        metrics, diff_image, video,
    )


def bind_comparison_events(
    compare_tab: CompareTab, tabs, image_tab=None, grid_tab=None, video_tab=None, frame_tab=None,
    upscale_image_tab=None, upscale_video_tab=None,
) -> None:
    refresh_inputs = [compare_tab.pool, compare_tab.reference, compare_tab.candidate, compare_tab.diff_amplify]
    refresh_outputs = [compare_tab.slider, compare_tab.metrics, compare_tab.diff_image]
    # Decoding + diffing a large or RAW image takes real time, so these stay on the
    # normal queue (unlike the trivial instant updates elsewhere in this file) rather
    # than running with queue=False, which would block the app while it works.
    refresh_kwargs = dict(show_progress="minimal")
    compare_tab.reference.change(refresh_comparison, inputs=refresh_inputs, outputs=refresh_outputs, **refresh_kwargs)
    compare_tab.candidate.change(refresh_comparison, inputs=refresh_inputs, outputs=refresh_outputs, **refresh_kwargs)
    compare_tab.diff_amplify.release(refresh_comparison, inputs=refresh_inputs, outputs=refresh_outputs, **refresh_kwargs)
    compare_tab.swap.click(
        swap_selection, inputs=[compare_tab.reference, compare_tab.candidate],
        outputs=[compare_tab.reference, compare_tab.candidate], queue=False,
    )
    # Every "send" action forces mode + both panels' visibility directly, rather than
    # relying on compare_tab.mode's own .change() event to cascade the panel toggle —
    # a programmatic update to a value Gradio already holds doesn't reliably re-fire
    # .change(), which was leaving the wrong panel showing until the user manually
    # toggled the radio themselves.
    #
    # **The tab switch itself is deliberately a separate, chained event (`.then()`),
    # not bundled into this same click's outputs.** Root-caused by reproducing it
    # directly (real Gradio 6.26.0 + a real headless browser, not just code review):
    # a `gr.Tabs(selected=...)` switch and a sibling-Column `visible=` change landing
    # in the *same* output batch race against the target TabItem (re)mounting, since
    # Send-to-Comparison always switches INTO the Comparison tab from a different one.
    # The very first send after a fresh page load wins that race (nothing has
    # mounted/unmounted yet), which is exactly why this looked intermittent in
    # testing — but every subsequent send, after the tab has been left and
    # re-entered even once, reliably loses it: the newly (re)mounted image panel's
    # `visible=False` doesn't stick, while the video panel's `visible=True` does —
    # confirmed with a minimal repro isolating just a Tabs+two-Columns page, with
    # no other app code involved. Selecting the tab as its own event first, and only
    # setting mode/panel-visibility/dropdowns in a `.then()` afterward, means those
    # updates always land on an already-settled, already-mounted tab rather than
    # racing its remount. Don't recombine these into one event without re-verifying
    # against a real browser first.
    receive_outputs = [
        compare_tab.pool, compare_tab.reference, compare_tab.candidate,
        compare_tab.mode, compare_tab.image_panel, compare_tab.video_panel,
    ]
    for tab in (image_tab, upscale_image_tab):
        if tab is not None:
            tab.send_to_compare.click(
                switch_to_compare_tab, outputs=[tabs], queue=False,
            ).then(
                send_batch_results_to_compare, inputs=[tab.sources, tab.results],
                outputs=receive_outputs, queue=False,
            )
    if grid_tab is not None:
        grid_tab.send_to_compare.click(
            switch_to_compare_tab, outputs=[tabs], queue=False,
        ).then(
            receive_items, inputs=grid_tab.comparison_items, outputs=receive_outputs, queue=False,
        )

    bind_video_compare_events(compare_tab.video)
    video_receive_outputs = [
        compare_tab.video.pool, compare_tab.video.reference, compare_tab.video.candidate,
        compare_tab.mode, compare_tab.image_panel, compare_tab.video_panel,
        compare_tab.video.suggested_frame_step,
        compare_tab.video.reference_video, compare_tab.video.candidate_video,
    ]
    for tab in (video_tab, upscale_video_tab):
        if tab is not None:
            tab.send_to_compare.click(
                switch_to_compare_tab, outputs=[tabs], queue=False,
            ).then(
                send_batch_results_to_video_compare,
                inputs=[tab.sources, tab.results, tab.last_preview_path],
                outputs=video_receive_outputs, queue=False,
            )
    if frame_tab is not None:
        frame_tab.send_to_compare.click(
            switch_to_compare_tab, outputs=[tabs], queue=False,
        ).then(
            send_frame_interpolation_results_to_video_compare,
            inputs=[frame_tab.sources, frame_tab.results, frame_tab.target_fps, frame_tab.last_preview_path],
            outputs=video_receive_outputs, queue=False,
        )

    # This is the deeper bug behind "leaving the Comparison tab and coming back shows
    # both panels stacked, even with nothing ever sent." Root-caused the same way as
    # the send-race above (real Gradio 6.26.0 + a real headless browser): simply
    # deselecting and reselecting a TabItem -- via the user clicking a *different* tab
    # and back, nothing to do with any of our own click handlers -- corrupts a nested
    # Column's `visible` state on remount. Specifically, whichever Column is supposed
    # to stay hidden reappears, while the one that's supposed to stay visible does.
    # This reproduces in a 30-line repro with nothing but Tabs + two Columns, with NO
    # send button, NO app.py wiring at all involved -- so no ordering/race fix on our
    # own events could ever prevent it; it isn't caused by anything we do.
    # Fix: re-assert the correct panel visibility every time ANY tab is (re)selected,
    # not just when Mode itself changes or a send happens. This "heals" the corrupted
    # state the instant the user lands back on Comparison, from whatever value Mode
    # currently holds -- regardless of whether they got there by clicking the tab
    # directly or via Send-to-Comparison.
    def reassert_panel_visibility(selected_mode):
        return gr.update(visible=selected_mode == "Image"), gr.update(visible=selected_mode == "Video")

    tabs.select(
        reassert_panel_visibility, inputs=compare_tab.mode,
        outputs=[compare_tab.image_panel, compare_tab.video_panel], queue=False,
    )
