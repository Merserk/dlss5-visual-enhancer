"""Feature-independent CUDA DLPack capsule ownership for native video surfaces."""
from __future__ import annotations
import ctypes
import threading
from typing import Any, Callable

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

LIVE_DLPACK_RECORDS: dict[int, tuple[Any, ...]] = {}
DLPACK_LOCK = threading.Lock()
_DL_DELETER = ctypes.CFUNCTYPE(None, ctypes.POINTER(_DLManagedTensor))

@_DL_DELETER
def _dlpack_deleter(pointer: ctypes.POINTER(_DLManagedTensor)) -> None:
    address = ctypes.addressof(pointer.contents)
    with DLPACK_LOCK:
        record = LIVE_DLPACK_RECORDS.pop(address, None)
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

class CudaDLPackPlane:
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
        with DLPACK_LOCK:
            LIVE_DLPACK_RECORDS[address] = (managed, shape, strides, self, self.release)
        return ctypes.pythonapi.PyCapsule_New(address, b"dltensor", _capsule_destructor)

