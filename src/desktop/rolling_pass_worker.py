"""Disposable, descriptor-driven passes. Only this process touches GPU pixels."""
from __future__ import annotations

import itertools
import math
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from ..core import ffmpeg
from ..core.jobs import JobController, use_job_controller
from ..core.pass_cache import FrameMapping, FrameTiming
from .rolling_workflow import VideoState, _Control, _timing, source_frames

def _state(values):
    values = dict(values)
    values["rate"] = Fraction(values["rate"])
    values["metadata"] = dict(values["metadata"])
    for key in ("rate", "time_base"):
        if values["metadata"].get(key):
            values["metadata"][key] = Fraction(values["metadata"][key])
    return VideoState(**values)

def _settings(values):
    from ..settings.models import UISettings
    values = dict(values)
    for key in ("image_stage_order", "video_stage_order", "image_enabled_stages", "video_enabled_stages"):
        if key in values:
            values[key] = tuple(values[key])
    return UISettings(**values)

def _timing_of(frame, index=-1):
    if isinstance(frame.opaque, FrameTiming):
        return frame.opaque
    return FrameTiming(frame.pts, frame.duration or 0, Fraction(frame.time_base), source_index=index)

class SourcePart:
    def __init__(self, payload, state, controller, directory, ordinal):
        self.payload, self.state, self.controller = payload, state, controller
        self.directory, self.ordinal = directory, ordinal
        self.window = payload["window"]
        for key in ("start", "end", "source_start", "source_stop"):
            if self.window.get(key) is not None:
                self.window[key] = Fraction(self.window[key])
        self.cursor = dict(self.window.get("cursor") or {})
        for key in ("origin", "last", "absolute"):
            if self.cursor.get(key) is not None:
                self.cursor[key] = Fraction(self.cursor[key])
        self.stats = {}

    def frames(self):
        window, state = self.window, self.state
        owned = int(window["owned_origin"])
        cap = int(window["frame_limit"])
        history = deque(maxlen=int(window.get("history_frames", 32)))
        start = Fraction(window["start"])
        boundary = start + Fraction(cap, 1) / state.rate
        cfr = bool(window.get("cfr", state.metadata.get("cfr", True)))
        cuda = self.ordinal if self.payload["stage"] in {
            "neural_model", "frame_generation", "super_resolution", "rtx_video_hdr"} else None
        frames = source_frames(Path(self.payload["source"]), state, self.controller, self.stats,
            cuda_device=cuda, pixel_format="rgba64le" if state.depth > 8 else "rgba",
            directory=self.directory, resume=self.cursor)
        count = 0
        end = start
        window.update(owned_stop=owned + cap, last=False)
        try:
            previous_cursor = dict(self.cursor)
            for frame in frames:
                index = int(self.cursor["count"]) - 1
                stamp = Fraction(frame.pts) * frame.time_base
                if cfr and abs(stamp - Fraction(index, 1) / state.rate) > frame.time_base:
                    cfr = False
                if index >= owned and (count >= cap or
                        (not cfr and self.payload.get("bounded_time") and stamp >= boundary)):
                    window.update(end=Fraction(index, 1) / state.rate if cfr else min(stamp, boundary),
                                  source_stop=stamp, owned_stop=index, last=False)
                    frame.opaque = FrameTiming(frame.pts, frame.duration, Fraction(frame.time_base), source_index=index)
                    yield frame  # The next part owns this right neighbor.
                    break
                history.append(previous_cursor)
                previous_cursor = dict(self.cursor)
                if index >= owned:
                    if count == 0:
                        window["source_start"] = stamp
                    count += 1
                    end = Fraction(index + 1, 1) / state.rate if cfr else stamp + frame.duration * frame.time_base
                frame.opaque = FrameTiming(frame.pts, frame.duration, Fraction(frame.time_base), source_index=index)
                yield frame
            else:
                # A VFR hold may need several time parts although no new source
                # frame is owned. Its retained left frame remains valid input.
                if not cfr:
                    end = max(end, Fraction(self.stats.get("duration") or start))
                stop = end if cfr or not self.payload.get("bounded_time") else min(end, boundary)
                window.update(end=stop, source_stop=end, owned_stop=owned + count, last=stop >= end)
            window.update(next_cursor=dict(history[0]) if history else dict(previous_cursor),
                          next_owned=int(window["owned_stop"]), cfr=cfr,
                          source_end=end, owned_frames=count, source_time_origin=self.cursor.get("origin"))
        finally:
            frames.close()

class Progress:
    def __init__(self, emit, stage, window):
        self.emit, self.stage, self.window = emit, stage, window
        self.started = time.perf_counter()
        self.first = self.last = None
        self.first_owned = self.last_owned = None
        self.count = self.owned = self.preroll = 0
        self.updated = 0.0
        self.samples = deque()
        self.timing = None
        self.emitted_count = 0

    def emit_current(self):
        if self.timing is None:
            return
        first_time, first_count = self.samples[0]
        fps = ((self.count - first_count) / (self.last - first_time)
               if self.last > first_time else 0.0)
        self.emit(dict(event="progress", frames=self.count, owned=self.owned, preroll=self.preroll,
                       fps=fps, stamp=str(self.timing.stamp), elapsed=self.last - self.started))
        self.updated, self.emitted_count = self.last, self.count

    def output(self, timing):
        now = time.perf_counter()
        if self.first is None:
            self.first = now
        self.last = now
        self.count += 1
        self.timing = timing
        self.samples.append((now, self.count))
        while len(self.samples) > 2 and now - self.samples[1][0] >= 1.0:
            self.samples.popleft()
        if timing.stamp < Fraction(self.window["start"]) - timing.time_base:
            self.preroll += 1
        elif timing.stamp < Fraction(self.window.get("end") or 10**12):
            self.owned += 1
            if self.first_owned is None:
                self.first_owned = now
            self.last_owned = now
        if not self.emitted_count or now - self.updated >= .2:
            self.emit_current()

    def report(self):
        if self.count != self.emitted_count:
            self.emit_current()
        return dict(frames=self.count, owned_frames=self.owned, preroll_frames=self.preroll,
                    elapsed_seconds=time.perf_counter() - self.started,
                    first_output_seconds=self.first - self.started if self.first else None,
                    first_owned_output_seconds=self.first_owned - self.started if self.first_owned else None,
                    owned_steady_fps=(self.owned - 1) / (self.last_owned - self.first_owned)
                        if self.owned > 1 and self.last_owned > self.first_owned else None,
                    steady_fps=(self.count - 1) / (self.last - self.first)
                        if self.count > 1 and self.last > self.first else None)

class MappingSink:
    def __init__(self, mapping, window, *, timeline=False):
        self.mapping, self.window, self.timeline = mapping, window, timeline
        self.context = self.written = 0
        self.started = False
        self.pending = deque()
        self.executor = None

    def context_frame(self, timing):
        if not self.timeline and timing.source_index >= 0:
            return timing.source_index < int(self.window["owned_origin"])
        cutoff = self.window.get("source_start") if not self.timeline else self.window["start"]
        return timing.stamp < Fraction(cutoff if cutoff is not None else self.window["start"])

    def slot(self, timing):
        if not self.started and self.context_frame(timing):
            slot = self.context % 32
            self.context += 1
        else:
            self.started = True
            slot = min(32, self.context) + self.written
            self.written += 1
        if slot >= self.mapping.capacity:
            raise RuntimeError("The planned part exceeds its reserved lossless cache capacity.")
        self.mapping.write_timing(slot, timing)
        return slot

    def write(self, pixels, timing):
        from ..frame_interpolation.native import DLSSGCudaSurface
        slot = self.slot(timing)
        if isinstance(pixels, DLSSGCudaSurface):
            if self.mapping.format == "gbrp10le":
                frame = pixels.to_host_gbrp10_frame()
                frame.pts, frame.duration, frame.time_base = timing.pts, timing.duration, timing.time_base
                self.mapping.write_frame(slot, frame, segment=timing.segment,
                    source_index=timing.source_index, generated=timing.generated)
            else:
                # Direct DMA into the mapped destination avoids an additional
                # host copy. On the matched adapter it outperforms the bounded
                # pinned-slot path while FG already waits for each NGX result.
                target = self.mapping.pixels(slot)
                pixels.to_host_rgb(target)
                del target
        elif isinstance(pixels, np.ndarray):
            target = self.mapping.pixels(slot)
            np.copyto(target, pixels)
            del target
        else:
            self.mapping.write_frame(slot, pixels, segment=timing.segment,
                source_index=timing.source_index, generated=timing.generated)

    def commit(self):
        self.drain()
        history = min(32, self.context)
        self.mapping.commit(history + self.written,
            history_start=(self.context - history) % 32, history_count=history)
        return history + self.written

    def drain(self):
        try:
            while self.pending:
                self.pending.popleft().result()
        finally:
            if self.executor:
                self.executor.shutdown(wait=True)
                self.executor = None

class CompressedRGBSink(MappingSink):
    def write(self, pixels, timing):
        from ..frame_interpolation.native import DLSSGCudaSurface
        slot = self.slot(timing)
        if isinstance(pixels, DLSSGCudaSurface):
            pixels = pixels.to_host_rgb()
        if self.executor is None:
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rgb-cache-write")
        if len(self.pending) == 3:
            self.pending.popleft().result()
        self.pending.append(self.executor.submit(self.mapping.write_rgb, slot, pixels, timing))

def _host_rgba(frame, depth):
    if frame.format.name == ("rgba64le" if depth > 8 else "rgba"):
        dtype = np.uint16 if frame.format.name == "rgba64le" else np.uint8
        plane = frame.planes[0]
        row = plane.line_size // (4 * np.dtype(dtype).itemsize)
        return np.ascontiguousarray(np.frombuffer(plane, dtype=dtype).reshape(frame.height, row, 4)[:, :frame.width])
    return np.ascontiguousarray(ffmpeg.decoded_rgba(frame, depth))

def _owned(timing, window, timeline):
    if not timeline and timing.source_index >= 0:
        return int(window["owned_origin"]) <= timing.source_index < int(window["owned_stop"])
    start = Fraction(window["start"] if timeline else window.get("source_start") or window["start"])
    end = Fraction((window.get("end") if timeline else window.get("source_stop") or window.get("end")) or 10**12)
    return start <= timing.stamp < end

def neural_pass(frames, before, after, settings, controller, sink, encoder, progress, payload):
    from ..core.gpu_selection import resolve_runtime_ai_gpu
    from ..core.neural_bridge import BRIDGE_MANAGER
    from ..core.runtime import DLSSFrameSession, prepare_runtime, resolve_native_settings, resolve_upscaling_mode
    from ..neural_rendering.video.cuda_pipeline import _matrix_code, _range_code
    from ..neural_rendering.video.guides import TemporalGuideGenerator
    from .workflow import _neural_video_options
    iterator = iter(frames)
    first = next(iterator)
    prepared = prepare_runtime()
    gpu = resolve_runtime_ai_gpu(prepared.gpus, prepared.runtime_bundle, settings.ai_gpu_uuid)
    factor, mode = resolve_upscaling_mode(1.0)
    opts = _neural_video_options(settings, before.hdr)
    session = DLSSFrameSession(input_width=before.width, input_height=before.height,
        output_width=after.width, output_height=after.height, frame_count=None, warmup_frames=opts.warmup_frames,
        factor=factor, mode=mode, native_settings=resolve_native_settings(opts), control_mask=opts.nr_mask,
        gpu=gpu, runtime_bundle=prepared.runtime_bundle, controller=controller, cuda_video=True)
    progress.emit(dict(event="model-loaded", instances=1))
    guides = TemporalGuideGenerator(before.width, before.height)
    matrix, color_range = _matrix_code(before.metadata), _range_code(before.metadata)
    pending = deque()
    executor = ThreadPoolExecutor(max_workers=2 if isinstance(sink, CompressedRGBSink) else 1,
                                  thread_name_prefix="lossless-cache-copy") if sink else None
    phase = int(payload.get("phase_origin", 0))
    def finish_transfer(token, slot, timing, target):
        BRIDGE_MANAGER.finish_cache_transfer(token)
        if isinstance(sink, CompressedRGBSink):
            sink.mapping.write_rgb(slot, target, timing)
    def finish_one():
        future, timing, target = pending.popleft()
        future.result()
        progress.output(timing)
        del target
    try:
        sequence = itertools.chain((first,), iterator)
        first = None
        for local, frame in enumerate(sequence):
            controller.check()
            timing = _timing_of(frame, phase + local)
            if len(pending) == 3:
                finish_one()
            if frame.format.name == "cuda":
                score, reset = session.score_cuda_frame(frame, color_matrix=matrix, color_range=color_range)
                prepared_frame = frame
                rotation = int(before.metadata.get("rotation") or 0)
            else:
                prepared_frame = _host_rgba(frame, before.depth)
                guide = guides.process(prepared_frame)
                score, reset, rotation = guide.scene_score, guide.reset, 0
            values = dict(index=phase + local, reset=reset, scene_score=score, pts=frame.pts,
                          color_matrix=matrix, color_range=color_range, rotation=rotation,
                          chroma_location=ffmpeg.chroma_location_code(before.metadata))
            if sink:
                slot = sink.slot(timing)
                target = (np.empty((after.height, after.width, 4), dtype=np.uint16 if before.depth > 8 else np.uint8)
                          if isinstance(sink, CompressedRGBSink) else sink.mapping.pixels(slot))
                _, token = session.process_frame_to_host(frame=prepared_frame, output_buffer=target,
                                                        async_transfer=True, **values)
                pending.append((executor.submit(finish_transfer, token, slot, timing, target), timing, target))
            else:
                processed, _ = session.process_video_frame(
                    frame=prepared_frame if frame.format.name == "cuda" else None,
                    rgba=None if frame.format.name == "cuda" else prepared_frame,
                    duration=frame.duration, time_base=frame.time_base,
                    output_p010=encoder.pixel_format == "p010le", **values)
                if _owned(timing, payload["window"], payload.get("timeline", False)):
                    start = payload["window"]["start"] if payload.get("timeline") else payload["window"]["source_start"]
                    encoder.write(processed, timing.stamp, start, frame.duration * frame.time_base)
                progress.output(timing)
                del processed
            frame = prepared_frame = None
        while pending:
            finish_one()
        result = dict(native=asdict(session.diagnostics), timings=dict(session.process_timings))
        return result
    finally:
        if executor:
            executor.shutdown(wait=True)
        pending.clear()
        first = iterator = frame = prepared_frame = None
        if encoder:
            encoder.close(abort=controller.cancel.is_set())
        session.close()
        progress.emit(dict(event="model-released", instances=0))

def _grid_timing(item, rate):
    base = Fraction(1, math.lcm(item.timestamp.denominator, rate.numerator))
    return FrameTiming(int(item.timestamp / base), max(1, round(Fraction(1, 1) / rate / base)), base,
                       item.segment, item.source_index if item.source_index is not None else -1,
                       item.provenance == "DLSSG")

def interpolation_pass(frames, cache, before, after, settings, controller, sink, encoder, progress, payload, directory):
    from ..frame_interpolation.native import DirectDLSSGSession, DLSSGCudaSurface
    from ..frame_interpolation.processor import (DLSSGStage, TimedFrame, NearestTimestampWriter,
        _matrix_code, _range_code, _primaries_code, _transfer_code)
    from ..frame_interpolation.scheduler import output_frame_count
    fg = payload["fg"]
    window = payload["window"]
    first_level = int(fg["level"]) == 0
    colors = dict(color_matrix=_matrix_code(before.metadata, hdr=before.hdr), color_range=_range_code(before.metadata),
                  color_primaries=_primaries_code(before.metadata), color_transfer=_transfer_code(before.metadata),
                  rotation=int(before.metadata.get("rotation") or 0))
    if before.hdr and cache and cache.format != "rgba16f":
        from .rolling_workflow import pipe_filter
        yuv = replace(before, metadata={**before.metadata, "color_space": "bt2020nc", "color_range": "tv"})
        frames = pipe_filter(frames, before, yuv, "ve_gpu,format=p010le", controller, directory, pixel_format="p010le")
        colors.update(color_matrix=2, color_range=0)
    session = DirectDLSSGSession(before.width, before.height, int(fg["generated"]), controller,
        int(payload["ordinal"]), hdr=before.hdr,
        chroma_location=before.metadata.get("chroma_location", "left"),
        surface_pool_size=64 if encoder else None,
        optical_flow_quality=settings.frame_interpolation_optical_flow_quality) if fg["generated"] else None
    if session:
        from ..frame_interpolation.native import _MANAGER
        active_models = _MANAGER.status()["active_sessions"]
        if active_models != 1:
            raise RuntimeError("A Frame Generation pass must own exactly one native feature.")
    else:
        active_models = 0
    progress.emit(dict(event="model-loaded", instances=active_models))
    # Cached packed RGB already contains the exact input samples. Keep those
    # host samples for the next cache rather than downloading them again.
    retain_rgb = encoder is not None or cache is None or (before.hdr and cache.format != "rgba16f")
    stage = DLSSGStage(session, int(fg["generated"]), detect_source_cuts=first_level, cuda_output=True,
                      output_p010=before.hdr, colors=colors, preserve_source_rgb=retain_rgb) if session else None
    selected = deque()
    writer = None
    if fg["final"]:
        target = after.rate
        bound = Fraction(window.get("end") or (Fraction(window["start"]) + Fraction(window["frame_limit"], 1) / Fraction(payload["source_rate"])))
        writer = NearestTimestampWriter(target, output_frame_count(bound, target) + 1,
                                        lambda item, index: selected.append((item, index)))
        writer.next_index = max(0, math.ceil((Fraction(window["start"]) - Fraction(32, 1) / target) * target))
        ratio = before.rate * (int(fg["generated"]) + 1) / target
        if ratio.denominator % 2 == 0:
            writer.tie_late = bool(max(0, (writer.next_index - 1 + ratio.denominator // 2) // ratio.denominator) % 2)
    last_stamp = None
    segment = 0
    outputs = []
    live = {}
    def release_unused():
        keep = set()
        for item in (stage.previous if stage else None, writer.previous if writer else None):
            if item:
                for pixels in (item.bridge_frame, item.encode_frame):
                    if isinstance(pixels, DLSSGCudaSurface):
                        keep.add(id(pixels))
        for key in tuple(live):
            if key not in keep:
                live.pop(key).close()
    def write_selected():
        while selected:
            item, index = selected.popleft()
            timing = FrameTiming(index, 1, Fraction(1, 1) / after.rate, item.segment, index,
                                 item.provenance == "DLSSG")
            if sink:
                sink.write(item.encode_frame, timing)
            elif _owned(timing, window, True):
                pixels = item.encode_frame
                if isinstance(pixels, DLSSGCudaSurface):
                    if encoder.stream.pix_fmt == "cuda":
                        surface = pixels.to_yuv_surface(color_matrix=2 if before.hdr else 1,
                            color_range=0, p010=encoder.pixel_format == "p010le")
                        try:
                            frame = surface.to_av_frame()
                            encoder.write(frame, timing.stamp, window["start"], timing.time_base)
                            del frame
                        finally:
                            surface.close()
                    else:
                        frame = (pixels.to_host_gbrp10_frame() if before.hdr else
                                 av.VideoFrame.from_ndarray(pixels.to_host_rgb(), format="rgba"))
                        from ..frame_interpolation.processor import _set_color_properties
                        _set_color_properties(frame, {**before.metadata, "color_space": "gbr", "color_range": "pc"}, before.hdr)
                        encoder.write(frame, timing.stamp, window["start"], timing.time_base)
                        del frame
                else:
                    frame = av.VideoFrame.from_ndarray(pixels, format="rgba") if isinstance(pixels, np.ndarray) else pixels
                    encoder.write(frame, timing.stamp, window["start"], timing.time_base)
            progress.output(timing)
    def inputs():
        if cache and cache.format == "rgba16f":
            yield from cache.timed_pixels()
        else:
            for frame in frames:
                yield frame, _timing_of(frame)
    sequence = inputs()
    try:
        for index, (pixels, timing) in enumerate(sequence):
            controller.check()
            if last_stamp is not None and (timing.stamp <= last_stamp or timing.stamp - last_stamp > Fraction(2, 1) / before.rate):
                segment += 1
            last_stamp = timing.stamp
            segment = max(segment, timing.segment)
            if isinstance(pixels, av.VideoFrame) and pixels.format.name not in {"cuda", "p010le", "p010"}:
                pixels = _host_rgba(pixels, before.depth if before.hdr else 8)
            previous = stage.previous if stage else None
            current = TimedFrame(pixels, pixels, timing.stamp, segment,
                "DLSSG" if timing.generated else "Source", timing.source_index if timing.source_index >= 0 else None)
            outputs = stage.push(current) if stage else [current]
            segment = outputs[-1].segment
            for item in outputs:
                for surface in (item.bridge_frame, item.encode_frame):
                    if isinstance(surface, DLSSGCudaSurface):
                        live[id(surface)] = surface
                if writer:
                    if window.get("end") is not None:
                        writer.output_count = output_frame_count(Fraction(window["end"]), after.rate) + 1
                    for _ in writer.iter_push(item):
                        write_selected()
                else:
                    stamp = _grid_timing(item, after.rate)
                    sink.write(item.encode_frame, stamp)
                    progress.output(stamp)
            outputs.clear()
            release_unused()
            previous = pixels = item = surface = current = None
        if writer:
            writer.output_count = output_frame_count(Fraction(window["end"]), after.rate) + int(not window["last"])
            for _ in writer.iter_finish():
                write_selected()
        if sink:
            sink.drain()
        return dict(native=session.diagnostics() if session else {}, input_frames=session.completed_frames if session else index + 1,
                    model_instances=active_models, fg_level=int(fg["level"]) + 1)
    finally:
        if sink:
            sink.drain()
        # NVENC must return every CUDA frame before the last native surface
        # release can destroy its owner. Destroying inside an encoder release
        # callback can wait on the encoder's own CUDA work and deadlock.
        if encoder:
            encoder.close(abort=controller.cancel.is_set())
        selected.clear()
        if writer:
            writer.previous = None
        if stage:
            stage.previous = None
        outputs.clear()
        for surface in live.values():
            surface.close()
        live.clear()
        sequence.close()
        close = getattr(frames, "close", None)
        if close:
            close()
        if session:
            session.close(abort=controller.cancel.is_set())
        progress.emit(dict(event="model-released", instances=0))

def process_request(payload, directory, emit):
    if payload.get("trace_stalls"):
        import faulthandler
        faulthandler.enable()
        faulthandler.dump_traceback_later(30, repeat=True)
    operation = payload.get("operation")
    if operation == "fg-probe":
        from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities
        emit(dict(event="result", result=asdict(probe_frame_interpolation_capabilities(payload["ai_gpu_uuid"]))))
        return
    if operation in {"pipeline-preview", "preview-stage"}:
        from ..settings.models import UISettings
        from .workflow import render_pipeline_preview
        from .rolling_passes import render_pass_video
        owner = JobController()
        settings = _settings(payload["settings"])
        owner.ffmpeg_device = settings.ffmpeg_device
        def progress(value, message):
            emit(dict(event="preview-progress", progress=value, message=message))
        with use_job_controller(owner):
            try:
                if operation == "preview-stage":
                    path = render_pass_video(Path(payload["source"]), settings, Path(payload["destination"]),
                        owner, directory / "preview-part", (payload["stage"],), progress)
                    result = dict(path=path)
                else:
                    media, message = render_pipeline_preview(payload["source"], settings, payload["mode"],
                        controller=owner, progress=progress, output_dir=Path(payload["output_dir"]),
                        cache_dir=Path(payload["cache_dir"]), start_seconds=payload["start_seconds"],
                        clip_seconds=payload["clip_seconds"])
                    result = dict(message=message)
                    if isinstance(media, np.ndarray):
                        path = directory / "preview-rgba.npy"
                        np.save(path, media, allow_pickle=False)
                        result["array"] = str(path.resolve())
                    else:
                        result["path"] = str(media)
                emit(dict(event="result", result=result))
            finally:
                owner.release_processes()
        return
    from ..core.gpu_selection import detect_gpu
    from ..settings.models import UISettings
    from .pass_delivery import SegmentEncoder
    from .rolling_workflow import native_stage, FILTER_STAGES, build_filter_group, pipe_filter
    settings = _settings(payload["settings"])
    before, after = _state(payload["before"]), _state(payload["after"])
    ordinal = int(detect_gpu(settings.ai_gpu_uuid)["cuda_ordinal"])
    payload["ordinal"] = ordinal
    owner = JobController()
    owner.ffmpeg_device = settings.ffmpeg_device
    controller = _Control(owner)
    cache = output = encoder = None
    source_part = None
    report = {}
    window = payload["window"]
    for key in ("start", "end", "source_start", "source_stop"):
        if window.get(key) is not None:
            window[key] = Fraction(window[key])
    progress = Progress(emit, payload["stage"], window)
    frames = transformed = None
    complete = False
    with use_job_controller(owner), ExitStack() as resources:
        try:
            if payload.get("input"):
                from ..core.frame_blocks import WorkerBlocks
                from ..core.rgb_cache import RGBWorkerBlocks
                cache = (RGBWorkerBlocks(payload["input"], emit) if payload["input"].get("kind") == "rgb-blocks" else
                         WorkerBlocks(payload["input"], emit) if payload["input"].get("kind") == "blocks"
                         else FrameMapping(payload["input"]))
                frames = cache.frames() if cache.format != "rgba16f" else None
            else:
                source_part = SourcePart(payload, before, controller, directory, ordinal)
                frames = source_part.frames()
            if frames:
                resources.callback(frames.close)
            if payload.get("output"):
                from ..core.frame_blocks import WorkerBlocks
                from ..core.rgb_cache import RGBWorkerBlocks
                output = (RGBWorkerBlocks(payload["output"], emit, output=True)
                          if payload["output"].get("kind") == "rgb-blocks" else WorkerBlocks(payload["output"], emit, output=True)
                          if payload["output"].get("kind") == "blocks" else FrameMapping(payload["output"]))
                sink_type = CompressedRGBSink if isinstance(output, RGBWorkerBlocks) else MappingSink
                sink = sink_type(output, window, timeline=bool(payload.get("timeline") or payload.get("fg")))
                resources.callback(sink.drain)
            else:
                sink = None
                cuda = "(NVIDIA NVENC)" in settings.codec and payload["stage"] in {"neural_model", "frame_generation"}
                encoder = SegmentEncoder(payload["segment"], after, settings, ordinal, cuda=cuda, controller=owner)
            emit(dict(event="state", state="load"))
            if payload["stage"] == "neural_model" and (sink or encoder.stream.pix_fmt == "cuda"):
                report.update(neural_pass(frames, before, after, settings, controller, sink, encoder, progress, payload))
            elif payload["stage"] == "frame_generation":
                report.update(interpolation_pass(frames, cache, before, after, settings, controller,
                    sink, encoder, progress, payload, directory))
            else:
                if payload["stage"] in FILTER_STAGES:
                    graph, _ = build_filter_group((payload["stage"],), before, settings, directory, controller,
                                                   grain_phase=int(payload.get("phase_origin", 0)))
                    transformed = pipe_filter(frames, before, after, graph, controller, directory)
                else:
                    transformed, _ = native_stage(payload["stage"], frames, before, settings, controller,
                        directory, phase_origin=int(payload.get("phase_origin", 0)), normalize_output=False)
                    emit(dict(event="model-loaded", instances=1))
                resources.callback(transformed.close)
                for index, frame in enumerate(transformed, int(payload.get("phase_origin", 0))):
                    timing = _timing_of(frame, index)
                    if sink:
                        sink.write(frame, timing)
                    elif _owned(timing, window, payload.get("timeline", False)):
                        start = window["start"] if payload.get("timeline") else window.get("source_start") or window["start"]
                        encoder.write(frame, timing.stamp, start, frame.duration * frame.time_base)
                    progress.output(timing)
                transformed.close()
                if payload["stage"] not in FILTER_STAGES:
                    emit(dict(event="model-released", instances=0))
            emit(dict(event="state", state="flush"))
            if encoder:
                encoder.close()
                report["delivery_frames"] = encoder.count
            if sink:
                report["cache_frames"] = sink.commit()
                report["cache_format"] = output.format
                if isinstance(sink, CompressedRGBSink):
                    report["cache_compression"] = "Zstandard fast RGB8"
                    report["cache_payload_bytes"] = sum(p.stat().st_size for block in output.blocks.values()
                        for p in block.directory.glob("*.rgbz"))
            report.update(progress.report(), stage=payload["stage"], window=window)
            complete = True
            emit(dict(event="result", result=report))
        finally:
            if encoder:
                encoder.close(abort=not complete)
            # Worker mappings are borrowed. AVFrames/NumPy may still own views
            # during unwinding; OS exit is the final owner after GPU cleanup.
            owner.release_processes()
