from __future__ import annotations

"""Process-wide D3D12/NGX feature-18 bridge management.

The NVIDIA NGX runtime is intentionally process-lifetime state.  In particular,
normal session close never calls ``NVSDK_NGX_D3D12_Shutdown`` and never unloads
driver modules: both operations have been observed to wedge after a successful
feature-18 evaluation.  Logical sessions own only their CUDA frame buffers and
their diagnostics.
"""

import ctypes
import contextlib
import itertools
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .paths import DLSSNR_BRIDGE, DLSSNR_DIR
from .ngx_runtime import NGX_RUNTIME_LOCK
from .cuda_dlpack import CudaDLPackPlane, LIVE_DLPACK_RECORDS, DLPACK_LOCK

BRIDGE_ABI_VERSION = 11
BRIDGE_FRAME_ABI_MAX_VERSION = 11
BRIDGE_WATCHDOG_SECONDS = 45.0
MEMORY_HOST = 0
MEMORY_CUDA = 1
MEMORY_NONE = 2
FORMAT_RGBA8 = 1
FORMAT_NV12 = 2
FORMAT_P010 = 3
FORMAT_RGBA16LE = 4

def _bridge_failure_requires_restart(detail: str) -> bool:
    """Return whether retrying through either memory path is unsafe/useless."""
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "corrupt",
            "access violation",
            "device recovery failed",
            "device recovery aborted",
            "device removal",
            "reinitialization failed",
            "gpu completion was not confirmed",
            "native fence wait failed",
            "cuda context is poisoned",
            "restart the application",
        )
    )

class FrameDescriptorV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("memory_type", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("planes", ctypes.c_uint64 * 3),
        ("strides", ctypes.c_uint32 * 3),
        ("color_matrix", ctypes.c_uint32),
        ("color_range", ctypes.c_uint32),
        ("rotation", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("timestamp", ctypes.c_int64),
    ]

    @classmethod
    def empty(cls) -> "FrameDescriptorV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value

class RenderParametersV11(ctypes.Structure):
    """Native NR controls, intensity resolve, pass count, and native RGBA mask."""
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("style", ctypes.c_int32), ("intensity", ctypes.c_float),
        ("tone", ctypes.c_float), ("structure", ctypes.c_float), ("skin", ctypes.c_float),
        ("automask", ctypes.c_int32), ("reset", ctypes.c_int32), ("nr_passes", ctypes.c_int32),
        ("native_intensity", ctypes.c_float), ("mask_memory_type", ctypes.c_uint32),
        ("mask_width", ctypes.c_uint32), ("mask_height", ctypes.c_uint32),
        ("mask_stride", ctypes.c_uint32), ("motion_mode", ctypes.c_uint32),
        ("mask_plane", ctypes.c_uint64), ("mask_revision", ctypes.c_uint64),
        ("optical_flow_quality", ctypes.c_uint32), ("reserved", ctypes.c_uint32),
    ]

class FrameResultV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("ngx_create_result", ctypes.c_int32),
        ("ngx_evaluate_result", ctypes.c_int32),
        ("cuda_result", ctypes.c_int32),
        ("scene_reset", ctypes.c_int32),
        ("scene_score", ctypes.c_float),
        ("reserved", ctypes.c_uint32),
        ("upload_bytes", ctypes.c_uint64),
        ("download_bytes", ctypes.c_uint64),
        ("timestamp", ctypes.c_int64),
    ]

    @classmethod
    def empty(cls) -> "FrameResultV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value

class NeuralBridgeError(RuntimeError):
    """An actionable failure returned by the native Neural Rendering bridge."""

class NeuralBridgePoisonedError(NeuralBridgeError):
    """The in-process native state cannot safely be reused before app restart."""

def _text(value: bytes | None) -> str:
    return value.decode("utf-8", "replace") if value else ""

class _CudaDriver:
    """Small CUDA Driver API wrapper; no CUDA toolkit or Python add-on needed."""

    def __init__(self, ordinal: int) -> None:
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        try:
            self.lib = loader("nvcuda.dll")
        except OSError as exc:
            raise NeuralBridgeError(
                "CUDA interoperability is unavailable because nvcuda.dll could not be "
                "loaded. Neural Rendering requires CUDA/D3D12 interoperability."
            ) from exc

        self._bind("cuInit", [ctypes.c_uint])
        self._bind("cuDeviceGet", [ctypes.POINTER(ctypes.c_int), ctypes.c_int])
        self._bind(
            "cuDevicePrimaryCtxRetain",
            [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int],
        )
        self.primary_ctx_release = self._bind("cuDevicePrimaryCtxRelease_v2", [ctypes.c_int])
        self._bind("cuCtxSetCurrent", [ctypes.c_void_p])
        self._bind("cuCtxSynchronize", [])
        self.mem_alloc = self._bind("cuMemAlloc_v2", [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t])
        self.mem_free = self._bind("cuMemFree_v2", [ctypes.c_uint64])
        self.copy_htod = self._bind("cuMemcpyHtoD_v2", [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t])
        self.copy_dtoh = self._bind("cuMemcpyDtoH_v2", [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t])
        self._bind(
            "cuGetErrorString",
            [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)],
        )

        self._check(self.cuInit(0), "cuInit")
        device = ctypes.c_int()
        self._check(self.cuDeviceGet(ctypes.byref(device), int(ordinal)), "cuDeviceGet")
        context = ctypes.c_void_p()
        self._check(
            self.cuDevicePrimaryCtxRetain(ctypes.byref(context), device.value),
            "cuDevicePrimaryCtxRetain",
        )
        self._check(self.cuCtxSetCurrent(context), "cuCtxSetCurrent")
        self.ordinal = int(ordinal)
        self.device = int(device.value)
        self.context = context
        self.closed = False

    def _bind(self, name: str, argtypes: list[Any]) -> Any:
        function = getattr(self.lib, name)
        function.argtypes = argtypes
        function.restype = ctypes.c_int
        setattr(self, name, function)
        return function

    def _check(self, result: int, operation: str) -> None:
        if result == 0:
            return
        message = ctypes.c_char_p()
        detail = ""
        try:
            if self.cuGetErrorString(result, ctypes.byref(message)) == 0:
                detail = _text(message.value)
        except Exception:
            pass
        suffix = f": {detail}" if detail else ""
        raise NeuralBridgeError(f"{operation} failed with CUDA result {result}{suffix}.")

    def activate(self) -> None:
        self._check(self.cuCtxSetCurrent(self.context), "cuCtxSetCurrent")

    def deactivate(self) -> None:
        self._check(self.cuCtxSetCurrent(None), "cuCtxSetCurrent(NULL)")

    def alloc(self, byte_count: int) -> int:
        self.activate()
        pointer = ctypes.c_uint64()
        self._check(self.mem_alloc(ctypes.byref(pointer), byte_count), "cuMemAlloc")
        return int(pointer.value)

    def free(self, pointer: int) -> None:
        if not pointer:
            return
        self.activate()
        self._check(self.mem_free(ctypes.c_uint64(pointer)), "cuMemFree")

    def upload(self, pointer: int, array: np.ndarray) -> None:
        self.activate()
        self._check(
            self.copy_htod(
                ctypes.c_uint64(pointer), ctypes.c_void_p(array.ctypes.data), array.nbytes
            ),
            "cuMemcpyHtoD",
        )

    def download(self, array: np.ndarray, pointer: int) -> None:
        self.activate()
        self._check(
            self.copy_dtoh(
                ctypes.c_void_p(array.ctypes.data), ctypes.c_uint64(pointer), array.nbytes
            ),
            "cuMemcpyDtoH",
        )

    def synchronize(self) -> None:
        self.activate()
        self._check(self.cuCtxSynchronize(), "cuCtxSynchronize")

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.deactivate()
        finally:
            self._check(
                self.primary_ctx_release(self.device),
                "cuDevicePrimaryCtxRelease",
            )
            self.context = ctypes.c_void_p()
            self.closed = True

@dataclass(slots=True)
class CudaFrameBuffers:
    driver: _CudaDriver
    elements: int
    input_pointer: int
    output_pointer: int
    closed: bool = False

    @classmethod
    def create(cls, driver: _CudaDriver, width: int, height: int) -> "CudaFrameBuffers":
        elements = int(width) * int(height) * 3
        byte_count = elements * np.dtype(np.float32).itemsize
        input_pointer = driver.alloc(byte_count)
        try:
            output_pointer = driver.alloc(byte_count)
        except Exception:
            driver.free(input_pointer)
            raise
        return cls(driver, elements, input_pointer, output_pointer)

    def close(self) -> None:
        if self.closed:
            return
        if BRIDGE_MANAGER._poisoned_reason:
            self.closed = True
            return
        try:
            self.driver.synchronize()
            self.driver.free(self.output_pointer)
            self.driver.free(self.input_pointer)
            self.input_pointer = 0
            self.output_pointer = 0
            self.closed = True
        finally:
            self.driver.deactivate()

_MASK_REVISIONS = itertools.count(1)

@dataclass(slots=True)
class CudaMaskBuffer:
    driver: _CudaDriver
    pointer: int
    byte_count: int
    revision: int
    closed: bool = False

    @classmethod
    def create(cls, driver: _CudaDriver, mask: np.ndarray) -> "CudaMaskBuffer":
        if (mask.dtype != np.float32 or mask.ndim != 3 or mask.shape[2] != 4
                or not mask.flags.c_contiguous or not np.isfinite(mask).all()):
            raise NeuralBridgeError("Native ControlMask must be contiguous finite float32 RGBA.")
        pointer = driver.alloc(mask.nbytes)
        try:
            driver.upload(pointer, mask)
            driver.synchronize()
        except Exception:
            driver.free(pointer)
            raise
        return cls(driver, pointer, int(mask.nbytes), next(_MASK_REVISIONS))

    def close(self) -> None:
        if self.closed:
            return
        if BRIDGE_MANAGER._poisoned_reason:
            self.closed = True
            return
        try:
            self.driver.synchronize()
            self.driver.free(self.pointer)
            self.pointer = 0
            self.closed = True
        finally:
            self.driver.deactivate()

class BridgeCudaSurface:
    """Bridge-owned NV12/P010 allocation with DLPack-managed plane lifetime."""

    def __init__(
        self,
        library: Any,
        handle: int,
        descriptor: FrameDescriptorV1,
        ordinal: int,
        released: threading.Event | None = None,
    ) -> None:
        self.library = library
        self.handle = int(handle)
        self.descriptor = descriptor
        self.ordinal = int(ordinal)
        self.closed = False
        self.released = released

    def retain(self) -> None:
        self.library.dlss5nr_surface_retain(ctypes.c_void_p(self.handle))

    def release(self) -> None:
        if BRIDGE_MANAGER._poisoned_reason:
            return
        self.library.dlss5nr_surface_release(ctypes.c_void_p(self.handle))
        if self.released is not None:
            self.released.set()

    def close(self) -> None:
        if not self.closed:
            self.release()
            self.closed = True

    def to_av_frame(self):
        import av

        desc = self.descriptor
        bits = 16 if desc.pixel_format == FORMAT_P010 else 8
        item_size = bits // 8
        y_plane = CudaDLPackPlane(
            pointer=int(desc.planes[0]),
            shape=(int(desc.height), int(desc.width)),
            strides=(int(desc.strides[0]) // item_size, 1),
            bits=bits,
            device_id=self.ordinal,
            retain=self.retain,
            release=self.release,
        )
        uv_plane = CudaDLPackPlane(
            pointer=int(desc.planes[1]),
            shape=((int(desc.height) + 1) // 2, (int(desc.width) + 1) // 2, 2),
            strides=(int(desc.strides[1]) // item_size, 2, 1),
            bits=bits,
            device_id=self.ordinal,
            retain=self.retain,
            release=self.release,
        )
        frame = av.VideoFrame.from_dlpack(
            [y_plane, uv_plane],
            format="p010le" if desc.pixel_format == FORMAT_P010 else "nv12",
            width=int(desc.width),
            height=int(desc.height),
            device_id=self.ordinal,
            primary_ctx=True,
        )
        # Drop the creator's reference. The two DLPack tensors now own the
        # allocation until the AVFrame and its encoder references are released.
        self.close()
        return frame

@dataclass(slots=True)
class BridgeSessionDiagnostics:
    gpu_mode: bool
    memory_path: str
    decode_backend: str = "software"
    encode_backend: str = "cpu"
    pixel_format: str = "rgba8"
    source_format: str = "unknown"
    working_format: str = "rgba8"
    ngx_format: str = "rgba16f"
    output_format: str = "unknown"
    frames: int = 0
    feature_evaluations: int = 0
    scene_resets: int = 0
    upload_bytes: int = 0
    download_bytes: int = 0
    ngx_create_result: str = "0x00000001"
    ngx_evaluate_result: str = "0x00000001"
    events: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "bridge_abi_version": BRIDGE_ABI_VERSION,
            "bridge_frame_abi_max_version": BRIDGE_FRAME_ABI_MAX_VERSION,
            "gpu_mode": self.gpu_mode,
            "memory_path": self.memory_path,
            "decode_backend": self.decode_backend,
            "encode_backend": self.encode_backend,
            "pixel_format": self.pixel_format,
            "source_format": self.source_format,
            "working_format": self.working_format,
            "ngx_format": self.ngx_format,
            "output_format": self.output_format,
            "frames": self.frames,
            "feature_evaluations": self.feature_evaluations,
            "scene_resets": self.scene_resets,
            "transfers": {
                "upload_bytes": self.upload_bytes,
                "download_bytes": self.download_bytes,
            },
            "ngx": {
                "create_result": self.ngx_create_result,
                "evaluate_result": self.ngx_evaluate_result,
            },
        }

class NeuralBridgeManager:
    """One serialized bridge instance shared by every logical render session."""

    def __init__(self) -> None:
        # NGX is process-wide even when feature APIs use different backends.
        self._lock = NGX_RUNTIME_LOCK
        self._library: Any | None = None
        self._initialized_ordinal: int | None = None
        self._cuda_driver: _CudaDriver | None = None
        self._poisoned_reason = ""
        self._timed_out_references: list[Any] = []
        self._active_sessions = 0
        self._image_batches = 0
        self._pending_session_release = False
        self._version = "unloaded"
        self._gpu_name = "unknown"
        self._calls: queue.SimpleQueue = queue.SimpleQueue()
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._surface_ready = threading.Event()

    def _run_calls(self) -> None:
        while True:
            request = self._calls.get()
            function, references, completed, result, failure = request
            try:
                self._guard_poison()
                result.append(function())
            except BaseException as exc:
                failure.append(exc)
            finally:
                # The caller owns the shared NGX lock. Taking it here would
                # deadlock that caller. Retain arguments until native returns.
                function = references = request = None
                completed.set()
                completed = result = failure = None

    @property
    def version(self) -> str:
        return self._version

    def preload(self) -> str:
        """Load and validate the bridge DLL without binding an adapter."""
        with self._lock:
            self._load()
            return self._version

    @property
    def gpu_name(self) -> str:
        return self._gpu_name

    def temporal_status(self) -> dict[str, Any]:
        self._guard_poison()
        with self._lock:
            if self._library is None:
                return {}
            buffer = ctypes.create_string_buffer(4096)
            try:
                self._library.dlss5nr_temporal_status(buffer, len(buffer))
                value = json.loads(_text(buffer.value) or "{}")
                return value if isinstance(value, dict) else {}
            except (AttributeError, json.JSONDecodeError, OSError):
                return {}

    def _load(self) -> None:
        if self._library is not None:
            return
        if not DLSSNR_BRIDGE.is_file():
            raise NeuralBridgeError(
                f"Neural Rendering bridge is missing: {DLSSNR_BRIDGE}"
            )
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        try:
            library = loader(str(DLSSNR_BRIDGE))
        except OSError as exc:
            raise NeuralBridgeError(
                f"Neural Rendering bridge could not be loaded: {exc}"
            ) from exc

        c_float_p = ctypes.POINTER(ctypes.c_float)
        library.dlss5nr_version.argtypes = []
        library.dlss5nr_version.restype = ctypes.c_char_p
        library.dlss5nr_gpu_name.argtypes = []
        library.dlss5nr_gpu_name.restype = ctypes.c_char_p
        library.dlss5nr_adapter_luid.argtypes = []
        library.dlss5nr_adapter_luid.restype = ctypes.c_char_p
        library.dlss5nr_frame_abi_version.argtypes = []
        library.dlss5nr_frame_abi_version.restype = ctypes.c_uint32
        version = _text(library.dlss5nr_version()) or "unknown"
        frame_abi = int(library.dlss5nr_frame_abi_version())
        if frame_abi != BRIDGE_ABI_VERSION:
            raise NeuralBridgeError(
                f"Neural Rendering bridge {version} uses frame ABI {frame_abi}, but "
                f"this application requires ABI {BRIDGE_ABI_VERSION}. Rebuild the native "
                "bridge or reinstall the matching runtime."
            )
        library.dlss5nr_init.argtypes = [
            ctypes.c_int,
            ctypes.c_wchar_p,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_init.restype = ctypes.c_int
        library.dlss5nr_rebind.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_rebind.restype = ctypes.c_int
        library.dlss5nr_process_v11.argtypes = [
            c_float_p, c_float_p, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(RenderParametersV11), ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_process_v11.restype = ctypes.c_int
        library.dlss5nr_process_cuda_v11.argtypes = [
            ctypes.c_uint64, ctypes.c_uint64, ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.POINTER(RenderParametersV11), ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_process_cuda_v11.restype = ctypes.c_int
        library.dlss5nr_cuda_supported.argtypes = []
        library.dlss5nr_cuda_supported.restype = ctypes.c_int
        library.dlss5nr_cuda_status.argtypes = [ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_cuda_status.restype = ctypes.c_int
        library.dlss5nr_process_frame_v11.argtypes = [
            ctypes.POINTER(FrameDescriptorV1), ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV11), ctypes.POINTER(FrameResultV1), ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_process_frame_v11.restype = ctypes.c_int
        library.dlss5nr_process_cache_v11.argtypes = [
            *library.dlss5nr_process_frame_v11.argtypes[:4],
            ctypes.POINTER(ctypes.c_uint64), ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_process_cache_v11.restype = ctypes.c_int
        library.dlss5nr_finish_cache_v11.argtypes = [ctypes.c_uint64]
        library.dlss5nr_finish_cache_v11.restype = ctypes.c_int
        library.dlss5nr_temporal_status.argtypes = [ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_temporal_status.restype = ctypes.c_int
        library.dlss5nr_scene_score_v1.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.c_float,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_scene_score_v1.restype = ctypes.c_int
        library.dlss5nr_surface_create.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_surface_create.restype = ctypes.c_void_p
        library.dlss5nr_surface_frame_desc.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FrameDescriptorV1),
        ]
        library.dlss5nr_surface_frame_desc.restype = ctypes.c_int
        library.dlss5nr_surface_retain.argtypes = [ctypes.c_void_p]
        library.dlss5nr_surface_retain.restype = None
        library.dlss5nr_surface_release.argtypes = [ctypes.c_void_p]
        library.dlss5nr_surface_release.restype = None
        library.dlss5nr_release_session.argtypes = []
        library.dlss5nr_release_session.restype = ctypes.c_int
        self._library = library
        self._version = version

    def _guard_poison(self) -> None:
        if self._poisoned_reason:
            raise NeuralBridgePoisonedError(
                f"The Neural Rendering native session is poisoned: "
                f"{self._poisoned_reason}. Restart the application before rendering again."
            )

    def _check_native_failure(self, detail: str) -> None:
        if _bridge_failure_requires_restart(detail):
            self._poisoned_reason = detail
            self._guard_poison()

    def _call_with_watchdog(
        self, label: str, function: Callable[[], Any], references: tuple[Any, ...] = (),
        *, timeout_seconds: float = BRIDGE_WATCHDOG_SECONDS,
    ) -> Any:
        self._guard_poison()
        completed = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        with self._worker_lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._run_calls, name="dlssnr-native", daemon=True)
                self._worker.start()
        self._calls.put((function, references, completed, result, failure))
        if not completed.wait(timeout_seconds):
            self._poisoned_reason = f"{label} exceeded {timeout_seconds:g} seconds"
            # Native code may still be touching these buffers. Keep them alive
            # until process exit instead of risking use-after-free.
            # The running request owns its arguments even after this caller
            # leaves. Pending GPU allocations remain protected by the poison
            # guards until application restart.
            self._timed_out_references.extend(references)
            raise NeuralBridgePoisonedError(
                f"Neural Rendering timed out during {label}; native state may be "
                "corrupted. Restart the application before rendering again."
            )
        if failure:
            self._poisoned_reason = f"native exception during {label}: {failure[0]}"
            raise NeuralBridgePoisonedError(
                f"Neural Rendering raised a native exception during {label}. Restart "
                f"the application before rendering again: {failure[0]}"
            ) from failure[0]
        return result[0]

    def initialize(self, gpu: dict[str, Any]) -> dict[str, Any]:
        self._guard_poison()
        ordinal = int(gpu.get("cuda_ordinal", gpu.get("index", 0)))
        with self._lock:
            self._guard_poison()
            self._load()
            if self._initialized_ordinal is None:
                assert self._library is not None
                error = ctypes.create_string_buffer(4096)
                ok = self._call_with_watchdog(
                    "initialization",
                    lambda: self._library.dlss5nr_init(
                        ordinal, str(DLSSNR_DIR), error, len(error)
                    ),
                    (error,),
                )
                if not ok:
                    detail = _text(error.value) or "unknown initialization failure"
                    raise NeuralBridgeError(f"Feature-18 bridge initialization failed: {detail}")
                self._initialized_ordinal = ordinal
                self._gpu_name = _text(self._library.dlss5nr_gpu_name()) or "unknown"
            elif ordinal != self._initialized_ordinal:
                if self._active_sessions:
                    raise NeuralBridgeError(
                        "The Neural Rendering adapter cannot change while a render is active."
                    )
                with DLPACK_LOCK:
                    outstanding_surfaces = len(LIVE_DLPACK_RECORDS)
                if outstanding_surfaces:
                    raise NeuralBridgeError(
                        "The Neural Rendering adapter cannot change while encoded CUDA "
                        "surfaces are still referenced. Wait for the current output to close."
                    )
                if self._cuda_driver is not None:
                    try:
                        self._cuda_driver.close()
                    except Exception as exc:
                        self._poisoned_reason = f"CUDA context release failed before adapter rebinding: {exc}"
                        self._cuda_driver = None
                        self._guard_poison()
                    self._cuda_driver = None
                assert self._library is not None
                error = ctypes.create_string_buffer(4096)
                ok = self._call_with_watchdog(
                    "adapter rebinding",
                    lambda: self._library.dlss5nr_rebind(ordinal, error, len(error)),
                    (error,),
                )
                if not ok:
                    detail = _text(error.value) or "unknown adapter rebinding failure"
                    self._poisoned_reason = detail
                    raise NeuralBridgePoisonedError(
                        f"Neural Rendering could not safely rebind to CUDA device {ordinal}: "
                        f"{detail}. Restart the application before rendering again."
                    )
                self._initialized_ordinal = ordinal
                self._gpu_name = _text(self._library.dlss5nr_gpu_name()) or "unknown"

            cuda_status = ctypes.create_string_buffer(2048)
            cuda_ready = bool(
                self._library.dlss5nr_cuda_status(cuda_status, len(cuda_status))
            )
            cuda_detail = _text(cuda_status.value) or "unavailable"
            if not cuda_ready:
                raise NeuralBridgeError(
                    f"CUDA/D3D12 interoperability is unavailable ({cuda_detail}). "
                    "Neural Rendering requires a supported RTX GPU and driver."
                )
            if self._cuda_driver is None:
                try:
                    self._cuda_driver = _CudaDriver(ordinal)
                    # Do not leave the primary context current on the UI/decoder
                    # thread. FFmpeg creates its CUDA hwdevice with this thread.
                    self._cuda_driver.deactivate()
                except Exception as exc:
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 interoperability setup failed: {exc}"
                    ) from exc
            return {
                "bridge_version": self._version,
                "bridge_abi_version": BRIDGE_ABI_VERSION,
                "bridge_frame_abi_max_version": BRIDGE_FRAME_ABI_MAX_VERSION,
                "gpu_name": self._gpu_name,
                "adapter_luid": _text(self._library.dlss5nr_adapter_luid()) or "unknown",
                "cuda_ordinal": ordinal,
                "cuda_supported": cuda_ready,
                "cuda_status": cuda_detail,
            }

    def open_session(self) -> None:
        with self._lock:
            self._guard_poison()
            self._active_sessions += 1

    @contextlib.contextmanager
    def image_batch(self):
        """Keep the current feature/resources between images in one queue run.

        Native EnsureFeature still rebuilds for a new size/style; every image
        evaluates with reset=True. Only feature-owned resources are retained,
        and the outermost batch releases them on success, cancellation or error.
        Logical sessions continue to free their own CUDA buffers/diagnostics.
        """
        with self._lock:
            self._guard_poison()
            self._image_batches += 1
        try:
            yield
        finally:
            with self._lock:
                self._image_batches -= 1
                self._release_idle_session()

    def close_session(self, *, discard: bool = False) -> None:
        with self._lock:
            if self._active_sessions:
                self._active_sessions -= 1
            self._pending_session_release = True
            self._release_idle_session(discard=discard)

    def _release_idle_session(self, *, discard: bool = False) -> None:
        # Call under the shared NGX lock. Failed sessions must discard feature
        # state immediately so an ordinary recoverable error cannot be reused.
        if (self._active_sessions or not self._pending_session_release
                or (self._image_batches and not discard)
                or self._library is None or self._poisoned_reason):
            return
        if not self._call_with_watchdog("session release", self._library.dlss5nr_release_session):
            self._poisoned_reason = "GPU completion was not confirmed during session release"
            self._guard_poison()
        self._pending_session_release = False

    def create_cuda_buffers(self, width: int, height: int) -> CudaFrameBuffers:
        with self._lock:
            self._guard_poison()
            if self._cuda_driver is None:
                raise NeuralBridgeError(
                    "CUDA/D3D12 interoperability is not initialized."
                )
            try:
                return CudaFrameBuffers.create(self._cuda_driver, width, height)
            finally:
                self._cuda_driver.deactivate()

    def create_cuda_mask(self, mask: np.ndarray) -> CudaMaskBuffer:
        with self._lock:
            self._guard_poison()
            if self._cuda_driver is None:
                raise NeuralBridgeError("CUDA mask upload requires GPU mode.")
            try:
                return CudaMaskBuffer.create(self._cuda_driver, mask)
            finally:
                self._cuda_driver.deactivate()

    def create_video_surface(
        self, width: int, height: int, pixel_format: int, *, timeout_seconds: float = BRIDGE_WATCHDOG_SECONDS
    ) -> BridgeCudaSurface:
        """Acquire a pooled CUDA surface without replacing a retained frame."""
        with self._lock:
            self._guard_poison()
            if self._library is None or self._cuda_driver is None:
                raise NeuralBridgeError(
                    "CUDA/D3D12 interoperability is not initialized."
                )
            if pixel_format not in (FORMAT_NV12, FORMAT_P010):
                raise NeuralBridgeError("CUDA video output must be NV12 or P010.")
            error = ctypes.create_string_buffer(4096)
            deadline = time.monotonic() + timeout_seconds
            while True:
                self._surface_ready.clear()
                handle = self._call_with_watchdog(
                    "CUDA output allocation",
                    lambda: self._library.dlss5nr_surface_create(
                        int(width), int(height), int(pixel_format), error, len(error)
                    ), (error,),
                )
                if handle or "pool is exhausted" not in _text(error.value):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NeuralBridgeError("Neural Rendering CUDA output pool remained exhausted.")
                self._surface_ready.wait(min(.01, remaining))
            if not handle:
                detail = _text(error.value) or "unknown CUDA output allocation failure"
                self._check_native_failure(detail)
                raise NeuralBridgeError(
                    f"CUDA/D3D12 output allocation failed: {detail}."
                )
            descriptor = FrameDescriptorV1.empty()
            if not self._library.dlss5nr_surface_frame_desc(
                ctypes.c_void_p(handle), ctypes.byref(descriptor)
            ):
                self._library.dlss5nr_surface_release(ctypes.c_void_p(handle))
                raise NeuralBridgeError("The bridge returned an invalid CUDA output surface.")
            return BridgeCudaSurface(
                self._library, handle, descriptor, self._cuda_driver.ordinal, self._surface_ready
            )

    @staticmethod
    def _render_parameters(
        settings: dict[str, int | float | bool], reset: bool,
        mask: np.ndarray | None = None,
        cuda_mask: CudaMaskBuffer | None = None,
    ) -> RenderParametersV11:
        value = RenderParametersV11()
        value.struct_size = ctypes.sizeof(RenderParametersV11)
        value.abi_version = BRIDGE_ABI_VERSION
        value.motion_mode = int(settings.get("motion_mode", 1))
        value.optical_flow_quality = int(settings.get("optical_flow_quality", 0))
        value.style = int(settings["style"])
        value.intensity = float(settings["intensity"])
        value.tone = float(settings["local_tone"])
        value.structure = float(settings["local_structure"])
        value.skin = float(settings["skin_structure"])
        value.automask = int(bool(settings["auto_mask"]))
        value.reset = int(bool(reset))
        value.nr_passes = int(settings.get("nr_passes", 1))
        value.native_intensity = float(settings.get("native_intensity", min(1.0, value.intensity)))
        if mask is not None and (mask.dtype != np.float32 or mask.ndim != 3 or
                mask.shape[2] != 4 or not mask.flags.c_contiguous or not np.isfinite(mask).all()):
            raise NeuralBridgeError("Native ControlMask must be contiguous finite float32 RGBA.")
        value.mask_revision = cuda_mask.revision if cuda_mask is not None else next(_MASK_REVISIONS)
        value.mask_memory_type = MEMORY_NONE
        if cuda_mask is not None:
            value.mask_memory_type = MEMORY_CUDA
            value.mask_width = int(mask.shape[1]) if mask is not None else 0
            value.mask_height = int(mask.shape[0]) if mask is not None else 0
            value.mask_stride = int(mask.strides[0]) if mask is not None else 0
            value.mask_plane = int(cuda_mask.pointer)
        elif mask is not None:
            value.mask_memory_type = MEMORY_HOST
            value.mask_width = int(mask.shape[1])
            value.mask_height = int(mask.shape[0])
            value.mask_stride = int(mask.strides[0])
            value.mask_plane = int(mask.ctypes.data)
        return value

    def process_cuda_video_frame(
        self,
        frame: Any,
        *,
        output_width: int,
        output_height: int,
        output_format: int,
        settings: dict[str, int | float | bool],
        mask: np.ndarray | None,
        cuda_mask: CudaMaskBuffer | None,
        reset: bool,
        timestamp: int,
        color_matrix: int = 1,
        color_range: int = 0,
        rotation: int = 0,
        chroma_location: int = 1,
    ) -> tuple[Any, dict[str, Any], float]:
        """Evaluate a PyAV CUDA frame and return a CUDA AVFrame for NVENC.

        Both the decoded input planes and returned output planes remain on the
        selected CUDA device. The returned AVFrame owns the native allocation
        through the DLPack deleters until the encoder releases it.
        """
        with self._lock:
            self._guard_poison()
            if self._library is None or self._cuda_driver is None:
                raise NeuralBridgeError(
                    "CUDA/D3D12 interoperability is not initialized."
                )
            format_name = str(getattr(getattr(frame, "format", None), "name", ""))
            if format_name != "cuda":
                raise NeuralBridgeError(
                    f"Expected a CUDA-decoded PyAV frame, received {format_name or 'unknown'}. "
                    "This source must use the reported software-decode boundary."
                )
            sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
            source_format = {
                "nv12": FORMAT_NV12,
                "p010": FORMAT_P010,
                "p010le": FORMAT_P010,
            }.get(sw_format)
            if source_format is None:
                raise NeuralBridgeError(
                    f"CUDA decode format {sw_format or 'unknown'} is outside the supported "
                    "NV12/P010 Neural Rendering boundary."
                )
            if len(frame.planes) < 2:
                raise NeuralBridgeError("The CUDA video frame does not expose two YUV planes.")

            source = FrameDescriptorV1.empty()
            source.memory_type = MEMORY_CUDA
            source.pixel_format = source_format
            source.width = int(frame.width)
            source.height = int(frame.height)
            source.planes[0] = int(frame.planes[0].buffer_ptr)
            source.planes[1] = int(frame.planes[1].buffer_ptr)
            source.strides[0] = int(frame.planes[0].line_size)
            source.strides[1] = int(frame.planes[1].line_size)
            source.color_matrix = int(color_matrix)
            source.color_range = int(color_range)
            source.rotation = int(rotation) % 360
            source.reserved = int(chroma_location)
            source.timestamp = int(timestamp)

            surface = self.create_video_surface(
                output_width, output_height, output_format
            )
            destination = surface.descriptor
            destination.color_matrix = int(color_matrix)
            destination.color_range = int(color_range)
            destination.timestamp = int(timestamp)
            params = self._render_parameters(settings, reset, mask, cuda_mask)
            result = FrameResultV1.empty()
            error = ctypes.create_string_buffer(4096)
            started = time.perf_counter()
            try:
                ok = self._call_with_watchdog(
                    "feature-18 CUDA video evaluation",
                    lambda: self._library.dlss5nr_process_frame_v11(
                        ctypes.byref(source),
                        ctypes.byref(destination),
                        ctypes.byref(params),
                        ctypes.byref(result),
                        error,
                        len(error),
                    ),
                    (frame, surface, source, destination, params, result, error),
                    timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
                )
                elapsed = time.perf_counter() - started
                if not ok:
                    detail = _text(error.value) or "unknown CUDA frame-ABI failure"
                    if _bridge_failure_requires_restart(detail):
                        self._poisoned_reason = detail
                        raise NeuralBridgePoisonedError(
                            f"{detail}. Restart the application before rendering again."
                        )
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 Neural Rendering failed: {detail}."
                    )
                # AVFrame.from_dlpack establishes its own primary-context
                # references; do not leak the bridge's current context into
                # PyAV's encoder setup on this thread.
                self._cuda_driver.deactivate()
                output = surface.to_av_frame()
            except BaseException:
                with contextlib.suppress(Exception):
                    self._cuda_driver.deactivate()
                surface.close()
                raise

            output.pts = getattr(frame, "pts", None)
            output.time_base = getattr(frame, "time_base", None)
            if getattr(frame, "duration", None) is not None:
                output.duration = frame.duration
            details = {
                "ngx_create_result": f"0x{int(result.ngx_create_result) & 0xFFFFFFFF:08X}",
                "ngx_evaluate_result": f"0x{int(result.ngx_evaluate_result) & 0xFFFFFFFF:08X}",
                "cuda_result": int(result.cuda_result),
                "scene_reset": bool(result.scene_reset),
                "scene_score": float(result.scene_score),
                "upload_bytes": int(result.upload_bytes),
                "download_bytes": int(result.download_bytes),
                "timestamp": int(result.timestamp),
                "input_format": sw_format,
                "output_format": "p010le" if output_format == FORMAT_P010 else "nv12",
            }
            return output, details, elapsed

    def score_cuda_video_frame(
        self,
        frame: Any,
        *,
        threshold: float = 0.24,
        color_matrix: int = 1,
        color_range: int = 0,
    ) -> tuple[float, bool]:
        """Score a reduced luma signature while the decoded frame stays on CUDA."""
        with self._lock:
            self._guard_poison()
            if self._library is None or self._cuda_driver is None:
                raise NeuralBridgeError("CUDA scene scoring requires GPU mode.")
            format_name = str(getattr(getattr(frame, "format", None), "name", ""))
            sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
            source_format = {
                "nv12": FORMAT_NV12,
                "p010": FORMAT_P010,
                "p010le": FORMAT_P010,
            }.get(sw_format)
            if format_name != "cuda" or source_format is None or len(frame.planes) < 2:
                raise NeuralBridgeError(
                    f"CUDA scene scoring supports NV12/P010, received "
                    f"{format_name or 'unknown'}/{sw_format or 'unknown'}."
                )
            source = FrameDescriptorV1.empty()
            source.memory_type = MEMORY_CUDA
            source.pixel_format = source_format
            source.width = int(frame.width)
            source.height = int(frame.height)
            source.planes[0] = int(frame.planes[0].buffer_ptr)
            source.planes[1] = int(frame.planes[1].buffer_ptr)
            source.strides[0] = int(frame.planes[0].line_size)
            source.strides[1] = int(frame.planes[1].line_size)
            source.color_matrix = int(color_matrix)
            source.color_range = int(color_range)
            source.timestamp = int(getattr(frame, "pts", None) or 0)
            score = ctypes.c_float()
            reset = ctypes.c_int()
            error = ctypes.create_string_buffer(4096)
            ok = self._call_with_watchdog(
                "CUDA reduced-luma scene score",
                lambda: self._library.dlss5nr_scene_score_v1(
                    ctypes.byref(source),
                    float(threshold),
                    ctypes.byref(score),
                    ctypes.byref(reset),
                    error,
                    len(error),
                ),
                (frame, source, score, reset, error),
            )
            self._cuda_driver.deactivate()
            if not ok:
                detail = _text(error.value) or "unknown CUDA scene-scoring failure"
                self._check_native_failure(detail)
                raise NeuralBridgeError(
                    f"CUDA reduced-luma scene scoring failed: {detail}."
                )
            return float(score.value), bool(reset.value)

    def process_host_to_cuda_video_frame(
        self,
        rgba: np.ndarray,
        *,
        output_format: int,
        settings: dict[str, int | float | bool],
        mask: np.ndarray | None,
        cuda_mask: CudaMaskBuffer | None,
        reset: bool,
        timestamp: int,
        color_matrix: int = 1,
        color_range: int = 0,
        time_base: Any | None = None,
        duration: int | None = None,
    ) -> tuple[Any, dict[str, Any], float]:
        """Upload one software-decoded RGBA frame and return CUDA YUV."""
        with self._lock:
            self._guard_poison()
            if self._library is None or self._cuda_driver is None:
                raise NeuralBridgeError("Host-to-CUDA video processing requires GPU mode.")
            if (
                rgba.dtype not in (np.uint8, np.uint16)
                or rgba.ndim != 3
                or rgba.shape[2] != 4
                or not rgba.flags.c_contiguous
            ):
                raise NeuralBridgeError("Host video input must be contiguous RGBA8 or RGBA16LE.")
            high_depth = rgba.dtype == np.uint16
            height, width = rgba.shape[:2]
            source = FrameDescriptorV1.empty()
            source.memory_type = MEMORY_HOST
            source.pixel_format = FORMAT_RGBA16LE if high_depth else FORMAT_RGBA8
            source.width = int(width)
            source.height = int(height)
            source.planes[0] = int(rgba.ctypes.data)
            source.strides[0] = int(rgba.strides[0])
            source.color_matrix = int(color_matrix)
            source.color_range = int(color_range)
            source.timestamp = int(timestamp)

            surface = self.create_video_surface(width, height, output_format)
            destination = FrameDescriptorV1.from_buffer_copy(surface.descriptor)
            destination.color_matrix = int(color_matrix)
            destination.color_range = int(color_range)
            destination.timestamp = int(timestamp)
            params = self._render_parameters(settings, reset, mask, cuda_mask)
            result = FrameResultV1.empty()
            error = ctypes.create_string_buffer(4096)
            started = time.perf_counter()
            try:
                ok = self._call_with_watchdog(
                    "feature-18 host-to-CUDA video evaluation",
                    lambda: self._library.dlss5nr_process_frame_v11(
                        ctypes.byref(source), ctypes.byref(destination),
                        ctypes.byref(params), ctypes.byref(result), error, len(error),
                    ),
                    (rgba, surface, source, destination, params, result, error),
                    timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
                )
                elapsed = time.perf_counter() - started
                self._cuda_driver.deactivate()
                if not ok:
                    detail = _text(error.value) or "unknown host-to-CUDA frame failure"
                    self._check_native_failure(detail)
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 Neural Rendering failed: {detail}."
                    )
                output = surface.to_av_frame()
            except BaseException:
                with contextlib.suppress(Exception):
                    self._cuda_driver.deactivate()
                surface.close()
                raise
            output.pts = int(timestamp)
            if time_base is not None:
                output.time_base = time_base
            if duration is not None:
                output.duration = int(duration)
            details = {
                "ngx_create_result": f"0x{int(result.ngx_create_result) & 0xFFFFFFFF:08X}",
                "ngx_evaluate_result": f"0x{int(result.ngx_evaluate_result) & 0xFFFFFFFF:08X}",
                "cuda_result": int(result.cuda_result),
                "scene_reset": bool(result.scene_reset),
                "scene_score": float(result.scene_score),
                "upload_bytes": int(result.upload_bytes),
                "download_bytes": int(result.download_bytes),
                "timestamp": int(result.timestamp),
                "input_format": "rgba16le" if high_depth else "rgba8",
                "output_format": "p010le" if output_format == FORMAT_P010 else "nv12",
            }
            return output, details, elapsed

    def process_video_to_host_frame(
        self,
        frame: Any,
        destination: np.ndarray,
        *,
        settings: dict[str, int | float | bool],
        mask: np.ndarray | None,
        cuda_mask: CudaMaskBuffer | None,
        reset: bool,
        timestamp: int,
        color_matrix: int = 1,
        color_range: int = 0,
        rotation: int = 0,
        chroma_location: int = 1,
        async_transfer: bool = False,
    ) -> tuple[dict[str, Any], float]:
        """Evaluate CUDA NV12/P010 or host RGBA and return packed RGBA."""
        with self._lock:
            self._guard_poison()
            host_input = isinstance(frame, np.ndarray)
            format_name = str(getattr(getattr(frame, "format", None), "name", ""))
            sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
            source_format = {
                "nv12": FORMAT_NV12,
                "p010": FORMAT_P010,
                "p010le": FORMAT_P010,
            }.get(sw_format)
            if host_input:
                if (frame.dtype not in (np.uint8, np.uint16) or frame.ndim != 3
                        or frame.shape[2] != 4 or not frame.flags.c_contiguous):
                    raise NeuralBridgeError("Software video input must be contiguous RGBA8 or RGBA16LE.")
                source_format = FORMAT_RGBA16LE if frame.dtype == np.uint16 else FORMAT_RGBA8
                sw_format = "rgba16le" if frame.dtype == np.uint16 else "rgba8"
            elif format_name != "cuda" or source_format is None or len(frame.planes) < 2:
                raise NeuralBridgeError(
                    f"CUDA-to-host processing supports NV12/P010, received "
                    f"{format_name or 'unknown'}/{sw_format or 'unknown'}."
                )
            if (
                destination.dtype not in (np.uint8, np.uint16)
                or destination.ndim != 3
                or destination.shape[2] != 4
                or not destination.flags.c_contiguous
            ):
                raise NeuralBridgeError("CUDA-to-host output must be contiguous RGBA8 or RGBA16LE.")
            output_16bit = destination.dtype == np.uint16
            high_depth = async_transfer or output_16bit or (host_input and frame.dtype == np.uint16)
            source = FrameDescriptorV1.empty()
            source.memory_type = MEMORY_HOST if host_input else MEMORY_CUDA
            source.pixel_format = source_format
            if host_input:
                source.width, source.height = int(frame.shape[1]), int(frame.shape[0])
                source.planes[0] = int(frame.ctypes.data)
                source.strides[0] = int(frame.strides[0])
            else:
                source.width, source.height = int(frame.width), int(frame.height)
                source.planes[0] = int(frame.planes[0].buffer_ptr)
                source.planes[1] = int(frame.planes[1].buffer_ptr)
                source.strides[0] = int(frame.planes[0].line_size)
                source.strides[1] = int(frame.planes[1].line_size)
            source.color_matrix = int(color_matrix)
            source.color_range = int(color_range)
            source.rotation = int(rotation) % 360
            source.reserved = int(chroma_location)
            source.timestamp = int(timestamp)
            output = FrameDescriptorV1.empty()
            output.memory_type = MEMORY_HOST
            output.pixel_format = FORMAT_RGBA16LE if output_16bit else FORMAT_RGBA8
            output.width = int(destination.shape[1])
            output.height = int(destination.shape[0])
            output.planes[0] = int(destination.ctypes.data)
            output.strides[0] = int(destination.strides[0])
            output.color_matrix = int(color_matrix)
            output.color_range = int(color_range)
            output.timestamp = int(timestamp)
            params = self._render_parameters(settings, reset, mask, cuda_mask)
            result = FrameResultV1.empty()
            error = ctypes.create_string_buffer(4096)
            token = ctypes.c_uint64()
            started = time.perf_counter()
            def evaluate():
                if async_transfer:
                    return self._library.dlss5nr_process_cache_v11(
                        ctypes.byref(source), ctypes.byref(output), ctypes.byref(params),
                        ctypes.byref(result), ctypes.byref(token), error, len(error))
                return self._library.dlss5nr_process_frame_v11(
                    ctypes.byref(source), ctypes.byref(output), ctypes.byref(params),
                    ctypes.byref(result), error, len(error))
            ok = self._call_with_watchdog(
                "feature-18 video-to-host evaluation",
                evaluate,
                (frame, destination, source, output, params, result, error),
                timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
            )
            elapsed = time.perf_counter() - started
            self._cuda_driver.deactivate()
            if not ok:
                detail = _text(error.value) or "unknown CUDA-to-host frame failure"
                self._check_native_failure(detail)
                raise NeuralBridgeError(
                    f"CUDA/D3D12 Neural Rendering failed: {detail}."
                )
            return {
                "ngx_create_result": f"0x{int(result.ngx_create_result) & 0xFFFFFFFF:08X}",
                "ngx_evaluate_result": f"0x{int(result.ngx_evaluate_result) & 0xFFFFFFFF:08X}",
                "cuda_result": int(result.cuda_result),
                "scene_reset": bool(result.scene_reset),
                "scene_score": float(result.scene_score),
                "upload_bytes": int(result.upload_bytes),
                "download_bytes": int(result.download_bytes),
                "timestamp": int(result.timestamp),
                "input_format": sw_format,
                "output_format": "rgba16le" if output_16bit else "rgba8",
                "cache_token": int(token.value),
            }, elapsed

    def finish_cache_transfer(self, token: int) -> None:
        # Independent transfer stream; do not take the model/watchdog lock.
        # The stage owns the mapping and joins its bounded copy queue first.
        if not self._library.dlss5nr_finish_cache_v11(int(token)):
            raise NeuralBridgeError("The asynchronous cache transfer failed.")

    # Preserve the existing CUDA boundary for preview and external callers.
    process_cuda_to_host_video_frame = process_video_to_host_frame

    def process_cuda(
        self,
        source: np.ndarray,
        destination: np.ndarray,
        buffers: CudaFrameBuffers,
        settings: dict[str, int | float | bool],
        reset: bool,
        mask: np.ndarray | None = None,
        cuda_mask: CudaMaskBuffer | None = None,
    ) -> tuple[float, float, float]:
        with self._lock:
            self._guard_poison()
            assert self._library is not None and self._cuda_driver is not None
            error = ctypes.create_string_buffer(4096)
            upload_started = time.perf_counter()
            buffers.driver.upload(buffers.input_pointer, source)
            upload_seconds = time.perf_counter() - upload_started
            evaluate_started = time.perf_counter()
            params = self._render_parameters(settings, reset, mask, cuda_mask)
            ok = self._call_with_watchdog(
                "feature-18 CUDA evaluation",
                lambda: self._library.dlss5nr_process_cuda_v11(
                    buffers.input_pointer,
                    buffers.output_pointer,
                    source.shape[1],
                    source.shape[0],
                    0,
                    ctypes.byref(params),
                    error,
                    len(error),
                ),
                (source, destination, buffers, mask, cuda_mask, params, error),
                timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
            )
            evaluate_seconds = time.perf_counter() - evaluate_started
            if not ok:
                detail = _text(error.value) or "unknown CUDA interoperability failure"
                if _bridge_failure_requires_restart(detail):
                    self._poisoned_reason = detail
                    raise NeuralBridgePoisonedError(
                        f"{detail}. Restart the application before rendering again."
                    )
                raise NeuralBridgeError(
                    f"CUDA/D3D12 Neural Rendering failed: {detail}."
                )
            download_started = time.perf_counter()
            buffers.driver.synchronize()
            buffers.driver.download(destination, buffers.output_pointer)
            download_seconds = time.perf_counter() - download_started
            return upload_seconds, evaluate_seconds, download_seconds

BRIDGE_MANAGER = NeuralBridgeManager()
