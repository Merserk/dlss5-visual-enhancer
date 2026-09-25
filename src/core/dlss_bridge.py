"""Independent NGX DLSS Super Resolution bridge (ABI 1).

This module deliberately never loads the Neural Rendering or RTX Video engines.
The native engine owns its D3D12 device, NGX feature, and estimated temporal guides.
"""

from __future__ import annotations

import ctypes as C
import threading

import numpy as np
from PIL import Image

from .dlss_modes import DLSS_MODES, DLSS_PRESETS, dlss_output_size, validate_dlss
from .gpu_selection import detect_gpu
from .paths import DLSSSR_BRIDGE, DLSSSR_DIR, DLSSSR_RUNTIME


class _SessionDesc(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "input_width", "input_height", "output_width", "output_height", "mode", "preset")]


class _FrameDesc(C.Structure):
    _fields_ = [
        ("size", C.c_uint32), ("abi", C.c_uint32), ("input_rgba16f", C.c_void_p),
        ("input_stride", C.c_uint32), ("output_rgba16f", C.c_void_p),
        ("output_stride", C.c_uint32), ("reset", C.c_uint32), ("phase", C.c_uint32),
    ]


class _FrameResult(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "render_width", "render_height", "guide_estimated", "ngx_result", "phase", "reserved")]
    _fields_.append(("milliseconds", C.c_double))


class _CudaFrameDesc(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("size", "abi", "width", "height", "pixel_format")]
    _fields_ += [("y_pointer", C.c_uint64), ("uv_pointer", C.c_uint64)]
    _fields_ += [(name, C.c_uint32) for name in (
        "y_stride", "uv_stride", "color_matrix", "color_range", "rotation",
        "reset", "phase", "output_p010")]


class _CudaSurfaceDesc(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "width", "height", "pixel_format", "stride")]
    _fields_ += [("y_pointer", C.c_uint64), ("uv_pointer", C.c_uint64)]


_library = None
_ordinal = None
_lock = threading.RLock()


def _error(buffer: C.Array) -> str:
    return buffer.value.decode("utf-8", "replace") or "DLSS native engine failed without a diagnostic."


def _load(ordinal: int):
    global _library, _ordinal
    with _lock:
        if _library is not None:
            if _ordinal != ordinal:
                raise RuntimeError("DLSS is already bound to another GPU; restart the application to switch GPUs.")
            return _library
        if not DLSSSR_RUNTIME.is_file() or not DLSSSR_BRIDGE.is_file():
            raise RuntimeError(f"DLSS bridge or NVIDIA runtime is missing from {DLSSSR_DIR}.")
        lib = C.CDLL(str(DLSSSR_BRIDGE))
        lib.dlsssr_abi_version.argtypes = []
        lib.dlsssr_abi_version.restype = C.c_uint32
        if lib.dlsssr_abi_version() != 1:
            raise RuntimeError("DLSS bridge ABI does not match this application.")
        lib.dlsssr_init.argtypes = [C.c_int, C.c_wchar_p, C.c_void_p, C.c_int]
        lib.dlsssr_init.restype = C.c_int
        lib.dlsssr_session_create.argtypes = [C.POINTER(_SessionDesc), C.c_void_p, C.c_int]
        lib.dlsssr_session_create.restype = C.c_void_p
        lib.dlsssr_session_release.argtypes = [C.c_void_p]
        lib.dlsssr_session_release.restype = None
        lib.dlsssr_process_rgba16f.argtypes = [C.c_void_p, C.POINTER(_FrameDesc), C.POINTER(_FrameResult), C.c_void_p, C.c_int]
        lib.dlsssr_process_rgba16f.restype = C.c_int
        lib.dlsssr_process_cuda_yuv.argtypes = [
            C.c_void_p, C.POINTER(_CudaFrameDesc), C.POINTER(_FrameResult),
            C.POINTER(C.c_void_p), C.c_void_p, C.c_int]
        lib.dlsssr_process_cuda_yuv.restype = C.c_int
        lib.dlsssr_cuda_surface_desc.argtypes = [C.c_void_p, C.POINTER(_CudaSurfaceDesc)]
        lib.dlsssr_cuda_surface_desc.restype = C.c_int
        lib.dlsssr_cuda_surface_retain.argtypes = [C.c_void_p]
        lib.dlsssr_cuda_surface_release.argtypes = [C.c_void_p]
        error = C.create_string_buffer(1024)
        if not lib.dlsssr_init(ordinal, str(DLSSSR_DIR), error, len(error)):
            raise RuntimeError(_error(error))
        _library, _ordinal = lib, ordinal
        return lib


def initialize_bridge(ordinal: int) -> None:
    """Register DLSS SR with NGX before another feature fixes search paths."""
    _load(ordinal)


class DLSSSession:
    def __init__(self, width: int, height: int, mode: str, preset: str = "Default", *,
                 gpu_uuid: str = "auto", even: bool = False):
        validate_dlss(mode, preset)
        self.width, self.height = width, height
        self.output_width, self.output_height = dlss_output_size(width, height, mode, even=even)
        self.mode, self.preset = mode, preset
        gpu = detect_gpu(gpu_uuid)
        self.gpu_name = str(gpu.get("name", "NVIDIA GPU"))
        self.ordinal = int(gpu.get("cuda_ordinal", gpu.get("index", 0)))
        self.lib = _load(self.ordinal)
        desc = _SessionDesc(C.sizeof(_SessionDesc), 1, width, height,
                            self.output_width, self.output_height,
                            tuple(DLSS_MODES).index(mode), DLSS_PRESETS.index(preset))
        error = C.create_string_buffer(1024)
        with _lock:
            self.handle = self.lib.dlsssr_session_create(C.byref(desc), error, len(error))
        if not self.handle:
            raise RuntimeError(_error(error))
        self.frames = 0
        self.last_result = None

    def process(self, rgba: np.ndarray, *, reset: bool = False, phase: int = 0) -> np.ndarray:
        if not self.handle:
            raise RuntimeError("DLSS session is closed.")
        if rgba.shape != (self.height, self.width, 4):
            raise ValueError("DLSS input shape does not match the session.")
        if rgba.dtype == np.uint8:
            source = np.ascontiguousarray(rgba[..., :4].astype(np.float32) / 255, dtype=np.float16)
        elif rgba.dtype == np.uint16:
            source = np.ascontiguousarray(rgba[..., :4].astype(np.float32) / 65535, dtype=np.float16)
        else:
            source = np.ascontiguousarray(rgba[..., :4], dtype=np.float16)
        output = np.empty((self.output_height, self.output_width, 4), dtype=np.float16)
        frame = _FrameDesc(C.sizeof(_FrameDesc), 1, source.ctypes.data, source.strides[0],
                           output.ctypes.data, output.strides[0], int(reset), phase)
        result = _FrameResult()
        result.size, result.abi = C.sizeof(_FrameResult), 1
        error = C.create_string_buffer(1024)
        with _lock:
            ok = self.lib.dlsssr_process_rgba16f(self.handle, C.byref(frame), C.byref(result), error, len(error))
        if not ok:
            raise RuntimeError(_error(error))
        self.frames += 1
        self.last_result = result
        return output

    def process_cuda_frame(self, frame, *, color_matrix: int = 1,
                           color_range: int = 0, rotation: int = 0,
                           reset: bool = False, phase: int = 0,
                           output_p010: bool = False):
        """Evaluate NVDEC NV12/P010 and return a CUDA AVFrame for NVENC/NR."""
        if not self.handle:
            raise RuntimeError("DLSS session is closed.")
        if getattr(getattr(frame, "format", None), "name", "") != "cuda":
            raise ValueError("DLSS CUDA input must be a CUDA decoded video frame.")
        pixel_format = {"nv12": 4, "p010": 5, "p010le": 5}.get(
            getattr(getattr(frame, "sw_format", None), "name", ""))
        if pixel_format is None or len(frame.planes) < 2:
            raise ValueError("DLSS CUDA input must expose NV12 or P010 planes.")
        source = _CudaFrameDesc(
            C.sizeof(_CudaFrameDesc), 1, frame.width, frame.height, pixel_format,
            int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr),
            int(frame.planes[0].line_size), int(frame.planes[1].line_size),
            int(color_matrix), int(color_range), int(rotation), int(reset),
            int(phase), int(output_p010))
        result = _FrameResult()
        result.size, result.abi = C.sizeof(_FrameResult), 1
        handle = C.c_void_p()
        error = C.create_string_buffer(1024)
        with _lock:
            ok = self.lib.dlsssr_process_cuda_yuv(
                self.handle, C.byref(source), C.byref(result), C.byref(handle), error, len(error))
        if not ok:
            raise RuntimeError(_error(error))
        if not handle.value:
            raise RuntimeError("DLSS returned no CUDA output surface.")
        surface = _CudaSurfaceDesc()
        surface.size, surface.abi = C.sizeof(_CudaSurfaceDesc), 1
        if not self.lib.dlsssr_cuda_surface_desc(handle, C.byref(surface)):
            self.lib.dlsssr_cuda_surface_release(handle)
            raise RuntimeError("DLSS returned an invalid CUDA output surface.")
        from .neural_bridge import _DLPackPlane
        import av
        released = False

        def retain():
            self.lib.dlsssr_cuda_surface_retain(handle)

        def release():
            self.lib.dlsssr_cuda_surface_release(handle)

        bits = 16 if surface.pixel_format == 5 else 8
        item_size = bits // 8
        y = _DLPackPlane(
            pointer=surface.y_pointer, shape=(surface.height, surface.width),
            strides=(surface.stride // item_size, 1), bits=bits,
            device_id=self.ordinal, retain=retain, release=release)
        uv = _DLPackPlane(
            pointer=surface.uv_pointer,
            shape=((surface.height + 1) // 2, (surface.width + 1) // 2, 2),
            strides=(surface.stride // item_size, 2, 1), bits=bits,
            device_id=self.ordinal, retain=retain, release=release)
        try:
            output = av.VideoFrame.from_dlpack(
                [y, uv], format="p010le" if bits == 16 else "nv12",
                width=surface.width, height=surface.height,
                device_id=self.ordinal, primary_ctx=True)
        finally:
            if not released:
                release()
                released = True
        self.frames += 1
        self.last_result = result
        return output, {
            "input_ms": 0.0, "ngx_ms": result.milliseconds,
            "output_ms": 0.0, "scene_cut": bool(result.reserved),
            "gpu_pre_resize": (result.render_width, result.render_height) != (self.width, self.height),
        }

    def close(self):
        if self.handle:
            with _lock:
                self.lib.dlsssr_session_release(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def process_image(rgba: np.ndarray, mode: str, preset: str = "Default", *,
                  gpu_uuid: str = "auto", controller=None):
    """Evaluate a still once; duplicate frames contain no new spatial samples."""
    height, width = rgba.shape[:2]
    with DLSSSession(width, height, mode, preset, gpu_uuid=gpu_uuid) as session:
        if controller is not None and controller.cancel.is_set():
            from .jobs import Cancelled
            raise Cancelled("DLSS image processing stopped by user.")
        result = session.process(rgba, reset=True)
        high_depth = rgba.dtype == np.uint16
        levels = 65535 if high_depth else 255
        processed = np.rint(np.clip(result.astype(np.float32), 0, 1) * levels).astype(
            np.uint16 if high_depth else np.uint8)
        if rgba.shape[2] == 4:
            alpha = Image.fromarray(rgba[..., 3]).resize(
                (session.output_width, session.output_height), Image.Resampling.LANCZOS)
            processed[..., 3] = np.clip(np.asarray(alpha), 0, levels).astype(processed.dtype)
        return processed, {
            "engine": "DLSS Super Resolution", "mode": mode, "preset": preset,
            "evaluations": 1, "guide_estimated": True,
            "media_pipeline": "source-anchored DLSS",
            "synthetic_jitter": False,
            "render_width": session.last_result.render_width,
            "render_height": session.last_result.render_height,
            "gpu_pre_resize": (session.last_result.render_width,
                               session.last_result.render_height) != (width, height),
            "ngx_result": f"0x{session.last_result.ngx_result:08X}",
            "gpu": session.gpu_name,
        }
