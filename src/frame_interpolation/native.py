from __future__ import annotations

"""Persistent in-process CUDA/D3D12/NVOF/DLSSG bridge."""

import ctypes
import gc
import threading
from pathlib import Path
from typing import Any

import av
import numpy as np

from ..core.jobs import Cancelled, JobController
from ..core.neural_bridge import _DLPackPlane
from ..core.ngx_runtime import NGX_RUNTIME_LOCK
from ..core.paths import RUNTIME

RUNTIME_DIR = RUNTIME / "dlssg"
BRIDGE = RUNTIME_DIR / "neuroframe_engine.dll"
BRIDGE_ABI_VERSION = 1
MEMORY_HOST, MEMORY_CUDA = 1, 2
FORMAT_RGBA8, FORMAT_NV12, FORMAT_P010 = 1, 4, 5
FLAG_FORCE_RESET, FLAG_DETECT_CUT = 1, 2
MAX_GENERATED = 4


class FrameDescriptorV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("memory_type", ctypes.c_uint32), ("pixel_format", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("planes", ctypes.c_uint64 * 4), ("strides", ctypes.c_uint32 * 4),
        ("color_matrix", ctypes.c_uint32), ("color_range", ctypes.c_uint32),
        ("color_primaries", ctypes.c_uint32), ("color_transfer", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 3),
    ]

    @classmethod
    def empty(cls) -> "FrameDescriptorV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value


class SessionDescriptorV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("width", ctypes.c_uint32), ("height", ctypes.c_uint32),
        ("generated_count", ctypes.c_uint32), ("hdr", ctypes.c_uint32),
        ("surface_pool_size", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 5),
    ]


class FrameResultV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("generated_count", ctypes.c_uint32), ("reset", ctypes.c_uint32),
        ("scene_cut", ctypes.c_uint32), ("duplicate", ctypes.c_uint32),
        ("interpolation_disabled", ctypes.c_uint32), ("nvof_active", ctypes.c_uint32),
        ("upload_bytes", ctypes.c_uint64), ("download_bytes", ctypes.c_uint64),
        ("surface_handles", ctypes.c_uint64 * MAX_GENERATED),
        ("scene_score", ctypes.c_double), ("input_milliseconds", ctypes.c_double),
        ("optical_flow_milliseconds", ctypes.c_double),
        ("ngx_milliseconds", ctypes.c_double), ("output_milliseconds", ctypes.c_double),
        ("total_milliseconds", ctypes.c_double),
    ]

    @classmethod
    def empty(cls) -> "FrameResultV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value


class BridgeStatusV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("gpu_ordinal", ctypes.c_int32),
        ("multi_frame_count_max", ctypes.c_uint32), ("active_sessions", ctypes.c_uint32),
        ("bridge_version", ctypes.c_char * 32), ("runtime_version", ctypes.c_char * 32),
        ("gpu_name", ctypes.c_char * 128), ("last_error", ctypes.c_char * 512),
    ]

    @classmethod
    def empty(cls) -> "BridgeStatusV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value


class DLSSGBridgeError(RuntimeError):
    pass


class DLSSGBridgePoisonedError(DLSSGBridgeError):
    pass


def _decode(value: Any) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8", "replace")


class _BridgeManager:
    def __init__(self) -> None:
        self.library: Any | None = None
        self.gpu_ordinal: int | None = None
        self.poisoned_reason = ""
        self.ffmpeg_devices: dict[int, tuple[Any, ctypes.c_void_p]] = {}

    def _ensure_ffmpeg_device(self, ordinal: int) -> None:
        if ordinal in self.ffmpeg_devices:
            return
        av_root = Path(av.__file__).resolve().parent.parent
        candidates = sorted((av_root / "av.libs").glob("avutil-*.dll"))
        if not candidates:
            raise DLSSGBridgeError("PyAV's CUDA runtime is missing.")
        avutil = ctypes.CDLL(str(candidates[0]))
        avutil.av_hwdevice_find_type_by_name.argtypes = [ctypes.c_char_p]
        avutil.av_hwdevice_find_type_by_name.restype = ctypes.c_int
        avutil.av_dict_set.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p,
                                       ctypes.c_char_p, ctypes.c_int]
        avutil.av_dict_set.restype = ctypes.c_int
        avutil.av_dict_free.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        avutil.av_hwdevice_ctx_create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int,
                                                  ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
        avutil.av_hwdevice_ctx_create.restype = ctypes.c_int
        kind = int(avutil.av_hwdevice_find_type_by_name(b"cuda"))
        options, reference = ctypes.c_void_p(), ctypes.c_void_p()
        if kind < 0 or avutil.av_dict_set(ctypes.byref(options), b"primary_ctx", b"1", 0) < 0:
            raise DLSSGBridgeError("Could not prepare FFmpeg's CUDA primary context.")
        try:
            result = int(avutil.av_hwdevice_ctx_create(
                ctypes.byref(reference), kind, str(int(ordinal)).encode("ascii"), options, 0))
        finally:
            avutil.av_dict_free(ctypes.byref(options))
        if result < 0 or not reference.value:
            raise DLSSGBridgeError(f"FFmpeg CUDA primary-context creation failed ({result}).")
        self.ffmpeg_devices[ordinal] = (avutil, reference)

    def _load(self) -> Any:
        if self.library is not None:
            return self.library
        if not BRIDGE.is_file():
            raise DLSSGBridgeError(f"Frame Interpolation bridge is missing: {BRIDGE}")
        try:
            library = ctypes.WinDLL(str(BRIDGE))
        except OSError as exc:
            raise DLSSGBridgeError(f"Could not load neuroframe_engine.dll: {exc}") from exc
        library.fi_abi_version.restype = ctypes.c_uint32
        library.fi_version.restype = ctypes.c_char_p
        library.fi_init.argtypes = [ctypes.c_int, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_int]
        library.fi_init.restype = ctypes.c_int
        library.fi_get_status_v1.argtypes = [ctypes.POINTER(BridgeStatusV1)]
        library.fi_get_status_v1.restype = ctypes.c_int
        library.fi_session_create.argtypes = [ctypes.POINTER(SessionDescriptorV1), ctypes.c_void_p, ctypes.c_int]
        library.fi_session_create.restype = ctypes.c_void_p
        library.fi_session_release.argtypes = [ctypes.c_void_p]
        library.fi_process_frame_v1.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(FrameResultV1), ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_process_frame_v1.restype = ctypes.c_int
        library.fi_surface_frame_desc.argtypes = [ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1)]
        library.fi_surface_frame_desc.restype = ctypes.c_int
        library.fi_surface_retain.argtypes = [ctypes.c_void_p]
        library.fi_surface_release.argtypes = [ctypes.c_void_p]
        library.fi_surface_copy_to_host.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_surface_copy_to_host.restype = ctypes.c_int
        library.fi_session_diagnostics.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_uint64), ctypes.c_uint32]
        library.fi_session_diagnostics.restype = ctypes.c_int
        if int(library.fi_abi_version()) != BRIDGE_ABI_VERSION:
            raise DLSSGBridgeError("Frame Interpolation bridge ABI does not match this application.")
        self.library = library
        return library

    def guard(self) -> None:
        if self.poisoned_reason:
            raise DLSSGBridgePoisonedError(
                f"The DLSSG engine is poisoned: {self.poisoned_reason}. Restart the application.")

    def initialize(self, ordinal: int) -> dict[str, Any]:
        with NGX_RUNTIME_LOCK:
            self.guard()
            if self.gpu_ordinal is not None and self.gpu_ordinal != int(ordinal):
                raise DLSSGBridgeError(
                    "Frame Interpolation is already bound to another AI GPU. Restart before changing it.")
            self._ensure_ffmpeg_device(int(ordinal))
            library = self._load()
            error = ctypes.create_string_buffer(4096)
            if not library.fi_init(int(ordinal), str(RUNTIME_DIR), error, len(error)):
                detail = error.value.decode("utf-8", "replace") or "unknown initialization failure"
                if "poison" in detail.casefold() or "access violation" in detail.casefold():
                    self.poisoned_reason = detail
                    self.guard()
                raise DLSSGBridgeError(f"DLSSG bridge initialization failed: {detail}")
            self.gpu_ordinal = int(ordinal)
            return self.status()

    def status(self) -> dict[str, Any]:
        record = BridgeStatusV1.empty()
        if not self._load().fi_get_status_v1(ctypes.byref(record)):
            raise DLSSGBridgeError("DLSSG bridge returned an invalid status record.")
        return {
            "abi_version": int(record.abi_version),
            "bridge_version": _decode(record.bridge_version),
            "runtime_version": _decode(record.runtime_version),
            "initialized": bool(record.flags & 1), "poisoned": bool(record.flags & 2),
            "available": bool(record.flags & 4), "cuda_interop": bool(record.flags & 8),
            "gpu_ordinal": int(record.gpu_ordinal), "gpu_name": _decode(record.gpu_name),
            "multi_frame_count_max": int(record.multi_frame_count_max),
            "active_sessions": int(record.active_sessions), "last_error": _decode(record.last_error),
        }

    def call(self, label: str, function, keepalive: tuple[Any, ...], timeout: float) -> Any:
        self.guard()
        done = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                with NGX_RUNTIME_LOCK:
                    result.append(function())
            except BaseException as exc:
                failure.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=invoke, name=f"dlssg-{label}", daemon=True)
        thread.start()
        if not done.wait(timeout):
            _ = keepalive
            self.poisoned_reason = f"{label} exceeded {timeout:g} seconds"
            self.guard()
        if failure:
            self.poisoned_reason = f"native exception during {label}: {failure[0]}"
            self.guard()
        return result[0]


_MANAGER = _BridgeManager()


def initialize_bridge(ordinal: int) -> dict[str, Any]:
    return _MANAGER.initialize(ordinal)


class DLSSGCudaSurface:
    def __init__(self, library: Any, handle: int, descriptor: FrameDescriptorV1, ordinal: int) -> None:
        self.library, self.handle, self.descriptor = library, int(handle), descriptor
        self.ordinal, self.closed = int(ordinal), False

    def retain(self) -> None:
        self.library.fi_surface_retain(ctypes.c_void_p(self.handle))

    def release(self) -> None:
        self.library.fi_surface_release(ctypes.c_void_p(self.handle))

    def close(self) -> None:
        if not self.closed:
            self.release()
            self.closed = True

    def to_av_frame(self) -> av.VideoFrame:
        descriptor = self.descriptor
        bits = 16 if descriptor.pixel_format == FORMAT_P010 else 8
        item_size = bits // 8
        y = _DLPackPlane(
            pointer=int(descriptor.planes[0]), shape=(int(descriptor.height), int(descriptor.width)),
            strides=(int(descriptor.strides[0]) // item_size, 1), bits=bits,
            device_id=self.ordinal, retain=self.retain, release=self.release)
        uv = _DLPackPlane(
            pointer=int(descriptor.planes[1]),
            shape=((int(descriptor.height) + 1) // 2, (int(descriptor.width) + 1) // 2, 2),
            strides=(int(descriptor.strides[1]) // item_size, 2, 1), bits=bits,
            device_id=self.ordinal, retain=self.retain, release=self.release)
        try:
            frame = av.VideoFrame.from_dlpack(
                [y, uv], format="p010le" if bits == 16 else "nv12",
                width=int(descriptor.width), height=int(descriptor.height),
                device_id=self.ordinal, primary_ctx=True)
        except BaseException:
            self.close()
            raise
        self.close()
        return frame

    def to_host_frame(self) -> av.VideoFrame:
        pixel_format = "p010le" if self.descriptor.pixel_format == FORMAT_P010 else "nv12"
        frame = av.VideoFrame(int(self.descriptor.width), int(self.descriptor.height), pixel_format)
        error = ctypes.create_string_buffer(2048)
        if not self.library.fi_surface_copy_to_host(
            ctypes.c_void_p(self.handle), ctypes.c_void_p(int(frame.planes[0].buffer_ptr)),
            int(frame.planes[0].line_size), ctypes.c_void_p(int(frame.planes[1].buffer_ptr)),
            int(frame.planes[1].line_size), error, len(error)):
            raise DLSSGBridgeError(error.value.decode("utf-8", "replace") or "CUDA download failed")
        self.close()
        return frame


class DirectDLSSGSession:
    """One persistent interpolation history; all frame pixels stay in-process."""

    def __init__(self, width: int, height: int, generated_count: int,
                 controller: JobController, gpu_ordinal: int, *, hdr: bool = False,
                 timeout: float = 180.0) -> None:
        self.width, self.height = int(width), int(height)
        self.generated_count, self.controller = int(generated_count), controller
        self.ordinal, self.timeout, self.hdr = int(gpu_ordinal), float(timeout), bool(hdr)
        self.library = _MANAGER._load()
        self.status = _MANAGER.initialize(self.ordinal)
        descriptor = SessionDescriptorV1()
        descriptor.struct_size, descriptor.abi_version = ctypes.sizeof(descriptor), BRIDGE_ABI_VERSION
        descriptor.width, descriptor.height = self.width, self.height
        descriptor.generated_count, descriptor.hdr = self.generated_count, int(self.hdr)
        descriptor.surface_pool_size = 8
        error = ctypes.create_string_buffer(4096)
        handle = _MANAGER.call(
            "create", lambda: self.library.fi_session_create(ctypes.byref(descriptor), error, len(error)),
            (descriptor, error), self.timeout)
        if not handle:
            raise DLSSGBridgeError(error.value.decode("utf-8", "replace") or "DLSSG session creation failed")
        self.handle, self.closed = int(handle), False
        self.completed_frames = self.scene_cuts = self.duplicates = 0
        self.upload_bytes = self.download_bytes = 0
        self.stage_timings = {"input_ms": 0.0, "optical_flow_ms": 0.0,
                              "ngx_ms": 0.0, "output_ms": 0.0, "total_ms": 0.0}

    def _source_descriptor(self, frame: Any, *, color_matrix: int, color_range: int,
                           color_primaries: int, color_transfer: int,
                           rotation: int) -> tuple[FrameDescriptorV1, tuple[Any, ...]]:
        descriptor = FrameDescriptorV1.empty()
        descriptor.color_matrix, descriptor.color_range = int(color_matrix), int(color_range)
        descriptor.color_primaries, descriptor.color_transfer = int(color_primaries), int(color_transfer)
        descriptor.rotation = int(rotation) % 360
        if isinstance(frame, np.ndarray):
            pixels = np.ascontiguousarray(frame, dtype=np.uint8)
            if pixels.shape != (self.height, self.width, 4):
                raise ValueError(f"Host DLSSG frame has unexpected shape {pixels.shape}.")
            descriptor.memory_type, descriptor.pixel_format = MEMORY_HOST, FORMAT_RGBA8
            descriptor.width, descriptor.height = self.width, self.height
            descriptor.planes[0], descriptor.strides[0] = int(pixels.ctypes.data), int(pixels.strides[0])
            descriptor.rotation = 0
            return descriptor, (pixels,)
        if frame.format.name != "cuda":
            if frame.format.name not in {"p010", "p010le"} or len(frame.planes) < 2:
                raise ValueError("Host DLSSG video input must be RGBA8 or P010.")
            descriptor.memory_type, descriptor.pixel_format = MEMORY_HOST, FORMAT_P010
            descriptor.width, descriptor.height = int(frame.width), int(frame.height)
            descriptor.planes[0], descriptor.planes[1] = (
                int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr))
            descriptor.strides[0], descriptor.strides[1] = (
                int(frame.planes[0].line_size), int(frame.planes[1].line_size))
            descriptor.rotation = 0
            return descriptor, (frame,)
        if len(frame.planes) < 2:
            raise ValueError("CUDA DLSSG input must be a CUDA NV12/P010 PyAV frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        if sw_format not in {"nv12", "p010", "p010le"}:
            raise ValueError(f"Unsupported CUDA interpolation format: {sw_format or 'unknown'}.")
        descriptor.memory_type = MEMORY_CUDA
        descriptor.pixel_format = FORMAT_P010 if sw_format != "nv12" else FORMAT_NV12
        descriptor.width, descriptor.height = int(frame.width), int(frame.height)
        descriptor.planes[0], descriptor.planes[1] = (int(frame.planes[0].buffer_ptr),
                                                       int(frame.planes[1].buffer_ptr))
        descriptor.strides[0], descriptor.strides[1] = (int(frame.planes[0].line_size),
                                                         int(frame.planes[1].line_size))
        return descriptor, (frame,)

    def process_frame(self, frame: Any, *, force_reset: bool = False,
                      detect_scene_cut: bool = True, output_cuda: bool = True,
                      output_p010: bool = False, color_matrix: int = 1,
                      color_range: int = 0, color_primaries: int = 1,
                      color_transfer: int = 0, rotation: int = 0) -> tuple[list[av.VideoFrame], dict[str, Any]]:
        if self.closed:
            raise DLSSGBridgeError("DLSSG session is closed.")
        if self.controller.cancel.is_set():
            raise Cancelled("Frame interpolation was cancelled.")
        source, keepalive = self._source_descriptor(
            frame, color_matrix=color_matrix, color_range=color_range,
            color_primaries=color_primaries, color_transfer=color_transfer, rotation=rotation)
        result = FrameResultV1.empty()
        error = ctypes.create_string_buffer(4096)
        flags = int(force_reset) * FLAG_FORCE_RESET | int(detect_scene_cut) * FLAG_DETECT_CUT
        output_format = FORMAT_P010 if output_p010 else FORMAT_NV12
        ok = _MANAGER.call(
            "process", lambda: self.library.fi_process_frame_v1(
                ctypes.c_void_p(self.handle), ctypes.byref(source), output_format, flags,
                ctypes.byref(result), error, len(error)),
            (*keepalive, source, result, error), self.timeout)
        if not ok:
            detail = error.value.decode("utf-8", "replace") or "unknown native failure"
            if "access violation" in detail.casefold() or "poison" in detail.casefold():
                _MANAGER.poisoned_reason = detail
            raise DLSSGBridgeError(f"DLSSG evaluation failed: {detail}")
        frames: list[av.VideoFrame] = []
        try:
            for index in range(int(result.generated_count)):
                handle = int(result.surface_handles[index])
                descriptor = FrameDescriptorV1.empty()
                if not handle or not self.library.fi_surface_frame_desc(
                        ctypes.c_void_p(handle), ctypes.byref(descriptor)):
                    raise DLSSGBridgeError("DLSSG returned an invalid output surface.")
                surface = DLSSGCudaSurface(self.library, handle, descriptor, self.ordinal)
                frames.append(surface.to_av_frame() if output_cuda else surface.to_host_frame())
        except BaseException:
            for index in range(len(frames), int(result.generated_count)):
                handle = int(result.surface_handles[index])
                if handle:
                    self.library.fi_surface_release(ctypes.c_void_p(handle))
            raise
        self.completed_frames += 1
        self.scene_cuts += int(result.scene_cut)
        self.duplicates += int(result.duplicate)
        self.upload_bytes += int(result.upload_bytes)
        self.download_bytes += int(result.download_bytes)
        detail = {
            "reset": bool(result.reset), "scene_cut": bool(result.scene_cut),
            "duplicate": bool(result.duplicate), "scene_score": float(result.scene_score),
            "interpolation_disabled": bool(result.interpolation_disabled),
            "nvof_active": bool(result.nvof_active),
            "input_ms": float(result.input_milliseconds),
            "optical_flow_ms": float(result.optical_flow_milliseconds),
            "ngx_ms": float(result.ngx_milliseconds),
            "output_ms": float(result.output_milliseconds),
            "total_ms": float(result.total_milliseconds),
            "upload_bytes": int(result.upload_bytes), "download_bytes": int(result.download_bytes),
        }
        for key in self.stage_timings:
            self.stage_timings[key] += detail[key]
        return frames, detail

    def diagnostics(self) -> dict[str, Any]:
        values = (ctypes.c_uint64 * 8)()
        if self.closed or not self.library.fi_session_diagnostics(
                ctypes.c_void_p(self.handle), values, len(values)):
            return {}
        return {
            "processed_frames": int(values[0]), "scene_cuts": int(values[1]),
            "duplicates": int(values[2]), "upload_bytes": int(values[3]),
            "download_bytes": int(values[4]), "surface_pool_waits": int(values[5]),
            "surface_pool_allocated": int(values[6]), "surface_pool_capacity": int(values[7]),
            "nvof_mode": "SLOW", "timings_ms": dict(self.stage_timings),
        }

    def close(self, *, abort: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        if _MANAGER.poisoned_reason:
            return
        # Drop Python/PyAV references before releasing the session. The native
        # bridge also defers destruction until every DLPack surface is returned,
        # which makes cancellation and encoder exceptions safe.
        gc.collect()
        _MANAGER.call("release", lambda: self.library.fi_session_release(
            ctypes.c_void_p(self.handle)), (self,), 10.0 if abort else self.timeout)

    def abort(self) -> None:
        self.close(abort=True)

    def __enter__(self) -> "DirectDLSSGSession":
        return self

    def __exit__(self, exc_type, *_args) -> None:
        self.close(abort=exc_type is not None)
