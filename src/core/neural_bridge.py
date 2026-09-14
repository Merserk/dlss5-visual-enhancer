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
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .paths import DLSSNR_BRIDGE, DLSSNR_DIR
from .ngx_runtime import NGX_RUNTIME_LOCK


BRIDGE_ABI_VERSION = 6
BRIDGE_WATCHDOG_SECONDS = 45.0
MEMORY_HOST = 0
MEMORY_CUDA = 1
MEMORY_NONE = 2
FORMAT_RGBA8 = 1
FORMAT_NV12 = 2
FORMAT_P010 = 3


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


class RenderParametersV1(ctypes.Structure):
    """Legacy ABI-v2 render controls retained for old frame entry points."""

    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("style", ctypes.c_int32),
        ("intensity", ctypes.c_float),
        ("tone", ctypes.c_float),
        ("structure", ctypes.c_float),
        ("skin", ctypes.c_float),
        ("automask", ctypes.c_int32),
        ("reset", ctypes.c_int32),
    ]


class RenderParametersV3(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("style", ctypes.c_int32),
        ("intensity", ctypes.c_float),
        ("tone", ctypes.c_float),
        ("structure", ctypes.c_float),
        ("skin", ctypes.c_float),
        ("automask", ctypes.c_int32),
        ("reset", ctypes.c_int32),
        ("color_strength", ctypes.c_float),
        ("tone_preservation", ctypes.c_float),
        ("mask_memory_type", ctypes.c_uint32),
        ("mask_width", ctypes.c_uint32),
        ("mask_height", ctypes.c_uint32),
        ("mask_stride", ctypes.c_uint32),
        ("mask_plane", ctypes.c_uint64),
    ]


class RenderParametersV4(ctypes.Structure):
    """ABI-v4 controls; the ABI-v3 prefix is deliberately unchanged."""

    _fields_ = [
        *RenderParametersV3._fields_,
        ("face_skin_protection", ctypes.c_float),
        ("grain_preservation", ctypes.c_float),
    ]


class RenderParametersV5(ctypes.Structure):
    """ABI-v5 controls; the ABI-v4 prefix is deliberately unchanged."""

    _fields_ = [
        *RenderParametersV4._fields_,
        ("nr_passes", ctypes.c_int32),
    ]


class RenderParametersV6(ctypes.Structure):
    """ABI-v6 controls; v5 callers retain suppression-disabled behavior."""

    _fields_ = [
        *RenderParametersV5._fields_,
        ("shimmer_suppression", ctypes.c_float),
        ("prefer_nvof", ctypes.c_int32),
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


class _DLDevice(ctypes.Structure):
    _fields_ = [("device_type", ctypes.c_int), ("device_id", ctypes.c_int)]


class _DLDataType(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint8), ("bits", ctypes.c_uint8), ("lanes", ctypes.c_uint16)]


class _DLTensor(ctypes.Structure):
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("device", _DLDevice),
        ("ndim", ctypes.c_int),
        ("dtype", _DLDataType),
        ("shape", ctypes.POINTER(ctypes.c_int64)),
        ("strides", ctypes.POINTER(ctypes.c_int64)),
        ("byte_offset", ctypes.c_uint64),
    ]


class _DLManagedTensor(ctypes.Structure):
    pass


_DLPACK_RECORDS: dict[int, tuple[Any, ...]] = {}
_DLPACK_LOCK = threading.Lock()
_DL_DELETER = ctypes.CFUNCTYPE(None, ctypes.POINTER(_DLManagedTensor))


@_DL_DELETER
def _dlpack_deleter(pointer: ctypes.POINTER(_DLManagedTensor)) -> None:
    address = ctypes.addressof(pointer.contents)
    with _DLPACK_LOCK:
        record = _DLPACK_RECORDS.pop(address, None)
    if record is not None:
        release = record[-1]
        try:
            release()
        except Exception:
            pass


_DLManagedTensor._fields_ = [
    ("dl_tensor", _DLTensor),
    ("manager_ctx", ctypes.c_void_p),
    ("deleter", _DL_DELETER),
]


_PYCAPSULE_DESTRUCTOR = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
ctypes.pythonapi.PyCapsule_New.argtypes = [
    ctypes.c_void_p,
    ctypes.c_char_p,
    _PYCAPSULE_DESTRUCTOR,
]
ctypes.pythonapi.PyCapsule_New.restype = ctypes.py_object
ctypes.pythonapi.PyCapsule_IsValid.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
ctypes.pythonapi.PyCapsule_IsValid.restype = ctypes.c_int
ctypes.pythonapi.PyCapsule_GetPointer.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
ctypes.pythonapi.PyCapsule_GetPointer.restype = ctypes.c_void_p


@_PYCAPSULE_DESTRUCTOR
def _capsule_destructor(capsule: int) -> None:
    try:
        if ctypes.pythonapi.PyCapsule_IsValid(capsule, b"dltensor"):
            address = ctypes.pythonapi.PyCapsule_GetPointer(capsule, b"dltensor")
            if address:
                _dlpack_deleter(ctypes.cast(address, ctypes.POINTER(_DLManagedTensor)))
    except Exception:
        pass


class _DLPackPlane:
    def __init__(
        self,
        *,
        pointer: int,
        shape: tuple[int, ...],
        strides: tuple[int, ...],
        bits: int,
        device_id: int,
        retain: Callable[[], None],
        release: Callable[[], None],
    ) -> None:
        self.pointer = int(pointer)
        self.shape = shape
        self.strides = strides
        self.bits = bits
        self.device_id = device_id
        self.retain = retain
        self.release = release

    def __dlpack_device__(self) -> tuple[int, int]:
        return (2, self.device_id)  # kDLCUDA

    def __dlpack__(self, stream=None, **_kwargs):
        del stream
        shape = (ctypes.c_int64 * len(self.shape))(*self.shape)
        strides = (ctypes.c_int64 * len(self.strides))(*self.strides)
        managed = _DLManagedTensor()
        managed.dl_tensor.data = ctypes.c_void_p(self.pointer)
        managed.dl_tensor.device = _DLDevice(2, self.device_id)
        managed.dl_tensor.ndim = len(self.shape)
        managed.dl_tensor.dtype = _DLDataType(1, self.bits, 1)  # kDLUInt
        managed.dl_tensor.shape = ctypes.cast(shape, ctypes.POINTER(ctypes.c_int64))
        managed.dl_tensor.strides = ctypes.cast(strides, ctypes.POINTER(ctypes.c_int64))
        managed.dl_tensor.byte_offset = 0
        managed.manager_ctx = None
        managed.deleter = _dlpack_deleter
        self.retain()
        address = ctypes.addressof(managed)
        with _DLPACK_LOCK:
            _DLPACK_RECORDS[address] = (managed, shape, strides, self, self.release)
        return ctypes.pythonapi.PyCapsule_New(address, b"dltensor", _capsule_destructor)


class NeuralBridgeError(RuntimeError):
    """An actionable failure returned by the native Neural Rendering bridge."""


class NeuralBridgePoisonedError(NeuralBridgeError):
    """The in-process native state cannot safely be reused before app restart."""


def _text(value: bytes | None) -> str:
    return value.decode("utf-8", "replace") if value else ""


def _json_line(event: str, **values: Any) -> str:
    return json.dumps(
        {"event": event, "monotonic_seconds": time.monotonic(), **values},
        sort_keys=True,
        separators=(",", ":"),
    )


class _CudaDriver:
    """Small CUDA Driver API wrapper; no CUDA toolkit or Python add-on needed."""

    def __init__(self, ordinal: int) -> None:
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        try:
            self.lib = loader("nvcuda.dll")
        except OSError as exc:
            raise NeuralBridgeError(
                "CUDA interoperability is unavailable because nvcuda.dll could not be "
                "loaded. Switch GPU OFF to use the bridge host-staging path."
            ) from exc

        self._bind("cuInit", [ctypes.c_uint])
        self._bind("cuDeviceGet", [ctypes.POINTER(ctypes.c_int), ctypes.c_int])
        self._bind(
            "cuDevicePrimaryCtxRetain",
            [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int],
        )
        self._bind_any(
            "primary_ctx_release",
            ("cuDevicePrimaryCtxRelease_v2", "cuDevicePrimaryCtxRelease"),
            [ctypes.c_int],
        )
        self._bind("cuCtxSetCurrent", [ctypes.c_void_p])
        self._bind("cuCtxSynchronize", [])
        self._bind_any(
            "mem_alloc", ("cuMemAlloc_v2", "cuMemAlloc"),
            [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t],
        )
        self._bind_any(
            "mem_free", ("cuMemFree_v2", "cuMemFree"), [ctypes.c_uint64]
        )
        self._bind_any(
            "copy_htod", ("cuMemcpyHtoD_v2", "cuMemcpyHtoD"),
            [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t],
        )
        self._bind_any(
            "copy_dtoh", ("cuMemcpyDtoH_v2", "cuMemcpyDtoH"),
            [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t],
        )
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

    def _bind_any(
        self, attribute: str, names: tuple[str, ...], argtypes: list[Any]
    ) -> Any:
        for name in names:
            function = getattr(self.lib, name, None)
            if function is not None:
                function.argtypes = argtypes
                function.restype = ctypes.c_int
                setattr(self, attribute, function)
                return function
        raise NeuralBridgeError(f"The NVIDIA driver does not export {names[0]}.")

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
        try:
            self.driver.synchronize()
            self.driver.free(self.output_pointer)
            self.driver.free(self.input_pointer)
            self.input_pointer = 0
            self.output_pointer = 0
            self.closed = True
        finally:
            self.driver.deactivate()


@dataclass(slots=True)
class CudaMaskBuffer:
    driver: _CudaDriver
    pointer: int
    byte_count: int
    closed: bool = False

    @classmethod
    def create(cls, driver: _CudaDriver, mask: np.ndarray) -> "CudaMaskBuffer":
        if mask.dtype != np.float32 or mask.ndim != 2 or not mask.flags.c_contiguous:
            raise NeuralBridgeError("Custom NR Mask must be contiguous float32 data.")
        pointer = driver.alloc(mask.nbytes)
        try:
            driver.upload(pointer, mask)
            driver.synchronize()
        except Exception:
            driver.free(pointer)
            raise
        return cls(driver, pointer, int(mask.nbytes))

    def close(self) -> None:
        if self.closed:
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
    ) -> None:
        self.library = library
        self.handle = int(handle)
        self.descriptor = descriptor
        self.ordinal = int(ordinal)
        self.closed = False

    def retain(self) -> None:
        self.library.dlss5nr_surface_retain(ctypes.c_void_p(self.handle))

    def release(self) -> None:
        self.library.dlss5nr_surface_release(ctypes.c_void_p(self.handle))

    def close(self) -> None:
        if not self.closed:
            self.release()
            self.closed = True

    def to_av_frame(self):
        import av

        desc = self.descriptor
        bits = 16 if desc.pixel_format == FORMAT_P010 else 8
        item_size = bits // 8
        y_plane = _DLPackPlane(
            pointer=int(desc.planes[0]),
            shape=(int(desc.height), int(desc.width)),
            strides=(int(desc.strides[0]) // item_size, 1),
            bits=bits,
            device_id=self.ordinal,
            retain=self.retain,
            release=self.release,
        )
        uv_plane = _DLPackPlane(
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
            "gpu_mode": self.gpu_mode,
            "memory_path": self.memory_path,
            "decode_backend": self.decode_backend,
            "encode_backend": self.encode_backend,
            "pixel_format": self.pixel_format,
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
        self._version = "unloaded"
        self._gpu_name = "unknown"

    @property
    def version(self) -> str:
        return self._version

    @property
    def gpu_name(self) -> str:
        return self._gpu_name

    def temporal_status(self) -> dict[str, Any]:
        with self._lock:
            if self._library is None:
                return {}
            buffer = ctypes.create_string_buffer(2048)
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
        library.dlss5nr_process.argtypes = [
            c_float_p,
            c_float_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process.restype = ctypes.c_int
        library.dlss5nr_process_cuda.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_cuda.restype = ctypes.c_int
        library.dlss5nr_process_v3.argtypes = [
            c_float_p,
            c_float_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(RenderParametersV3),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_v3.restype = ctypes.c_int
        library.dlss5nr_process_cuda_v3.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(RenderParametersV3),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_cuda_v3.restype = ctypes.c_int
        library.dlss5nr_process_v4.argtypes = [
            c_float_p,
            c_float_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(RenderParametersV4),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_v4.restype = ctypes.c_int
        library.dlss5nr_process_cuda_v4.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(RenderParametersV4),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_cuda_v4.restype = ctypes.c_int
        library.dlss5nr_process_v5.argtypes = [
            c_float_p,
            c_float_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(RenderParametersV5),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_v5.restype = ctypes.c_int
        library.dlss5nr_process_v6.argtypes = [
            c_float_p,
            c_float_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(RenderParametersV6),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_v6.restype = ctypes.c_int
        library.dlss5nr_process_cuda_v5.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(RenderParametersV5),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_cuda_v5.restype = ctypes.c_int
        library.dlss5nr_process_cuda_v6.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.POINTER(RenderParametersV6),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_cuda_v6.restype = ctypes.c_int
        library.dlss5nr_cuda_supported.argtypes = []
        library.dlss5nr_cuda_supported.restype = ctypes.c_int
        library.dlss5nr_cuda_status.argtypes = [ctypes.c_char_p, ctypes.c_int]
        library.dlss5nr_cuda_status.restype = ctypes.c_int
        library.dlss5nr_process_frame_v1.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV1),
            ctypes.POINTER(FrameResultV1),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_frame_v1.restype = ctypes.c_int
        library.dlss5nr_process_frame_v3.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV3),
            ctypes.POINTER(FrameResultV1),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_frame_v3.restype = ctypes.c_int
        library.dlss5nr_process_frame_v4.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV4),
            ctypes.POINTER(FrameResultV1),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_frame_v4.restype = ctypes.c_int
        library.dlss5nr_process_frame_v5.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV5),
            ctypes.POINTER(FrameResultV1),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_frame_v5.restype = ctypes.c_int
        library.dlss5nr_process_frame_v6.argtypes = [
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(RenderParametersV6),
            ctypes.POINTER(FrameResultV1),
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        library.dlss5nr_process_frame_v6.restype = ctypes.c_int
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
        release = getattr(library, "dlss5nr_release_session", None)
        if release is not None:
            release.argtypes = []
            release.restype = ctypes.c_int
        self._library = library
        self._version = version

    def _guard_poison(self) -> None:
        if self._poisoned_reason:
            raise NeuralBridgePoisonedError(
                f"The Neural Rendering native session is poisoned: "
                f"{self._poisoned_reason}. Restart the application before rendering again."
            )

    def _call_with_watchdog(
        self, label: str, function: Callable[[], Any], references: tuple[Any, ...] = (),
        *, timeout_seconds: float = BRIDGE_WATCHDOG_SECONDS,
    ) -> Any:
        completed = threading.Event()
        result: list[Any] = []
        failure: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(function())
            except BaseException as exc:
                failure.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(
            target=invoke, name=f"dlssnr-{label}", daemon=True
        )
        thread.start()
        if not completed.wait(timeout_seconds):
            self._poisoned_reason = f"{label} exceeded {timeout_seconds:g} seconds"
            # Native code may still be touching these buffers. Keep them alive
            # until process exit instead of risking use-after-free.
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

    def initialize(self, gpu: dict[str, Any], *, require_cuda: bool) -> dict[str, Any]:
        with self._lock:
            self._guard_poison()
            ordinal = int(gpu.get("cuda_ordinal", gpu.get("index", 0)))
            if self._initialized_ordinal is None:
                # The NVIDIA D3D12 NGX core keeps the first feature search path
                # for the lifetime of the process. If Neural Rendering starts
                # first, a later DLSSG initialization succeeds superficially
                # but reports Frame Generation as unavailable. Prime DLSSG on
                # the same adapter before Feature 18; failure remains isolated
                # so systems without the optional DLSSG runtime can still use
                # Neural Rendering.
                try:
                    from ..frame_interpolation.native import initialize_bridge

                    initialize_bridge(ordinal)
                except Exception:
                    pass
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
                with _DLPACK_LOCK:
                    outstanding_surfaces = len(_DLPACK_RECORDS)
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
            if require_cuda and not cuda_ready:
                raise NeuralBridgeError(
                    f"CUDA/D3D12 interoperability is unavailable ({cuda_detail}). "
                    "Switch GPU OFF to use host staging; Neural Rendering still requires "
                    "an RTX GPU."
                )
            if require_cuda and self._cuda_driver is None:
                try:
                    self._cuda_driver = _CudaDriver(ordinal)
                    # Do not leave the primary context current on the UI/decoder
                    # thread. FFmpeg creates its CUDA hwdevice with this thread.
                    self._cuda_driver.deactivate()
                except Exception as exc:
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 interoperability setup failed: {exc} Switch GPU OFF "
                        "to use host staging."
                    ) from exc
            return {
                "bridge_version": self._version,
                "bridge_abi_version": BRIDGE_ABI_VERSION,
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

    def close_session(self) -> None:
        with self._lock:
            if self._active_sessions:
                self._active_sessions -= 1
            # Release feature-owned surfaces at the idle boundary, but never
            # invoke NGX core shutdown or unload the driver/runtime modules.
            if self._active_sessions == 0 and self._library is not None:
                release = getattr(self._library, "dlss5nr_release_session", None)
                if release is not None and not self._poisoned_reason:
                    try:
                        self._call_with_watchdog("session release", release)
                    except NeuralBridgePoisonedError:
                        raise

    def create_cuda_buffers(self, width: int, height: int) -> CudaFrameBuffers:
        with self._lock:
            self._guard_poison()
            if self._cuda_driver is None:
                raise NeuralBridgeError(
                    "CUDA/D3D12 interoperability is not initialized. Switch GPU OFF to "
                    "use host staging."
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
        self, width: int, height: int, pixel_format: int
    ) -> BridgeCudaSurface:
        """Allocate one bridge-owned CUDA NV12/P010 output surface."""
        with self._lock:
            self._guard_poison()
            if self._library is None or self._cuda_driver is None:
                raise NeuralBridgeError(
                    "CUDA/D3D12 interoperability is not initialized. Switch GPU OFF to "
                    "use host staging."
                )
            if pixel_format not in (FORMAT_NV12, FORMAT_P010):
                raise NeuralBridgeError("CUDA video output must be NV12 or P010.")
            error = ctypes.create_string_buffer(4096)
            handle = self._call_with_watchdog(
                "CUDA output allocation",
                lambda: self._library.dlss5nr_surface_create(
                    int(width), int(height), int(pixel_format), error, len(error)
                ),
                (error,),
            )
            if not handle:
                detail = _text(error.value) or "unknown CUDA output allocation failure"
                raise NeuralBridgeError(
                    f"CUDA/D3D12 output allocation failed: {detail}. Switch GPU OFF to "
                    "use host staging."
                )
            descriptor = FrameDescriptorV1.empty()
            if not self._library.dlss5nr_surface_frame_desc(
                ctypes.c_void_p(handle), ctypes.byref(descriptor)
            ):
                self._library.dlss5nr_surface_release(ctypes.c_void_p(handle))
                raise NeuralBridgeError("The bridge returned an invalid CUDA output surface.")
            return BridgeCudaSurface(
                self._library, handle, descriptor, self._cuda_driver.ordinal
            )

    @staticmethod
    def _render_parameters(
        settings: dict[str, int | float | bool], reset: bool,
        mask: np.ndarray | None = None,
        cuda_mask: CudaMaskBuffer | None = None,
    ) -> RenderParametersV6:
        value = RenderParametersV6()
        value.struct_size = ctypes.sizeof(RenderParametersV6)
        value.abi_version = BRIDGE_ABI_VERSION
        value.style = int(settings["style"])
        value.intensity = float(settings["intensity"])
        value.tone = float(settings["local_tone"])
        value.structure = float(settings["local_structure"])
        value.skin = float(settings["skin_structure"])
        value.automask = int(bool(settings["auto_mask"]))
        value.reset = int(bool(reset))
        value.color_strength = float(settings["color_strength"])
        value.tone_preservation = float(settings["tone_preservation"])
        value.face_skin_protection = float(settings["face_skin_protection"])
        value.grain_preservation = float(settings["grain_preservation"])
        value.nr_passes = int(settings.get("nr_passes", 1))
        value.shimmer_suppression = float(settings.get("shimmer_suppression", 0.0))
        value.prefer_nvof = int(bool(settings.get("prefer_nvof", False)))
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
                    "CUDA/D3D12 interoperability is not initialized. Switch GPU OFF to "
                    "use host staging."
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
                    lambda: self._library.dlss5nr_process_frame_v6(
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
                    if "corrupt" in detail.lower() or "access violation" in detail.lower():
                        self._poisoned_reason = detail
                        raise NeuralBridgePoisonedError(
                            f"{detail}. Restart the application before rendering again."
                        )
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 Neural Rendering failed: {detail}. Switch GPU OFF to "
                        "use host staging."
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
                raise NeuralBridgeError(
                    f"CUDA reduced-luma scene scoring failed: {detail}. Switch GPU OFF "
                    "to use host staging."
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
                rgba.dtype != np.uint8
                or rgba.ndim != 3
                or rgba.shape[2] != 4
                or not rgba.flags.c_contiguous
            ):
                raise NeuralBridgeError("Host video input must be contiguous RGBA8.")
            height, width = rgba.shape[:2]
            source = FrameDescriptorV1.empty()
            source.memory_type = MEMORY_HOST
            source.pixel_format = FORMAT_RGBA8
            source.width = int(width)
            source.height = int(height)
            source.planes[0] = int(rgba.ctypes.data)
            source.strides[0] = int(rgba.strides[0])
            source.color_matrix = int(color_matrix)
            source.color_range = int(color_range)
            source.timestamp = int(timestamp)

            surface = self.create_video_surface(width, height, output_format)
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
                    "feature-18 host-to-CUDA video evaluation",
                    lambda: self._library.dlss5nr_process_frame_v6(
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
                    raise NeuralBridgeError(
                        f"CUDA/D3D12 Neural Rendering failed: {detail}. Switch GPU OFF "
                        "to use host staging."
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
                "input_format": "rgba8",
                "output_format": "p010le" if output_format == FORMAT_P010 else "nv12",
            }
            return output, details, elapsed

    def process_cuda_to_host_video_frame(
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
    ) -> tuple[dict[str, Any], float]:
        """Evaluate an NVDEC CUDA frame and download only the final RGBA result."""
        with self._lock:
            self._guard_poison()
            format_name = str(getattr(getattr(frame, "format", None), "name", ""))
            sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
            source_format = {
                "nv12": FORMAT_NV12,
                "p010": FORMAT_P010,
                "p010le": FORMAT_P010,
            }.get(sw_format)
            if format_name != "cuda" or source_format is None or len(frame.planes) < 2:
                raise NeuralBridgeError(
                    f"CUDA-to-host processing supports NV12/P010, received "
                    f"{format_name or 'unknown'}/{sw_format or 'unknown'}."
                )
            if (
                destination.dtype != np.uint8
                or destination.ndim != 3
                or destination.shape[2] != 4
                or not destination.flags.c_contiguous
            ):
                raise NeuralBridgeError("CUDA-to-host output must be contiguous RGBA8.")
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
            source.timestamp = int(timestamp)
            output = FrameDescriptorV1.empty()
            output.memory_type = MEMORY_HOST
            output.pixel_format = FORMAT_RGBA8
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
            started = time.perf_counter()
            ok = self._call_with_watchdog(
                "feature-18 CUDA-to-host video evaluation",
                lambda: self._library.dlss5nr_process_frame_v6(
                    ctypes.byref(source), ctypes.byref(output), ctypes.byref(params),
                    ctypes.byref(result), error, len(error),
                ),
                (frame, destination, source, output, params, result, error),
                timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
            )
            elapsed = time.perf_counter() - started
            self._cuda_driver.deactivate()
            if not ok:
                detail = _text(error.value) or "unknown CUDA-to-host frame failure"
                raise NeuralBridgeError(
                    f"CUDA/D3D12 Neural Rendering failed: {detail}. Switch GPU OFF to "
                    "use host staging."
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
                "output_format": "rgba8",
            }, elapsed

    def process_host(
        self,
        source: np.ndarray,
        destination: np.ndarray,
        settings: dict[str, int | float | bool],
        reset: bool,
        mask: np.ndarray | None = None,
    ) -> float:
        with self._lock:
            self._guard_poison()
            assert self._library is not None
            error = ctypes.create_string_buffer(4096)
            started = time.perf_counter()
            params = self._render_parameters(settings, reset, mask, None)
            ok = self._call_with_watchdog(
                "feature-18 host evaluation",
                lambda: self._library.dlss5nr_process_v6(
                    source.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    destination.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    source.shape[1],
                    source.shape[0],
                    ctypes.byref(params),
                    error,
                    len(error),
                ),
                (source, destination, mask, params, error),
                timeout_seconds=min(180.0, BRIDGE_WATCHDOG_SECONDS * params.nr_passes),
            )
            elapsed = time.perf_counter() - started
            if not ok:
                detail = _text(error.value) or "unknown feature-18 failure"
                if "corrupt" in detail.lower() or "access violation" in detail.lower():
                    self._poisoned_reason = detail
                    raise NeuralBridgePoisonedError(
                        f"{detail}. Restart the application before rendering again."
                    )
                raise NeuralBridgeError(f"Feature-18 evaluation failed: {detail}")
            return elapsed

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
                lambda: self._library.dlss5nr_process_cuda_v6(
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
                if "corrupt" in detail.lower() or "access violation" in detail.lower():
                    self._poisoned_reason = detail
                    raise NeuralBridgePoisonedError(
                        f"{detail}. Restart the application before rendering again."
                    )
                raise NeuralBridgeError(
                    f"CUDA/D3D12 Neural Rendering failed: {detail}. Switch GPU OFF to "
                    "use host staging."
                )
            download_started = time.perf_counter()
            buffers.driver.synchronize()
            buffers.driver.download(destination, buffers.output_pointer)
            download_seconds = time.perf_counter() - download_started
            return upload_seconds, evaluate_seconds, download_seconds


BRIDGE_MANAGER = NeuralBridgeManager()
