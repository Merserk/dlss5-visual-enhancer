from __future__ import annotations

import json
import contextlib
import math
import mmap
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .gpu_detection import detect_gpus
from .gpu_memory import GPUVramTracker
from .gpu_selection import resolve_runtime_ai_gpu
from .jobs import Cancelled, JobController
from .neural_bridge import (
    BRIDGE_ABI_VERSION,
    BRIDGE_MANAGER,
    BridgeSessionDiagnostics,
    CudaFrameBuffers,
    CudaMaskBuffer,
    FORMAT_NV12,
    FORMAT_P010,
)
from .nr_control_mask import mask_report, mask_selection, prepare_nr_mask
from .paths import (
    DLSSNR_BRIDGE,
    DLSSNR_DIR,
    DLSSG_DIR,
    FFMPEG,
    FFPROBE,
    NEURAL_RUNTIME,
    RUNTIME,
)

NR_STYLES = {
    "Style 0": 0,
    "Style 1": 1,
    "Style 2": 2,
}

# Feature 18 is evaluated at the final neural dimensions. Scaling below 1x is
# an explicit Lanczos downscale before Neural Rendering; scaling above 1x is
# an explicit Lanczos upscale before Neural Rendering.
UPSCALING_MODES = {
    1.0: {"label": "Source (Original)", "name": "Source", "perf_quality": 0},
    2.0: {"label": "200%", "name": "Lanczos 200%", "perf_quality": 0},
    1.75: {"label": "175%", "name": "Lanczos 175%", "perf_quality": 0},
    1.5: {"label": "150%", "name": "Lanczos 150%", "perf_quality": 0},
    1.25: {"label": "125%", "name": "Lanczos 125%", "perf_quality": 0},
    0.75: {"label": "75%", "name": "Lanczos 75%", "perf_quality": 0},
    0.5: {"label": "50%", "name": "Lanczos 50%", "perf_quality": 0},
    0.25: {"label": "25%", "name": "Lanczos 25%", "perf_quality": 0},
}
UPSCALING_CHOICES = tuple(
    (mode["label"], factor) for factor, mode in UPSCALING_MODES.items()
)

def resolve_upscaling_mode(raw_factor: float) -> tuple[float, dict[str, str | int]]:
    try:
        factor = float(raw_factor)
    except (TypeError, ValueError) as exc:
        raise ValueError("Scale must be one of: Source, 200%, 175%, 150%, 125%, 75%, 50%, 25%.") from exc
    if not math.isfinite(factor):
        raise ValueError("Scale must be one of: Source, 200%, 175%, 150%, 125%, 75%, 50%, 25%.")
    for supported, mode in UPSCALING_MODES.items():
        if math.isclose(factor, supported, rel_tol=0.0, abs_tol=1e-9):
            return supported, mode
    raise ValueError("Scale must be one of: Source, 200%, 175%, 150%, 125%, 75%, 50%, 25%.")

def _nearest_even(value: float) -> int:
    return max(2, int(math.floor(value / 2.0 + 0.5)) * 2)

def resolve_output_size(width: int, height: int, factor: float) -> tuple[int, int]:
    factor, _ = resolve_upscaling_mode(factor)
    output_width = _nearest_even(int(width) * factor)
    output_height = _nearest_even(int(height) * factor)
    if min(output_width, output_height) < 64:
        raise ValueError(
            f"The requested {output_width}×{output_height} output is below the supported "
            f"64×64 minimum. Choose Source, 75%, or 50% for this input."
        )
    return output_width, output_height

OPTICAL_FLOW_QUALITIES = ("High", "Medium", "Low")


def resolve_native_settings(options: Any) -> dict[str, int | float | bool]:
    try:
        style = NR_STYLES[options.nr_style]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Unknown NR Style: {options.nr_style!r}. Choose one of: {', '.join(NR_STYLES)}.") from exc
    controls = {
        "intensity": ("NR Intensity", options.nr_intensity, 0.0, 2.0),
        "local_tone": ("Local Tone Strength", options.local_tone_strength, 0.0, 1.0),
        "local_structure": ("Local Structure Strength", options.local_structure_strength, 0.0, 1.0),
        "skin_structure": ("Skin Structure Strength", options.skin_structure_strength, 0.0, 1.0),
    }
    validated = {}
    for key, (label, raw, minimum, maximum) in controls.items():
        if isinstance(raw, bool):
            raise ValueError(f"{label} must be a finite number between {minimum:g} and {maximum:g}.")
        try:
            value = float(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label} must be a finite number between {minimum:g} and {maximum:g}.") from exc
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        validated[key] = value
    if not isinstance(options.automatic_mask, bool):
        raise ValueError("Automatic Mask must be a boolean value.")
    passes = getattr(options, "nr_passes", 1)
    if isinstance(passes, bool) or not isinstance(passes, int) or not 1 <= passes <= 4:
        raise ValueError("NR Passes must be an integer from 1 to 4.")
    flow_quality = getattr(options, "nr_optical_flow_quality", "High")
    if not isinstance(flow_quality, str) or flow_quality not in OPTICAL_FLOW_QUALITIES:
        raise ValueError("Optical Flow Quality must be High, Medium, or Low.")
    return {"style": style, "auto_mask": int(options.automatic_mask and not getattr(options, "nr_mask", None)),
            "nr_passes": passes, "native_intensity": min(1.0, validated["intensity"]),
            "optical_flow_quality": OPTICAL_FLOW_QUALITIES.index(flow_quality),
            "gpu_mode": True, **validated}

def inspect_runtime_bundle(
    bridge_path: Path | None = None,
    neural_path: Path | None = None,
) -> dict[str, Any]:
    bridge = bridge_path or DLSSNR_BRIDGE
    neural = neural_path or NEURAL_RUNTIME
    return {
        "bridge": {
            "path": str(bridge.resolve()),
            "version": BRIDGE_MANAGER.version,
            "abi_version": BRIDGE_ABI_VERSION,
            "release": "D3D12/NGX CUDA bridge",
        },
        "caller": {"integrated": True, "path": str(bridge.resolve())},
        "neural_runtime": {
            "path": str(neural.resolve()),
            "version": "driver-compatible",
            "release": "NVIDIA DLSS Neural Rendering runtime",
        },
    }

def validate_gpu_runtime(
    gpu: dict[str, Any], bundle: dict[str, Any] | None = None
) -> dict[str, Any]:
    del gpu
    return bundle or inspect_runtime_bundle()

def write_failure_report(
    *,
    operation: str,
    source: str,
    error: BaseException | str,
    gpu: dict[str, Any] | None,
    runtime_bundle: dict[str, Any] | None,
    bridge_status: dict[str, Any] | None = None,
    bridge_log: list[str] | None = None,
    logs_dir: Path | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> Path:
    """Persist a compact ``.err`` failure file and log one line."""
    from . import app_log

    safe_operation = re.sub(r"[^A-Za-z0-9_.-]+", "-", operation).strip("-") or "render"
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    gpu_name = ""
    try:
        gpu_name = str((gpu or {}).get("display_name") or (gpu or {}).get("name") or "")
    except Exception:
        gpu_name = ""
    tails: dict[str, object] = {}
    if bridge_log:
        tails["bridge"] = list(bridge_log)[-40:]
    if diagnostics:
        tails["diagnostics"] = str(diagnostics)[-4000:]
    return app_log.fail(
        safe_operation,
        f"{safe_operation}-failure-{stamp}",
        f"{error} | src={Path(source).name} {gpu_name}".strip(),
        tails or None,
    )

def resize_fit(rgba: np.ndarray, width: int, height: int, *,
               interpolation: str = "Lanczos4", controller=None) -> np.ndarray:
    from .ffmpeg.filters import resize_fit as vulkan_resize
    return vulkan_resize(rgba, width, height, interpolation=interpolation, controller=controller)

def rotate_frame(frame: np.ndarray, rotation: int, *, controller=None) -> np.ndarray:
    if rotation not in (90, 180, 270):
        return frame
    from .ffmpeg.filters import filter_array
    from .ffmpeg.vulkan import libplacebo
    width = frame.shape[0] if rotation != 180 else frame.shape[1]
    height = frame.shape[1] if rotation != 180 else frame.shape[0]
    graph = libplacebo(f"rotate={rotation // 90}:w={width}:h={height}")
    return filter_array(frame, graph, width=width, height=height, controller=controller)

def validate_runtime_files() -> None:
    required = [FFMPEG, FFPROBE, DLSSNR_BRIDGE, NEURAL_RUNTIME]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "The Neural Rendering runtime is incomplete:\n" + "\n".join(missing)
        )

class DLSSFrameSession:
    """Virtual feature-18 render session running on the shared system bridge."""

    def __init__(
        self,
        *,
        input_width: int,
        input_height: int,
        output_width: int,
        output_height: int,
        frame_count: int | None,
        warmup_frames: int,
        factor: float,
        mode: dict[str, str | int],
        native_settings: dict[str, int | float | bool],
        gpu: dict[str, Any],
        runtime_bundle: dict[str, Any],
        controller: JobController,
        cuda_video: bool = False,
        still_image: bool = False,
        control_mask: object | None = None,
    ) -> None:
        del input_width, input_height, warmup_frames
        if frame_count is not None and (
            isinstance(frame_count, bool)
            or not isinstance(frame_count, int)
            or not 0 < frame_count <= 0xFFFFFFFF
        ):
            raise ValueError("Native frame count must be a positive uint32 or None.")
        if output_width < 64 or output_height < 64:
            raise ValueError("Neural dimensions must be at least 64×64.")
        self.controller = controller
        self._streaming = frame_count is None
        self._expected_frames = frame_count
        self._processed_frames = 0
        self._next_frame_index: int | None = None
        self.completed_frames: int | None = None
        self.closed = False
        self.factor = factor
        self.mode = mode
        self.still_image = bool(still_image)
        self.native_settings = {**native_settings, "motion_mode": 0 if self.still_image else 1}
        self.control_mask = mask_selection(control_mask)
        self.gpu = gpu
        self.runtime_bundle = runtime_bundle
        self.output_width = int(output_width)
        self.output_height = int(output_height)
        self.render_width = int(output_width)
        self.render_height = int(output_height)
        self.minimum_width = 64
        self.minimum_height = 64
        self.maximum_width = 16384
        self.maximum_height = 16384
        self.setup_result = 1
        self.process_timings = {
            "input_conversion_seconds": 0.0,
            "input_transfer_seconds": 0.0,
            "evaluation_wait_seconds": 0.0,
            "output_transfer_seconds": 0.0,
            "output_conversion_seconds": 0.0,
            "optical_flow_seconds": 0.0,
        }
        if native_settings.get("gpu_mode", True) is not True:
            raise ValueError("Neural Rendering requires CUDA/D3D12 GPU processing.")
        self.cuda_video = bool(cuda_video)
        self.diagnostics = BridgeSessionDiagnostics(
            gpu_mode=True,
            memory_path="cuda_d3d12_shared" if self.cuda_video else "host_cuda_d3d12_shared",
        )
        self._input_float = (
            None if self.cuda_video else np.empty(
                (self.output_height, self.output_width, 3), dtype=np.float32
            )
        )
        self._output_float = (
            None if self._input_float is None else np.empty_like(self._input_float)
        )
        self._cuda_buffers: CudaFrameBuffers | None = None
        self._mask_host = prepare_nr_mask(
            self.control_mask, self.output_width, self.output_height,
        )
        self._cuda_mask: CudaMaskBuffer | None = None
        self._logs: list[str] = []
        self._temporal_status_cache: dict[str, Any] = {}
        status = BRIDGE_MANAGER.initialize(gpu)
        self.bridge_status = {
            **status,
            "memory_path": self.diagnostics.memory_path,
            "neural_dimensions": {
                "width": self.output_width,
                "height": self.output_height,
            },
            "resize_method": "none" if factor == 1.0 else "lanczos",
            "nr_passes": int(native_settings.get("nr_passes", 1)),
            "allocated_feature_instances": int(native_settings.get("nr_passes", 1)),
            "native_control_mask": mask_report(self.control_mask),
            "temporal_guides": {"motion_backend": "initializing", "depth_bound": False, "motion_bound": False},
        }
        BRIDGE_MANAGER.open_session()
        self._manager_open = True
        self._vram_tracker = GPUVramTracker(int(gpu.get("index", 0)))
        self._vram_status: dict[str, object] | None = None
        try:
            if not self.cuda_video:
                self._cuda_buffers = BRIDGE_MANAGER.create_cuda_buffers(
                    self.output_width, self.output_height
                )
            if self._mask_host is not None:
                self._cuda_mask = BRIDGE_MANAGER.create_cuda_mask(self._mask_host)
            self._logs.append(
                json.dumps(
                    {"event": "session_open", **self.bridge_status},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:
            self._vram_tracker.close()
            if self._cuda_mask is not None:
                with contextlib.suppress(Exception):
                    self._cuda_mask.close()
                self._cuda_mask = None
            if self._cuda_buffers is not None:
                with contextlib.suppress(Exception):
                    self._cuda_buffers.close()
                self._cuda_buffers = None
            BRIDGE_MANAGER.close_session(discard=True)
            self._manager_open = False
            raise

    @property
    def bridge_logs(self) -> list[str]:
        return list(self._logs)

    @property
    def logs(self) -> list[str]:
        return self.bridge_logs

    @property
    def bridge_log_dropped_lines(self) -> int:
        return 0

    def structured_status(self) -> dict[str, Any]:
        temporal = dict(self.bridge_status.get("temporal_guides", {}))
        current_temporal = BRIDGE_MANAGER.temporal_status()
        if current_temporal and (
            bool(current_temporal.get("depth_bound"))
            or not self._temporal_status_cache
        ):
            self._temporal_status_cache = current_temporal
        temporal.update(self._temporal_status_cache)
        if self.diagnostics.frames:
            # The native feature is released at the final logical-session
            # boundary, so retain deterministic counters even when a report is
            # assembled after close.
            temporal["motion_backend"] = temporal.get("motion_backend") if (
                temporal.get("motion_frames", 0)
            ) else ("static_image" if self.still_image else "nvidia_nvofa")
            temporal["motion_frames"] = max(
                int(temporal.get("motion_frames", 0)), self.diagnostics.frames
            )
            temporal["reset_frames"] = max(
                int(temporal.get("reset_frames", 0)), self.diagnostics.scene_resets + 1
            )
            temporal["depth_bound"] = True
            temporal["motion_bound"] = True
        return {
            **self.bridge_status,
            **self.diagnostics.as_dict(),
            "gpu_memory": self._vram_status or self._vram_tracker.snapshot(),
            "temporal_guides": temporal,
        }

    def update_control_mask(self, selection: object | None) -> None:
        """Swap the native mask resource at a frame boundary."""
        selected = mask_selection(selection)
        prepared = prepare_nr_mask(selected, self.output_width, self.output_height)
        replacement = None if prepared is None else BRIDGE_MANAGER.create_cuda_mask(prepared)
        previous = self._cuda_mask
        self._mask_host, self._cuda_mask, self.control_mask = prepared, replacement, selected
        self.bridge_status["native_control_mask"] = mask_report(selected)
        if previous is not None:
            previous.close()

    def _capture_temporal_status(self) -> None:
        current = BRIDGE_MANAGER.temporal_status()
        if current:
            self._temporal_status_cache = current

    def process(
        self,
        *,
        index: int,
        rgba: np.ndarray,
        motion: np.ndarray | None = None,
        reset: bool,
        pts: int,
        output_buffer: np.ndarray | None = None,
    ) -> tuple[np.ndarray, int]:
        del motion
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.closed:
            raise RuntimeError("The Neural Rendering bridge session is closed.")
        if self._next_frame_index is None:
            self._next_frame_index = int(index)
        if index != self._next_frame_index:
            raise ValueError("Neural Rendering frames must have consecutive uint32 indices.")
        if rgba.dtype not in (np.uint8, np.uint16) or rgba.shape != (
            self.output_height,
            self.output_width,
            4,
        ):
            raise ValueError("Neural Rendering input must be RGBA8 or RGBA16 at final size.")
        rgba = np.ascontiguousarray(rgba)
        self.diagnostics.working_format = "rgba16le" if rgba.dtype == np.uint16 else "rgba8"
        self.diagnostics.output_format = self.diagnostics.working_format
        if output_buffer is None:
            output = np.empty_like(rgba)
        else:
            output = output_buffer
            if (
                output.dtype != rgba.dtype
                or output.shape != rgba.shape
                or not output.flags.c_contiguous
            ):
                raise ValueError("Neural Rendering output buffer has the wrong shape or layout.")

        started = time.perf_counter()
        assert self._input_float is not None and self._output_float is not None
        max_sample = float(np.iinfo(rgba.dtype).max)
        np.multiply(rgba[..., :3], 1.0 / max_sample, out=self._input_float, casting="unsafe")
        self.process_timings["input_conversion_seconds"] += time.perf_counter() - started

        assert self._cuda_buffers is not None
        upload, evaluate, download = BRIDGE_MANAGER.process_cuda(
            self._input_float,
            self._output_float,
            self._cuda_buffers,
            {**self.native_settings, "motion_mode": 0 if self.still_image else 1},
            bool(reset),
            self._mask_host,
            self._cuda_mask,
        )
        self.process_timings["input_transfer_seconds"] += upload
        self.process_timings["evaluation_wait_seconds"] += evaluate
        self.process_timings["output_transfer_seconds"] += download

        started = time.perf_counter()
        np.multiply(
            np.clip(self._output_float, 0.0, 1.0),
            max_sample,
            out=self._output_float,
        )
        np.rint(self._output_float, out=self._output_float)
        np.copyto(output[..., :3], self._output_float, casting="unsafe")
        output[..., 3] = rgba[..., 3]
        self.process_timings["output_conversion_seconds"] += time.perf_counter() - started

        transferred = self._input_float.nbytes
        self.diagnostics.upload_bytes += transferred
        self.diagnostics.download_bytes += self._output_float.nbytes
        self.diagnostics.frames += 1
        self.diagnostics.feature_evaluations += int(self.native_settings.get("nr_passes", 1))
        self.diagnostics.scene_resets += int(reset and index != 0)
        self._processed_frames += 1
        self._next_frame_index += 1
        self._capture_temporal_status()
        self._logs.append(
            json.dumps(
                {
                    "event": "frame",
                    "index": index,
                    "pts": pts,
                    "reset": bool(reset),
                    "ngx_result": "0x00000001",
                    "memory_path": self.diagnostics.memory_path,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if len(self._logs) > 500:
            del self._logs[: len(self._logs) - 500]
        return output, int(pts)

    def score_cuda_frame(self, frame: Any, *, color_matrix: int, color_range: int) -> tuple[float, bool]:
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        return BRIDGE_MANAGER.score_cuda_video_frame(
            frame, threshold=0.24, color_matrix=color_matrix, color_range=color_range
        )

    def process_frame_to_host(
        self,
        *,
        index: int,
        frame: Any,
        reset: bool,
        scene_score: float,
        pts: int,
        color_matrix: int,
        color_range: int,
        rotation: int = 0,
        chroma_location: int = 1,
        output_buffer: np.ndarray | None = None,
        async_transfer: bool = False,
    ) -> tuple[np.ndarray, int]:
        """Use the native video evaluator for CUDA frames or software RGBA."""
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.closed:
            raise RuntimeError("The CUDA Neural Rendering session is unavailable.")
        if self._next_frame_index is None:
            self._next_frame_index = int(index)
        if index != self._next_frame_index:
            raise ValueError("Neural Rendering frames must have consecutive uint32 indices.")
        output = output_buffer
        if output is None:
            output = np.empty((self.output_height, self.output_width, 4), dtype=np.uint8)
        if (
            output.dtype not in (np.uint8, np.uint16)
            or output.shape != (self.output_height, self.output_width, 4)
            or not output.flags.c_contiguous
        ):
            raise ValueError("CUDA-to-host output buffer has the wrong shape or layout.")
        result, elapsed = BRIDGE_MANAGER.process_video_to_host_frame(
            frame,
            output,
            settings={**self.native_settings, "motion_mode": 0 if self.still_image else 1},
            mask=self._mask_host,
            cuda_mask=self._cuda_mask,
            reset=reset,
            timestamp=pts,
            color_matrix=color_matrix,
            color_range=color_range,
            rotation=rotation,
            chroma_location=chroma_location,
            async_transfer=async_transfer,
        )
        self.process_timings["evaluation_wait_seconds"] += elapsed
        self.diagnostics.decode_backend = "software" if isinstance(frame, np.ndarray) else "nvdec"
        self.diagnostics.working_format = "float32-rgb"
        self.diagnostics.output_format = result["output_format"]
        self.diagnostics.pixel_format = result["input_format"]
        self.diagnostics.upload_bytes += int(result["upload_bytes"])
        self.diagnostics.download_bytes += int(result["download_bytes"])
        self.diagnostics.ngx_create_result = result["ngx_create_result"]
        self.diagnostics.ngx_evaluate_result = result["ngx_evaluate_result"]
        self.diagnostics.frames += 1
        self.diagnostics.feature_evaluations += int(self.native_settings.get("nr_passes", 1))
        self.diagnostics.scene_resets += int(reset and index != 0)
        self._processed_frames += 1
        self._next_frame_index += 1
        self._capture_temporal_status()
        self._logs.append(json.dumps({
            "event": "frame", "index": index, "pts": pts,
            "reset": bool(reset), "scene_score": float(scene_score),
            "ngx_create_result": result["ngx_create_result"],
            "ngx_evaluate_result": result["ngx_evaluate_result"],
            "memory_path": self.diagnostics.memory_path,
            "input_format": result["input_format"], "output_format": result["output_format"],
            "upload_bytes": result["upload_bytes"],
            "download_bytes": result["download_bytes"],
        }, sort_keys=True, separators=(",", ":")))
        if len(self._logs) > 500:
            del self._logs[: len(self._logs) - 500]
        return output, int(result["cache_token"] if async_transfer else result["timestamp"])

    process_cuda_frame_to_host = process_frame_to_host

    def process_video_frame(
        self,
        *,
        index: int,
        frame: Any | None = None,
        rgba: np.ndarray | None = None,
        reset: bool,
        scene_score: float,
        pts: int,
        duration: int | None,
        time_base: Any,
        color_matrix: int,
        color_range: int,
        rotation: int = 0,
        chroma_location: int = 1,
        output_p010: bool = False,
    ) -> tuple[Any, int]:
        """Process a hardware or software decoded frame into CUDA NV12/P010."""
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.closed:
            raise RuntimeError("The Neural Rendering bridge session is closed.")
        if not self.cuda_video:
            raise RuntimeError("The CUDA video frame boundary is not enabled.")
        if self._next_frame_index is None:
            self._next_frame_index = int(index)
        if index != self._next_frame_index:
            raise ValueError("Neural Rendering frames must have consecutive uint32 indices.")
        output_format = FORMAT_P010 if output_p010 else FORMAT_NV12
        if frame is not None and rgba is not None:
            raise ValueError("Pass either a CUDA frame or host RGBA pixels, not both.")
        if frame is not None:
            output, result, elapsed = BRIDGE_MANAGER.process_cuda_video_frame(
                frame,
                output_width=self.output_width,
                output_height=self.output_height,
                output_format=output_format,
                settings={**self.native_settings, "motion_mode": 0 if self.still_image else 1},
                mask=self._mask_host,
                cuda_mask=self._cuda_mask,
                reset=reset,
                timestamp=pts,
                color_matrix=color_matrix,
                color_range=color_range,
                rotation=rotation,
                chroma_location=chroma_location,
            )
            self.diagnostics.decode_backend = "nvdec"
            self.diagnostics.working_format = "float32-rgb"
        elif rgba is not None:
            rgba = np.ascontiguousarray(rgba)
            if rgba.dtype not in (np.uint8, np.uint16):
                raise ValueError("Software video input must be RGBA8 or RGBA16.")
            if rgba.shape != (self.output_height, self.output_width, 4):
                raise ValueError("Software video input must be RGBA at final neural dimensions.")
            output, result, elapsed = BRIDGE_MANAGER.process_host_to_cuda_video_frame(
                rgba,
                output_format=output_format,
                settings={**self.native_settings, "motion_mode": 0 if self.still_image else 1},
                mask=self._mask_host,
                cuda_mask=self._cuda_mask,
                reset=reset,
                timestamp=pts,
                color_matrix=color_matrix,
                color_range=color_range,
                time_base=time_base,
                duration=duration,
            )
            self.diagnostics.decode_backend = "software"
            self.diagnostics.working_format = "rgba16le" if rgba.dtype == np.uint16 else "rgba8"
        else:
            raise ValueError("A CUDA frame or host RGBA frame is required.")
        output.pts = int(pts)
        output.time_base = time_base
        if duration is not None:
            output.duration = int(duration)
        self.process_timings["evaluation_wait_seconds"] += elapsed
        self.diagnostics.encode_backend = "nvenc"
        self.diagnostics.pixel_format = result["output_format"]
        self.diagnostics.output_format = result["output_format"]
        self.diagnostics.upload_bytes += int(result["upload_bytes"])
        self.diagnostics.download_bytes += int(result["download_bytes"])
        self.diagnostics.ngx_create_result = result["ngx_create_result"]
        self.diagnostics.ngx_evaluate_result = result["ngx_evaluate_result"]
        self.diagnostics.frames += 1
        self.diagnostics.feature_evaluations += int(self.native_settings.get("nr_passes", 1))
        self.diagnostics.scene_resets += int(reset and index != 0)
        self._processed_frames += 1
        self._next_frame_index += 1
        self._capture_temporal_status()
        event = {
            "event": "frame",
            "index": index,
            "pts": pts,
            "reset": bool(reset),
            "scene_score": float(scene_score),
            "ngx_create_result": result["ngx_create_result"],
            "ngx_evaluate_result": result["ngx_evaluate_result"],
            "memory_path": self.diagnostics.memory_path,
            "input_format": result["input_format"],
            "output_format": result["output_format"],
            "upload_bytes": result["upload_bytes"],
            "download_bytes": result["download_bytes"],
        }
        self._logs.append(json.dumps(event, sort_keys=True, separators=(",", ":")))
        if len(self._logs) > 500:
            del self._logs[: len(self._logs) - 500]
        return output, int(result["timestamp"])

    def close(self) -> None:
        if self.closed:
            return
        if self.controller.cancel.is_set():
            self.abort()
            raise Cancelled("Render stopped by user.")
        if self._streaming and not self._processed_frames:
            self.abort()
            raise RuntimeError("The input contains no decodable frames.")
        if self._expected_frames is not None and self._processed_frames != self._expected_frames:
            self.abort()
            raise RuntimeError(
                f"Neural Rendering processed {self._processed_frames} of "
                f"{self._expected_frames} expected frames."
            )
        self.completed_frames = self._processed_frames
        self._close_resources()

    def _close_resources(self, *, discard: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        from contextlib import ExitStack
        buffers, self._cuda_buffers = self._cuda_buffers, None
        mask, self._cuda_mask = self._cuda_mask, None
        manager_open, self._manager_open = self._manager_open, False
        # Every owner must be released even if diagnostics or a CUDA buffer's
        # cleanup fails. Mark closed first so abort/close remain idempotent.
        def close_tracker():
            try:
                self._vram_tracker.close()
            finally:
                self._vram_status = self._vram_tracker.snapshot()

        with ExitStack() as cleanup:
            if manager_open:
                cleanup.callback(BRIDGE_MANAGER.close_session, discard=discard)
            if mask is not None:
                cleanup.callback(mask.close)
            if buffers is not None:
                cleanup.callback(buffers.close)
            cleanup.callback(close_tracker)
            if manager_open:
                current_temporal = BRIDGE_MANAGER.temporal_status()
                if current_temporal:
                    self._temporal_status_cache = current_temporal

    def abort(self) -> None:
        self._close_resources(discard=True)

def verify_feature_18(
    bridge_logs: list[str], bridge_status: dict[str, Any] | str | None = None
) -> dict[str, object]:
    """Return structured feature evidence without parsing external text logs."""
    status = bridge_status if isinstance(bridge_status, dict) else {}
    frames = 0
    for line in bridge_logs:
        try:
            frames += int(json.loads(line).get("event") == "frame")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return {
        "bridge_status": status,
        "feature_id": 18,
        "feature_created": True,
        "feature_evaluated": frames > 0,
        "successful_frames": frames,
        "evidence": list(bridge_logs[-20:]),
    }

@dataclass(slots=True)
class PreparedRuntime:
    gpu: dict[str, Any]
    gpus: tuple[dict[str, Any], ...]
    runtime_bundle: dict[str, Any]
    encoder_inventory: dict[str, bool]
    warmed_files: tuple[str, ...]
    _mappings: list[mmap.mmap] = field(default_factory=list, repr=False)

    def close(self) -> None:
        while self._mappings:
            mapping = self._mappings.pop()
            try:
                mapping.close()
            except (BufferError, OSError):
                pass

_PREPARE_LOCK = threading.Lock()
_PREPARED: PreparedRuntime | None = None

def _warm_mapping(path: Path) -> mmap.mmap | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    with path.open("rb") as stream:
        mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    checksum = 0
    # Touch every OS page so the complete installed runtime is resident in the
    # file cache before the window is shown, rather than faulting pages later.
    for offset in range(0, len(mapping), mmap.PAGESIZE):
        checksum ^= mapping[offset]
    checksum ^= mapping[-1]
    del checksum
    return mapping

def _encoder_inventory() -> dict[str, bool]:
    result = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "FFmpeg encoder inventory failed.")
    output = result.stdout
    return {
        "h264_nvenc": "h264_nvenc" in output,
        "hevc_nvenc": "hevc_nvenc" in output,
        "av1_nvenc": "av1_nvenc" in output,
        "prores_ks": "prores_ks" in output,
        "ffv1": "ffv1" in output,
    }

def prepare_runtime() -> PreparedRuntime:
    global _PREPARED
    if _PREPARED is not None:
        return _PREPARED
    with _PREPARE_LOCK:
        if _PREPARED is not None:
            return _PREPARED
        validate_runtime_files()
        BRIDGE_MANAGER.preload()
        gpus = detect_gpus()
        runtime_bundle = inspect_runtime_bundle()
        gpu = resolve_runtime_ai_gpu(gpus, runtime_bundle)
        inventory = _encoder_inventory()
        required_paths = (DLSSNR_BRIDGE, NEURAL_RUNTIME, FFMPEG, FFPROBE)
        optional_paths = (
            DLSSG_DIR / "neuroframe_engine_frame_interpolation.dll",
            DLSSG_DIR / "nvngx_dlssg.dll",
            RUNTIME / "rtx_video" / "neuroframe_engine_upscaling.dll",
            RUNTIME / "rtx_video" / "nvngx_vsr.dll",
            RUNTIME / "rtx_video" / "nvngx_truehdr.dll",
        )
        mappings: list[mmap.mmap] = []
        warmed_paths: list[Path] = []
        try:
            for path in required_paths:
                mapping = _warm_mapping(path)
                if mapping is not None:
                    mappings.append(mapping)
                    warmed_paths.append(path)
        except Exception:
            for mapping in mappings:
                mapping.close()
            raise
        for path in (() if os.environ.get("VE_STAGE_WORKER") else optional_paths):
            try:
                mapping = _warm_mapping(path)
                if mapping is not None:
                    mappings.append(mapping)
                    warmed_paths.append(path)
            except Exception:
                # A damaged optional feature must not disable Neural Rendering.
                continue
        _PREPARED = PreparedRuntime(
            gpu=dict(gpu),
            gpus=tuple(dict(device) for device in gpus),
            runtime_bundle=runtime_bundle,
            encoder_inventory=inventory,
            warmed_files=tuple(str(path.resolve()) for path in warmed_paths),
            _mappings=mappings,
        )
        return _PREPARED

def close_prepared_runtime() -> None:
    global _PREPARED
    with _PREPARE_LOCK:
        prepared = _PREPARED
        _PREPARED = None
    if prepared is not None:
        prepared.close()
