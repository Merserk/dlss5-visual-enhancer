from __future__ import annotations

import traceback
from pathlib import Path

import gradio as gr

from ..core.ffmpeg import probe_video
from ..core.ffmpeg.preview import (
    is_browser_playable, make_browser_preview, normalize_preview_encoding,
    resolve_preview_codec, wants_compat_preview,
)
from ..core.preview_duration import resolve_duration
from ..settings.storage import current_preview_encoding, processing_gpu_settings
from .capabilities import probe_frame_interpolation_capabilities
from .models import FrameInterpolationOptions
from .processor import interpolate_video
from .scheduler import choose_interpolation_plan


def normalize_video_paths(paths: list[str] | str | None) -> list[str]:
    if not paths:
        return []
    return [paths] if isinstance(paths, str) else list(paths)


def first_video_path(paths: list[str] | str | None) -> str | None:
    normalized = normalize_video_paths(paths)
    return normalized[0] if normalized else None

def frame_interpolation_capability_text() -> str:
    ai_gpu_uuid, _video_gpu_uuid = processing_gpu_settings()
    capabilities = probe_frame_interpolation_capabilities(ai_gpu_uuid)
    hags = "Enabled" if capabilities.hags_enabled else "Disabled"
    native = (
        f"{capabilities.native_multiplier}× "
        f"({capabilities.native_generated_frame_max} generated frame per evaluation)"
    )
    cascade = "Available" if capabilities.cascade_available else "Unavailable"
    state = "Ready" if capabilities.available else "Unavailable"
    detail = f"\n{capabilities.detail}" if capabilities.detail else ""
    return (
        f"{state} — GPU: {capabilities.gpu} | Driver: {capabilities.driver} | "
        f"HAGS: {hags}\nNative maximum: {native} | Experimental cascade: {cascade} | "
        f"DLSSG runtime: {capabilities.runtime_version}{detail}"
    )


def describe_frame_interpolation_plan(
    paths: list[str] | str | None,
    target_fps: str,
    engine: str,
) -> str:
    selected = first_video_path(paths)
    if not selected:
        return "Choose a video to preflight its DLSSG path and temporal precision."
    try:
        metadata = probe_video(selected, count_mode="metadata")
        ai_gpu_uuid, _video_gpu_uuid = processing_gpu_settings()
        capabilities = probe_frame_interpolation_capabilities(ai_gpu_uuid)
        plan = choose_interpolation_plan(
            metadata["rate"],
            FrameInterpolationOptions(target_fps=target_fps).target_rate,
            engine,
            capabilities.native_multiplier,
            cfr=bool(metadata.get("cfr", True)),
        )
        if plan.cascade_stages:
            precision = (
                f"≤ {float(plan.maximum_temporal_error) * 1000:.3f} ms "
                f"(1/{2 * plan.grid_multiplier} source-frame interval)"
            )
        elif plan.path == "Native DLSSG":
            precision = "Exact native temporal grid"
        else:
            precision = "Nearest real source frame"
        return (
            f"{Path(selected).name}: {metadata['rate']} FPS → {target_fps} FPS | "
            f"Path: {plan.path} | Cascade stages: {plan.cascade_stages} | "
            f"Internal grid: {plan.grid_multiplier}× | Precision: {precision}"
        )
    except Exception as exc:
        return f"Preflight: {exc}"


def update_frame_interpolation_preview_mode(
    paths: list[str] | str | None,
    target_fps: str,
    engine: str,
):
    normalized = normalize_video_paths(paths)
    available = bool(normalized)
    single = len(normalized) == 1
    label = "Input video preview" if len(normalized) <= 1 else f"Input video preview (first of {len(normalized)})"
    return (
        gr.update(value=normalized[0] if available else None, visible=available, label=label),
        gr.update(value=None, visible=True),
        gr.update(visible=single),
        gr.update(visible=single),
    )

def preview_frame_interpolation(
    input_paths: list[str] | str | None,
    target_fps: str,
    engine: str,
    codec: str,
    container: str,
    quality: str,
    duration: str,
    progress=gr.Progress(track_tqdm=False),
):
    selected = first_video_path(input_paths)
    if not selected:
        raise gr.Error("Choose one video first.")
    try:
        preview_mode = normalize_preview_encoding(current_preview_encoding())
    except Exception:
        preview_mode = "Auto"
    effective_codec, effective_container = resolve_preview_codec(
        codec, container, preview_mode
    )
    compat_preview = wants_compat_preview(codec, container, preview_mode)
    # Frame Interpolation only ever offers timed clips (no "1 frame" choice --
    # see core.preview_duration), so this always resolves to a seconds value.
    preview_seconds, _preview_frames = resolve_duration(duration)
    options = FrameInterpolationOptions(
        ai_gpu_uuid=processing_gpu_settings()[0],
        video_gpu_uuid=processing_gpu_settings()[1],
        target_fps=target_fps,
        engine=engine,
        codec=effective_codec,
        container=effective_container,
        quality=quality,
        preview_seconds=preview_seconds,
        preview_compat=compat_preview,
    )
    try:
        result = interpolate_video(
            selected,
            options,
            lambda value, message: progress(value, desc=message),
        )
    except Exception as exc:
        traceback.print_exc()
        return None, f"Failed: {exc}"
    output_preview = result.output_path
    derived_note = ""
    if preview_mode == "Auto" and not compat_preview:
        try:
            playable = is_browser_playable(result.output_path)
        except Exception:
            playable = False
        if not playable:
            try:
                output_preview = make_browser_preview(result.output_path)
                derived_note = " (browser preview transcoded to H.264)"
            except Exception:
                output_preview = result.output_path
    return output_preview, (
        f"Preview complete: {result.output_frames} frames, {result.selected_path}, "
        f"{result.cascade_stages} cascade stage(s), {result.generated_frames} DLSSG-selected "
        f"frames in {result.elapsed_seconds:.1f}s. Report: {result.report_path}"
        f"{derived_note}"
    )
