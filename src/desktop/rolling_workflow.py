"""Continuous video stages, a bounded encoded spool and one delivery encoder.

GPU sessions and the interpolation timeline live for the entire source. Only
the first stage is spooled; later stages exchange frames through bounded pipes,
so a high-FPS or upscaled later stage cannot create a second full-video cache.
"""
from __future__ import annotations

import itertools
import math
import subprocess
import threading
import time
from collections import deque
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ..core import app_log, ffmpeg
from ..core.disk_paths import OutputFile
from ..core.ffmpeg.audio import plan_audio_streams
from ..core.ffmpeg.encoder import _codec_command
from ..core.ffmpeg.filters import scaling_filter
from ..core.ffmpeg.frames import _Reader, open_video_decoder, packed_frame
from ..core.ffmpeg.nut import RawVideoPacketMuxer
from ..core.ffmpeg.sharpening import sharpening_filter
from ..core.ffmpeg.vulkan import prepare_command
from ..core.jobs import BoundedLogBuffer, Cancelled, active_job, drain_bounded_text
from ..core.paths import FFMPEG
from ..core.rolling_cache import CACHE_BYTES, RollingFrameCache
from ..core.runtime import resolve_output_size
from ..frame_interpolation.models import resolve_target_rate
from ..settings.models import UISettings
from .coloring import has_lut_adjustments, load_cube_lut, save_cube_lut


FILTER_STAGES = {"coloring", "scale_method", "cas_sharpening"}


class _Cancellation:
    def __init__(self, owner, stop):
        self.owner, self.stop = owner, stop

    def is_set(self):
        return self.stop.is_set() or self.owner.is_set()


class _Control:
    def __init__(self, owner):
        self.owner = owner
        self.stop = threading.Event()
        self.cancel = _Cancellation(owner.cancel, self.stop)

    def __getattr__(self, name):
        return getattr(self.owner, name)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Video processing stopped.")


@dataclass(frozen=True)
class VideoState:
    width: int
    height: int
    rate: Fraction
    depth: int
    hdr: bool
    metadata: dict

    @classmethod
    def from_metadata(cls, meta):
        return cls(int(meta["width"]), int(meta["height"]), Fraction(meta["rate"]),
                   int(meta["depth"]), bool(meta["hdr"]), dict(meta))

    def rgb(self, *, width=None, height=None, rate=None, hdr=None):
        hdr = self.hdr if hdr is None else hdr
        width, height = width or self.width, height or self.height
        rate = rate or self.rate
        meta = {**self.metadata, "width": width, "height": height, "rate": rate,
                "fps": float(rate), "depth": 10, "hdr": hdr, "rotation": 0,
                "pixel_format": "gbrp10le", "color_space": "gbr", "color_range": "pc"}
        if hdr != self.hdr:
            meta.update(color_primaries="bt2020", color_transfer="smpte2084")
        return VideoState(width, height, rate, 10, hdr, meta)


def filter_groups(stages):
    groups = []
    for stage in stages:
        if stage in FILTER_STAGES and groups and isinstance(groups[-1], tuple):
            groups[-1] += (stage,)
        else:
            groups.append((stage,) if stage in FILTER_STAGES else stage)
    return groups


def build_filter_group(stages, state, settings, directory, controller):
    filters = []
    for stage in stages:
        if stage == "scale_method":
            w, h = resolve_output_size(state.width, state.height, settings.upscaling_factor)
            filters.append(scaling_filter(w, h, settings.video_scaling_filter))
            state = state.rgb(width=w, height=h)
        elif stage == "coloring":
            lut = directory / "reference.cube"
            if settings.lut_path and not has_lut_adjustments(settings):
                import shutil
                shutil.copyfile(settings.lut_path, lut)
            else:
                save_cube_lut(lut, settings.lut_path or None, settings, controller=controller)
            load_cube_lut(lut)
            filters.append("libplacebo=lut=reference.cube:lut_type=native:deband=0:dithering=-1:format=gbrp10le")
            state = state.rgb()
        elif stage == "cas_sharpening":
            if settings.cas_sharpness:
                transfer = {"iec61966-2-1": "srgb", "linear": "linear"}.get(
                    state.metadata.get("color_transfer"), "bt709")
                filters.append(sharpening_filter(settings.sharpening_method, settings.cas_sharpness, transfer))
            state = state.rgb()
        # Preserve the same 10-bit boundary as the original FFV1 workflow,
        # without writing and decoding a checkpoint at every card.
        filters.append("format=gbrp10le")
    return "ve_gpu," + ",".join(filters), state


def _timing(frame, original):
    frame.pts, frame.time_base, frame.duration = original.pts, original.time_base, original.duration
    return frame


def _color_tags(meta, *, rgb=True):
    primary = meta.get("color_primaries")
    transfer = meta.get("color_transfer")
    primary = primary if primary not in {None, "", "unknown", "unspecified"} else "bt2020" if meta.get("hdr") else "bt709"
    transfer = transfer if transfer not in {None, "", "unknown", "unspecified"} else "smpte2084" if meta.get("hdr") else "bt709"
    matrix = "gbr" if rgb else meta.get("color_space", "bt709")
    if matrix in {"unknown", "unspecified", None, ""}:
        matrix = "bt2020nc" if meta.get("hdr") else "bt709"
    return f"setparams=colorspace={matrix}:range={'full' if rgb else 'limited'}:color_primaries={primary}:color_trc={transfer}"


def _close_process(process, controller, log_thread=None):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    controller.unregister(process)
    if log_thread:
        log_thread.join(timeout=2)
    for pipe in (process.stdin, process.stdout, process.stderr):
        if pipe:
            with suppress(OSError):
                pipe.close()


def pipe_filter(frames, before, after, graph, controller, directory, *, pixel_format="gbrp10le", tag_output=True):
    """Persistent CLI filter; the input writer and output reader run together."""
    frames = iter(frames)
    first = next(frames)
    tags = _color_tags(before.metadata, rgb=first.format.is_rgb)
    colors = ["-color_primaries", "bt2020" if after.hdr else str(after.metadata.get("color_primaries") or "bt709"),
              "-color_trc", "smpte2084" if after.hdr else str(after.metadata.get("color_transfer") or "bt709")]
    for i in (1, 3):
        if colors[i] in {"unknown", "unspecified"}:
            colors[i] = "bt709"
    if not tag_output:
        colors = []
    command = [str(FFMPEG), "-v", "warning", "-xerror", "-copyts", "-probesize", "32", "-analyzeduration", "0",
               "-f", "nut", "-i", "pipe:0", "-map", "0:v:0", "-an",
               "-vf", tags + "," + graph, "-c:v", "rawvideo", "-pix_fmt", pixel_format,
               *colors, "-fps_mode", "passthrough", "-enc_time_base", "demux",
               "-f", "nut", "-write_index", "0", "-flush_packets", "1", "pipe:1"]
    command = prepare_command(command, selection=controller.ffmpeg_device,
                              dimensions=(after.width, after.height), input_format=first.format.name,
                              rate=str(before.rate), time_base=str(first.time_base))
    process = subprocess.Popen(command, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    logs = BoundedLogBuffer(max_tail=60)
    logger = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
    logger.start()
    failure = []
    written = [0]

    def write():
        muxer = None
        try:
            muxer = RawVideoPacketMuxer(process.stdin, width=first.width, height=first.height,
                                       rate=before.rate, time_base=first.time_base, pix_fmt=first.format.name)
            for frame in itertools.chain((first,), frames):
                controller.check()
                if (frame.width, frame.height, frame.format.name) != (first.width, first.height, first.format.name):
                    raise ValueError("Filter input dimensions or sample format changed.")
                pts = round(Fraction(frame.pts) * frame.time_base / muxer.time_base)
                duration = round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base)
                muxer.write(packed_frame(frame), pts, duration)
                written[0] += 1
            muxer.close()
            muxer = None
            process.stdin.close()
        except BaseException as exc:
            failure.append(exc)
            if process.poll() is None:
                process.kill()
        finally:
            if muxer:
                with suppress(Exception):
                    muxer.close()
            close = getattr(frames, "close", None)
            if close:
                close()

    writer = threading.Thread(target=write, name="rolling-filter-input", daemon=True)
    writer.start()
    container = None
    read = 0
    complete = False
    try:
        container = av.open(_Reader(process.stdout), format="nut")
        for frame in container.decode(video=0):
            controller.check()
            if frame.is_corrupt:
                raise RuntimeError("A processing filter produced a corrupt frame.")
            read += 1
            yield frame
        writer.join(timeout=30)
        if writer.is_alive():
            raise RuntimeError("Processing filter input did not finish.")
        if failure:
            raise failure[0]
        process.wait(timeout=30)
        logger.join(timeout=2)
        if process.returncode:
            raise RuntimeError("Video filter failed:\n" + "\n".join(logs.snapshot()))
        if read != written[0]:
            raise RuntimeError(f"Video filter emitted {read} frames for {written[0]} inputs.")
        complete = True
    except (av.error.FFmpegError, OSError) as exc:
        logger.join(timeout=1)
        if failure:
            raise failure[0] from exc
        raise RuntimeError("Video filter failed:\n" + "\n".join(logs.snapshot())) from exc
    finally:
        if not complete:
            controller.stop.set()
            controller.terminate_processes()
        _close_process(process, controller, logger)
        writer.join(timeout=30)
        if container:
            container.close()


def source_frames(source, state, controller, stats, *, pixel_format=None, video_filter=None, directory=None):
    rotation = int(state.metadata.get("rotation") or 0)
    graph = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(rotation, "")
    if video_filter is not None:
        graph = video_filter
    origin = last = None
    count = 0
    with open_video_decoder(source, controller, pixel_format=pixel_format, video_filter=graph, cwd=directory) as decoder:
        for frame in decoder.decode(video=0):
            controller.check()
            if frame.is_corrupt:
                raise RuntimeError("Source decoder returned a corrupt frame.")
            tb = frame.time_base or state.metadata["time_base"]
            stamp = Fraction(frame.pts) * tb if frame.pts is not None else Fraction(count, 1) / state.rate
            if origin is None:
                origin = stamp
            stamp -= origin
            if last is not None and stamp <= last:
                raise ValueError("Source video has non-increasing presentation timestamps.")
            frame.pts = round(stamp / tb)
            frame.time_base = tb
            if not frame.duration:
                frame.duration = max(1, round(Fraction(1, 1) / state.rate / tb))
            last = stamp
            count += 1
            stats["source_frames"] = count
            stats["duration"] = stamp + Fraction(frame.duration) * tb
            yield frame
    declared = int(state.metadata.get("frames") or 0)
    if not count or (declared and declared != count):
        raise RuntimeError(f"Decoded {count} source frames; metadata declares {declared}.")


def neural_frames(frames, state, settings, controller):
    from ..core.gpu_selection import resolve_runtime_ai_gpu
    from ..core.runtime import DLSSFrameSession, prepare_runtime, resize_fit, resolve_native_settings, resolve_upscaling_mode
    from ..neural_rendering.video.guides import TemporalGuideGenerator
    from .workflow import _neural_video_options
    prepared = prepare_runtime()
    gpu = resolve_runtime_ai_gpu(prepared.gpus, prepared.runtime_bundle, settings.ai_gpu_uuid)
    opts = _neural_video_options(settings, state.hdr)
    factor, mode = resolve_upscaling_mode(1.0)
    session = DLSSFrameSession(input_width=state.width, input_height=state.height,
                              output_width=state.width, output_height=state.height,
                              frame_count=None, warmup_frames=opts.warmup_frames, factor=factor, mode=mode,
                              native_settings=resolve_native_settings(opts), composition_mask=opts.nr_mask,
                              gpu=gpu, runtime_bundle=prepared.runtime_bundle, controller=controller)
    guides = TemporalGuideGenerator(session.render_width, session.render_height)
    try:
        for index, frame in enumerate(frames):
            controller.check()
            rgba = ffmpeg.decoded_rgba(frame, state.depth)
            if rgba.shape[:2] != (session.render_height, session.render_width):
                rgba = resize_fit(rgba, session.render_width, session.render_height, controller=controller)
            guide = guides.process(rgba)
            processed, _ = session.process(index=index, rgba=np.ascontiguousarray(rgba),
                                           reset=guide.reset, pts=frame.pts)
            yield _timing(av.VideoFrame.from_ndarray(processed, format="rgba64le" if processed.dtype == np.uint16 else "rgba"), frame)
    finally:
        session.close()


def dlss_frames(frames, state, settings, controller):
    from ..core.dlss_bridge import DLSSSession
    from ..neural_rendering.video.guides import TemporalGuideGenerator
    session = DLSSSession(state.width, state.height, settings.upscale_dlss_mode, settings.upscale_dlss_preset,
                          gpu_uuid=settings.ai_gpu_uuid, even=True)
    guides = TemporalGuideGenerator(state.width, state.height, cut_threshold=.10)
    try:
        for index, frame in enumerate(frames):
            controller.check()
            rgba = np.ascontiguousarray(ffmpeg.decoded_rgba(frame, state.depth))
            guide = guides.process(rgba)
            result = session.process(rgba, reset=guide.reset, phase=index)
            pixels = np.rint(np.clip(result.astype(np.float32), 0, 1) * 65535).astype(np.uint16)
            pixels[..., 3] = 65535
            yield _timing(av.VideoFrame.from_ndarray(pixels, format="rgba64le"), frame)
    finally:
        session.close()


def rtx_frames(frames, state, after, settings, stage, controller, directory, *, normalized_input=False):
    from ..upscale.video.host_pipeline import _PinnedFramePool
    from ..upscale.video.media import packed_bytes, sdr_normalization_filter
    from ..upscale.video.native import RTXVideoSession, FORMAT_R10, FORMAT_RGBA8, FORMAT_YUV422P10, probe_capabilities
    from .workflow import _video_upscale_stage_options
    opts = _video_upscale_stage_options(settings, stage)
    caps = probe_capabilities(settings.ai_gpu_uuid, controller=controller)
    fmt = "gbrp10le" if state.depth > 8 else "rgba"
    # RTX Video accepts gamma-2.2 BT.709 RGB, matching the existing host path.
    primary = state.metadata.get("color_primaries") or "bt709"
    transfer = state.metadata.get("color_transfer") or "bt709"
    primary = "bt709" if primary in {"unknown", "unspecified"} else primary
    transfer = "bt709" if transfer in {"unknown", "unspecified"} else transfer
    source_format = av.VideoFormat(state.metadata.get("pixel_format") or "gbrp10le")
    matrix = "gbr" if source_format.is_rgb else state.metadata.get("color_space")
    if matrix in {None, "", "unknown", "unspecified"}:
        matrix = "bt470bg" if state.height == 576 else "smpte170m" if state.height < 576 else "bt709"
    range_in = "full" if source_format.is_rgb or state.metadata.get("color_range") == "pc" else "limited"
    planar = "gbrp10le" if state.depth > 8 else "gbrp"
    graph = f"ve_gpu,{sdr_normalization_filter(matrix, primary, transfer, range_in)},format={planar},scale={state.width}:{state.height}:flags=lanczos,setsar=1,format={fmt}"
    gamma = replace(state, metadata={**state.metadata, "color_primaries": "bt709", "color_transfer": "bt470m"})
    normalized = (iter(frames) if normalized_input else
                  pipe_filter(frames, state, gamma, graph, controller, directory, pixel_format=fmt, tag_output=False))
    session = RTXVideoSession(state.width, state.height, after.width, after.height, opts,
                              FORMAT_R10 if state.depth > 8 else FORMAT_RGBA8, caps, controller)
    pool = None
    try:
        pool = _PinnedFramePool(int(caps.gpu["cuda_ordinal"]), after.width, after.height,
                                FORMAT_YUV422P10, "yuv422p10le", controller, capacity=1)
        for frame in normalized:
            controller.check()
            slot = pool.acquire()
            try:
                session.process_host_to_host_planar(packed_bytes(frame), output_format=FORMAT_YUV422P10,
                                                    plane_pointers=slot.plane_pointers, strides=slot.strides)
                slot.pts, slot.time_base, slot.duration = frame.pts, frame.time_base, frame.duration
                result, _ = slot.to_av_frame()
            finally:
                slot.release()
            result.colorspace, result.color_range = (9 if after.hdr else 1), 1
            result.color_primaries, result.color_trc = (9, 16) if after.hdr else (1, 1)
            yield result
    finally:
        normalized.close()
        if pool:
            pool.close(abort=controller.cancel.is_set())
        session.close()


def interpolation_frames(frames, state, settings, controller, directory):
    from ..core.gpu_selection import detect_gpu
    from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities
    from ..frame_interpolation.native import DirectDLSSGSession, DLSSGCudaSurface
    from ..frame_interpolation.processor import (DLSSGStage, NearestTimestampWriter, TimedFrame,
                                                _matrix_code, _primaries_code, _range_code, _transfer_code)
    from ..frame_interpolation.scheduler import choose_interpolation_plan, output_frame_count
    target = resolve_target_rate(settings.frame_interpolation_target_fps)
    caps = probe_frame_interpolation_capabilities(settings.ai_gpu_uuid)
    plan = choose_interpolation_plan(state.rate, target, settings.frame_interpolation_engine,
                                     caps.native_multiplier, cfr=bool(state.metadata.get("cfr", True)))
    count = 1 if plan.path == "Native DLSSG" else plan.cascade_stages
    generated = plan.generated_per_interval if plan.path == "Native DLSSG" else 1
    ordinal = int(detect_gpu(settings.ai_gpu_uuid)["cuda_ordinal"])
    colors = {"color_matrix": _matrix_code(state.metadata, hdr=state.hdr),
              "color_range": _range_code(state.metadata), "color_primaries": _primaries_code(state.metadata),
              "color_transfer": _transfer_code(state.metadata), "rotation": 0}
    if state.hdr:
        yuv = replace(state, metadata={**state.metadata, "color_space": "bt2020nc", "color_range": "tv"})
        frames = pipe_filter(frames, state, yuv, "ve_gpu,format=p010le", controller, directory, pixel_format="p010le")
        colors["color_matrix"], colors["color_range"] = 2, 0
    sessions = []
    stages = []
    ready = deque()

    def emit(selected, index):
        pixels = selected.encode_frame
        if isinstance(pixels, DLSSGCudaSurface):
            pixels = pixels.to_host_rgb()
            if state.hdr:
                rgb = np.rint(np.clip(pixels[..., :3].astype(np.float32), 0, 1) * 1023).astype(np.uint16)
                pixels = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb[..., [1, 2, 0]]), format="gbrp10le")
            else:
                pixels = av.VideoFrame.from_ndarray(pixels, format="rgba")
        elif isinstance(pixels, np.ndarray):
            pixels = av.VideoFrame.from_ndarray(pixels, format="rgba")
        # A selected source frame can be emitted more than once; give each its
        # own header so assigning the new PTS never mutates temporal history.
        copied = av.VideoFrame(pixels.width, pixels.height, pixels.format.name)
        for dst, src in zip(copied.planes, pixels.planes):
            if dst.line_size == src.line_size:
                dst.update(src)
            else:
                source = np.frombuffer(src, dtype=np.uint8).reshape(src.height, src.line_size)
                target_data = np.zeros((dst.height, dst.line_size), dtype=np.uint8)
                row = min(src.line_size, dst.line_size)
                target_data[:, :row] = source[:, :row]
                dst.update(target_data)
        copied.pts, copied.time_base, copied.duration = index, Fraction(1, 1) / target, 1
        ready.append(copied)

    # Online nearest-timestamp resampling. The true output count is determined
    # at source EOF rather than decoding the whole video in a preflight scan.
    writer = NearestTimestampWriter(target, 2**63 - 1, emit)
    last = None
    segment = 0
    end = Fraction(0)
    try:
        for i in range(count):
            session = DirectDLSSGSession(state.width, state.height, generated, controller, ordinal, hdr=state.hdr)
            sessions.append(session)
            stages.append(DLSSGStage(session, generated, detect_source_cuts=i == 0,
                                    cuda_output=False, output_p010=state.hdr, colors=colors,
                                    preserve_source_rgb=i == 0))
        for index, frame in enumerate(frames):
            controller.check()
            stamp = Fraction(frame.pts) * frame.time_base
            if last is not None and (stamp <= last or stamp - last > Fraction(2, 1) / state.rate):
                segment += 1
            last = stamp
            end = stamp + (Fraction(frame.duration) * frame.time_base if frame.duration else Fraction(1, 1) / state.rate)
            bridge = frame if state.hdr else np.ascontiguousarray(frame.to_ndarray(format="rgba"))
            items = [TimedFrame(bridge, frame, stamp, segment, "Source", index)]
            for stage in stages:
                items = [produced for item in items for produced in stage.push(item)]
            for item in items:
                writer.push(item)
                while ready:
                    yield ready.popleft()
            del frame, items
        if bool(state.metadata.get("cfr", True)) and last is not None:
            # Matroska's millisecond PTS/duration rounding can make an 18-frame
            # 30-FPS clip look like .601 s. Its CFR duration is exactly .600 s;
            # otherwise ceil(duration * target) introduces an extra last frame.
            end = Fraction(index + 1, 1) / state.rate
        writer.output_count = output_frame_count(end, target)
        writer.finish()
        while ready:
            yield ready.popleft()
    finally:
        ready.clear()
        for session in reversed(sessions):
            session.close()
        close = getattr(frames, "close", None)
        if close:
            close()


def native_stage(stage, frames, state, settings, controller, directory, *, normalized_input=False):
    from .workflow import _video_upscale_stage_options, video_upscale_size
    after = state.rgb()
    if stage == "neural_model":
        transformed = neural_frames(frames, state, settings, controller)
    elif stage == "dlss_super_resolution":
        from ..core.dlss_modes import dlss_output_size
        w, h = dlss_output_size(state.width, state.height, settings.upscale_dlss_mode, even=True)
        after = state.rgb(width=w, height=h)
        transformed = dlss_frames(frames, state, settings, controller)
    elif stage in {"super_resolution", "rtx_video_hdr"}:
        w, h, _ = video_upscale_size(state.width, state.height, _video_upscale_stage_options(settings, stage))
        after = state.rgb(width=w, height=h, hdr=stage == "rtx_video_hdr")
        transformed = rtx_frames(frames, state, after, settings, stage, controller, directory,
                                  normalized_input=normalized_input)
    elif stage == "frame_generation":
        after = state.rgb(rate=resolve_target_rate(settings.frame_interpolation_target_fps))
        transformed = interpolation_frames(frames, state, settings, controller, directory)
    else:
        raise ValueError(f"Unknown rolling processing stage: {stage}.")
    # Output packing/clamping matches the existing FFV1 stage boundaries.
    # RTX's bridge returns tagged YUV, other native engines return RGB.
    raw_meta = after.metadata
    if stage in {"super_resolution", "rtx_video_hdr"}:
        raw_meta = {**raw_meta, "color_space": "bt2020nc" if after.hdr else "bt709", "color_range": "tv"}
    raw = replace(after, metadata=raw_meta)
    return pipe_filter(transformed, raw, after, "ve_gpu,format=gbrp10le", controller, directory), after


def _encode(frames, state, source, destination, settings, controller, progress, stats):
    frames = iter(frames)
    first = next(frames)
    output = OutputFile(destination)
    process = muxer = logger = None
    completed = False
    delivered = 0
    meta = dict(state.metadata)
    if settings.codec != "FFV1 Lossless RGB 10-bit":
        meta["color_space"] = "bt2020nc" if state.hdr else stats["source_metadata"].get("color_space")
        if meta["color_space"] in {None, "", "unknown", "gbr"}:
            meta["color_space"] = "bt709"
        meta["color_range"] = "tv"
    gpu = ffmpeg.resolve_video_gpu((), settings.video_gpu_uuid, settings.codec, state.width, state.height)
    codec_args, selected, _ = _codec_command(settings.codec, settings.quality, state.width, state.height,
                                            float(state.rate), None if gpu is None else int(gpu["cuda_ordinal"]),
                                            hdr_mode=state.hdr, hdr_metadata=meta)
    audio = plan_audio_streams(source, ffmpeg.resolve_container(settings.codec, settings.container), controller)
    tags = _color_tags(state.metadata, rgb=first.format.is_rgb)
    duration = float(state.metadata.get("video_duration") or state.metadata.get("duration") or 0)
    command = [str(FFMPEG), "-v", "warning", "-xerror", "-y", "-probesize", "32", "-analyzeduration", "0",
               "-f", "nut", "-i", "pipe:0",
               *(["-t", f"{duration:.9f}"] if duration > 0 else []), "-i", str(source),
               "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1", "-map_chapters", "1",
               "-vf", tags, *codec_args, *audio.encoder_args(), "-fps_mode", "passthrough",
               str(output.temporary)]
    command = prepare_command(command, selection=settings.ffmpeg_device,
                              dimensions=(state.width, state.height), input_format=first.format.name,
                              rate=str(state.rate), time_base=str(first.time_base))
    logs = BoundedLogBuffer(max_tail=60)
    last_update = 0.0
    expected = max(1, duration * float(state.rate))
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        controller.register(process)
        logger = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
        logger.start()
        muxer = RawVideoPacketMuxer(process.stdin, width=state.width, height=state.height,
                                   rate=state.rate, time_base=first.time_base, pix_fmt=first.format.name)
        for frame in itertools.chain((first,), frames):
            controller.check()
            pts = round(Fraction(frame.pts) * frame.time_base / muxer.time_base)
            frame_duration = round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base)
            muxer.write(packed_frame(frame), pts, frame_duration)
            delivered += 1
            now = time.perf_counter()
            if progress and now - last_update >= .2:
                progress(min(.97, delivered / expected * .97), f"Processing video: {delivered:,} frames")
                last_update = now
        muxer.close()
        muxer = None
        process.stdin.close()
        if progress:
            progress(.98, "Finalizing video")
        while process.poll() is None:
            controller.check()
            try:
                process.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                continue
        logger.join(timeout=2)
        if process.returncode:
            raise RuntimeError("Video export failed:\n" + "\n".join(logs.snapshot()))
        if progress:
            progress(.99, "Verifying output")
        saved = ffmpeg.probe_video(output.temporary, count_mode="packets", controller=controller)
        if (int(saved["frames"]) != delivered or
                (int(saved["width"]), int(saved["height"])) != (state.width, state.height)):
            raise RuntimeError("Rolling output frame count or dimensions do not match the processing pipeline.")
        if state.hdr and (not saved["hdr"] or int(saved["depth"]) < 10):
            raise RuntimeError("Rolling output lost HDR signaling or precision.")
        controller.check()
        output.publish()
        stats.update(output_frames=delivered, encoder=selected)
        completed = True
    except (BrokenPipeError, OSError) as exc:
        if logger:
            logger.join(timeout=1)
        raise RuntimeError("Video export failed:\n" + "\n".join(logs.snapshot())) from exc
    finally:
        if not completed:
            controller.stop.set()
            controller.terminate_processes()
        if muxer:
            with suppress(Exception):
                muxer.close()
        if process:
            _close_process(process, controller, logger)
        output.cleanup()
        close = getattr(frames, "close", None)
        if close:
            close()


def render_rolling_video(source: Path, settings: UISettings, destination: Path,
                         controller, directory: Path, stages, progress=None, *, cache_limit=CACHE_BYTES):
    from .workflow import _export_video
    started = time.perf_counter()
    control = _Control(controller)
    meta = ffmpeg.probe_video(source, count_mode="metadata", controller=controller,
                              inspect_timestamps="frame_generation" in stages)
    state = VideoState.from_metadata(meta)
    groups = filter_groups(stages)
    def direct_progress(value, message):
        if progress:
            progress(min(.99, float(value)), message)
    # Stateless-only workflows can be delivered in one CLI pass. No cache or
    # extra decode is useful here, even when the user enables rolling mode.
    if len(groups) == 1 and isinstance(groups[0], tuple):
        graph, after = build_filter_group(groups[0], state, settings, directory, controller)
        _export_video(source, source, destination, settings, controller, pipeline_hdr=after.hdr,
                      progress=direct_progress, video_filter=graph, output_metadata=after.metadata,
                      working_directory=directory, verify_output=True)
        app_log.info("rolling-cache", f"direct src={source.name} elapsed={time.perf_counter()-started:.3f}s cache=0")
        if progress:
            progress(1.0, "Export complete")
        return str(destination)
    if not groups:
        _export_video(source, source, destination, settings, controller, pipeline_hdr=state.hdr,
                      progress=direct_progress, verify_output=True)
        if progress:
            progress(1.0, "Export complete")
        return str(destination)
    stats = {"source_metadata": meta}
    cache = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with active_job(controller), ExitStack() as resources:
        first_stage = groups[0]
        decode_format = None
        initial_filter = None
        first_filtered_state = None
        if isinstance(first_stage, tuple):
            initial_filter, first_filtered_state = build_filter_group(first_stage, state, settings, directory, control)
            rotation = int(state.metadata.get("rotation") or 0)
            rotate = {90: "transpose=clock,", 180: "hflip,vflip,", 270: "transpose=cclock,"}.get(rotation, "")
            initial_filter = rotate + initial_filter
            decode_format = "gbrp10le"
        elif first_stage in ("super_resolution", "rtx_video_hdr"):
            from ..upscale.video.media import inspect_video, decode_filter
            initial_filter, _ = decode_filter(inspect_video(source, control, reject_hdr=True))
            decode_format = "gbrp10le" if state.depth > 8 else "rgba"
        if first_stage in ("neural_model", "dlss_super_resolution"):
            decode_format = "rgba64le" if state.depth > 8 else "rgba"
        elif first_stage == "frame_generation":
            decode_format = "p010le" if state.hdr else "rgba"
        frames = source_frames(source, state, control, stats, pixel_format=decode_format,
                               video_filter=initial_filter, directory=directory)
        resources.callback(frames.close)
        try:
            for index, group in enumerate(groups):
                before = state
                if isinstance(group, tuple):
                    if index == 0:
                        state = first_filtered_state
                    else:
                        graph, state = build_filter_group(group, state, settings, directory, control)
                        frames = pipe_filter(frames, before, state, graph, control, directory)
                else:
                    if index == 0 and group in ("super_resolution", "rtx_video_hdr"):
                        frames, state = native_stage(group, frames, state, settings, control, directory,
                                                     normalized_input=True)
                    else:
                        frames, state = native_stage(group, frames, state, settings, control, directory)
                resources.callback(frames.close)
                if index == 0 and len(groups) > 1:
                    cache = RollingFrameCache(frames, directory / "rolling-cache", control,
                                               limit=cache_limit, max_frames=max(1, math.ceil(float(state.rate) * 60)),
                                               codec=settings.cache_codec)
                    resources.callback(cache.close)
                    frames = iter(cache)
                    resources.callback(frames.close)
            _encode(frames, state, source, destination, settings, control, progress, stats)
        finally:
            control.stop.set()
            controller.terminate_processes()
    app_log.info("rolling-cache", f"done src={source.name} elapsed={time.perf_counter()-started:.3f}s "
                 f"source_frames={stats.get('source_frames')} output_frames={stats.get('output_frames')} "
                 f"peak_bytes={cache.peak_bytes if cache else 0} limit={cache_limit} "
                 f"cache_waits={cache.waits if cache else 0} codec={settings.cache_codec}")
    if progress:
        progress(1.0, "Export complete")
    return str(destination)
