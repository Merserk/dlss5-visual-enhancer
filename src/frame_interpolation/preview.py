from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..core.ffmpeg import probe_video
from ..core.ffmpeg.preview import LOSSLESS_PREVIEW_CODEC
from ..settings.storage import processing_gpu_settings
from .capabilities import probe_frame_interpolation_capabilities
from .models import FrameInterpolationOptions
from .processor import interpolate_video
from .scheduler import choose_interpolation_plan

PREVIEW_SECONDS = 3.0


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
        f"{state} — GPU: {capabilities.gpu} | Driver: {capabilities.driver} | HAGS: {hags}\n"
        f"Native maximum: {native} | Cascade: {cascade} | "
        f"NVOF: {'SLOW/available' if capabilities.nvof_available else 'unavailable'}\n"
        f"Bridge: {capabilities.bridge_version} (ABI {capabilities.bridge_abi_version}) | "
        f"CUDA interop: {'ready' if capabilities.cuda_interop else 'unavailable'} | "
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
        metadata = probe_video(selected, count_mode="metadata", inspect_timestamps=True)
        ai_gpu_uuid, _video_gpu_uuid = processing_gpu_settings()
        capabilities = probe_frame_interpolation_capabilities(ai_gpu_uuid)
        plan = choose_interpolation_plan(
            metadata["rate"], FrameInterpolationOptions(target_fps=target_fps).target_rate,
            engine, capabilities.native_multiplier, cfr=bool(metadata.get("cfr", True)),
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


def preview_frame_interpolation_native(
    input_path: str,
    options: FrameInterpolationOptions,
    *,
    progress=None,
    output_dir=None,
    controller=None,
    start_seconds: float | None = None,
    frame_index: int | None = None,
    preview_seconds: float | None = None,
) -> tuple[str | None, str]:
    if not input_path or not Path(input_path).is_file():
        raise ValueError("Choose one valid video first.")
    try:
        length_seconds = max(0.0, float(preview_seconds if preview_seconds is not None else PREVIEW_SECONDS))
    except (TypeError, ValueError):
        length_seconds = PREVIEW_SECONDS
    if not (length_seconds > 0):
        length_seconds = PREVIEW_SECONDS
    try:
        start = max(0.0, float(start_seconds or 0.0))
    except (TypeError, ValueError):
        start = 0.0
    try:
        frame_no = max(1, int(frame_index or 1)) if frame_index else 1
    except (TypeError, ValueError):
        frame_no = 1
    effective_path = str(input_path)
    temp_clip: str | None = None
    if start > 0:
        from ..core.ffmpeg.preview import extract_preview_subclip
        temp_clip = extract_preview_subclip(
            str(input_path), dest_dir=output_dir, start_seconds=start,
            length_seconds=length_seconds, controller=controller,
        )
        effective_path = temp_clip
    try:
        effective = replace(
            options,
            codec=LOSSLESS_PREVIEW_CODEC,
            container="MKV",
            quality="Auto (Default)",
            hdr_mode=bool(options.hdr_mode or probe_video(effective_path, count_mode="metadata", controller=controller).get("hdr")),
            preview_seconds=length_seconds,
        )
        result = interpolate_video(
            effective_path, effective, progress, output_dir=output_dir, controller=controller)
    finally:
        if temp_clip:
            try:
                Path(temp_clip).unlink(missing_ok=True)
            except OSError:
                pass
    return result.output_path, (
        f"Preview complete f{frame_no} @ {start:.2f}s: {result.output_frames} frames, {result.selected_path}, "
        f"{result.cascade_stages} cascade stage(s), {result.generated_frames} DLSSG-selected "
        f"frames in {result.elapsed_seconds:.1f}s. Cached clip: {result.output_path}\nReport: {result.report_path}"
    )
