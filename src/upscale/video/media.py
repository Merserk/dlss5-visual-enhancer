from __future__ import annotations

import math
import subprocess
from ...core.ffmpeg.vulkan import prepare_command
import threading
from contextlib import suppress
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ...core import app_log
from ...core.ffmpeg.probe import _run_json
from ...core.jobs import BoundedLogBuffer, Cancelled, drain_bounded_text
from ...core.paths import FFMPEG, FFPROBE

HDR_TAGS = {"smpte2084", "arib-std-b67"}


def positive_fraction(value, default=Fraction(1)):
    try:
        result = Fraction(str(value).replace(":", "/"))
        return result if result > 0 else default
    except (ValueError, ZeroDivisionError):
        return default


class PipeReader:
    """Do not expose the Windows pipe's unusable seek method to libavformat."""
    def __init__(self, stream):
        self.stream = stream

    def read(self, size):
        return self.stream.read(size)


class PipeWriter:
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        view = memoryview(data)
        size = len(view)
        while view:
            n = self.stream.write(view)
            if not n:
                raise BrokenPipeError("Encoder pipe closed.")
            view = view[n:]
        return size

    def flush(self):
        self.stream.flush()


def inspect_video(path: Path, controller=None, *, reject_hdr=False) -> dict:
    data = _run_json([str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_streams",
                      "-show_format", "-of", "json", str(path)], controller=controller)
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("The selected file contains no video stream.")
    stream = streams[0]
    side = stream.get("side_data_list") or []
    hdr = stream.get("color_transfer") in HDR_TAGS or any("dovi" in str(s).lower() or "dolby vision" in str(s).lower() for s in side)
    if reject_hdr and hdr:
        raise ValueError("Existing HDR input is not supported by Upscale. Supply SDR footage; RTX Video HDR converts SDR to new HDR.")
    width, height = int(stream["width"]), int(stream["height"])
    rotation = int(next((s["rotation"] for s in side if "rotation" in s), (stream.get("tags") or {}).get("rotate", 0))) % 360
    if rotation not in (0, 90, 180, 270):
        raise ValueError("Upscale supports rotation metadata in multiples of 90 degrees.")
    raw_sar = stream.get("sample_aspect_ratio", "1:1")
    sar = positive_fraction(raw_sar)
    if rotation in (90, 270):
        width, height, sar = height, width, 1 / sar
    pixel_format = stream.get("pix_fmt", "yuv420p")
    depth = max(c.bits for c in av.VideoFormat(pixel_format).components)
    rate = positive_fraction(stream.get("avg_frame_rate"),
                             positive_fraction(stream.get("r_frame_rate"), Fraction(30)))
    origin = float(data.get("format", {}).get("start_time") or stream.get("start_time") or 0)
    return {"stream": stream, "hdr": hdr, "depth": depth, "rotation": rotation,
            "source_width": width, "source_height": height, "sar": sar,
            "width": math.ceil(width * sar / 2) * 2, "height": math.ceil(height / 2) * 2,
            "rate": rate, "duration": float(data.get("format", {}).get("duration") or stream.get("duration") or 0),
            "origin": origin, "frames": int(stream.get("nb_frames") or 0) if str(stream.get("nb_frames", "0")).isdigit() else 0}


def sdr_normalization_filter(matrix: str, primaries: str, transfer: str, range_in: str) -> str:
    """Convert to the SDK's pure gamma-2.2 RGB without display black scaling.

    libplacebo's BT.1886 conversion assumes a display black point. RTX's CUDA
    output conversion, like zscale, uses ideal gamma 2.4 for BT.709. Decode
    the source curve explicitly before libplacebo handles the gamut/matrix;
    otherwise the two conversions lift shadows even with TrueHDR disabled.
    MAIN runs on nonlinear RGB after YUV decoding, before gamut conversion.
    """
    from ...core.ffmpeg.filters import shader_filter
    srgb_curve = ("vec3 linear = mix(pow((v + 0.055) / 1.055, vec3(2.4)), "
                  "v / 12.92, lessThanEqual(v, vec3(0.04045))); "
                  "return pow(linear, vec3(1.0 / 2.2));")
    curve = {
        "bt470m": "return v;", "gamma22": "return v;",
        "bt470bg": "return pow(v, vec3(2.8 / 2.2));",
        "gamma28": "return pow(v, vec3(2.8 / 2.2));",
        "linear": "return pow(v, vec3(1.0 / 2.2));",
        "iec61966-2-1": srgb_curve, "srgb": srgb_curve,
    }.get(transfer)
    if curve is None:
        if transfer in {"bt709", "smpte170m", "smpte240m", "bt2020-10", "bt2020-12",
                        "iec61966-2-4", "bt1361e"}:
            curve = "return pow(v, vec3(2.4 / 2.2));"
        else:
            # Preserve the existing library conversion for less common curves
            # (e.g. logarithmic footage), rather than assume they are BT.709.
            return (f"zscale=matrixin={matrix}:primariesin={primaries}:transferin={transfer}:rangein={range_in}:"
                    "matrix=gbr:primaries=bt709:transfer=bt470m:range=full")
    shader = """//!HOOK MAIN
//!BIND HOOKED
vec3 rtx_gamma22(vec3 v) {
    v = max(v, vec3(0.0));
    %s
}
vec4 hook() {
    vec4 pixel = HOOKED_tex(HOOKED_pos);
    return vec4(rtx_gamma22(pixel.rgb), pixel.a);
}
""" % curve
    # The shader supplies the transfer conversion. Equal gamma-2.2 tags keep
    # the renderer from adding a second conversion or a display black offset.
    tags = f"setparams=colorspace={matrix}:color_primaries={primaries}:color_trc=bt470m:range={range_in}"
    conversion = shader_filter(shader, options="format=gbrp16le:colorspace=0:color_primaries=bt709:"
                               "color_trc=bt470m:range=pc:gamut_mode=clip")
    return tags + "," + conversion


def decode_filter(meta: dict) -> tuple[str, list[str]]:
    s = meta["stream"]
    sd = int(s["height"]) <= 576
    fallback = "bt470bg" if int(s["height"]) == 576 else "smpte170m" if sd else "bt709"
    assumptions = []
    def color(key, default):
        v = s.get(key)
        if not v or v in ("unknown", "unspecified", "reserved"):
            assumptions.append(f"Unspecified {key}: assumed {default}.")
            return default
        return v
    matrix = color("color_space", "gbr" if av.VideoFormat(s.get("pix_fmt", "yuv420p")).is_rgb else fallback)
    primaries = color("color_primaries", fallback)
    transfer = color("color_transfer", "bt709")
    range_in = "full" if s.get("color_range") == "pc" or av.VideoFormat(s.get("pix_fmt", "yuv420p")).is_rgb else "limited"
    planar = "gbrp10le" if meta["depth"] > 8 else "gbrp"
    # Planar 10-bit has a stable NUT tag across bundled FFmpeg/PyAV versions.
    # Some releases disagree on the packed x2bgr10 tag (decoded as rgb555).
    packed = "gbrp10le" if meta["depth"] > 8 else "rgba"
    filters = [sdr_normalization_filter(matrix, primaries, transfer, range_in), f"format={planar}"]
    if meta["rotation"] == 90:
        filters.append("transpose=cclock")
    elif meta["rotation"] == 270:
        filters.append("transpose=clock")
    elif meta["rotation"] == 180:
        filters += ["hflip", "vflip"]
    filters += [f"scale={meta['width']}:{meta['height']}:flags=lanczos", "setsar=1", f"format={packed}", f"setpts=PTS-({meta['origin']:.9f})/TB"]
    if meta["depth"] > 10:
        assumptions.append("Source precision converted to the SDK's supported 10-bit RGB input.")
    return ",".join(filters), assumptions


def cuda_normalization_supported(meta: dict) -> bool:
    """Use CUDA only for the pixel layouts and color transforms it implements."""
    stream = meta["stream"]
    return (stream.get("pix_fmt") in {"yuv420p", "yuvj420p", "nv12", "yuv420p10le", "p010le", "p010"}
            and stream.get("color_space", "unknown") in {
                "unknown", "unspecified", "reserved", "bt709", "bt470bg", "smpte170m", "bt2020nc"}
            and stream.get("color_primaries", "unknown") in {
                "unknown", "unspecified", "reserved", "bt709", "bt470bg", "smpte170m", "smpte240m", "bt2020"}
            and stream.get("color_transfer", "unknown") in {
                "unknown", "unspecified", "reserved", "bt709", "smpte170m", "smpte240m",
                "bt2020-10", "bt2020-12", "bt470m", "gamma22", "bt470bg", "gamma28",
                "iec61966-2-1", "srgb", "linear"})


def open_rtx_decoder(source, meta, ordinal, controller, *, start_seconds=0.0):
    """Return an owned decoder, first frame, iterator and raw-timestamp flag.

    Prefer NVDEC without a host boundary. Keep the established normalized
    decoder for unsupported CUDA layouts/curves and accurate seek previews.
    """
    from ...core.ffmpeg.frames import open_video_decoder
    from av.codec.hwaccel import HWAccel

    container = None
    try:
        if start_seconds == 0 and cuda_normalization_supported(meta):
            device = HWAccel("cuda", device=str(ordinal), allow_software_fallback=True,
                             options={"primary_ctx": "1"}, is_hw_owned=True)
            try:
                container = av.open(str(source), hwaccel=device)
                stream = container.streams.video[0]
                stream.thread_type = "AUTO"
                stream.codec_context.options = {**stream.codec_context.options, "err_detect": "explode"}
                decoder = iter(container.decode(video=0))
                first = next(decoder)
                if first.is_corrupt:
                    raise RuntimeError("The video decoder returned a corrupt frame.")
                if (first.format.name == "cuda" and
                        first.sw_format.name in {"nv12", "p010", "p010le"}):
                    return container, first, decoder, True
            except av.FFmpegError as exc:
                app_log.info("upscale", f"NVDEC unavailable; using normalized decoder: {exc}")
            if container is not None:
                container.close()
                container = None
        normalization, _ = decode_filter(meta)
        container = open_video_decoder(
            source, controller, pixel_format="gbrp10le" if meta["depth"] > 8 else "rgba",
            video_filter=normalization, start_seconds=start_seconds)
        decoder = iter(container.decode(video=0))
        return container, next(decoder), decoder, False
    except BaseException:
        if container is not None:
            container.close()
        raise


def output_filter(pixel_format: int, hdr: bool) -> str:
    planar = {1: "gbrp", 2: "gbrp10le", 3: "gbrpf32le"}[pixel_format]
    pin = "bt2020" if hdr and pixel_format == 2 else "bt709"
    tin = "linear" if pixel_format == 3 else "smpte2084" if hdr else "bt470m"
    pout, tout, matrix = ("bt2020", "smpte2084", "bt2020nc") if hdr else ("bt709", "bt709", "bt709")
    return (f"format={planar},zscale=matrixin=gbr:primariesin={pin}:transferin={tin}:rangein=full:"
            f"matrix={matrix}:primaries={pout}:transfer={tout}:range=limited" + (":npl=80" if pixel_format == 3 else ""))


def start_decoder(source, meta, controller):
    vf, assumptions = decode_filter(meta)
    command = [str(FFMPEG), "-hide_banner", "-v", "warning", "-xerror", "-copyts", "-noautorotate", "-i", str(source),
               "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", vf, "-c:v", "rawvideo", "-pix_fmt",
               "gbrp10le" if meta["depth"] > 8 else "rgba", "-fps_mode", "passthrough", "-enc_time_base", "demux",
               "-f", "nut", "pipe:1"]
    command = prepare_command(command)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    logs = BoundedLogBuffer(max_tail=60)
    thread = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
    thread.start()
    return process, thread, logs, assumptions


def packed_bytes(frame: av.VideoFrame) -> np.ndarray:
    if frame.format.name == "gbrp10le":
        channels = [np.frombuffer(p, dtype="<u2").reshape(frame.height, p.line_size // 2)[:, :frame.width].astype(np.uint32)
                    for p in frame.planes]
        g,b,r = channels
        return np.ascontiguousarray(r | (g << 10) | (b << 20) | np.uint32(3 << 30))
    if frame.format.name != "rgba":
        raise RuntimeError(f"Unexpected decoder pixel format {frame.format.name}; expected rgba or gbrp10le.")
    p = frame.planes[0]
    return np.ascontiguousarray(np.frombuffer(p, dtype=np.uint8).reshape(frame.height, p.line_size)[:, :frame.width * 4])


def result_frame(data, width, height, pixel_format):
    if pixel_format == 2:
        packed = np.frombuffer(data, dtype="<u4").reshape(height, width)
        out = av.VideoFrame(width, height, "gbrp10le")
        for plane, shift in zip(out.planes, (10, 20, 0)):
            padded = np.zeros((height, plane.line_size // 2), dtype="<u2")
            padded[:, :width] = (packed >> shift) & 1023
            plane.update(padded)
        return out
    if pixel_format == 3:
        rgba = np.frombuffer(data, dtype="<f2").reshape(height, width, 4).astype(np.float32)
        if not np.isfinite(rgba[:, :, :3]).all():
            raise RuntimeError("RTX Video HDR returned non-finite pixels.")
        out = av.VideoFrame(width, height, "gbrpf32le")
        for plane, channel in zip(out.planes, (1, 2, 0)):
            padded = np.zeros((height, plane.line_size // 4), dtype="<f4")
            padded[:, :width] = rgba[:, :, channel]
            plane.update(padded)
        return out
    out = av.VideoFrame(width, height, "rgba")
    p = out.planes[0]
    raw = np.frombuffer(data, dtype=np.uint8).reshape(height, width * 4)
    if p.line_size == width * 4:
        p.update(raw)
    else:
        padded = np.zeros((height, p.line_size), dtype=np.uint8)
        padded[:, :width * 4] = raw
        p.update(padded)
    return out


def finish_process(process, thread, logs, controller, *, expected_stop=False):
    try:
        if expected_stop and process.poll() is None:
            process.terminate()
        process.wait(timeout=120)
        thread.join(timeout=2)
        if controller.cancel.is_set():
            raise Cancelled("Upscale stopped by user.")
        if process.returncode and not expected_stop:
            detail = logs.snapshot() if hasattr(logs, "snapshot") else logs[-60:]
            app_log.error("ffmpeg", "video subprocess failed", "\n".join(detail)[-500:])
            raise RuntimeError("Video subprocess failed:\n" + "\n".join(detail))
    finally:
        controller.unregister(process)


def close_process(process, controller, thread=None):
    """Reap before deleting the job directory, including failed/partial encodes."""
    try:
        if process.poll() is None:
            with suppress(OSError):
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
    finally:
        controller.unregister(process)
        if thread is not None:
            thread.join(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                with suppress(OSError):
                    stream.close()
