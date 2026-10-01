from __future__ import annotations

"""Persistent in-process CUDA/D3D12/NVOF/DLSSG bridge."""

import ctypes
import atexit
import queue
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
BRIDGE = RUNTIME_DIR / "neuroframe_engine_frame_interpolation.dll"
BRIDGE_ABI_VERSION = 3
MEMORY_HOST, MEMORY_CUDA = 1, 2
FORMAT_RGBA8, FORMAT_RGBA16F, FORMAT_NV12, FORMAT_P010 = 1, 2, 4, 5
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
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._worker_lock = threading.Lock()
        self._initialize_lock = threading.RLock()
        self._timed_out_references: list[Any] = []

    def _run_calls(self) -> None:
        while True:
            request = self._calls.get()
            if request is None:
                return
            function, references, done, result, failure = request
            try:
                with NGX_RUNTIME_LOCK:
                    self.guard()  # A queued call may have timed out while waiting for NGX.
                    result.append(function())
            except BaseException as exc:
                if not isinstance(exc, DLSSGBridgePoisonedError):
                    self.poisoned_reason = f"native exception: {exc}"
                failure.append(exc)
            finally:
                # Release successful requests before waking the caller. Retaining the
                # last AVFrame here would keep a decoder/output surface occupied.
                function = references = request = None
                done.set()
                done = result = failure = None

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
            raise DLSSGBridgeError(
                f"Could not load neuroframe_engine_frame_interpolation.dll: {exc}"
            ) from exc
        library.fi_abi_version.restype = ctypes.c_uint32
        library.fi_version.restype = ctypes.c_char_p
        library.fi_init.argtypes = [ctypes.c_int, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_int]
        library.fi_init.restype = ctypes.c_int
        library.fi_get_status_v1.argtypes = [ctypes.POINTER(BridgeStatusV1)]
        library.fi_get_status_v1.restype = ctypes.c_int
        library.fi_session_create.argtypes = [ctypes.POINTER(SessionDescriptorV1), ctypes.c_void_p, ctypes.c_int]
        library.fi_session_create.restype = ctypes.c_void_p
        library.fi_session_release.argtypes = [ctypes.c_void_p]
        library.fi_session_release.restype = None
        library.fi_process_frame_v1.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1), ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(FrameResultV1), ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_process_frame_v1.restype = ctypes.c_int
        library.fi_surface_frame_desc.argtypes = [ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1)]
        library.fi_surface_frame_desc.restype = ctypes.c_int
        library.fi_session_copy_input_rgb.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        library.fi_session_copy_input_rgb.restype = ctypes.c_void_p
        library.fi_surface_retain.argtypes = [ctypes.c_void_p]
        library.fi_surface_retain.restype = None
        library.fi_surface_release.argtypes = [ctypes.c_void_p]
        library.fi_surface_release.restype = None
        library.fi_surface_copy_to_host.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_surface_copy_to_host.restype = ctypes.c_int
        library.fi_surface_copy_rgb_to_host.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_surface_copy_rgb_to_host.restype = ctypes.c_int
        library.fi_surface_copy_gbrp10_to_host.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_surface_copy_gbrp10_to_host.restype = ctypes.c_int
        library.fi_surface_convert.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_surface_convert.restype = ctypes.c_void_p
        library.fi_surface_convert_video.argtypes = library.fi_surface_convert.argtypes
        library.fi_surface_convert_video.restype = ctypes.c_void_p
        library.fi_session_copy_motion.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_session_copy_motion.restype = ctypes.c_int
        library.fi_session_copy_nvof_flow.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p, ctypes.c_int,
        ]
        library.fi_session_copy_nvof_flow.restype = ctypes.c_int
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
        with self._initialize_lock:
            self.guard()
            if self.gpu_ordinal is not None and self.gpu_ordinal != int(ordinal):
                raise DLSSGBridgeError(
                    "Frame Interpolation is already bound to another AI GPU. Restart before changing it.")
            self._ensure_ffmpeg_device(int(ordinal))
            library = self._load()
            error = ctypes.create_string_buffer(4096)
            if not self.call("initialize", lambda: library.fi_init(
                    int(ordinal), str(RUNTIME_DIR), error, len(error)), (error,), 180.0):
                detail = error.value.decode("utf-8", "replace") or "unknown initialization failure"
                self.raise_native("DLSSG bridge initialization", detail)
            self.gpu_ordinal = int(ordinal)
            return self.status()

    def status(self) -> dict[str, Any]:
        record = BridgeStatusV1.empty()
        library = self._load()
        if not self.call("status", lambda: library.fi_get_status_v1(ctypes.byref(record)),
                         (record,), 180.0):
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
        done, result, failure = self._submit(function, keepalive)
        if not done.wait(timeout):
            # Native code may still use these pointers even after its caller exits.
            self._timed_out_references.extend(keepalive)
            self.poisoned_reason = f"{label} exceeded {timeout:g} seconds"
            self.guard()
        if failure:
            self.poisoned_reason = f"native exception during {label}: {failure[0]}"
            self.guard()
        return result[0]

    def _submit(self, function, keepalive: tuple[Any, ...]):
        done = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        with self._worker_lock:
            if self._stopping:
                raise DLSSGBridgeError("Frame Interpolation is shutting down.")
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_calls, name="dlssg-native", daemon=True)
                self._worker.start()
                atexit.register(self._shutdown_worker)
            self._calls.put((function, keepalive, done, result, failure))
        return done, result, failure

    def _shutdown_worker(self) -> None:
        # Finish queued encoder decrements before Python destroys the daemon
        # thread/DLLs. Native GPU teardown must not run under Windows' loader lock.
        with self._worker_lock:
            if self._worker is None or self._stopping:
                return
            self._stopping = True
            worker = self._worker
            self._calls.put(None)
        worker.join(timeout=5.0)
        if worker.is_alive():
            self.poisoned_reason = "native worker did not drain during shutdown"

    def release_surface(self, library, handle: int, owner: Any) -> None:
        if self.poisoned_reason or self._stopping:
            return
        # A DLPack destructor can run on FFmpeg's encoding thread. Queue its
        # decrement instead of blocking that thread behind an NGX evaluation.
        # The request retains its owner/native reference until the worker runs.
        # FIFO ordering also drains these decrements before session close/status.
        self._submit(lambda: library.fi_surface_release(ctypes.c_void_p(handle)), (owner,))

    def raise_native(self, label: str, detail: str) -> None:
        if "poison" in detail.casefold() or "access violation" in detail.casefold():
            self.poisoned_reason = detail
        elif self.library is not None and self.status()["poisoned"]:
            self.poisoned_reason = detail
        self.guard()
        raise DLSSGBridgeError(f"{label} failed: {detail}")


_MANAGER = _BridgeManager()


def preload_bridge() -> None:
    """Load the bridge before showing the UI without fixing the GPU choice."""
    with NGX_RUNTIME_LOCK:
        _MANAGER._load()


def initialize_bridge(ordinal: int) -> dict[str, Any]:
    return _MANAGER.initialize(ordinal)


class DLSSGCudaSurface:
    def __init__(self, library: Any, handle: int, descriptor: FrameDescriptorV1, ordinal: int,
                 timeout: float = 180.0) -> None:
        self.library, self.handle, self.descriptor = library, int(handle), descriptor
        self.ordinal, self.closed = int(ordinal), False
        self.timeout = float(timeout)

    def retain(self) -> None:
        if not _MANAGER.poisoned_reason:
            self.library.fi_surface_retain(ctypes.c_void_p(self.handle))

    def release(self) -> None:
        _MANAGER.release_surface(self.library, self.handle, self)

    def close(self) -> None:
        if not self.closed:
            self.release()
            self.closed = True

    def __del__(self) -> None:
        if not self.closed:
            self.close()

    def _require_yuv(self) -> None:
        self._require_open()
        if self.descriptor.pixel_format not in {FORMAT_NV12, FORMAT_P010}:
            raise DLSSGBridgeError("An RGB surface must be converted before YUV encoding.")

    def _require_open(self) -> None:
        _MANAGER.guard()
        if self.closed:
            raise DLSSGBridgeError("DLSSG surface is closed.")

    def to_av_frame(self) -> av.VideoFrame:
        self._require_yuv()
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
        self._require_yuv()
        pixel_format = "p010le" if self.descriptor.pixel_format == FORMAT_P010 else "nv12"
        frame = av.VideoFrame(int(self.descriptor.width), int(self.descriptor.height), pixel_format)
        error = ctypes.create_string_buffer(2048)
        ok = _MANAGER.call("YUV download", lambda: self.library.fi_surface_copy_to_host(
            ctypes.c_void_p(self.handle), ctypes.c_void_p(int(frame.planes[0].buffer_ptr)),
            int(frame.planes[0].line_size), ctypes.c_void_p(int(frame.planes[1].buffer_ptr)),
            int(frame.planes[1].line_size), error, len(error)), (self, frame, error), self.timeout)
        if not ok:
            _MANAGER.raise_native("CUDA download", error.value.decode("utf-8", "replace"))
        self.close()
        return frame

    def to_host_rgb(self) -> np.ndarray:
        self._require_open()
        if self.descriptor.pixel_format not in {FORMAT_RGBA8, FORMAT_RGBA16F}:
            raise DLSSGBridgeError("The surface does not contain RGB pixels.")
        dtype = np.float16 if self.descriptor.pixel_format == FORMAT_RGBA16F else np.uint8
        pixels = np.empty((int(self.descriptor.height), int(self.descriptor.width), 4), dtype=dtype)
        error = ctypes.create_string_buffer(2048)
        ok = _MANAGER.call("RGB download", lambda: self.library.fi_surface_copy_rgb_to_host(
                ctypes.c_void_p(self.handle), ctypes.c_void_p(int(pixels.ctypes.data)),
                int(pixels.strides[0]), error, len(error)), (self, pixels, error), self.timeout)
        if not ok:
            _MANAGER.raise_native("RGB download", error.value.decode("utf-8", "replace"))
        return pixels

    def to_yuv_surface(self, *, color_matrix: int, color_range: int,
                       p010: bool, video_color: bool = False) -> "DLSSGCudaSurface":
        self._require_open()
        error = ctypes.create_string_buffer(2048)
        convert = self.library.fi_surface_convert_video if video_color else self.library.fi_surface_convert
        handle = _MANAGER.call("RGB conversion", lambda: convert(
            ctypes.c_void_p(self.handle), FORMAT_P010 if p010 else FORMAT_NV12,
            int(color_matrix), int(color_range), error, len(error)), (self, error), self.timeout)
        if not handle:
            _MANAGER.raise_native("RGB conversion", error.value.decode("utf-8", "replace"))
        descriptor = FrameDescriptorV1.empty()
        if not self.library.fi_surface_frame_desc(ctypes.c_void_p(handle), ctypes.byref(descriptor)):
            self.library.fi_surface_release(ctypes.c_void_p(handle))
            raise DLSSGBridgeError("RGB conversion returned an invalid CUDA surface.")
        return DLSSGCudaSurface(self.library, int(handle), descriptor, self.ordinal, self.timeout)

    def to_host_gbrp10_frame(self) -> av.VideoFrame:
        self._require_open()
        if self.descriptor.pixel_format != FORMAT_RGBA16F:
            raise DLSSGBridgeError("GBRP10 packing requires an HDR RGB surface.")
        frame = av.VideoFrame(int(self.descriptor.width), int(self.descriptor.height), "gbrp10le")
        error = ctypes.create_string_buffer(2048)
        args = tuple(value for plane in frame.planes
                     for value in (ctypes.c_void_p(int(plane.buffer_ptr)), int(plane.line_size)))
        if not _MANAGER.call("GBRP10 download", lambda: self.library.fi_surface_copy_gbrp10_to_host(
                ctypes.c_void_p(self.handle), *args, error, len(error)), (self, frame, error), self.timeout):
            _MANAGER.raise_native("GBRP10 download", error.value.decode("utf-8", "replace"))
        return frame


class DirectDLSSGSession:
    """One persistent interpolation history; all frame pixels stay in-process."""

    def __init__(self, width: int, height: int, generated_count: int,
                 controller: JobController, gpu_ordinal: int, *, hdr: bool = False,
                 timeout: float = 180.0, video_color: bool = False,
                 chroma_location: str = "left") -> None:
        self.width, self.height = int(width), int(height)
        self.generated_count, self.controller = int(generated_count), controller
        self.ordinal, self.timeout, self.hdr = int(gpu_ordinal), float(timeout), bool(hdr)
        self.video_color = bool(video_color)
        self.chroma_location = {"left": 1, "center": 2, "topleft": 3, "top": 4,
                                "bottomleft": 5, "bottom": 6}.get(str(chroma_location), 1)
        self.library = _MANAGER._load()
        self.status = _MANAGER.initialize(self.ordinal)
        descriptor = SessionDescriptorV1()
        descriptor.struct_size, descriptor.abi_version = ctypes.sizeof(descriptor), BRIDGE_ABI_VERSION
        descriptor.width, descriptor.height = self.width, self.height
        descriptor.generated_count, descriptor.hdr = self.generated_count, int(self.hdr)
        # Multi-frame hardware can return four RGB frames while NVENC retains
        # YUV frames for its queues. Capacity is lazy, rather than preallocated.
        descriptor.surface_pool_size = 32 if self.generated_count > 1 else 16
        error = ctypes.create_string_buffer(4096)
        handle = _MANAGER.call(
            "create", lambda: self.library.fi_session_create(ctypes.byref(descriptor), error, len(error)),
            (descriptor, error), self.timeout)
        if not handle:
            _MANAGER.raise_native("DLSSG session creation", error.value.decode("utf-8", "replace"))
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
        # ABI 3's formerly reserved fields opt video into the CLI-equivalent
        # chroma filter/siting; existing direct callers keep their pixel math.
        descriptor.reserved[0] = int(self.video_color)
        descriptor.reserved[1] = self.chroma_location if self.video_color else 0
        if isinstance(frame, DLSSGCudaSurface):
            if frame.closed:
                raise DLSSGBridgeError("A released RGB surface cannot be interpolated.")
            if frame.ordinal != self.ordinal:
                raise DLSSGBridgeError("RGB surface belongs to a different CUDA device.")
            descriptor.memory_type = MEMORY_CUDA
            descriptor.pixel_format = frame.descriptor.pixel_format
            descriptor.width, descriptor.height = frame.descriptor.width, frame.descriptor.height
            descriptor.planes[:] = frame.descriptor.planes[:]
            descriptor.strides[:] = frame.descriptor.strides[:]
            descriptor.rotation = 0
            return descriptor, (frame,)
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
                      output_p010: bool = False, output_rgb: bool = False,
                      color_matrix: int = 1,
                      color_range: int = 0, color_primaries: int = 1,
                      color_transfer: int = 0, rotation: int = 0) -> tuple[list[Any], dict[str, Any]]:
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
        output_format = (FORMAT_RGBA16F if self.hdr else FORMAT_RGBA8) if output_rgb else (
            FORMAT_P010 if output_p010 else FORMAT_NV12)
        ok = _MANAGER.call(
            "process", lambda: self.library.fi_process_frame_v1(
                ctypes.c_void_p(self.handle), ctypes.byref(source), output_format, flags,
                ctypes.byref(result), error, len(error)),
            (*keepalive, source, result, error), self.timeout)
        if not ok:
            detail = error.value.decode("utf-8", "replace") or "unknown native failure"
            _MANAGER.raise_native("DLSSG evaluation", detail)
        frames: list[Any] = []
        unclaimed = {int(result.surface_handles[i]) for i in range(int(result.generated_count))}
        try:
            for index in range(int(result.generated_count)):
                handle = int(result.surface_handles[index])
                descriptor = FrameDescriptorV1.empty()
                if not handle or not self.library.fi_surface_frame_desc(
                        ctypes.c_void_p(handle), ctypes.byref(descriptor)):
                    raise DLSSGBridgeError("DLSSG returned an invalid output surface.")
                surface = DLSSGCudaSurface(self.library, handle, descriptor, self.ordinal, self.timeout)
                unclaimed.discard(handle)  # This wrapper now owns the native reference.
                try:
                    frames.append(surface if output_rgb else (
                        surface.to_av_frame() if output_cuda else surface.to_host_frame()))
                finally:
                    if not output_rgb:
                        surface.close()
        except BaseException:
            for handle in unclaimed:
                if handle and not _MANAGER.poisoned_reason:
                    self.library.fi_surface_release(ctypes.c_void_p(handle))
            for frame in frames:
                if isinstance(frame, DLSSGCudaSurface):
                    frame.close()
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

    def copy_current_input_rgb(self) -> DLSSGCudaSurface:
        """Retain the RGB input last supplied to this DLSSG session."""
        if self.closed or not self.completed_frames:
            raise DLSSGBridgeError("No current DLSSG RGB input is available.")
        error = ctypes.create_string_buffer(2048)
        handle = _MANAGER.call(
            "copy input RGB", lambda: self.library.fi_session_copy_input_rgb(
                ctypes.c_void_p(self.handle), error, len(error)),
            (self, error), self.timeout)
        if not handle:
            _MANAGER.raise_native("Input RGB retention", error.value.decode("utf-8", "replace"))
        descriptor = FrameDescriptorV1.empty()
        if not self.library.fi_surface_frame_desc(ctypes.c_void_p(handle), ctypes.byref(descriptor)):
            self.library.fi_surface_release(ctypes.c_void_p(handle))
            raise DLSSGBridgeError("Input RGB copy returned an invalid surface.")
        expected = FORMAT_RGBA16F if self.hdr else FORMAT_RGBA8
        if descriptor.pixel_format != expected:
            self.library.fi_surface_release(ctypes.c_void_p(handle))
            raise DLSSGBridgeError("Input RGB copy returned an unexpected format.")
        return DLSSGCudaSurface(self.library, int(handle), descriptor, self.ordinal, self.timeout)

    def diagnostics(self) -> dict[str, Any]:
        values = (ctypes.c_uint64 * 12)()
        if self.closed or not _MANAGER.call("diagnostics", lambda: self.library.fi_session_diagnostics(
                ctypes.c_void_p(self.handle), values, len(values)), (self, values), self.timeout):
            return {}
        return {
            "processed_frames": int(values[0]), "scene_cuts": int(values[1]),
            "duplicates": int(values[2]), "upload_bytes": int(values[3]),
            "download_bytes": int(values[4]), "surface_pool_waits": int(values[5]),
            "surface_pool_allocated": int(values[6]), "surface_pool_capacity": int(values[7]),
            "source_pool_allocated": int(values[8]), "source_frames_retained": int(values[9]),
            "temporal_descriptors": int(values[10]), "host_staging_bytes": int(values[11]),
            "nvof_mode": "SLOW", "timings_ms": dict(self.stage_timings),
        }

    def motion_diagnostics(self) -> np.ndarray:
        """Download the final current-to-previous pixel vectors for diagnostics."""
        if self.closed:
            raise DLSSGBridgeError("DLSSG session is closed.")
        values = np.empty((self.height, self.width, 2), dtype=np.float16)
        error = ctypes.create_string_buffer(2048)
        ok = _MANAGER.call(
            "motion diagnostics", lambda: self.library.fi_session_copy_motion(
                ctypes.c_void_p(self.handle), ctypes.c_void_p(int(values.ctypes.data)),
                int(values.strides[0]), error, len(error)),
            (self, values, error), self.timeout)
        if not ok:
            raise DLSSGBridgeError(error.value.decode("utf-8", "replace") or "Motion readback failed")
        return values.astype(np.float32)

    def nvof_flow_diagnostics(self, *, backward: bool = False) -> np.ndarray:
        """Download raw NVOF grid vectors in pixel units for motion validation."""
        if self.closed:
            raise DLSSGBridgeError("DLSSG session is closed.")
        width, height = ctypes.c_uint32(), ctypes.c_uint32()
        error = ctypes.create_string_buffer(2048)
        args = (ctypes.c_void_p(self.handle), int(backward))
        if not _MANAGER.call("NVOF dimensions", lambda: self.library.fi_session_copy_nvof_flow(
                *args, None, 0, ctypes.byref(width), ctypes.byref(height), error, len(error)),
                (self, width, height, error), self.timeout):
            _MANAGER.raise_native("NVOF dimensions", error.value.decode("utf-8", "replace"))
        values = np.empty((height.value, width.value, 2), dtype=np.int16)
        ok = _MANAGER.call(
            "NVOF flow diagnostics", lambda: self.library.fi_session_copy_nvof_flow(
                *args, ctypes.c_void_p(int(values.ctypes.data)), int(values.strides[0]),
                ctypes.byref(width), ctypes.byref(height), error, len(error)),
            (self, values, error), self.timeout)
        if not ok:
            raise DLSSGBridgeError(error.value.decode("utf-8", "replace") or "NVOF flow readback failed")
        return values.astype(np.float32) / 32.0

    def close(self, *, abort: bool = False) -> None:
        if self.closed:
            return
        self.closed = True
        if _MANAGER.poisoned_reason:
            return
        # Native ownership defers destruction until every retained RGB/DLPack
        # surface is returned. A full Python heap collection is unnecessary here.
        _MANAGER.call("release", lambda: self.library.fi_session_release(
            ctypes.c_void_p(self.handle)), (self,), 10.0 if abort else self.timeout)

    def abort(self) -> None:
        self.close(abort=True)

    def __enter__(self) -> "DirectDLSSGSession":
        return self

    def __exit__(self, exc_type, *_args) -> None:
        self.close(abort=exc_type is not None)
