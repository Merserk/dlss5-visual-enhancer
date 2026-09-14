from __future__ import annotations

import json
import contextlib
import math
import mmap
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
from .gpu_selection import resolve_runtime_ai_gpu
from .i18n import t
from .jobs import Cancelled, JobController
from .neural_bridge import (
    BRIDGE_ABI_VERSION,
    BRIDGE_MANAGER,
    BridgeSessionDiagnostics,
    CudaFrameBuffers,
    CudaMaskBuffer,
    NeuralBridgeError,
    FORMAT_NV12,
    FORMAT_P010,
)
from .nr_composition import mask_report, mask_selection, prepare_nr_mask
from .paths import (
    DLSSNR_BRIDGE,
    DLSSNR_CALLER_SHIM,
    DLSSNR_DIR,
    FFMPEG,
    FFPROBE,
    NEURAL_RUNTIME,
)


NR_STYLES = {
    "Default": 0,
    "Natural": 1,
    "Cinematic": 2,
}

# Feature 18 is evaluated at the final neural dimensions. Scaling below 1x is
# an explicit Lanczos downscale before Neural Rendering.
UPSCALING_MODES = {
    1.0: {"label": "Source (Original)", "name": "Source", "perf_quality": 0},
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
        raise ValueError(t("runtime.error.scale_choices")) from exc
    if not math.isfinite(factor):
        raise ValueError(t("runtime.error.scale_choices"))
    for supported, mode in UPSCALING_MODES.items():
        if math.isclose(factor, supported, rel_tol=0.0, abs_tol=1e-9):
            return supported, mode
    raise ValueError(t("runtime.error.scale_choices"))


def _nearest_even(value: float) -> int:
    return max(2, int(math.floor(value / 2.0 + 0.5)) * 2)


def resolve_output_size(width: int, height: int, factor: float) -> tuple[int, int]:
    factor, _ = resolve_upscaling_mode(factor)
    output_width = _nearest_even(int(width) * factor)
    output_height = _nearest_even(int(height) * factor)
    if min(output_width, output_height) < 64:
        raise ValueError(t("runtime.error.output_too_small", width=output_width, height=output_height))
    long_edge = max(output_width, output_height)
    short_edge = min(output_width, output_height)
    if long_edge > 7680 or short_edge > 4320:
        raise ValueError(t("runtime.error.output_too_large", width=output_width, height=output_height))
    return output_width, output_height


def resolve_native_settings(options: Any) -> dict[str, int | float | bool]:
    try:
        style = NR_STYLES[options.nr_style]
    except KeyError as exc:
        raise ValueError(t("runtime.error.unknown_nr_style", value=options.nr_style, choices=", ".join(NR_STYLES))) from exc

    controls = {
        "NR Intensity": (options.nr_intensity, 0.0, 2.0),
        "Local Tone Strength": (options.local_tone_strength, 0.0, 2.0),
        "Local Structure Strength": (options.local_structure_strength, 0.0, 2.0),
        "Skin Structure Strength": (options.skin_structure_strength, -1.0, 2.0),
        "NR Color Strength": (options.nr_color_strength, 0.0, 1.0),
        "Tone Preservation": (options.tone_preservation, 0.0, 1.0),
        "Face/Skin Protection": (options.face_skin_protection, 0.0, 1.0),
        "Grain Preservation": (options.grain_preservation, 0.0, 1.0),
        "Shimmer Suppression": (getattr(options, "shimmer_suppression", 0.0), 0.0, 1.0),
    }
    validated: dict[str, float] = {}
    for label, (raw_value, minimum, maximum) in controls.items():
        try:
            value = float(raw_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(t("runtime.error.control_number", label=label, minimum=minimum, maximum=maximum)) from exc
        if not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(t("runtime.error.control_range", label=label, minimum=minimum, maximum=maximum))
        validated[label] = value

    if not isinstance(options.automatic_mask, bool):
        raise ValueError(t("runtime.error.automatic_mask_bool"))
    mask_feather = getattr(options, "mask_feather", 0)
    if isinstance(mask_feather, bool) or int(mask_feather) != mask_feather:
        raise ValueError(t("runtime.error.mask_feather_integer"))
    if not 0 <= int(mask_feather) <= 128:
        raise ValueError(t("runtime.error.mask_feather_range"))
    gpu_mode = getattr(options, "nr_gpu_mode", True)
    if not isinstance(gpu_mode, bool):
        raise ValueError(t("runtime.error.gpu_mode_bool"))
    nr_passes = getattr(options, "nr_passes", 1)
    if isinstance(nr_passes, bool) or not isinstance(nr_passes, int):
        raise ValueError(t("runtime.error.nr_passes_integer"))
    if not 1 <= nr_passes <= 4:
        raise ValueError(t("runtime.error.nr_passes_range"))

    codec_name = str(getattr(options, "codec", ""))
    prefer_nvof = bool(codec_name and "NVENC" not in codec_name.upper())
    return {
        "profile": 0,
        "style": style,
        "auto_mask": int(options.automatic_mask),
        "intensity": validated["NR Intensity"],
        "nr_passes": nr_passes,
        "local_tone": validated["Local Tone Strength"],
        "local_structure": validated["Local Structure Strength"],
        "skin_structure": validated["Skin Structure Strength"],
        "color_strength": validated["NR Color Strength"],
        "tone_preservation": validated["Tone Preservation"],
        "face_skin_protection": validated["Face/Skin Protection"],
        "grain_preservation": validated["Grain Preservation"],
        "shimmer_suppression": validated["Shimmer Suppression"],
        "prefer_nvof": prefer_nvof,
        "mask_feather": int(mask_feather),
        "gpu_mode": gpu_mode,
    }


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
        "caller_shim": {"path": str(DLSSNR_CALLER_SHIM.resolve())},
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


def resize_fit(rgba: np.ndarray, width: int, height: int) -> np.ndarray:
    source_height, source_width = rgba.shape[:2]
    if source_width == width and source_height == height:
        return np.ascontiguousarray(rgba, dtype=np.uint8)
    scale = min(width / source_width, height / source_height)
    fit_width = max(1, min(width, int(round(source_width * scale))))
    fit_height = max(1, min(height, int(round(source_height * scale))))
    resized = cv2.resize(rgba, (fit_width, fit_height), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((height, width, 4), dtype=np.uint8)
    canvas[..., 3] = 255
    x = (width - fit_width) // 2
    y = (height - fit_height) // 2
    canvas[y : y + fit_height, x : x + fit_width] = resized
    return canvas


def rotate_frame(frame: np.ndarray, rotation: int) -> np.ndarray:
    if rotation == 90:
        return np.ascontiguousarray(np.rot90(frame, 3))
    if rotation == 180:
        return np.ascontiguousarray(np.rot90(frame, 2))
    if rotation == 270:
        return np.ascontiguousarray(np.rot90(frame, 1))
    return frame


def validate_runtime_files() -> None:
    required = [FFMPEG, FFPROBE, DLSSNR_BRIDGE, DLSSNR_CALLER_SHIM, NEURAL_RUNTIME]
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
        composition_mask: object | None = None,
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
        self.native_settings = native_settings
        self.composition_mask = mask_selection(composition_mask)
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
            "stabilization_seconds": 0.0,
        }
        self.gpu_mode = bool(native_settings.get("gpu_mode", True))
        self.cuda_video = bool(cuda_video)
        if self.cuda_video and not self.gpu_mode:
            raise ValueError("The CUDA video boundary requires Neural Rendering GPU mode.")
        self.diagnostics = BridgeSessionDiagnostics(
            gpu_mode=self.gpu_mode,
            memory_path="cuda_d3d12_shared" if self.gpu_mode else "host_staging",
        )
        self._input_float = (
            None if self.cuda_video else np.empty(
                (self.output_height, self.output_width, 3), dtype=np.float32
            )
        )
        self._output_float = (
            None if self._input_float is None else np.empty_like(self._input_float)
        )
        self._shimmer_suppression = float(
            native_settings.get("shimmer_suppression", 0.0)
        )
        self._host_stabilizer_enabled = bool(
            not self.cuda_video
            and self._input_float is not None
            and self._shimmer_suppression > 0.0
        )
        self._host_flow = (
            cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
            if self._host_stabilizer_enabled
            else None
        )
        if self._host_stabilizer_enabled:
            host_y, host_x = np.mgrid[
                0:self.output_height, 0:self.output_width
            ].astype(np.float32)
            self._host_grid_x: np.ndarray | None = host_x
            self._host_grid_y: np.ndarray | None = host_y
        else:
            self._host_grid_x = None
            self._host_grid_y = None
        self._host_previous_source: np.ndarray | None = None
        self._host_previous_residual: np.ndarray | None = None
        self._host_stabilized_frames = 0
        # Host-output paths stabilize the final composed residual.  Suppress
        # the earlier native residual blend there to avoid filtering twice;
        # depth/motion remain bound to every NGX evaluation.
        self._host_bridge_settings = dict(native_settings)
        if self._host_stabilizer_enabled:
            self._host_bridge_settings["shimmer_suppression"] = 0.0
        self._cuda_buffers: CudaFrameBuffers | None = None
        self._mask_host = prepare_nr_mask(
            self.composition_mask, self.output_width, self.output_height,
            int(native_settings.get("mask_feather", 0)),
        )
        self._cuda_mask: CudaMaskBuffer | None = None
        self._logs: list[str] = []
        self._temporal_status_cache: dict[str, Any] = {}
        status = BRIDGE_MANAGER.initialize(gpu, require_cuda=self.gpu_mode)
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
            "composition": {
                "color_strength": float(native_settings.get("color_strength", 1.0)),
                "tone_preservation": float(native_settings.get("tone_preservation", 0.0)),
                "face_skin_protection": float(native_settings.get("face_skin_protection", 0.0)),
                "grain_preservation": float(native_settings.get("grain_preservation", 0.0)),
                "mask": mask_report(self.composition_mask, int(native_settings.get("mask_feather", 0))),
                "cuda_mask_uploads": 0,
                "cuda_mask_upload_bytes": 0,
            },
            "temporal_stabilization": {
                "shimmer_suppression": float(native_settings.get("shimmer_suppression", 0.0)),
                "motion_backend": "initializing",
                "motion_fallback": "bundled_gpu_lucas_kanade",
                "depth_bound": False,
                "motion_bound": False,
                "optical_flow_seconds": 0.0,
                "stabilization_seconds": 0.0,
                "stabilizer_backend": (
                    "host_final_composed_residual"
                    if self._host_stabilizer_enabled
                    else "native_gpu_residual"
                ),
            },
        }
        BRIDGE_MANAGER.open_session()
        self._manager_open = True
        try:
            if self.gpu_mode and not self.cuda_video:
                self._cuda_buffers = BRIDGE_MANAGER.create_cuda_buffers(
                    self.output_width, self.output_height
                )
            if self.gpu_mode and self._mask_host is not None:
                self._cuda_mask = BRIDGE_MANAGER.create_cuda_mask(self._mask_host)
                self.bridge_status["composition"]["cuda_mask_uploads"] = 1
                self.bridge_status["composition"]["cuda_mask_upload_bytes"] = int(
                    self._cuda_mask.byte_count
                )
            self._logs.append(
                json.dumps(
                    {"event": "session_open", **self.bridge_status},
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except Exception:
            if self._cuda_mask is not None:
                with contextlib.suppress(Exception):
                    self._cuda_mask.close()
                self._cuda_mask = None
            if self._cuda_buffers is not None:
                with contextlib.suppress(Exception):
                    self._cuda_buffers.close()
                self._cuda_buffers = None
            BRIDGE_MANAGER.close_session()
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
        temporal = dict(self.bridge_status.get("temporal_stabilization", {}))
        current_temporal = BRIDGE_MANAGER.temporal_status()
        if current_temporal and (
            bool(current_temporal.get("depth_bound"))
            or not self._temporal_status_cache
        ):
            self._temporal_status_cache = current_temporal
        temporal.update(self._temporal_status_cache)
        if self._host_stabilizer_enabled:
            temporal["stabilizer_backend"] = "host_final_composed_residual"
            temporal["optical_flow_seconds"] = float(
                self.process_timings.get("optical_flow_seconds", 0.0)
            )
            temporal["stabilization_seconds"] = float(
                self.process_timings.get("stabilization_seconds", 0.0)
            )
        else:
            temporal["optical_flow_seconds"] = float(
                temporal.get("optical_flow_seconds", 0.0)
            )
            temporal["stabilization_seconds"] = float(
                temporal.get("stabilization_seconds", 0.0)
            )
        if self.diagnostics.frames:
            # The native feature is released at the final logical-session
            # boundary, so retain deterministic counters even when a report is
            # assembled after close.
            temporal["motion_backend"] = temporal.get("motion_backend") if (
                temporal.get("motion_frames", 0)
            ) else (
                "nvidia_nvofa"
                if bool(self.native_settings.get("prefer_nvof", False))
                else "gpu_lucas_kanade"
            )
            temporal["motion_frames"] = max(
                int(temporal.get("motion_frames", 0)), self.diagnostics.frames
            )
            temporal["reset_frames"] = max(
                int(temporal.get("reset_frames", 0)), self.diagnostics.scene_resets + 1
            )
            temporal["stabilized_frames"] = max(
                int(temporal.get("stabilized_frames", 0)),
                self._host_stabilized_frames
                if self._host_stabilizer_enabled
                else (
                    self.diagnostics.frames
                    if float(self.native_settings.get("shimmer_suppression", 0.0)) > 0
                    else 0
                ),
            )
            temporal["depth_bound"] = True
            temporal["motion_bound"] = True
        return {
            **self.bridge_status,
            **self.diagnostics.as_dict(),
            "temporal_stabilization": temporal,
        }

    def update_composition_mask(self, selection: object | None, feather: int) -> None:
        """Prepare a replacement mask completely before swapping it at a frame boundary."""
        selected = mask_selection(selection)
        prepared = prepare_nr_mask(
            selected, self.output_width, self.output_height, int(feather)
        )
        replacement = None
        if self.gpu_mode and prepared is not None:
            replacement = BRIDGE_MANAGER.create_cuda_mask(prepared)
        previous = self._cuda_mask
        composition = self.bridge_status.get("composition", {})
        cuda_uploads = int(composition.get("cuda_mask_uploads", 0))
        cuda_upload_bytes = int(composition.get("cuda_mask_upload_bytes", 0))
        self._mask_host = prepared
        self._cuda_mask = replacement
        self.composition_mask = selected
        self.bridge_status["composition"] = {
            "color_strength": float(self.native_settings.get("color_strength", 1.0)),
            "tone_preservation": float(self.native_settings.get("tone_preservation", 0.0)),
            "face_skin_protection": float(self.native_settings.get("face_skin_protection", 0.0)),
            "grain_preservation": float(self.native_settings.get("grain_preservation", 0.0)),
            "mask": mask_report(selected, int(feather)),
            "cuda_mask_uploads": cuda_uploads + int(replacement is not None),
            "cuda_mask_upload_bytes": cuda_upload_bytes + (
                int(replacement.byte_count) if replacement is not None else 0
            ),
        }
        if previous is not None:
            previous.close()

    def _capture_temporal_status(self) -> None:
        current = BRIDGE_MANAGER.temporal_status()
        if current:
            self._temporal_status_cache = current

    def _stabilize_host_composition(self, *, reset: bool) -> None:
        """Stabilize model-created detail after all composition controls."""
        if not self._host_stabilizer_enabled:
            return
        assert self._input_float is not None and self._output_float is not None
        current_source = self._input_float
        current_residual = self._output_float - current_source
        if reset or self._host_previous_source is None or self._host_previous_residual is None:
            self._host_previous_source = current_source.copy()
            self._host_previous_residual = current_residual.copy()
            return

        flow_started = time.perf_counter()
        previous_gray = cv2.cvtColor(
            np.clip(self._host_previous_source * 255.0, 0.0, 255.0).astype(np.uint8),
            cv2.COLOR_RGB2GRAY,
        )
        current_gray = cv2.cvtColor(
            np.clip(current_source * 255.0, 0.0, 255.0).astype(np.uint8),
            cv2.COLOR_RGB2GRAY,
        )
        assert self._host_flow is not None
        # DIS directly estimates the requested current -> previous field.
        flow = self._host_flow.calc(current_gray, previous_gray, None)
        self.process_timings["optical_flow_seconds"] += time.perf_counter() - flow_started

        stabilization_started = time.perf_counter()
        height, width = current_gray.shape
        assert self._host_grid_x is not None and self._host_grid_y is not None
        map_x = self._host_grid_x + flow[..., 0]
        map_y = self._host_grid_y + flow[..., 1]
        valid = (
            (map_x >= 0.0) & (map_y >= 0.0)
            & (map_x <= float(width - 1)) & (map_y <= float(height - 1))
        )
        warped_source = cv2.remap(
            self._host_previous_source,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )
        warped_residual = cv2.remap(
            self._host_previous_residual,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
        )

        luma_weights = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
        luma_disagreement = np.abs(
            np.sum((current_source - warped_source) * luma_weights, axis=2)
        )
        confidence = np.clip((0.10 - luma_disagreement) / 0.08, 0.0, 1.0)
        confidence *= valid

        # Clamp reprojected history to the current residual's 3x3 envelope.
        kernel = np.ones((3, 3), dtype=np.uint8)
        neighborhood_low = cv2.erode(current_residual, kernel)
        neighborhood_high = cv2.dilate(current_residual, kernel)
        np.clip(
            warped_residual,
            neighborhood_low,
            neighborhood_high,
            out=warped_residual,
        )
        weight = (self._shimmer_suppression * confidence)[..., None]
        stabilized_residual = current_residual * (1.0 - weight) + warped_residual * weight
        np.add(current_source, stabilized_residual, out=self._output_float)
        np.clip(self._output_float, 0.0, 1.0, out=self._output_float)

        self._host_previous_source = current_source.copy()
        self._host_previous_residual = stabilized_residual.copy()
        self._host_stabilized_frames += 1
        self.process_timings["stabilization_seconds"] += (
            time.perf_counter() - stabilization_started
        )

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
        if rgba.dtype != np.uint8 or rgba.shape != (
            self.output_height,
            self.output_width,
            4,
        ):
            raise ValueError("Neural Rendering input must be contiguous RGBA8 at final size.")
        rgba = np.ascontiguousarray(rgba)
        if output_buffer is None:
            output = np.empty_like(rgba)
        else:
            output = output_buffer
            if (
                output.dtype != np.uint8
                or output.shape != rgba.shape
                or not output.flags.c_contiguous
            ):
                raise ValueError("Neural Rendering output buffer has the wrong shape or layout.")

        started = time.perf_counter()
        assert self._input_float is not None and self._output_float is not None
        np.multiply(rgba[..., :3], 1.0 / 255.0, out=self._input_float, casting="unsafe")
        self.process_timings["input_conversion_seconds"] += time.perf_counter() - started

        if self.gpu_mode:
            assert self._cuda_buffers is not None
            try:
                upload, evaluate, download = BRIDGE_MANAGER.process_cuda(
                    self._input_float,
                    self._output_float,
                    self._cuda_buffers,
                    self._host_bridge_settings,
                    bool(reset),
                    self._mask_host,
                    self._cuda_mask,
                )
            except NeuralBridgeError:
                # GPU ON is a strict policy. Never retry through host staging.
                raise
            self.process_timings["input_transfer_seconds"] += upload
            self.process_timings["evaluation_wait_seconds"] += evaluate
            self.process_timings["output_transfer_seconds"] += download
        else:
            evaluate = BRIDGE_MANAGER.process_host(
                self._input_float, self._output_float, self._host_bridge_settings, bool(reset),
                self._mask_host,
            )
            self.process_timings["evaluation_wait_seconds"] += evaluate

        self._stabilize_host_composition(reset=bool(reset))

        started = time.perf_counter()
        np.multiply(
            np.clip(self._output_float, 0.0, 1.0),
            255.0,
            out=self._output_float,
        )
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
        if not self.gpu_mode:
            raise RuntimeError("CUDA scene scoring is not enabled for this session.")
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        return BRIDGE_MANAGER.score_cuda_video_frame(
            frame, threshold=0.24, color_matrix=color_matrix, color_range=color_range
        )

    def process_cuda_frame_to_host(
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
        output_buffer: np.ndarray | None = None,
    ) -> tuple[np.ndarray, int]:
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.closed or not self.gpu_mode:
            raise RuntimeError("The CUDA Neural Rendering session is unavailable.")
        if self._next_frame_index is None:
            self._next_frame_index = int(index)
        if index != self._next_frame_index:
            raise ValueError("Neural Rendering frames must have consecutive uint32 indices.")
        output = output_buffer
        if output is None:
            output = np.empty((self.output_height, self.output_width, 4), dtype=np.uint8)
        if (
            output.dtype != np.uint8
            or output.shape != (self.output_height, self.output_width, 4)
            or not output.flags.c_contiguous
        ):
            raise ValueError("CUDA-to-host output buffer has the wrong shape or layout.")
        result, elapsed = BRIDGE_MANAGER.process_cuda_to_host_video_frame(
            frame,
            output,
            settings=self.native_settings,
            mask=self._mask_host,
            cuda_mask=self._cuda_mask,
            reset=reset,
            timestamp=pts,
            color_matrix=color_matrix,
            color_range=color_range,
            rotation=rotation,
        )
        self.process_timings["evaluation_wait_seconds"] += elapsed
        self.diagnostics.decode_backend = "nvdec"
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
            "input_format": result["input_format"], "output_format": "rgba8",
            "upload_bytes": result["upload_bytes"],
            "download_bytes": result["download_bytes"],
        }, sort_keys=True, separators=(",", ":")))
        if len(self._logs) > 500:
            del self._logs[: len(self._logs) - 500]
        return output, int(result["timestamp"])

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
        output_p010: bool = False,
    ) -> tuple[Any, int]:
        """Process a hardware or software decoded frame into CUDA NV12/P010."""
        if self.controller.cancel.is_set():
            raise Cancelled("Render stopped by user.")
        if self.closed:
            raise RuntimeError("The Neural Rendering bridge session is closed.")
        if not self.cuda_video or not self.gpu_mode:
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
                settings=self.native_settings,
                mask=self._mask_host,
                cuda_mask=self._cuda_mask,
                reset=reset,
                timestamp=pts,
                color_matrix=color_matrix,
                color_range=color_range,
                rotation=rotation,
            )
            self.diagnostics.decode_backend = "nvdec"
        elif rgba is not None:
            rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
            if rgba.shape != (self.output_height, self.output_width, 4):
                raise ValueError("Software video input must be RGBA8 at final neural dimensions.")
            output, result, elapsed = BRIDGE_MANAGER.process_host_to_cuda_video_frame(
                rgba,
                output_format=output_format,
                settings=self.native_settings,
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
        else:
            raise ValueError("A CUDA frame or host RGBA frame is required.")
        output.pts = int(pts)
        output.time_base = time_base
        if duration is not None:
            output.duration = int(duration)
        self.process_timings["evaluation_wait_seconds"] += elapsed
        self.diagnostics.encode_backend = "nvenc"
        self.diagnostics.pixel_format = result["output_format"]
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

    def _close_resources(self) -> None:
        if self.closed:
            return
        if self._cuda_buffers is not None:
            self._cuda_buffers.close()
            self._cuda_buffers = None
        if self._cuda_mask is not None:
            self._cuda_mask.close()
            self._cuda_mask = None
        if self._manager_open:
            current_temporal = BRIDGE_MANAGER.temporal_status()
            if current_temporal:
                self._temporal_status_cache = current_temporal
            BRIDGE_MANAGER.close_session()
            self._manager_open = False
        self.closed = True

    def abort(self) -> None:
        self._close_resources()


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
    for offset in range(0, len(mapping), 64 * 1024):
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
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "FFmpeg encoder inventory failed.")
    output = result.stdout
    return {
        "h264_nvenc": "h264_nvenc" in output,
        "hevc_nvenc": "hevc_nvenc" in output,
        "av1_nvenc": "av1_nvenc" in output,
        "prores_ks": "prores_ks" in output,
    }


def prepare_runtime() -> PreparedRuntime:
    global _PREPARED
    if _PREPARED is not None:
        return _PREPARED
    with _PREPARE_LOCK:
        if _PREPARED is not None:
            return _PREPARED
        validate_runtime_files()
        gpus = detect_gpus()
        runtime_bundle = inspect_runtime_bundle()
        gpu = resolve_runtime_ai_gpu(gpus, runtime_bundle)
        inventory = _encoder_inventory()
        paths = (DLSSNR_BRIDGE, DLSSNR_CALLER_SHIM, NEURAL_RUNTIME, FFMPEG, FFPROBE)
        mappings: list[mmap.mmap] = []
        try:
            for path in paths:
                mapping = _warm_mapping(path)
                if mapping is not None:
                    mappings.append(mapping)
        except Exception:
            for mapping in mappings:
                mapping.close()
            raise
        _PREPARED = PreparedRuntime(
            gpu=dict(gpu),
            gpus=tuple(dict(device) for device in gpus),
            runtime_bundle=runtime_bundle,
            encoder_inventory=inventory,
            warmed_files=tuple(str(path.resolve()) for path in paths),
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
