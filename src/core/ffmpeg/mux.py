from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .. import app_log
from ..jobs import Cancelled, JobController
from ..render_metadata import (
    VIDEO_NOTE_FORMATS, MetadataNoteError, check_cancelled, embedding_warning,
    merge_render_note, record_embedding,
)
from ..paths import FFMPEG, FFPROBE
from .audio import AudioPlan, plan_audio_streams
from .codecs import _hdr_color_args
from .probe import _run_json

def _probe_rendered_duration(path: Path, controller: JobController | None = None) -> float:
    """Read the intermediate video's duration without decoding/counting its frames."""
    data = _run_json(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=duration:format=duration",
            "-of",
            "json",
            str(path),
        ], controller=controller,
    )
    streams = data.get("streams") or []
    values = [
        (streams[0] if streams else {}).get("duration"),
        (data.get("format") or {}).get("duration"),
    ]
    for raw_value in values:
        try:
            duration = float(raw_value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RuntimeError(
        "Could not determine the rendered video's duration for the final audio mux."
    )



def prepare_mux_comment(
    source: Path, container: str, render_note: str | None,
    metadata_diagnostics: dict | None = None,
    controller: JobController | None = None,
) -> str | None:
    """Prepare the optional merged comment before a direct encode/mux pass."""
    check_cancelled(controller)
    if render_note is None or container not in VIDEO_NOTE_FORMATS:
        if render_note is not None:
            record_embedding(metadata_diagnostics, "skipped", reason="unsupported_format")
        elif metadata_diagnostics is not None and not metadata_diagnostics:
            record_embedding(metadata_diagnostics, "not_requested")
        return None
    try:
        comment = merge_render_note(_read_comment(source, controller), render_note)
        if len(comment.encode("utf-16-le")) > 16000:
            raise MetadataNoteError("Existing comment is too large to safely extend")
        return comment
    except Cancelled:
        raise
    except (ValueError, TypeError, RuntimeError) as exc:
        embedding_warning(metadata_diagnostics, exc)
        return None


def verify_mux_comment(
    output: Path, comment: str | None, metadata_diagnostics: dict | None = None,
    controller: JobController | None = None,
) -> None:
    """Verify optional direct-mux metadata without ever remuxing the video."""
    if comment is None:
        return
    try:
        if _read_comment(output, controller) != comment:
            raise MetadataNoteError("Saved video did not retain the settings note")
    except Cancelled:
        raise
    except (ValueError, RuntimeError) as exc:
        embedding_warning(metadata_diagnostics, exc)
        return
    record_embedding(metadata_diagnostics, "embedded", field="format.comment")


def final_mux(
    temp_video: Path,
    source: Path,
    output: Path,
    container: str,
    controller: JobController | None = None,
    preserve_supported_subtitles: bool = False,
    *, render_note: str | None = None, metadata_diagnostics: dict | None = None,
    source_time_origin: float | None = None, audio_diagnostics: dict | None = None,
    video_bitstream_filter: str | None = None,
    video_color_metadata: dict | None = None,
) -> None:
    check_cancelled(controller)
    audio_plan = plan_audio_streams(source, container, controller)
    if audio_diagnostics is not None:
        audio_diagnostics["streams"] = audio_plan.diagnostics()
    if render_note is None or container not in VIDEO_NOTE_FORMATS:
        if render_note is not None:
            record_embedding(metadata_diagnostics, "skipped", reason="unsupported_format")
        elif metadata_diagnostics is not None and not metadata_diagnostics:
            record_embedding(metadata_diagnostics, "not_requested")
        _final_mux_once(temp_video, source, output, container, controller, preserve_supported_subtitles, audio_plan, source_time_origin=source_time_origin, video_bitstream_filter=video_bitstream_filter, video_color_metadata=video_color_metadata)
        return

    try:
        comment = merge_render_note(_read_comment(source, controller), render_note)
        # Windows has a finite command-line length. Never truncate source text.
        if len(comment.encode("utf-16-le")) > 16000:
            raise MetadataNoteError("Existing comment is too large to safely extend")
    except Cancelled:
        raise
    except (ValueError, TypeError, RuntimeError) as exc:
        check_cancelled(controller)
        embedding_warning(metadata_diagnostics, exc)
        _final_mux_once(temp_video, source, output, container, controller, preserve_supported_subtitles, audio_plan, source_time_origin=source_time_origin, video_bitstream_filter=video_bitstream_filter, video_color_metadata=video_color_metadata)
        return

    try:
        _final_mux_once(temp_video, source, output, container, controller,
                        preserve_supported_subtitles, audio_plan, comment=comment, source_time_origin=source_time_origin, video_bitstream_filter=video_bitstream_filter, video_color_metadata=video_color_metadata)
    except Cancelled:
        raise
    except (ValueError, RuntimeError) as exc:
        check_cancelled(controller)
        if isinstance(exc, _MuxFailure) and exc.filesystem_error:
            raise
        # A mux failure caused by optional metadata should not destroy the render.
        # Retry once without the note because there is not yet a valid output.
        embedding_warning(metadata_diagnostics, exc)
        _final_mux_once(temp_video, source, output, container, controller, preserve_supported_subtitles, audio_plan, source_time_origin=source_time_origin, video_bitstream_filter=video_bitstream_filter, video_color_metadata=video_color_metadata)
        return

    # Verification is deliberately read-only.  If the optional settings note did
    # not survive, keep the already-valid mux instead of copying the complete
    # video through a second remux solely to remove metadata.
    try:
        if _read_comment(output, controller) != comment:
            raise MetadataNoteError("Saved video did not retain the settings note")
    except Cancelled:
        raise
    except (ValueError, RuntimeError) as exc:
        embedding_warning(metadata_diagnostics, exc)
        return
    record_embedding(metadata_diagnostics, "embedded", field="format.comment")


def _read_comment(path: Path, controller: JobController | None) -> str | None:
    data = _run_json([
        str(FFPROBE), "-v", "error", "-show_entries", "format_tags=comment",
        "-of", "json", str(path),
    ], controller=controller)
    return next((value for key, value in (data.get("format", {}).get("tags") or {}).items()
                 if key.casefold() == "comment"), None)


class _MuxFailure(RuntimeError):
    def __init__(self, stderr: str):
        super().__init__("Final audio/metadata mux failed:\n" + stderr[-4000:])
        self.filesystem_error = any(message in stderr.casefold() for message in (
            "permission denied", "access is denied", "no space left", "disk full",
            "input/output error", "i/o error", "read-only file system", "error opening output",
        ))


def _final_mux_once(
    temp_video: Path, source: Path, output: Path, container: str,
    controller: JobController | None, preserve_supported_subtitles: bool, audio_plan: AudioPlan,
    *, comment: str | None = None, source_time_origin: float | None = None,
    video_bitstream_filter: str | None = None,
    video_color_metadata: dict | None = None,
) -> None:
    check_cancelled(controller)
    duration = _probe_rendered_duration(temp_video, controller)
    if container == "MKV":
        maps = ["-map", "0:v:0", "-map", "1:a?", "-map", "1:s?"]
        streams = ["-c:v", "copy", *audio_plan.encoder_args(), "-c:s", "copy"]
    else:
        maps = ["-map", "0:v:0", "-map", "1:a?"]
        streams = [
            "-c:v",
            "copy",
            *audio_plan.encoder_args(),
            "-movflags",
            "+faststart",
        ]
        if preserve_supported_subtitles:
            subtitle_data = _run_json(
                [
                    str(FFPROBE),
                    "-v",
                    "error",
                    "-select_streams",
                    "s",
                    "-show_entries",
                    "stream=index,codec_name",
                    "-of",
                    "json",
                    str(source),
                ], controller=controller,
            )
            supported_text = {"ass", "ssa", "subrip", "mov_text", "webvtt", "text"}
            subtitle_indices = [
                int(stream["index"])
                for stream in subtitle_data.get("streams") or []
                if stream.get("codec_name") in supported_text
            ]
            for stream_index in subtitle_indices:
                maps.extend(["-map", f"1:{stream_index}"])
            if subtitle_indices:
                streams.extend(["-c:s", "mov_text"])
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        *(["-copyts"] if source_time_origin is not None else []),
        "-i",
        str(temp_video),
        "-t",
        f"{duration:.9f}",
        *(["-itsoffset", f"{-source_time_origin:.9f}"] if source_time_origin is not None else []),
        "-i",
        str(source),
        *maps,
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        *streams,
        *_hdr_color_args(video_color_metadata),
        *(["-bsf:v", video_bitstream_filter] if video_bitstream_filter else []),
        *(["-avoid_negative_ts", "disabled", "-metadata:s:v:0", "rotate=0"] if source_time_origin is not None else []),
        *(["-metadata", "comment=" + comment] if comment is not None else []),
        str(output),
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if controller is not None:
        controller.register(process)
    try:
        while True:
            check_cancelled(controller)
            try:
                _stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        check_cancelled(controller)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if controller is not None:
            controller.unregister(process)
    if process.returncode:
        app_log.error("ffmpeg-mux", "final audio/metadata mux failed", stderr[-500:])
        raise _MuxFailure(stderr)
