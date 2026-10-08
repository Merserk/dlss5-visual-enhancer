"""Timestamped video stages and one delivery encoder for rolling exports."""
from __future__ import annotations

import itertools
import math
import subprocess
import tempfile
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
from ..core.ffmpeg.preview import decode_timeline_frame
from ..core.ffmpeg.nut import RawVideoPacketMuxer
from ..core.ffmpeg.sharpening import sharpening_filter
from ..core.ffmpeg.grain import GrainOptions, grain_filter
from ..core.ffmpeg.vulkan import prepare_command, quote_filter_path
from ..core.frame_process import spawn_frame_process
from ..core.jobs import BoundedLogBuffer, Cancelled, active_job, drain_bounded_text
from ..core.paths import FFMPEG
from ..core.runtime import resolve_output_size
from ..frame_interpolation.models import resolve_target_rate
from ..settings.models import UISettings
from .coloring import has_lut_adjustments, load_cube_lut, save_cube_lut

FILTER_STAGES = {"coloring", "scale_method", "cas_sharpening", "grain"}

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
        self.owner.check()

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
            meta.update(color_primaries="bt2020" if hdr else "bt709",
                        color_transfer="smpte2084" if hdr else "bt709")
        return VideoState(width, height, rate, 10, hdr, meta)

def filter_groups(stages):
    groups = []
    for stage in stages:
        if stage in FILTER_STAGES and groups and isinstance(groups[-1], tuple):
            groups[-1] += (stage,)
        else:
            groups.append((stage,) if stage in FILTER_STAGES else stage)
    return groups

def build_filter_group(stages, state, settings, directory, controller, *, grain_phase=0):
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
            filters.append("libplacebo=lut=" + quote_filter_path(lut.resolve()) +
                           ":lut_type=native:deband=0:dithering=-1:format=gbrp10le")
            state = state.rgb()
        elif stage == "cas_sharpening":
            if settings.cas_sharpness:
                transfer = {"iec61966-2-1": "srgb", "linear": "linear"}.get(
                    state.metadata.get("color_transfer"), "bt709")
                filters.append(sharpening_filter(settings.sharpening_method, settings.cas_sharpness, transfer))
            state = state.rgb()
        elif stage == "grain":
            graph = grain_filter(GrainOptions.from_settings(settings), hdr=state.hdr, frame_offset=grain_phase)
            if graph:
                filters.append(graph)
            state = state.rgb()
        # Preserve the same 10-bit boundary as the original FFV1 workflow,
        # without writing and decoding a checkpoint at every card.
        filters.append("format=gbrp10le")
    return "ve_gpu," + ",".join(filters), state

def _timing(frame, original):
    frame.pts, frame.time_base, frame.duration = original.pts, original.time_base, original.duration
    frame.opaque = original.opaque
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
    process = spawn_frame_process(command, cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    controller.register(process)
    logs = BoundedLogBuffer(max_tail=60)
    logger = threading.Thread(target=drain_bounded_text, args=(process.stderr, logs), daemon=True)
    logger.start()
    failure = []
    written = [0]
    import queue
    timing_queue = queue.Queue(maxsize=16)

    def write():
        nonlocal first
        muxer = None
        try:
            muxer = RawVideoPacketMuxer(process.stdin, width=first.width, height=first.height,
                                       rate=before.rate, time_base=first.time_base, pix_fmt=first.format.name)
            geometry = first.width, first.height, first.format.name
            sequence = itertools.chain((first,), frames)
            first = None
            for frame in sequence:
                controller.check()
                if (frame.width, frame.height, frame.format.name) != geometry:
                    raise ValueError("Filter input dimensions or sample format changed.")
                pts = round(Fraction(frame.pts) * frame.time_base / muxer.time_base)
                duration = round(Fraction(frame.duration or 0) * frame.time_base / muxer.time_base)
                while True:
                    controller.check()
                    try:
                        timing_queue.put((frame.pts, frame.time_base, frame.duration, frame.opaque), timeout=.05)
                        break
                    except queue.Full:
                        continue
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
        container = av.open(_Reader(process.stdout), format="nut", options={"probesize": "32", "analyzeduration": "0"})
        for frame in container.decode(video=0):
            controller.check()
            if frame.is_corrupt:
                raise RuntimeError("A processing filter produced a corrupt frame.")
            original_pts, original_base, original_duration, opaque = timing_queue.get(timeout=30)
            frame.pts, frame.time_base, frame.duration, frame.opaque = original_pts, original_base, original_duration, opaque
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
        if controller.cancel.is_set():
            raise Cancelled("Video processing stopped.") from exc
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

def source_frames(source, state, controller, stats, *, pixel_format=None, video_filter=None, directory=None,
                  cuda_device=None, rtx_metadata=None, resume=None):
    rotation = int(state.metadata.get("rotation") or 0)
    graph = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(rotation, "")
    if video_filter is not None:
        graph = video_filter
    cursor = resume if resume is not None else {}
    origin, last = cursor.get("origin"), cursor.get("last")
    count = cursor.get("count", 0)
    absolute = cursor.get("absolute")
    checkpoint = absolute
    decoded_frames = None
    if rtx_metadata is not None and cuda_device is not None:
        from ..upscale.video.media import open_rtx_decoder
        decoder, first, remaining, _ = open_rtx_decoder(source, rtx_metadata, cuda_device, controller)
        decoded_frames = itertools.chain((first,), remaining)
    elif cuda_device is None:
        decoder = open_video_decoder(source, controller, pixel_format=pixel_format, video_filter=graph, cwd=directory,
                                     start_seconds=max(0.0, float(absolute or 0)), seek_timestamp=resume is not None,
                                     gpu_threads=2 if resume is not None else None)
    else:
        from av.codec.hwaccel import HWAccel
        # Open FFmpeg's CUDA primary context before the neural session, as in
        # the standalone NVDEC path. Rotation is handled by the native bridge.
        device = HWAccel("cuda", device=str(cuda_device), allow_software_fallback=True,
                         options={"primary_ctx": "1"}, is_hw_owned=True)
        decoder = av.open(str(source), hwaccel=device)
        decoder.streams.video[0].thread_type = "AUTO"
        if checkpoint is not None:
            stream = decoder.streams.video[0]
            decoder.seek(max(0, int(checkpoint / stream.time_base)), stream=stream, backward=True)
    with decoder:
        for frame in decoded_frames if decoded_frames is not None else decoder.decode(video=0):
            controller.check()
            if frame.is_corrupt:
                raise RuntimeError("Source decoder returned a corrupt frame.")
            if rtx_metadata is None and cuda_device is not None and frame.format.name != "cuda" and rotation:
                # PyAV's software fallback does not autorotate like the CLI
                # decoder. Keep the same oriented dimensions in both cases.
                from ..core.runtime import rotate_frame
                pixels = rotate_frame(ffmpeg.decoded_rgba(frame, state.depth), rotation)
                frame = _timing(av.VideoFrame.from_ndarray(
                    pixels, format="rgba64le" if pixels.dtype == np.uint16 else "rgba"), frame)
            tb = frame.time_base or state.metadata["time_base"]
            stamp = Fraction(frame.pts) * tb if frame.pts is not None else Fraction(count, 1) / state.rate
            if checkpoint is not None and stamp <= checkpoint:
                # The resumed decoder starts before the checkpoint to retain
                # reference pictures. Its preroll must not be owned twice.
                continue
            absolute = stamp
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
            cursor.update(origin=origin, last=last, absolute=absolute, count=count)
            yield frame
    declared = int(state.metadata.get("frames") or 0)
    if not count or (declared and declared != count):
        raise RuntimeError(f"Decoded {count} source frames; metadata declares {declared}.")

def neural_frames(frames, state, settings, controller, *, phase_origin=0):
    from ..core.gpu_selection import resolve_runtime_ai_gpu
    from ..core.runtime import DLSSFrameSession, prepare_runtime, resize_fit, resolve_native_settings, resolve_upscaling_mode
    from ..neural_rendering.video.guides import TemporalGuideGenerator
    from ..neural_rendering.video.cuda_pipeline import _matrix_code, _range_code
    from .workflow import _neural_video_options
    frames = iter(frames)
    first = next(frames)
    prepared = prepare_runtime()
    gpu = resolve_runtime_ai_gpu(prepared.gpus, prepared.runtime_bundle, settings.ai_gpu_uuid)
    opts = _neural_video_options(settings, state.hdr)
    factor, mode = resolve_upscaling_mode(1.0)
    session = DLSSFrameSession(input_width=state.width, input_height=state.height,
                              output_width=state.width, output_height=state.height,
                              frame_count=None, warmup_frames=opts.warmup_frames, factor=factor, mode=mode,
                              native_settings=resolve_native_settings(opts), control_mask=opts.nr_mask,
                              gpu=gpu, runtime_bundle=prepared.runtime_bundle, controller=controller,
                              cuda_video=True)
    guides = TemporalGuideGenerator(session.render_width, session.render_height)
    matrix, color_range = _matrix_code(state.metadata), _range_code(state.metadata)
    output = np.empty((state.height, state.width, 4), dtype=np.uint16 if state.depth > 8 else np.uint8)
    try:
        sequence = itertools.chain((first,), frames)
        first = None
        for index, frame in enumerate(sequence, start=phase_origin):
            controller.check()
            if frame.format.name == "cuda":
                score, reset = session.score_cuda_frame(frame, color_matrix=matrix, color_range=color_range)
                prepared_frame = frame
                rotation = int(state.metadata.get("rotation") or 0)
            else:
                rgba = ffmpeg.decoded_rgba(frame, state.depth)
                if rgba.shape[:2] != (session.render_height, session.render_width):
                    rgba = resize_fit(rgba, session.render_width, session.render_height, controller=controller)
                guide = guides.process(rgba)
                score, reset = guide.scene_score, guide.reset
                prepared_frame, rotation = np.ascontiguousarray(rgba), 0
            # Return the final packed result for the cache and filters.
            # Software decoding also keeps motion guidance on the GPU,
            # instead of selecting full-resolution CPU DIS and NumPy.
            processed, _ = session.process_frame_to_host(
                index=index, frame=prepared_frame, reset=reset, scene_score=score, pts=frame.pts,
                color_matrix=matrix, color_range=color_range, rotation=rotation,
                chroma_location=ffmpeg.chroma_location_code(state.metadata), output_buffer=output)
            yield _timing(av.VideoFrame.from_ndarray(processed, format="rgba64le" if processed.dtype == np.uint16 else "rgba"), frame)
    except BaseException:
        session.abort()
        raise
    finally:
        session.close()

def dlss_frames(frames, state, settings, controller, *, phase_origin=0):
    from ..core.dlss_bridge import DLSSSession
    from ..neural_rendering.video.guides import TemporalGuideGenerator
    session = DLSSSession(state.width, state.height, settings.upscale_dlss_mode, settings.upscale_dlss_preset,
                          gpu_uuid=settings.ai_gpu_uuid, even=True,
                          optical_flow_quality=settings.upscale_optical_flow_quality)
    guides = TemporalGuideGenerator(state.width, state.height, cut_threshold=.10)
    try:
        for index, frame in enumerate(frames, start=phase_origin):
            controller.check()
            rgba = np.ascontiguousarray(ffmpeg.decoded_rgba(frame, state.depth))
            guide = guides.process(rgba)
            pixels = session.process(rgba, reset=guide.reset, phase=index, output_dtype=np.uint16)
            yield _timing(av.VideoFrame.from_ndarray(pixels, format="rgba64le"), frame)
    finally:
        session.close()

def rtx_frames(frames, state, after, settings, stage, controller, directory, *, normalized_input=False):
    from ..upscale.video.host_pipeline import _PinnedFramePool
    from ..upscale.video.media import sdr_normalization_filter
    from ..upscale.video.native import RTXVideoSession, FORMAT_R10, FORMAT_RGBA8, FORMAT_GBRP10, probe_capabilities
    from ..upscale.video.cuda_pipeline import (_matrix_code, _range_code, _primaries_code,
                                              _transfer_code, _chroma_location_code)
    from .workflow import _video_upscale_stage_options
    opts = _video_upscale_stage_options(settings, stage)
    caps = probe_capabilities(settings.ai_gpu_uuid, controller=controller)
    fmt = "gbrp10le" if state.depth > 8 else "rgba"
    incoming = iter(frames)
    try:
        first = next(incoming)
    except StopIteration:
        return
    def complete_input():
        nonlocal first
        yield first
        first = None
        yield from incoming
    sequence = complete_input()
    cuda_input = first.format.name == "cuda"
    if cuda_input or normalized_input:
        normalized = sequence
    else:
        # Build the host normalizer only when needed; creating its shader also
        # performs filesystem work that the direct CUDA route does not need.
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
        normalized = pipe_filter(sequence, state, gamma, graph, controller, directory, pixel_format=fmt, tag_output=False)
    cuda_metadata = {"stream": {**state.metadata, "pix_fmt": state.metadata.get("pixel_format", "yuv420p"),
                                "height": state.metadata.get("coded_height", state.height)},
                     "height": state.height}
    session = RTXVideoSession(state.width, state.height, after.width, after.height, opts,
                              FORMAT_R10 if state.depth > 8 else FORMAT_RGBA8, caps, controller)
    pool = None
    try:
        pool = _PinnedFramePool(int(caps.gpu["cuda_ordinal"]), after.width, after.height,
                                FORMAT_GBRP10, "gbrp10le", controller, capacity=1)
        for frame in normalized:
            controller.check()
            slot = pool.acquire()
            try:
                if frame.format.name == "cuda":
                    session.process_cuda_to_host_planar(
                        frame, output_format=FORMAT_GBRP10,
                        plane_pointers=slot.plane_pointers, strides=slot.strides,
                        color_matrix=_matrix_code(cuda_metadata), color_range=_range_code(cuda_metadata),
                        color_primaries=_primaries_code(cuda_metadata), color_transfer=_transfer_code(cuda_metadata),
                        chroma_location=_chroma_location_code(cuda_metadata),
                        rotation=int(state.metadata.get("rotation") or 0))
                else:
                    session.process_host_to_host_planar(frame, output_format=FORMAT_GBRP10,
                                                        plane_pointers=slot.plane_pointers, strides=slot.strides)
                slot.pts, slot.time_base, slot.duration = frame.pts, frame.time_base, frame.duration
                result, _ = slot.to_av_frame()
            finally:
                slot.release()
            result.colorspace, result.color_range = 0, 2
            result.opaque = frame.opaque
            result.color_primaries, result.color_trc = (9, 16) if after.hdr else (1, 1)
            yield result
    finally:
        with ExitStack() as cleanup:
            cleanup.callback(session.close)
            if pool:
                cleanup.callback(pool.close, abort=controller.cancel.is_set())
            cleanup.callback(sequence.close)
            cleanup.callback(normalized.close)

def interpolation_frames(frames, state, settings, controller, directory, *, window_end=None, last_window=True,
                         window_descriptor=None, window_start=Fraction(0), window_max_end=None):
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
    bound = window_end or window_max_end
    writer = NearestTimestampWriter(target, output_frame_count(bound, target) + 1 if bound is not None else 2**63 - 1, emit)
    last = None
    segment = 0
    end = Fraction(0)
    items = []
    item = frame = bridge = None
    try:
        for i in range(count):
            session = DirectDLSSGSession(state.width, state.height, generated, controller, ordinal, hdr=state.hdr,
                                        optical_flow_quality=settings.frame_interpolation_optical_flow_quality)
            sessions.append(session)
            stages.append(DLSSGStage(session, generated, detect_source_cuts=i == 0,
                                    cuda_output=False, output_p010=state.hdr, colors=colors,
                                    preserve_source_rgb=i == 0))
        for index, frame in enumerate(frames):
            controller.check()
            if window_descriptor is not None and Path(window_descriptor).is_file():
                import json
                window = json.loads(Path(window_descriptor).read_text(encoding="utf-8"))
                writer.output_count = output_frame_count(Fraction(window["end"]), target) + int(not window["last"])
            stamp = Fraction(frame.pts) * frame.time_base
            if last is None:
                from ..core.rolling_cache import HISTORY_FRAMES
                history_start = window_start - Fraction(HISTORY_FRAMES, 1) / state.rate
                writer.next_index = max(0, math.ceil(stamp * target), math.ceil(history_start * target))
            if last is not None and (stamp <= last or stamp - last > Fraction(2, 1) / state.rate):
                segment += 1
            last = stamp
            end = stamp + (Fraction(frame.duration) * frame.time_base if frame.duration else Fraction(1, 1) / state.rate)
            bridge = frame if state.hdr else np.ascontiguousarray(frame.to_ndarray(format="rgba"))
            items = [TimedFrame(bridge, frame, stamp, segment, "Source", index)]
            for stage in stages:
                items = [produced for item in items for produced in stage.push(item)]
            for item in items:
                for _ in writer.iter_push(item):
                    while ready:
                        yield ready.popleft()
            frame = bridge = item = None
            items = []
        if window_descriptor is not None:
            import json
            window = json.loads(Path(window_descriptor).read_text(encoding="utf-8"))
            window_end, last_window = Fraction(window["end"]), bool(window["last"])
        if window_end is not None:
            end = window_end
        elif bool(state.metadata.get("cfr", True)) and last is not None:
            # Matroska's millisecond PTS/duration rounding can make an 18-frame
            # 30-FPS clip look like .601 s. Its CFR duration is exactly .600 s;
            # otherwise ceil(duration * target) introduces an extra last frame.
            end = Fraction(index + 1, 1) / state.rate
        writer.output_count = output_frame_count(end, target) + int(window_end is not None and not last_window)
        for _ in writer.iter_finish():
            while ready:
                yield ready.popleft()
    finally:
        ready.clear()
        # The bridge deliberately defers destruction while a retained RGB
        # surface exists. Return writer/stage history before closing sessions.
        retained = [writer.previous, item, *items, *(stage.previous for stage in stages)]
        surfaces = {}
        for retained_frame in retained:
            if retained_frame is not None:
                for pixels in (retained_frame.bridge_frame, retained_frame.encode_frame):
                    if isinstance(pixels, DLSSGCudaSurface):
                        surfaces[id(pixels)] = pixels
        writer.previous = None
        for stage in stages:
            stage.previous = None
        items.clear()
        item = frame = bridge = None
        with ExitStack() as cleanup:
            close = getattr(frames, "close", None)
            if close:
                cleanup.callback(close)
            for session in sessions:
                cleanup.callback(session.close, abort=controller.cancel.is_set())
            for pixels in surfaces.values():
                cleanup.callback(pixels.close)

def native_output_state(stage, state, settings):
    from .workflow import _video_upscale_stage_options, video_upscale_size
    after = state.rgb()
    if stage == "dlss_super_resolution":
        from ..core.dlss_modes import dlss_output_size
        w, h = dlss_output_size(state.width, state.height, settings.upscale_dlss_mode, even=True)
        after = state.rgb(width=w, height=h)
    elif stage in {"super_resolution", "rtx_video_hdr"}:
        w, h, _ = video_upscale_size(state.width, state.height, _video_upscale_stage_options(settings, stage))
        after = state.rgb(width=w, height=h, hdr=stage == "rtx_video_hdr")
    elif stage == "frame_generation":
        after = state.rgb(rate=resolve_target_rate(settings.frame_interpolation_target_fps))
    elif stage != "neural_model":
        raise ValueError(f"Unknown rolling processing stage: {stage}.")
    return after

def native_stage(stage, frames, state, settings, controller, directory, *, normalized_input=False,
                 normalize_output=True, window_end=None, last_window=True, phase_origin=0, window_descriptor=None,
                 window_start=Fraction(0), window_max_end=None):
    after = native_output_state(stage, state, settings)
    if stage == "neural_model":
        transformed = neural_frames(frames, state, settings, controller, phase_origin=phase_origin)
    elif stage == "dlss_super_resolution":
        transformed = dlss_frames(frames, state, settings, controller, phase_origin=phase_origin)
    elif stage in {"super_resolution", "rtx_video_hdr"}:
        transformed = rtx_frames(frames, state, after, settings, stage, controller, directory,
                                  normalized_input=normalized_input)
    elif stage == "frame_generation":
        transformed = interpolation_frames(frames, state, settings, controller, directory,
                                           window_end=window_end, last_window=last_window,
                                           window_descriptor=window_descriptor, window_start=window_start,
                                           window_max_end=window_max_end)
    else:
        raise ValueError(f"Unknown rolling processing stage: {stage}.")
    if not normalize_output or stage in {"super_resolution", "rtx_video_hdr"}:
        return transformed, after
    # Other engines use the established RGB packing/clamping boundary.
    raw = after
    return pipe_filter(transformed, raw, after, "ve_gpu,format=gbrp10le", controller, directory), after

def render_preview_frame(source: Path, settings: UISettings, controller,
                         start_seconds: float, stages, metadata: dict) -> np.ndarray:
    """Process one parked frame in memory; the caller maps HDR for display."""
    from ..core.paths import JOBS

    state = VideoState.from_metadata(metadata)
    pixels = decode_timeline_frame(source, metadata, start_seconds=start_seconds,
                                   controller=controller)
    frame = av.VideoFrame.from_ndarray(pixels, format="rgba64le" if pixels.dtype == np.uint16 else "rgba")
    frame.pts = 0
    frame.time_base = Fraction(1, 1) / state.rate
    frame.duration = 1
    frames = iter((frame,))
    with active_job(controller), ExitStack() as resources:
        control = _Control(controller)
        JOBS.mkdir(parents=True, exist_ok=True)
        directory = Path(resources.enter_context(
            tempfile.TemporaryDirectory(prefix="visual-frame-preview-", dir=JOBS)))
        for group in filter_groups(stages):
            before = state
            if isinstance(group, tuple):
                graph, state = build_filter_group(group, state, settings, directory, control)
                frames = pipe_filter(frames, before, state, graph, control, directory)
            elif group == "neural_model" and len(stages) == 1:
                frames = neural_frames(frames, state, settings, control)
                state = state.rgb()
            elif group == "dlss_super_resolution" and len(stages) == 1:
                from ..core.dlss_modes import dlss_output_size
                width, height = dlss_output_size(state.width, state.height,
                                                   settings.upscale_dlss_mode, even=True)
                frames = dlss_frames(frames, state, settings, control)
                state = state.rgb(width=width, height=height)
            else:
                frames, state = native_stage(group, frames, state, settings,
                                             control, directory)
            resources.callback(frames.close)
        rendered = list(frames)
        if len(rendered) != 1:
            raise RuntimeError(f"Frame preview produced {len(rendered)} frames instead of one.")
        result = rendered[0].to_ndarray(format="rgba64le")
        return np.ascontiguousarray(result)

def _encode(frames, state, source, destination, settings, controller, progress, stats, *, video_filter="",
            frame_progress=True):
    frames = iter(frames)
    first = next(frames)
    output = None
    process = muxer = logger = None
    completed = False
    delivered = 0
    meta = dict(state.metadata)
    if settings.codec != "FFV1 Lossless RGB 10-bit":
        meta["color_space"] = "bt2020nc" if state.hdr else stats["source_metadata"].get("color_space")
        if meta["color_space"] in {None, "", "unknown", "gbr"}:
            meta["color_space"] = "bt709"
        meta["color_range"] = "tv"
    from ..core.gpu_detection import detect_gpus
    gpu = ffmpeg.resolve_video_gpu(detect_gpus(), settings.video_gpu_uuid,
                                   settings.codec, state.width, state.height)
    codec_args, selected, _ = _codec_command(settings.codec, settings.quality, state.width, state.height,
                                            float(state.rate), None if gpu is None else int(gpu["cuda_ordinal"]),
                                            hdr_mode=state.hdr, hdr_metadata=meta,
                                            output_depth=ffmpeg.output_video_depth(state.depth, settings.codec, state.hdr))
    audio = plan_audio_streams(source, ffmpeg.resolve_container(settings.codec, settings.container), controller)
    tags = _color_tags(state.metadata, rgb=first.format.is_rgb)
    duration = float(state.metadata.get("video_duration") or state.metadata.get("duration") or 0)
    command = [str(FFMPEG), "-v", "warning", "-xerror", "-y", "-probesize", "32", "-analyzeduration", "0",
               "-f", "nut", "-i", "pipe:0",
               *(["-t", f"{duration:.9f}"] if duration > 0 else []), "-i", str(source),
               "-map", "0:v:0", "-map", "1:a?", "-map_metadata", "1", "-map_chapters", "1",
               "-vf", tags + ("," + video_filter if video_filter else ""), *codec_args, *audio.encoder_args(),
               "-fps_mode", "passthrough", "-enc_time_base:v", "demux",
               str(destination)]
    command = prepare_command(command, selection=settings.ffmpeg_device,
                              dimensions=(state.width, state.height), input_format=first.format.name,
                              rate=str(state.rate), time_base=str(first.time_base))
    logs = BoundedLogBuffer(max_tail=60)
    last_update = 0.0
    expected = max(1, duration * float(state.rate))
    try:
        # Capability/audio probes can be cancelled before encoding starts.
        # Create the partial file only after that setup, inside its owner.
        output = OutputFile(destination)
        command[-1] = str(output.temporary)
        process = spawn_frame_process(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
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
            if progress and frame_progress and now - last_update >= .2:
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
        controller.check()
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
        if controller.cancel.is_set():
            raise Cancelled("Video processing stopped.") from exc
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
        if output:
            output.cleanup()
        close = getattr(frames, "close", None)
        if close:
            close()

def render_rolling_video(source: Path, settings: UISettings, destination: Path,
                         controller, directory: Path, stages, progress=None, *, cache_limit=None, window_frames=None):
    from .rolling_passes import render_pass_video
    return render_pass_video(source, settings, destination, controller, directory, stages, progress,
                             cache_limit=cache_limit, window_frames=window_frames)
