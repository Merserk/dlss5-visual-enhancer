"""Separate card/model passes with a fresh, strictly bounded cache per part."""
from __future__ import annotations

import json
import math
import shutil
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from pathlib import Path

from ..core import app_log, ffmpeg
from ..core.cache_video import cache_size_bytes, validate_cache_mode
from ..core.jobs import Paused, active_job, prepare_job
from ..core.pass_cache import FORMATS, frame_bytes
from ..core.frame_blocks import FrameBlocks, block_frame_limit, blocks_storage_bytes, RETAINED_BLOCKS
from ..core.rgb_cache import RGBFrameBlocks
from ..core.rolling_cache import CacheBudget, HISTORY_FRAMES
from ..core.ui_messages import UiMessage
from .pass_delivery import PacketDelivery
from .pass_process import run_request
from .rolling_workflow import FILTER_STAGES, VideoState, build_filter_group, native_output_state


@dataclass(frozen=True)
class Pass:
    stage: str
    before: VideoState
    after: VideoState
    format: str
    timeline: bool = False
    fg: dict | None = None
    mode: str = "Fast lossless"

    @property
    def label(self):
        from .workflow import STAGE_LABELS
        label = UiMessage(STAGE_LABELS[self.stage])
        return (UiMessage("%1 · pass %2/%3", label, self.fg["level"] + 1, self.fg["levels"])
                if self.fg and self.fg["levels"] else label)


def plan_passes(stages, state, settings, directory, controller):
    """One pass per card, and exactly one DLSSG instance per cascade level."""
    validate_cache_mode(settings.cache_codec)
    from ..frame_interpolation.capabilities import probe_frame_interpolation_capabilities
    from ..frame_interpolation.models import resolve_target_rate
    from ..frame_interpolation.scheduler import choose_interpolation_plan
    result = []
    timeline = False
    def cache_pass(step, final):
        if step.mode == "Fast compressed":
            if step.after.hdr:
                # The 8-bit option is SDR. Keep PQ/HLG and linear FP16 HDR
                # intermediates on the existing exact-precision path.
                return replace(step, mode="Fast lossless")
            if not final:
                after = replace(step.after, depth=8, metadata={**step.after.metadata,
                    "depth": 8, "pixel_format": "rgb24", "color_space": "gbr", "color_range": "pc"})
                return replace(step, after=after, format="rgb24")
        return step
    for stage_index, stage in enumerate(stages):
        final_stage = stage_index == len(stages) - 1
        if stage in FILTER_STAGES:
            _, after = build_filter_group((stage,), state, settings, directory, controller)
            result.append(cache_pass(Pass(stage, state, after, "gbrp10le", timeline, mode=settings.cache_codec), final_stage))
        elif stage == "frame_generation":
            caps = probe_frame_interpolation_capabilities(settings.ai_gpu_uuid)
            if not caps.available:
                raise RuntimeError("DLSS Frame Generation is unavailable. " + caps.detail)
            target = resolve_target_rate(settings.frame_interpolation_target_fps)
            plan = choose_interpolation_plan(state.rate, target, settings.frame_interpolation_engine,
                                             caps.native_multiplier, cfr=bool(state.metadata.get("cfr", True)))
            count = 1 if plan.path == "Native DLSSG" else plan.cascade_stages
            generated = plan.generated_per_interval if plan.path == "Native DLSSG" else 1
            for level in range(max(1, count)):
                final = level == max(1, count) - 1
                rate = target if final else state.rate * (generated + 1)
                after = state.rgb(rate=rate)
                fmt = "gbrp10le" if final and state.hdr else "rgba16f" if state.hdr else "rgba"
                depth = 10 if final and state.hdr else 16 if state.hdr else 8
                after = replace(after, depth=depth, metadata={**after.metadata, "depth": depth, "pixel_format": fmt})
                fg = dict(level=level, levels=count, generated=generated if count else 0, final=final)
                result.append(cache_pass(Pass(stage, state, after, fmt, True, fg, settings.cache_codec), final and final_stage))
                state = result[-1].after
            timeline = True
        else:
            after = native_output_state(stage, state, settings)
            fmt = "gbrp10le"
            if stage == "neural_model":
                fmt = "rgba64le" if state.depth > 8 else "rgba"
                depth = 16 if state.depth > 8 else 8
                after = replace(after, depth=depth, metadata={**after.metadata, "depth": depth, "pixel_format": fmt})
            result.append(cache_pass(Pass(stage, state, after, fmt, timeline, mode=settings.cache_codec), final_stage))
        state = result[-1].after
    return result


def _capacity(step, source_rate, owned):
    # Temporal history plus the part and its right neighbour. Keep one extra
    # output slot for fractional timestamp rounding instead of six source slots.
    return HISTORY_FRAMES + math.ceil(Fraction(owned + 1, 1) * step.after.rate / source_rate) + 1


def _working_reserve(steps):
    # Three pinned NR readbacks, filter/codec queues, bounded AVFrame copies,
    # source checkpoints and control/timing metadata share the same allocator.
    largest = 0
    for step in steps:
        input_format = step.before.metadata.get("pixel_format")
        if input_format not in FORMATS:
            input_format = "rgba64le" if step.before.depth > 8 else "rgba"
        largest = max(largest,
            frame_bytes(step.before.width, step.before.height, input_format),
            frame_bytes(step.after.width, step.after.height, step.format),
            frame_bytes(step.after.width, step.after.height,
                "rgba64le" if step.stage == "neural_model" and step.before.depth > 8 else "rgba"))
    # Three NR transfers, host conversion/packing and codec/filter copies are
    # bounded independently of the 32-frame model history held in the cache.
    return largest * 12 + 8 * 1024 * 1024


def part_frame_limit(steps, source_rate, limit):
    reserve = _working_reserve(steps)
    if reserve >= limit:
        raise ValueError("Rolling cache capacity is too small for this size and its bounded transfer buffers.")
    def storage(step, capacity):
        return blocks_storage_bytes(step.after.width, step.after.height, step.format, capacity) + \
            (capacity * 8 if step.mode == "Fast compressed" else 0)
    def fits(count):
        previous = 0
        previous_step = None
        peak = 0
        for index, step in enumerate(steps):
            if index == len(steps) - 1:
                peak = max(peak, previous)
                break
            capacity = _capacity(step, source_rate, count)
            block = block_frame_limit(step.after.width, step.after.height, step.format)
            raw = storage(step, capacity)
            if previous_step is None:
                transition = raw
            elif bool(step.before.metadata.get("cfr", True)):
                previous_block = block_frame_limit(previous_step.after.width, previous_step.after.height, previous_step.format)
                retained = storage(previous_step, RETAINED_BLOCKS * previous_block)
                ratio = step.after.rate / previous_step.after.rate
                startup_count = min(capacity, math.ceil((RETAINED_BLOCKS * previous_block + 2) * ratio) + block)
                startup = storage(step, startup_count)
                transition = max(previous + startup, raw + min(previous, retained))
            else:
                transition = previous + raw
            peak = max(peak, transition)
            previous = raw
            previous_step = step
        # Fresh parts can use almost the entire ceiling; a small extra margin
        # covers control bookkeeping in addition to the explicitly sized data.
        return peak + reserve < int(limit) - max(8 * 1024 * 1024, int(limit) // 1000)
    if not fits(1):
        raise ValueError("Rolling cache capacity cannot hold one frame and the validated 32-frame temporal context at this size.")
    low, high = 1, 2**31 - 1
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low, reserve


def _clean_part(directory):
    target = (Path(directory) / "active-part").resolve()
    if target.parent != Path(directory).resolve() or target.name != "active-part":
        raise RuntimeError("Invalid owned cache cleanup path.")
    if target.exists():
        deadline = time.monotonic() + 3
        while True:
            try:
                shutil.rmtree(target)
                break
            except PermissionError:
                # Windows may finish closing a terminated process's CWD after
                # its exit event. Keep the reservation until files are removed.
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)


def render_pass_video(source, settings, destination, controller, directory, stages, progress=None,
                      *, cache_limit=None, window_frames=None):
    configured_limit = cache_size_bytes(settings.cache_size_gb)
    cache_limit = configured_limit if cache_limit is None else cache_limit
    started = time.perf_counter()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    controller.ffmpeg_device = settings.ffmpeg_device
    meta = prepare_job(controller, lambda: ffmpeg.probe_video(
        source, count_mode="metadata", controller=controller, inspect_timestamps=True))
    source_state = VideoState.from_metadata(meta)
    duration = Fraction(str(meta.get("video_stream_duration") or meta.get("duration") or 0))
    progress_value = 0.0

    def report_progress(value, message):
        nonlocal progress_value
        progress_value = max(progress_value, max(0.0, min(1.0, float(value))))
        if progress:
            progress(progress_value, message)

    stats = dict(source=str(source), cache_limit=cache_limit, cache_size_gb=settings.cache_size_gb,
                 passes=[], parts=0,
                 part_caches=[], cache_peak_bytes=0)
    controller.render_stats = stats
    budget = CacheBudget(cache_limit)
    controller.cache_budget = budget
    with active_job(controller):
        steps = prepare_job(controller, lambda: plan_passes(stages, source_state, settings, directory, controller))
        # FG may require several interpolation passes for one enabled card.
        # The visible stage belongs to the card, while progress weights each pass.
        stage_indices = {stage: number for number, stage in enumerate(stages, 1)}
        if not steps:
            from .workflow import _export_video
            controller.report_phase(state="delivery", worker_processes=0, cache_bytes=0, fps=0.0)
            prepare_job(controller, lambda: _export_video(source, source, destination, settings, controller,
                pipeline_hdr=source_state.hdr, progress=progress, verify_output=True))
            controller.report_phase(state="complete", cache_bytes=0, worker_processes=0)
            return str(destination)
        cap, working_reserve = part_frame_limit(steps, source_state.rate, cache_limit)
        if window_frames is not None:
            cap = min(cap, max(1, int(window_frames)))
        stats["part_frame_limit"] = cap
        reserve = 0
        from ..core.rolling_cleanup import mark_owner
        owner_marker = mark_owner(directory)
        delivery = PacketDelivery(directory, source, destination, settings, controller)
        checkpoint = dict(start="0", owned_origin=0, cursor={}, cfr=bool(meta.get("cfr", True)))
        part = 1
        try:
            while True:
                if controller.pause_requested.is_set():
                    controller.release_processes()
                    controller.wait_for_resume()
                if budget.used_bytes:
                    raise RuntimeError("The previous part still owns cache memory.")
                budget = CacheBudget(cache_limit)
                controller.cache_budget = budget
                part_started = time.perf_counter()
                paused = committed = False
                current = None
                remaining = int(meta.get("frames") or 0) - int(checkpoint["owned_origin"])
                frame_limit = min(cap, remaining) if remaining > 0 else cap
                window = {**checkpoint, "frame_limit": frame_limit, "end": None,
                          "source_start": None, "source_stop": None, "owned_stop": int(checkpoint["owned_origin"]) + frame_limit}
                segment = directory / "active-delivery.nut"
                part_directory = directory / "active-part"
                try:
                    controller.check()
                    budget.reserve(working_reserve)
                    reserve = working_reserve
                    with ExitStack() as resources:
                        for index, step in enumerate(steps):
                            controller.check()
                            final = index == len(steps) - 1
                            output = None
                            if not final:
                                cache_type = RGBFrameBlocks if step.mode == "Fast compressed" else FrameBlocks
                                output = cache_type(part_directory, budget, step.after.width, step.after.height,
                                    step.format, _capacity(step, source_state.rate, frame_limit))
                                resources.callback(output.close)
                            def report_pass(position, message):
                                start = Fraction(window["start"])
                                end = (Fraction(window["end"]) if window.get("end") is not None
                                       else start + Fraction(frame_limit, 1) / source_state.rate)
                                span = max(0.0, float(end - start))
                                local = max(0.0, min(span, float(Fraction(position) - start)))
                                fraction = (float(start) + (index * span + local) / len(steps)) / max(.001, float(duration))
                                report_progress(min(.97, .97 * fraction), message)

                            stage_index = stage_indices[step.stage]
                            title = f"Part {part} · Stage {stage_index} of {len(stages)} · {step.label}"
                            controller.report_phase(state="load", part=part, stage=step.label, fps=0.0,
                                                    stage_index=stage_index, stage_count=len(stages),
                                                    frames=0, owned_frames=0, preroll=0,
                                                    total_frames=math.ceil(Fraction(frame_limit, 1) * step.after.rate / source_state.rate),
                                                    cache_bytes=budget.used_bytes, cache_peak_bytes=budget.peak_bytes)
                            report_pass(window["start"], f"{title} · loading")
                            payload = dict(stage=step.stage, before=asdict(step.before), after=asdict(step.after),
                                settings=asdict(settings), source=str(Path(source).resolve()), source_rate=str(source_state.rate),
                                input=current.descriptor if current else None,
                                output=output.descriptor if output else None, segment=str(segment.resolve()),
                                window=window, phase_origin=int(checkpoint["owned_origin"]), timeline=step.timeline,
                                fg=step.fg, bounded_time=any(s.fg for s in steps))
                            if current:
                                # The global phase follows the cached input's first
                                # sample, rather than restarting grain/NR at each part.
                                first_timing = current.timing(0)
                                payload["phase_origin"] = (first_timing.source_index if first_timing.source_index >= 0
                                    else math.floor(first_timing.stamp * step.before.rate))
                            else:
                                # An empty checkpoint seeks from frame zero
                                # for preroll, even when this part owns later
                                # frames. Grain phase follows decoded input.
                                payload["phase_origin"] = int((window.get("cursor") or {}).get("count", 0))
                            def on_event(event):
                                kind = event.get("event")
                                if kind == "progress":
                                    controller.report_phase(state="process", fps=float(event.get("fps") or 0),
                                        frames=int(event.get("frames") or 0), owned_frames=int(event.get("owned") or 0),
                                        preroll=int(event.get("preroll") or 0), worker_processes=1,
                                        cache_bytes=budget.used_bytes, cache_peak_bytes=budget.peak_bytes)
                                    report_pass(event["stamp"],
                                        f"{title}: {event['frames']} frames · {event['fps']:.1f} FPS · cache {budget.used_bytes / 1e9:.2f} GB")
                                elif kind in {"state", "model-loaded", "model-released"}:
                                    state = event.get("state", "process" if kind == "model-loaded" else "unload")
                                    controller.report_phase(state=state, fps=0.0, worker_processes=1,
                                        cache_bytes=budget.used_bytes, cache_peak_bytes=budget.peak_bytes)
                            result = run_request(payload, part_directory / f"pass-{index}", controller, on_event=on_event,
                                output_cache=output, input_cache=current if isinstance(current, FrameBlocks) else None)
                            window = result["window"]
                            if window.get("source_time_origin") is not None:
                                delivery.source_time_origin = Fraction(window["source_time_origin"])
                            result["switch_overhead_seconds"] = max(0, result["worker"]["scope_seconds"] -
                                result["elapsed_seconds"] - result.get("cache_io_seconds", 0))
                            stats["passes"].append({**result, "part": part, "label": step.label})
                            controller.report_phase(state="unload", fps=0.0, swap_seconds=result["switch_overhead_seconds"],
                                                    startup_seconds=result.get("first_output_seconds") or 0,
                                                    worker_processes=0, cache_bytes=budget.used_bytes)
                            report_pass(window["end"], f"{title} · complete")
                            if current:
                                current.close()
                            current = output
                        controller.report_phase(state="commit", fps=0.0, cache_bytes=budget.used_bytes)
                        start = window["start"] if steps[-1].timeline else window["source_start"]
                        delivery.append(segment, Fraction(start))
                        checkpoint = dict(start=window["end"], owned_origin=window["next_owned"],
                                          cursor=window["next_cursor"], cfr=window["cfr"])
                        stats.update(parts=part,
                                     output_frames=delivery.frames, source_frames=window["next_owned"])
                        (directory / "checkpoint.json").write_text(json.dumps(checkpoint, default=str), encoding="utf-8")
                        committed = True
                except Paused:
                    controller.release_processes()
                    paused = True
                finally:
                    stats["cache_peak_bytes"] = max(stats["cache_peak_bytes"], budget.peak_bytes)
                    segment.unlink(missing_ok=True)
                    Path(str(segment) + ".json").unlink(missing_ok=True)
                    _clean_part(directory)
                    budget.release(reserve)
                    reserve = 0
                    stats["part_caches"].append(dict(part=part, frame_limit=frame_limit,
                        source_frames=int(window.get("owned_frames") or 0) if committed else 0,
                        peak_bytes=budget.peak_bytes, remaining_bytes=budget.used_bytes,
                        committed=committed, paused=paused,
                        seconds=time.perf_counter() - part_started))
                    if budget.used_bytes:
                        raise RuntimeError("Part cleanup left a live cache reservation.")
                    controller.report_phase(state="cache-cleared", fps=0.0, worker_processes=0,
                        cache_bytes=0, cache_peak_bytes=stats["cache_peak_bytes"])
                if paused:
                    # A paused part is replayed from the last saved checkpoint;
                    # all its buffers are gone before waiting for the user.
                    controller.wait_for_resume()
                    continue
                if window["last"]:
                    duration = Fraction(window["end"])
                    break
                part += 1
            # Every frame/model buffer has been returned. CPU-only delivery
            # starts after the last part's cache has already been cleared.
            controller.report_phase(state="delivery", fps=0.0, worker_processes=0, cache_bytes=0)
            report_progress(.98, "Finalizing video")
            delivery.finish(steps[-1].after, duration)
            stats.update(elapsed_seconds=time.perf_counter() - started,
                         process_trees_released=not controller._processes)
        finally:
            delivery.close()
            controller.release_processes()
            budget.release(reserve)
            owner_marker.unlink(missing_ok=True)
            stats["cache_peak_bytes"] = max(stats["cache_peak_bytes"], budget.peak_bytes)
            (directory / "rolling-report.json").write_text(json.dumps(stats, indent=2, default=str), encoding="utf-8")
            if budget.used_bytes:
                raise RuntimeError("Rolling cache cleanup left a live reservation.")
        controller.report_phase(state="complete", fps=0.0, cache_bytes=0, worker_processes=0)
    app_log.info("rolling-cache", f"complete parts={part} peak={stats['cache_peak_bytes']} limit={budget.limit} "
                 f"elapsed={time.perf_counter() - started:.3f}s")
    report_progress(1.0, "Export complete")
    return str(destination)
