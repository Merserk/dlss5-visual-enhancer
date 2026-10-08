"""File-based reference scheduler for timing and lifetime oracles.

Production video exports use rolling_passes and the two frame-block cache modes.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from ..core import app_log
from ..core.rolling_cache import HISTORY_FRAMES, CacheBudget, LosslessFrameCache
from .rolling_worker import stage_frames
from .rolling_workflow import build_filter_group, native_output_state, pipe_filter, source_frames


@dataclass
class Step:
    stage: str
    before: object
    model_output: object
    after: object
    graph: str
    directory: Path
    filter_stages: tuple = ()


def plan_steps(groups, state, settings, directory, controller):
    """Fuse stateless cards with the cache/delivery encoder after a model."""
    steps = []
    leading = ""
    for group in groups:
        if isinstance(group, tuple):
            graph, after = build_filter_group(group, state, settings, directory, controller)
            if steps:
                steps[-1].graph += "," + graph.removeprefix("ve_gpu,")
                steps[-1].after = after
                steps[-1].filter_stages += group
            else:
                leading = graph
            state = after
        else:
            model_output = native_output_state(group, state, settings)
            stage_directory = directory / f"stage-{len(steps)}"
            stage_directory.mkdir(parents=True, exist_ok=True)
            steps.append(Step(group, state, model_output, model_output, "ve_gpu,format=gbrp10le", stage_directory))
            state = model_output
    return steps, leading


def _cache_costs(steps, source_rate):
    frames = [step.after.width * step.after.height * 12 + 4096 for step in steps[:-1]]
    rates = [math.ceil(size * step.after.rate / source_rate) for size, step in zip(frames, steps)]
    return frames, rates


def _largest_transition(costs):
    return max([0, *costs, *(a + b for a, b in zip(costs, costs[1:]))])


def _model_input_format(stage, hdr):
    if stage in {"super_resolution", "rtx_video_hdr"}:
        return "gbrp10le"
    return "rgba" if stage == "frame_generation" and not hdr else "rgba64le"


def cache_history_limit(steps, source_rate, limit):
    frames, _ = _cache_costs(steps, source_rate)
    largest = _largest_transition(frames)
    return min(HISTORY_FRAMES, max(1, int(limit * .4) // largest)) if largest else HISTORY_FRAMES


def window_frame_limit(steps, source_rate, limit, history_frames=HISTORY_FRAMES):
    """Reserve for the largest adjacent caches, including FPS/size expansion.

    Incompressible FFV1 can exceed raw RGB10 size. Two raw frames' worth per
    cached frame plus header/slice allowance avoids relying on compression of
    a particular scene. The byte writer still enforces the actual shared cap.
    """
    frames, costs = _cache_costs(steps, source_rate)
    transition = _largest_transition(costs)
    if not transition:
        return 2**31 - 1
    available = int(limit) - 128 * 1024 * max(1, len(costs))
    available -= history_frames * _largest_transition(frames)
    count = available // transition - 2
    if count < 1:
        raise ValueError("Rolling cache capacity is too small for a frame and its temporal context at this output size.")
    return count


class _PausableSource:
    """Close the GPU decoder between passes, retaining only a host checkpoint."""

    def __init__(self, factory):
        self.factory, self.stream = factory, None
        self.cursor = {}
        self.eof = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.eof:
            raise StopIteration
        if self.stream is None:
            self.stream = iter(self.factory(self.cursor))
        try:
            return next(self.stream)
        except StopIteration:
            self.eof = True
            self.pause()
            raise

    def pause(self):
        if self.stream is not None:
            close = getattr(self.stream, "close", None)
            if close:
                close()
            self.stream = None

    def close(self):
        self.eof = True
        self.pause()


class _WindowCapacity:
    """Stop input from actual cache growth, reserving unencoded/next-pass bytes.

    No compression ratio is assumed for outstanding frames or later caches.
    Packet acknowledgements retire input as its encoded bytes enter the budget.
    """

    def __init__(self, steps, source_rate, budget, history_frames):
        self.frames, self.costs = _cache_costs(steps, source_rate)
        self.rate, self.budget = Fraction(source_rate), budget
        self.first_ratio = steps[0].after.rate / self.rate
        self.history_frames = history_frames
        self.headers = 128 * 1024 * max(1, len(self.costs))
        self.pending = deque()
        self.lock = threading.Lock()

    def reset(self, context):
        with self.lock:
            self.pending = deque(Fraction(frame.pts) * frame.time_base for frame in context[-self.history_frames:])

    def issued(self, frame):
        with self.lock:
            self.pending.append(Fraction(frame.pts) * frame.time_base)

    def acknowledge(self, timing):
        pts, _, time_base = timing
        stamp = Fraction(pts) * time_base
        with self.lock:
            while self.pending and self.pending[0] <= stamp:
                self.pending.popleft()

    def full(self, count, span):
        if not self.costs or count < 1:
            return False
        with self.lock:
            # Current/right-neighbor frames and codec flushing still need room
            # when the producer closes its input at the byte boundary.
            outstanding = math.ceil((len(self.pending) + 3) * self.first_ratio)
        first = self.budget.used_bytes + outstanding * self.frames[0]
        units = max(count + 2, math.ceil(max(Fraction(0), span) * self.rate) + 2)
        later = [units * cost + (self.history_frames + 2) * size
                 for cost, size in zip(self.costs[1:], self.frames[1:])]
        peak = max(first + (later[0] if later else 0), _largest_transition(later))
        return peak + self.headers >= self.budget.limit


class SourceWindows:
    """Retain host context and one right neighbor for boundary interpolation."""

    def __init__(self, frames, rate, max_frames, *, seconds=None, history_frames=HISTORY_FRAMES, cfr=True,
                 capacity=None, vfr_seconds=None):
        self.frames = iter(frames)
        self.rate, self.max_frames = Fraction(rate), int(max_frames)
        self.seconds = Fraction(max_frames, 1) / self.rate
        if seconds is not None:
            self.seconds = min(Fraction(seconds), self.seconds)
        self.capacity = capacity
        self.vfr_seconds = Fraction(vfr_seconds) if vfr_seconds is not None else self.seconds
        self.boundary_callback = None
        self.history = deque(maxlen=history_frames)
        self.pending = None
        self.index = 0
        self.cfr = cfr
        self.start = self.end = Fraction(0)
        self.source_start = self.source_stop = Fraction(0)
        self.phase_origin = 0
        self.owned_origin = 0
        self.last = False
        self.source_eof = self.started = False
        self.source_end = Fraction(0)

    def next(self):
        if self.last:
            return None
        first = self.pending
        if first is None and not self.source_eof:
            first = next(self.frames, None)
        if first is None and not self.history:
            self.last = True
            return None
        self.pending = None
        context = tuple(self.history)
        if self.capacity:
            self.capacity.reset(context)
        first_stamp = Fraction(first.pts) * first.time_base if first is not None else self.end
        self.start = Fraction(self.index, 1) / self.rate if self.cfr else self.end if self.started else first_stamp
        self.started = True
        self.source_start = first_stamp
        self.phase_origin = self.index - len(context)
        self.owned_origin = self.index
        boundary = self.start + self.seconds

        def window():
            nonlocal boundary
            yield from context
            frame = first
            count = 0
            while frame is not None:
                stamp = Fraction(frame.pts) * frame.time_base
                if self.cfr and abs(stamp - Fraction(self.index, 1) / self.rate) > frame.time_base:
                    # Container averages can hide cadence changes later in a
                    # video, outside the metadata probe's opening sample.
                    self.cfr = False
                    self.seconds = min(self.seconds, self.vfr_seconds)
                    boundary = self.start + self.seconds
                if (count >= self.max_frames or (not self.cfr and stamp >= boundary) or
                        (self.capacity and self.capacity.full(count, stamp - self.start))):
                    self.pending = frame
                    self.end = Fraction(self.index, 1) / self.rate if self.cfr else min(stamp, boundary)
                    self.source_stop = stamp
                    if self.boundary_callback:
                        self.boundary_callback()
                    # This frame is context only; the next window owns it.
                    if self.capacity:
                        self.capacity.issued(frame)
                    yield frame
                    return
                self.history.append(frame)
                self.index += 1
                count += 1
                self.source_end = Fraction(self.index, 1) / self.rate if self.cfr else stamp + Fraction(frame.duration) * frame.time_base
                self.end = self.source_end
                self.source_stop = stamp + Fraction(frame.duration) * frame.time_base
                if self.capacity:
                    self.capacity.issued(frame)
                yield frame
                frame = next(self.frames, None)
            self.source_eof = True
            self.end = self.source_end if self.cfr else min(self.source_end, boundary)
            self.last = self.end >= self.source_end
            if self.boundary_callback:
                self.boundary_callback()

        return window()

    def close(self):
        close = getattr(self.frames, "close", None)
        if close:
            close()
        self.pending = None
        self.history.clear()


def scheduled_frames(source, source_state, groups, settings, controller, directory, stats, progress, cache_limit,
                     *, window_frames=None):
    """Return the final stream plus its state and fused delivery filter graph."""
    from .workflow import STAGE_LABELS
    # Raw host transport preserves packed precision; all resampling, colorspace
    # conversion, cache compression and decompression use Vulkan when supported.
    pixel_format = "rgba64le" if source_state.depth > 8 else "rgba"
    working = replace(source_state, metadata={**source_state.metadata, "pixel_format": pixel_format,
                                              "color_space": "gbr", "color_range": "pc", "rotation": 0})
    steps, leading = plan_steps(groups, working, settings, directory, controller)
    if not steps:
        raise ValueError("The rolling model scheduler requires a model stage.")
    leading_grain = bool(leading and isinstance(groups[0], tuple) and "grain" in groups[0] and settings.grain_animated)
    if leading and not leading_grain:
        # The first model's state already describes the fused source filters.
        # Pack its input on the GPU too, avoiding a host RGB10 reformatter.
        pixel_format = _model_input_format(steps[0].stage, steps[0].before.hdr)
    rotation = int(source_state.metadata.get("rotation") or 0)
    rotate = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}.get(rotation, "")
    initial_graph = ",".join(part for part in (rotate, "" if leading_grain else leading) if part)
    budget = CacheBudget(cache_limit)
    cache_history = cache_history_limit(steps, source_state.rate, cache_limit)
    reserved_cap = window_frame_limit(steps, source_state.rate, cache_limit, cache_history)
    cfr = bool(source_state.metadata.get("cfr", True))
    cap = max(1, int(source_state.metadata.get("frames") or 2**31 - 1)) if cfr else reserved_cap
    if window_frames is not None:
        cap = min(cap, max(1, int(window_frames)))
    decoded = _PausableSource(lambda cursor: source_frames(
        source, source_state, controller, stats, pixel_format=pixel_format,
        video_filter=initial_graph, directory=directory, resume=cursor))
    capacity = _WindowCapacity(steps, source_state.rate, budget, cache_history) if len(steps) > 1 else None
    windows = SourceWindows(decoded, source_state.rate, cap, cfr=cfr, capacity=capacity,
                            vfr_seconds=Fraction(reserved_cap, 1) / source_state.rate)
    final = steps[-1]

    def output():
        sequence = 0
        try:
            while True:
                inputs = windows.next()
                if inputs is None:
                    break
                sequence += 1
                caches = []
                # Exhaust the bounded source window first while the first model
                # is evaluating. Its true end/EOF becomes known in that pass.
                with ExitStack() as cleanup:
                    first_step = steps[0]
                    # FI needs the end before finish(). The writer can obtain it
                    # after reading input EOF via a small window descriptor file.
                    descriptor = first_step.directory / "window.json"
                    descriptor.unlink(missing_ok=True)
                    def describe_boundary():
                        temporary = descriptor.with_suffix(".tmp")
                        temporary.write_text(json.dumps({"end": str(windows.end), "last": windows.last}), encoding="utf-8")
                        temporary.replace(descriptor)
                    windows.boundary_callback = describe_boundary

                    def described_input():
                        try:
                            yield from inputs
                        finally:
                            decoded.pause()

                    frames = described_input()
                    cleanup.callback(frames.close)
                    if leading_grain:
                        filters, _ = build_filter_group(groups[0], working, settings, directory, controller,
                                                        grain_phase=windows.phase_origin)
                        frames = pipe_filter(frames, working, steps[0].before, filters, controller, directory,
                                             pixel_format=_model_input_format(steps[0].stage, steps[0].before.hdr))
                        cleanup.callback(frames.close)
                    phase = windows.phase_origin
                    interpolated = False
                    for index, step in enumerate(steps):
                        controller.check()
                        if progress:
                            duration = float(stats["source_metadata"].get("duration") or 1)
                            fraction = min(.96, float(windows.start) / duration)
                            progress(fraction, f"Processing cache window {sequence}: {STAGE_LABELS[step.stage]}")
                        frames = stage_frames(step.stage, frames, step.before, step.model_output, settings,
                                              controller, step.directory, phase_origin=phase,
                                              window_end=None if index == 0 else windows.end,
                                              last_window=windows.last, window_descriptor=descriptor if index == 0 else None,
                                              window_start=windows.start, window_max_end=windows.start + windows.seconds)
                        cleanup.callback(frames.close)
                        if progress:
                            def reported(stream=frames, label=STAGE_LABELS[step.stage], current=index):
                                updated = 0.0
                                try:
                                    for count, frame in enumerate(stream, 1):
                                        now = time.perf_counter()
                                        if now - updated >= .2:
                                            duration = max(.001, float(stats["source_metadata"].get("duration") or 1))
                                            stamp = Fraction(frame.pts) * frame.time_base
                                            span = max(0.0, float(windows.end - windows.start))
                                            owned = max(0.0, min(span, float(stamp - windows.start)))
                                            fraction = (float(windows.start) + (current * span + owned) / len(steps)) / duration
                                            progress(min(.97, fraction * .97),
                                                     f"Processing cache window {sequence}: {label}: {count:,} frames")
                                            updated = now
                                        yield frame
                                finally:
                                    stream.close()
                            frames = reported()
                            cleanup.callback(frames.close)
                        interpolated = interpolated or step.stage == "frame_generation"
                        if index == len(steps) - 1:
                            # One model plus sharpening/coloring uses the delivery
                            # encoder directly, without a redundant codec round trip.
                            for frame in frames:
                                stamp = Fraction(frame.pts) * frame.time_base
                                start = windows.start if interpolated else windows.source_start
                                end = windows.end if interpolated else windows.source_stop
                                if start <= stamp < end:
                                    yield frame
                            continue
                        cache_origin = [None]
                        owned_origin = math.ceil(windows.start * step.after.rate) if interpolated else windows.owned_origin
                        def cached_context(stream=frames, rate=step.after.rate, owner=owned_origin,
                                           origin=phase, generated=step.stage == "frame_generation", recorded=cache_origin):
                            # High-FPS stages retain a bounded number of output
                            # context frames, rather than multiplying the source
                            # preroll into hundreds of cached generated frames.
                            earliest = max(0, owner - cache_history)
                            try:
                                for frame_index, frame in enumerate(stream):
                                    stamp = Fraction(frame.pts) * frame.time_base
                                    ordinal = round(stamp * rate) if generated else origin + frame_index
                                    # Count context frames instead of estimating
                                    # their count from a VFR average rate. A
                                    # long-held source frame remains valid too.
                                    if ordinal >= earliest:
                                        if recorded[0] is None:
                                            recorded[0] = ordinal
                                        yield frame
                            finally:
                                stream.close()

                        frames = cached_context()
                        cleanup.callback(frames.close)
                        graph = step.graph
                        if "grain" in step.filter_stages and settings.grain_animated:
                            def phased_graph(first, current=step, recorded=cache_origin):
                                filters, _ = build_filter_group(current.filter_stages, current.model_output, settings,
                                                               directory, controller, grain_phase=recorded[0])
                                return "ve_gpu,format=gbrp10le," + filters.removeprefix("ve_gpu,")
                            graph = phased_graph
                        cache = LosslessFrameCache(frames, directory / f"window-{sequence}-stage-{index}", controller,
                                                  budget=budget,
                                                  metadata=step.after.metadata,
                                                  rate=step.after.rate, dimensions=(step.after.width, step.after.height),
                                                  video_filter=graph,
                                                  decode_format=_model_input_format(steps[index + 1].stage, step.after.hdr),
                                                  on_packet=capacity.acknowledge if index == 0 else None)
                        cleanup.callback(cache.close)
                        cache.build()
                        phase = cache_origin[0]
                        caches.append(cache)
                        frames = iter(cache)
                        cleanup.callback(frames.close)
                    descriptor.unlink(missing_ok=True)
                for cache in caches:
                    cache.directory.rmdir()
                stats.update(cache_peak_bytes=budget.peak_bytes, cache_windows=sequence,
                             cache_encoders=sorted({cache.encoder for cache in caches}))
                app_log.info("rolling-cache", f"window={sequence} end={float(windows.end):.3f}s models_unloaded=1 peak={budget.peak_bytes} limit={budget.limit}")
        finally:
            windows.close()
            for step in steps:
                (step.directory / "window.json").unlink(missing_ok=True)
                (step.directory / "window.tmp").unlink(missing_ok=True)
            stats["cache_peak_bytes"] = budget.peak_bytes
            if budget.used_bytes:
                raise RuntimeError("Rolling cache cleanup did not return its disk budget.")

    return output(), final.after, final.graph
