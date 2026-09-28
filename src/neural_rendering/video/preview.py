from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Any

from ...core.jobs import Cancelled
from ...core.ffmpeg import probe_video
from ...core.ffmpeg.preview import LOSSLESS_PREVIEW_CODEC, decode_preview_frame
from ...settings.models import coerce_hdr_mode
from .models import ConversionOptions
from .processor import convert_video

PREVIEW_SECONDS = 3.0
ProgressCallback = Callable[[float, str], None]


def _emit_progress(progress: Any, value: float, message: str) -> None:
    if progress is None:
        return
    try:
        progress(float(value), str(message))
    except TypeError:
        progress(float(value), desc=str(message))


def process_video_preview(
    input_path: str,
    options: ConversionOptions,
    *,
    preview_seconds: float | None = None,
    preview_frames: int | None = None,
    progress: ProgressCallback | Any | None = None,
    output_dir=None,
    controller=None,
    ephemeral_preview: bool = False,
    start_seconds: float | None = None,
    frame_index: int | None = None,
) -> tuple[object | None, str]:
    """Qt-native Neural Rendering preview used by the desktop UI.

    Returns cached video or full-resolution RGBA pixels with a status string.
    """
    if not input_path:
        raise ValueError("Choose a video first.")
    source = Path(input_path)
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        start = max(0.0, float(start_seconds or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    try:
        frame_no = max(1, int(frame_index or 1)) if frame_index else 1
    except (TypeError, ValueError):
        frame_no = 1
    effective_source = str(source)
    temp_clip: str | None = None
    if start > 0 and (preview_frames is not None or preview_seconds is not None):
        from ...core.ffmpeg.preview import extract_preview_subclip
        _emit_progress(progress, 0.02, "Seeking to timeline frame…")
        temp_clip = extract_preview_subclip(
            str(source), dest_dir=output_dir, start_seconds=start,
            single_frame=preview_frames is not None,
            length_seconds=preview_seconds if preview_frames is None else None,
            controller=controller,
        )
        effective_source = temp_clip

    is_preview = preview_seconds is not None or preview_frames is not None
    if is_preview:
        effective_codec, effective_container = LOSSLESS_PREVIEW_CODEC, "MKV"
    else:
        effective_codec, effective_container = options.codec, options.container

    try:
        effective_hdr = coerce_hdr_mode(effective_codec, options.preserve_hdr)
        if is_preview:
            effective_hdr = effective_hdr or bool(probe_video(effective_source, count_mode="metadata", controller=controller).get("hdr"))
        effective = replace(
            options,
            codec=effective_codec,
            container=effective_container,
            preserve_hdr=effective_hdr,
            quality="Auto (Default)" if is_preview else options.quality,
            preview_seconds=preview_seconds,
            preview_frames=preview_frames,
        )
        result = convert_video(
            effective_source,
            effective,
            progress=lambda value, message: _emit_progress(progress, value, message),
            output_dir=output_dir,
            controller=controller,
        )
    except Cancelled:
        return None, "Preview cancelled."
    finally:
        if temp_clip:
            try:
                Path(temp_clip).unlink(missing_ok=True)
            except OSError:
                pass

    def finish(media_path: object | None, status: str) -> tuple[object | None, str]:
        if ephemeral_preview:
            try:
                if Path(result.report_path).name.endswith(".report.json"):
                    Path(result.report_path).unlink(missing_ok=True)
            except OSError:
                pass
            try:
                if media_path is not None and (not isinstance(media_path, (str, Path))
                                              or Path(media_path).resolve() != Path(result.output_path).resolve()):
                    Path(result.output_path).unlink(missing_ok=True)
            except OSError:
                pass
        return media_path, status

    source_name = source.name
    if is_preview:
        output_preview = result.output_path
        if preview_frames is not None:
            if preview_frames == 1:
                output_preview = decode_preview_frame(result.output_path, controller=controller)
            stamp = f" f{frame_no} @ {start:.2f}s" if start > 0.05 else ""
            return finish(
                output_preview,
                f"One-frame preview complete{stamp} for {source_name} on {result.gpu} "
                f"in {result.elapsed_seconds:.1f}s. Neural dimensions "
                f"{result.render_width}×{result.render_height}; {result.resize_method}, "
                f"{result.memory_path}.",
            )
        clip_seconds = preview_seconds if preview_seconds is not None else PREVIEW_SECONDS
        start_note = f" from {start:.2f}s" if start > 0.05 else ""
        return finish(
            output_preview,
            f"Preview complete for {source_name}:{start_note} {result.frames} frames from the "
            f"{clip_seconds:g}-second clip processed on {result.gpu} in "
            f"{result.elapsed_seconds:.1f}s. Neural dimensions "
            f"{result.render_width}×{result.render_height}; {result.resize_method}, "
            f"{result.memory_path}.",
        )

    status = (
        f"Complete: {result.frames} frames processed on {result.gpu} in "
        f"{result.elapsed_seconds:.1f}s. All {result.nr_count_evidence} frames returned "
        f"feature-18 success. Neural dimensions {result.render_width}×{result.render_height}; "
        f"{result.resize_method}, {result.memory_path}."
    )
    return finish(result.output_path, status)
