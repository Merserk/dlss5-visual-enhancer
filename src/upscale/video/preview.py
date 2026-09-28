from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from ...core.ffmpeg.preview import LOSSLESS_PREVIEW_CODEC, decode_preview_frame
from .processor import upscale_video


def preview_upscale_native(
    path: str,
    options,
    *,
    one_frame: bool = False,
    progress=None,
    controller=None,
    output_dir=None,
    start_seconds: float | None = None,
    frame_index: int | None = None,
    preview_seconds: float | None = None,
):
    if not path or not Path(path).is_file():
        raise ValueError("Select one valid video to preview.")
    try:
        start = max(0.0, float(start_seconds or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    try:
        frame_no = max(1, int(frame_index or 1)) if frame_index else 1
    except (TypeError, ValueError):
        frame_no = 1
    length_seconds = 3.0 if preview_seconds is None else float(preview_seconds)
    if not math.isfinite(length_seconds) or length_seconds <= 0:
        raise ValueError("Preview duration must be positive and finite.")
    effective_path = str(path)
    temp_clip: str | None = None
    # Both preview modes start at the parked timeline frame. Extract only
    # when seeking: processing the original directly avoids an extra encode
    # at the beginning of the source.
    if start > 0:
        from ...core.ffmpeg.preview import extract_preview_subclip
        if progress:
            try:
                progress(0.02, "Seeking to timeline frame…" if one_frame else "Seeking to preview clip…")
            except TypeError:
                pass
        temp_clip = extract_preview_subclip(
            path, dest_dir=output_dir, start_seconds=start,
            length_seconds=None if one_frame else length_seconds,
            single_frame=one_frame, controller=controller,
        )
        effective_path = temp_clip
    try:
        opts = replace(
            options,
            codec=LOSSLESS_PREVIEW_CODEC,
            container="MKV",
            quality="Auto (Default)",
            preview_frames=1 if one_frame else None,
            preview_seconds=None if one_frame else length_seconds,
        )
        result = upscale_video(
            effective_path, opts, progress=progress, controller=controller, output_dir=output_dir
        )
    finally:
        if temp_clip:
            try:
                Path(temp_clip).unlink(missing_ok=True)
            except OSError:
                pass
    display = (decode_preview_frame(result.output_path, controller=controller)
               if one_frame else result.output_path)
    stamp = (f" f{frame_no} @ {start:.2f}s" if one_frame and start > 0.05 else
             f" {length_seconds:g}s @ {start:.2f}s" if not one_frame else "")
    return display, (
        f"Preview complete{stamp}: {result.output_width}×{result.output_height}, {result.frames} frames.\n"
        f"Cached clip: {result.output_path}\nReport: {result.report_path}"
    )
