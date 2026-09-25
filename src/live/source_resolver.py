from __future__ import annotations

import json
import av
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

from ..core import app_log
from ..core.jobs import Cancelled, JobController
from ..core.paths import FFPROBE, YTDLP
from .models import LIVE_SOURCE_QUALITY_CHOICES, ResolvedSource


def run_capture(command: list[str], controller: JobController | None = None,
                timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Drain both pipes while allowing Stop during resolution and probing."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller:
        controller.register(process)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if controller and controller.cancel.is_set():
                raise Cancelled("Live stopped.")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{Path(command[0]).stem} timed out after {timeout:g} seconds.")
            try:
                out, err = process.communicate(timeout=0.2)
                if controller and controller.cancel.is_set():
                    raise Cancelled("Live stopped.")
                return subprocess.CompletedProcess(command, process.returncode,
                    out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.communicate(timeout=5)
        if controller:
            controller.unregister(process)


def _classify(raw: str) -> str:
    text = raw.strip().strip('"')
    if not text:
        raise ValueError("Enter a local video path, direct stream URL, YouTube or Twitch URL.")
    if Path(text).is_file():
        return "file"
    parsed = urlparse(text)
    host = (parsed.hostname or "").casefold()
    for domain, kind in (("youtube.com", "youtube"), ("youtu.be", "youtube"), ("twitch.tv", "twitch")):
        if host == domain or host.endswith("." + domain):
            return kind
    if parsed.scheme in ("http", "https", "rtmp", "rtmps", "rtsp", "srt"):
        return "direct"
    raise ValueError("Source is not an existing video file or a supported stream URL.")


def _resolved_metadata(kind: str, data: dict) -> ResolvedSource:
    formats = data.get("requested_formats") or [data]
    video = next((f for f in formats if f.get("vcodec") != "none" and f.get("url")), None)
    if video is None:
        raise RuntimeError("yt-dlp returned no playable video for this page.")
    audio = next((f for f in formats if f.get("vcodec") == "none" and f.get("acodec") != "none"), None)
    # Muxed Twitch/HLS already contains audio. A second connection can join at
    # a different live edge and desynchronize it from the video.
    return ResolvedSource(kind, str(data.get("title") or data.get("id") or kind),
        video["url"], audio.get("url") if audio else None,
        bool(data.get("is_live") or data.get("live_status") == "is_live"),
        dict(video.get("http_headers") or data.get("http_headers") or {}),
        dict((audio or {}).get("http_headers") or data.get("http_headers") or {}))


def resolve_source(raw: str, max_height: int,
                   controller: JobController | None = None, *,
                   source_quality: str = "Auto") -> ResolvedSource:
    if source_quality not in LIVE_SOURCE_QUALITY_CHOICES:
        raise ValueError("Choose a valid Source quality.")
    kind = _classify(raw)
    text = raw.strip().strip('"')
    if kind == "file":
        path = Path(text).resolve()
        return ResolvedSource(kind, path.name, str(path), None, False)
    if kind == "direct":
        path = urlparse(text).path
        live = Path(path).suffix.lower() not in (".mp4", ".mkv", ".mov", ".webm", ".avi")
        return ResolvedSource(kind, Path(path).name or "Network stream", text, None, live)
    if not YTDLP.is_file():
        raise RuntimeError(f"yt-dlp is missing: {YTDLP}")
    source_height = max_height if source_quality == "Auto" else int(source_quality)
    result = run_capture([str(YTDLP), "--no-playlist", "--no-warnings", "--skip-download",
        "--dump-single-json", "--socket-timeout", "15", "--retries", "2", "-f",
        f"bv*[height<={source_height}]+ba/b[height<={source_height}]/b", text], controller, 90)
    if result.returncode:
        detail = "\n".join(result.stderr.strip().splitlines()[-4:])
        if "offline" in detail.lower() or "not currently live" in detail.lower():
            app_log.error("live-yt-dlp", "channel offline", detail)
            raise RuntimeError("This channel is currently offline. Try again when the broadcast is live.")
        app_log.error("live-yt-dlp", "could not resolve video", detail)
        raise RuntimeError(f"Could not resolve this video: {detail}")
    return _resolved_metadata(kind, json.loads(result.stdout))


def input_args(url: str, headers: dict[str, str], timeout: float, *, reconnect: bool = True) -> list[str]:
    if not url.startswith(("http://", "https://", "rtmp://", "rtmps://", "rtsp://", "srt://")):
        return []
    args = ["-rw_timeout", str(int(timeout * 1_000_000))]
    if url.startswith(("http://", "https://")):
        if reconnect:
            args += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_on_network_error", "1",
                     "-reconnect_delay_max", "3", "-reconnect_max_retries", "3"]
        clean = {str(k).replace("\r", "").replace("\n", ""): str(v).replace("\r", "").replace("\n", "")
                 for k, v in headers.items()}
        if clean:
            args += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in clean.items())]
    return args


def probe_source(source: ResolvedSource, controller: JobController, timeout: float) -> dict:
    result = run_capture([str(FFPROBE), "-v", "error",
        *input_args(source.video_url, source.video_headers, timeout, reconnect=False),
        "-analyzeduration", "2000000", "-probesize", "5000000", "-show_streams", "-show_format",
        "-of", "json", source.video_url], controller, timeout + 10)
    if result.returncode:
        app_log.error("live-ffprobe", "source probe failed", result.stderr.strip()[-500:])
        raise RuntimeError(f"Source probe failed: {result.stderr.strip()[-2000:]}")
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise RuntimeError("This source contains no decodable video.")
    rotation = int((video.get("tags") or {}).get("rotate") or 0)
    for side in video.get("side_data_list") or []:
        rotation = int(side.get("rotation", rotation))
    width, height = int(video["width"]), int(video["height"])
    if rotation % 360 in (90, 270):
        width, height = height, width

    def rate(key: str) -> Fraction:
        try:
            return Fraction(video.get(key) or "0")
        except (ValueError, ZeroDivisionError):
            return Fraction(0)

    avg, nominal = rate("avg_frame_rate"), rate("r_frame_rate")
    fps = nominal if nominal > 0 and (avg <= 0 or abs(float(avg / nominal) - 1) < 0.001) else avg
    if not 1 <= fps <= 240:
        fps = Fraction(30)
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    if source.kind == "direct" and duration > 0:
        source.is_live = False
    pixel_format = video.get("pix_fmt") or "unknown"
    try:
        depth = max(component.bits for component in av.VideoFormat(pixel_format).components)
    except (ValueError, TypeError):
        depth = int(video.get("bits_per_raw_sample") or 8)
    return {"width": width, "height": height, "coded_width": int(video["width"]),
            "coded_height": int(video["height"]), "rotation": rotation % 360,
            "rate": fps, "duration": duration, "pixel_format": pixel_format, "depth": depth,
            "chroma_location": video.get("chroma_location") or "unknown",
            "color_space": video.get("color_space") or "unknown",
            "color_range": video.get("color_range") or "unknown",
            "color_primaries": video.get("color_primaries") or "unknown",
            "color_transfer": video.get("color_transfer") or "unknown",
            "has_audio": bool(source.audio_url) or any(s.get("codec_type") == "audio" for s in data.get("streams", []))}
