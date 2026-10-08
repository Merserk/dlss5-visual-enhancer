"""Independent NGX DLSS Super Resolution bridge (ABI 2).

This module deliberately never loads the Neural Rendering or RTX Video engines.
The native engine owns its D3D12 device, NGX feature, and estimated temporal guides.
"""

from __future__ import annotations

import ctypes as C
import atexit
import json
import queue
import threading

import numpy as np
from PIL import Image

from .dlss_modes import DLSS_MODES, DLSS_PRESETS, dlss_output_size, validate_dlss
from .gpu_selection import detect_gpu
from .paths import DLSSSR_BRIDGE, DLSSSR_DIR, DLSSSR_RUNTIME
from .ngx_runtime import NGX_RUNTIME_LOCK
from .runtime import OPTICAL_FLOW_QUALITIES

BRIDGE_ABI = 2


class _SessionDesc(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "input_width", "input_height", "output_width", "output_height", "mode", "preset", "still_image", "optical_flow_quality")]


class _FrameDesc(C.Structure):
    _fields_ = [
        ("size", C.c_uint32), ("abi", C.c_uint32), ("input_rgba16f", C.c_void_p),
        ("input_stride", C.c_uint32), ("output_rgba16f", C.c_void_p),
        ("output_stride", C.c_uint32), ("reset", C.c_uint32), ("phase", C.c_uint32),
        ("input_format", C.c_uint32), ("output_format", C.c_uint32),
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
        "reset", "phase", "output_p010", "video_color", "chroma_location")]


class _CudaSurfaceDesc(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        "size", "abi", "width", "height", "pixel_format", "stride")]
    _fields_ += [("y_pointer", C.c_uint64), ("uv_pointer", C.c_uint64)]


_library = None
_ordinal = None
_lock = threading.RLock()


class _NativeWorker:
    """One ordered NGX worker; DLPack destructors enqueue nonblocking releases."""
    def __init__(self):
        self.calls = queue.SimpleQueue()
        self.lock = threading.Lock()
        self.thread = None
        self.stopping = False
        self.poisoned = ""
        self.retained = []

    def guard(self):
        if self.poisoned:
            raise RuntimeError(f"DLSS SR engine is poisoned: {self.poisoned}. Restart the application.")

    def run(self):
        while True:
            request = self.calls.get()
            if request is None:
                return
            function, references, done, result, failures = request
            try:
                with NGX_RUNTIME_LOCK:
                    self.guard()
                    result.append(function())
            except BaseException as exc:
                self.poisoned = f"native exception: {exc}"
                self.retained.append(references)
                failures.append(exc)
            finally:
                function = references = request = None
                done.set()
                done = result = failures = None

    def submit(self, function, references=()):
        self.guard()
        done, result, failures = threading.Event(), [], []
        with self.lock:
            if self.stopping:
                raise RuntimeError("DLSS SR is shutting down.")
            if self.thread is None:
                self.thread = threading.Thread(target=self.run, name="dlsssr-native", daemon=True)
                self.thread.start()
                atexit.register(self.shutdown)
            self.calls.put((function, references, done, result, failures))
        return done, result, failures

    def call(self, function, references=(), timeout=180.0):
        done, result, failures = self.submit(function, references)
        if not done.wait(timeout):
            self.retained.append((function, references))
            self.poisoned = f"native call exceeded {timeout:g} seconds"
        self.guard()
        if failures:
            raise failures[0]
        return result[0]

    def release(self, library, handle, owner):
        if self.poisoned or self.stopping:
            return
        self.submit(lambda: library.dlsssr_cuda_surface_release(handle), (owner,))

    def shutdown(self):
        with self.lock:
            if self.thread is None or self.stopping:
                return
            self.stopping = True
            self.calls.put(None)
            thread = self.thread
        thread.join(5.0)
        if thread.is_alive():
            self.poisoned = "native worker did not drain during shutdown"


_WORKER = _NativeWorker()


def _error(buffer: C.Array) -> str:
    return buffer.value.decode("utf-8", "replace") or "DLSS native engine failed without a diagnostic."


def _raise_native(buffer, references=()):
    detail = _error(buffer)
    if "poison" in detail.casefold():
        _WORKER.poisoned = detail
        _WORKER.retained.append(references)
        _WORKER.guard()
    raise RuntimeError(detail)


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
        if lib.dlsssr_abi_version() != BRIDGE_ABI:
            raise RuntimeError("DLSS bridge ABI does not match this application. Rebuild the SR engine and restart the application.")
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
        lib.dlsssr_cuda_surface_retain.restype = None
        lib.dlsssr_cuda_surface_release.argtypes = [C.c_void_p]
        lib.dlsssr_cuda_surface_release.restype = None
        lib.dlsssr_session_temporal_status.argtypes = [C.c_void_p, C.c_void_p, C.c_int]
        lib.dlsssr_session_temporal_status.restype = C.c_int
        lib.dlsssr_session_diagnostics.argtypes = [C.c_void_p, C.POINTER(C.c_uint64), C.c_uint32]
        lib.dlsssr_session_diagnostics.restype = C.c_int
        lib.dlsssr_get_diagnostics.argtypes = [C.POINTER(C.c_uint64), C.c_uint32]
        lib.dlsssr_get_diagnostics.restype = C.c_int
        lib.dlsssr_version.argtypes = []
        lib.dlsssr_version.restype = C.c_char_p
        lib.dlsssr_host_formats.argtypes = []
        lib.dlsssr_host_formats.restype = C.c_uint32
        lib.dlsssr_host_output_formats.argtypes = []
        lib.dlsssr_host_output_formats.restype = C.c_uint32
        error = C.create_string_buffer(1024)
        if not _WORKER.call(lambda: lib.dlsssr_init(ordinal, str(DLSSSR_DIR), error, len(error)), (error, lib)):
            _raise_native(error, (error, lib))
        _library, _ordinal = lib, ordinal
        return lib


def initialize_bridge(ordinal: int) -> None:
    """Register DLSS SR with NGX before another feature fixes search paths."""
    _load(ordinal)


class DLSSSession:
    def __init__(self, width: int, height: int, mode: str, preset: str = "Default", *,
                 gpu_uuid: str = "auto", even: bool = False, still_image: bool = False,
                 optical_flow_quality: str = "High"):
        validate_dlss(mode, preset)
        if not isinstance(optical_flow_quality, str) or optical_flow_quality not in OPTICAL_FLOW_QUALITIES:
            raise ValueError("Optical Flow Quality must be High, Medium, or Low.")
        self.width, self.height = width, height
        self.output_width, self.output_height = dlss_output_size(width, height, mode, even=even)
        self.mode, self.preset = mode, preset
        gpu = detect_gpu(gpu_uuid)
        self.gpu_name = str(gpu.get("name", "NVIDIA GPU"))
        self.ordinal = int(gpu.get("cuda_ordinal", gpu.get("index", 0)))
        self.lib = _load(self.ordinal)
        self.bridge_version = self.lib.dlsssr_version().decode("ascii")
        desc = _SessionDesc(C.sizeof(_SessionDesc), BRIDGE_ABI, width, height,
                            self.output_width, self.output_height,
                            tuple(DLSS_MODES).index(mode), DLSS_PRESETS.index(preset), int(still_image),
                            0 if still_image else OPTICAL_FLOW_QUALITIES.index(optical_flow_quality))
        error = C.create_string_buffer(1024)
        self._session_lock = threading.RLock()
        self.handle = _WORKER.call(lambda: self.lib.dlsssr_session_create(C.byref(desc), error, len(error)),
                                   (self, desc, error))
        if not self.handle:
            _raise_native(error, (self, desc, error))
        self.frames = 0
        self.last_result = None

    def process(self, rgba: np.ndarray, *, reset: bool = False, phase: int = 0,
                output_dtype=np.float16) -> np.ndarray:
        with self._session_lock:
            return self._process(rgba, reset=reset, phase=phase, output_dtype=output_dtype)

    def _process(self, rgba: np.ndarray, *, reset: bool, phase: int, output_dtype) -> np.ndarray:
        if not self.handle:
            raise RuntimeError("DLSS session is closed.")
        if rgba.shape != (self.height, self.width, 4):
            raise ValueError("DLSS input shape does not match the session.")
        output_dtype = np.dtype(output_dtype)
        if output_dtype not in (np.float16, np.uint8, np.uint16):
            raise ValueError("DLSS output must be float16, uint8, or uint16.")
        output_format = {np.dtype(np.uint8): 1, np.dtype(np.uint16): 2}.get(output_dtype, 0)
        input_format = 0
        if rgba.dtype in (np.uint8, np.uint16):
            source = np.ascontiguousarray(rgba)
            input_format = 1 if rgba.dtype == np.uint8 else 2
        else:
            source = np.ascontiguousarray(rgba, dtype=np.float16)
        output = np.empty((self.output_height, self.output_width, 4), dtype=output_dtype)
        frame = _FrameDesc(C.sizeof(_FrameDesc), BRIDGE_ABI, source.ctypes.data, source.strides[0],
                           output.ctypes.data, output.strides[0], int(reset), phase, input_format, output_format)
        result = _FrameResult()
        result.size, result.abi = C.sizeof(_FrameResult), BRIDGE_ABI
        error = C.create_string_buffer(1024)
        ok = _WORKER.call(lambda: self.lib.dlsssr_process_rgba16f(
            self.handle, C.byref(frame), C.byref(result), error, len(error)),
            (self, source, output, frame, result, error))
        if not ok:
            _raise_native(error, (self, source, output, frame, result, error))
        self.frames += 1
        self.last_result = result
        return output

    def process_cuda_frame(self, frame, *, color_matrix: int = 1,
                           color_range: int = 0, rotation: int = 0,
                           reset: bool = False, phase: int = 0,
                           output_p010: bool = False,
                           chroma_location: int = 1):
        with self._session_lock:
            return self._process_cuda_frame(frame, color_matrix=color_matrix, color_range=color_range,
                                            rotation=rotation, reset=reset, phase=phase,
                                            output_p010=output_p010,
                                            chroma_location=chroma_location)

    def _process_cuda_frame(self, frame, *, color_matrix, color_range, rotation, reset, phase,
                            output_p010, chroma_location):
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
            C.sizeof(_CudaFrameDesc), BRIDGE_ABI, frame.width, frame.height, pixel_format,
            int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr),
            int(frame.planes[0].line_size), int(frame.planes[1].line_size),
            int(color_matrix), int(color_range), int(rotation), int(reset),
            int(phase), int(output_p010), 1, int(chroma_location))
        result = _FrameResult()
        result.size, result.abi = C.sizeof(_FrameResult), BRIDGE_ABI
        handle = C.c_void_p()
        error = C.create_string_buffer(1024)
        ok = _WORKER.call(lambda: self.lib.dlsssr_process_cuda_yuv(
            self.handle, C.byref(source), C.byref(result), C.byref(handle), error, len(error)),
            (self, frame, source, result, handle, error))
        if not ok:
            _raise_native(error, (self, frame, source, result, handle, error))
        if not handle.value:
            raise RuntimeError("DLSS returned no CUDA output surface.")
        surface = _CudaSurfaceDesc()
        surface.size, surface.abi = C.sizeof(_CudaSurfaceDesc), BRIDGE_ABI
        if not self.lib.dlsssr_cuda_surface_desc(handle, C.byref(surface)):
            _WORKER.release(self.lib, handle, self)
            raise RuntimeError("DLSS returned an invalid CUDA output surface.")
        from .cuda_dlpack import CudaDLPackPlane
        import av
        released = False

        def retain():
            self.lib.dlsssr_cuda_surface_retain(handle)

        def release():
            _WORKER.release(self.lib, handle, self)

        bits = 16 if surface.pixel_format == 5 else 8
        item_size = bits // 8
        y = CudaDLPackPlane(
            pointer=surface.y_pointer, shape=(surface.height, surface.width),
            strides=(surface.stride // item_size, 1), bits=bits,
            device_id=self.ordinal, retain=retain, release=release)
        uv = CudaDLPackPlane(
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

    def diagnostics(self):
        with self._session_lock:
            if not self.handle:
                return {}
            values = (C.c_uint64 * 9)()
            if not _WORKER.call(lambda: self.lib.dlsssr_session_diagnostics(self.handle, values, len(values)),
                                (self, values)):
                raise RuntimeError("DLSS SR session returned invalid diagnostics.")
            status = C.create_string_buffer(4096)
            if not _WORKER.call(lambda: self.lib.dlsssr_session_temporal_status(self.handle, status, len(status)),
                                (self, status)):
                raise RuntimeError("DLSS SR session returned invalid temporal status.")
            return dict(zip(("host_frames", "cuda_frames", "upload_bytes", "download_bytes", "surface_allocations",
                             "guide_descriptors", "still_image", "render_alias", "surface_slots"), values)) | json.loads(status.value)

    def close(self):
        with self._session_lock:
            if self.handle:
                _WORKER.call(lambda: self.lib.dlsssr_session_release(self.handle), (self,))
                self.handle = None
        
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def process_image(rgba: np.ndarray, mode: str, preset: str = "Default", *,
                  gpu_uuid: str = "auto", controller=None):
    """Evaluate a still once; duplicate frames contain no new spatial samples."""
    height, width = rgba.shape[:2]
    with DLSSSession(width, height, mode, preset, gpu_uuid=gpu_uuid, still_image=True) as session:
        if controller is not None and controller.cancel.is_set():
            from .jobs import Cancelled
            raise Cancelled("DLSS image processing stopped by user.")
        high_depth = rgba.dtype == np.uint16
        levels = 65535 if high_depth else 255
        processed = session.process(rgba, reset=True, output_dtype=np.uint16 if high_depth else np.uint8)
        if rgba.shape[2] == 4:
            alpha_source = rgba[..., 3]
            if alpha_source.dtype.kind == "f":
                alpha_source = np.rint(np.clip(alpha_source.astype(np.float32), 0, 1) * levels).astype(processed.dtype)
            alpha = Image.fromarray(alpha_source).resize(
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
            "bridge_version": session.bridge_version,
        }
