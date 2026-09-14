from __future__ import annotations

"""Cross-adapter CUDA frame transfer with DLPack-owned surface lifetimes."""

import ctypes
import gc
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import av

from ...core.neural_bridge import _DLPackPlane
from ...core.jobs import Cancelled


CUDA_SUCCESS = 0
CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED = 704
CU_STREAM_NON_BLOCKING = 1
CU_MEMHOSTALLOC_PORTABLE = 1


class CudaTransferError(RuntimeError):
    pass


class _CudaApi:
    """The small CUDA Driver API subset needed for adapter-to-adapter copies."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        self.library = loader("nvcuda.dll")
        self._bind("cuInit", [ctypes.c_uint])
        self._bind("cuDeviceGet", [ctypes.POINTER(ctypes.c_int), ctypes.c_int])
        self._bind("cuDeviceCanAccessPeer", [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int])
        self._bind("cuDevicePrimaryCtxRetain", [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int])
        self._bind_any("primary_release", ("cuDevicePrimaryCtxRelease_v2", "cuDevicePrimaryCtxRelease"),
                       [ctypes.c_int])
        self._bind("cuCtxGetCurrent", [ctypes.POINTER(ctypes.c_void_p)])
        self._bind("cuCtxSetCurrent", [ctypes.c_void_p])
        self._bind("cuCtxEnablePeerAccess", [ctypes.c_void_p, ctypes.c_uint])
        self._bind("cuStreamCreate", [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint])
        self._bind_any("stream_destroy", ("cuStreamDestroy_v2", "cuStreamDestroy"),
                       [ctypes.c_void_p])
        self._bind("cuStreamSynchronize", [ctypes.c_void_p])
        self._bind_any("mem_alloc", ("cuMemAlloc_v2", "cuMemAlloc"),
                       [ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t])
        self._bind_any("mem_free", ("cuMemFree_v2", "cuMemFree"), [ctypes.c_uint64])
        self._bind("cuMemcpyPeerAsync", [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p])
        self._bind("cuMemHostAlloc", [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t, ctypes.c_uint])
        self._bind("cuMemFreeHost", [ctypes.c_void_p])
        self._bind_any("copy_dtoh_async", ("cuMemcpyDtoHAsync_v2", "cuMemcpyDtoHAsync"),
                       [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_size_t, ctypes.c_void_p])
        self._bind_any("copy_htod_async", ("cuMemcpyHtoDAsync_v2", "cuMemcpyHtoDAsync"),
                       [ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p])
        self._bind("cuGetErrorString", [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p)])
        self.check(self.cuInit(0), "cuInit")

    def _bind(self, name: str, arguments: list[Any]) -> Any:
        function = getattr(self.library, name)
        function.argtypes = arguments
        function.restype = ctypes.c_int
        setattr(self, name, function)
        return function

    def _bind_any(self, attribute: str, names: tuple[str, ...], arguments: list[Any]) -> Any:
        for name in names:
            function = getattr(self.library, name, None)
            if function is not None:
                function.argtypes = arguments
                function.restype = ctypes.c_int
                setattr(self, attribute, function)
                return function
        raise CudaTransferError(f"The NVIDIA driver does not export {names[0]}.")

    def check(self, result: int, operation: str, *, allowed: tuple[int, ...] = ()) -> int:
        result = int(result)
        if result == CUDA_SUCCESS or result in allowed:
            return result
        message = ctypes.c_char_p()
        detail = ""
        try:
            if self.cuGetErrorString(result, ctypes.byref(message)) == CUDA_SUCCESS and message.value:
                detail = message.value.decode("utf-8", "replace")
        except Exception:
            pass
        raise CudaTransferError(
            f"{operation} failed with CUDA result {result}" + (f": {detail}" if detail else "") + ".")

    def device(self, ordinal: int) -> int:
        value = ctypes.c_int()
        self.check(self.cuDeviceGet(ctypes.byref(value), int(ordinal)), "cuDeviceGet")
        return int(value.value)

    def primary_context(self, device: int) -> ctypes.c_void_p:
        value = ctypes.c_void_p()
        self.check(self.cuDevicePrimaryCtxRetain(ctypes.byref(value), int(device)),
                   "cuDevicePrimaryCtxRetain")
        return value

    @contextmanager
    def current(self, context: ctypes.c_void_p) -> Iterator[None]:
        previous = ctypes.c_void_p()
        self.check(self.cuCtxGetCurrent(ctypes.byref(previous)), "cuCtxGetCurrent")
        self.check(self.cuCtxSetCurrent(context), "cuCtxSetCurrent")
        try:
            yield
        finally:
            self.check(self.cuCtxSetCurrent(previous), "cuCtxSetCurrent(restore)")


@dataclass(slots=True)
class _TransferSurface:
    owner: "CudaTransferPool"
    pointer: int
    host_pointer: int
    width: int
    height: int
    stride: int
    bits: int
    references: int = 0

    @property
    def byte_count(self) -> int:
        return self.stride * (self.height + (self.height + 1) // 2)

    def retain(self) -> None:
        with self.owner.condition:
            if self.references <= 0:
                raise CudaTransferError("Cannot retain a released CUDA transfer surface.")
            self.references += 1

    def release(self) -> None:
        with self.owner.condition:
            if self.references <= 0:
                return
            self.references -= 1
            if self.references == 0:
                self.owner.condition.notify_all()

    def to_av_frame(self) -> av.VideoFrame:
        item_size = self.bits // 8
        y = _DLPackPlane(
            pointer=self.pointer,
            shape=(self.height, self.width),
            strides=(self.stride // item_size, 1),
            bits=self.bits,
            device_id=self.owner.destination_ordinal,
            retain=self.retain,
            release=self.release,
        )
        uv = _DLPackPlane(
            pointer=self.pointer + self.stride * self.height,
            shape=((self.height + 1) // 2, (self.width + 1) // 2, 2),
            strides=(self.stride // item_size, 2, 1),
            bits=self.bits,
            device_id=self.owner.destination_ordinal,
            retain=self.retain,
            release=self.release,
        )
        try:
            frame = av.VideoFrame.from_dlpack(
                [y, uv], format="p010le" if self.bits == 16 else "nv12",
                width=self.width, height=self.height,
                device_id=self.owner.destination_ordinal, primary_ctx=True,
            )
        except BaseException:
            self.release()
            raise
        self.release()  # the two DLPack tensors now own the creator reference
        return frame


class CudaTransferPool:
    """Eight reusable destination surfaces for explicit cross-GPU NVENC."""

    def __init__(self, source_ordinal: int, destination_ordinal: int, controller: Any,
                 *, capacity: int = 8, timeout: float = 180.0) -> None:
        if int(source_ordinal) == int(destination_ordinal):
            raise ValueError("A cross-GPU transfer pool requires two different CUDA adapters.")
        self.api = _CudaApi()
        self.source_ordinal = int(source_ordinal)
        self.destination_ordinal = int(destination_ordinal)
        self.controller = controller
        self.capacity = int(capacity)
        self.timeout = float(timeout)
        self.condition = threading.Condition()
        self.surfaces: list[_TransferSurface] = []
        self.pool_waits = 0
        self.peer_copy_bytes = 0
        self.pinned_download_bytes = 0
        self.pinned_upload_bytes = 0
        self.copy_seconds = 0.0
        self.closed = False

        self.source_device = self.api.device(self.source_ordinal)
        self.destination_device = self.api.device(self.destination_ordinal)
        self.source_context = self.api.primary_context(self.source_device)
        try:
            self.destination_context = self.api.primary_context(self.destination_device)
        except BaseException:
            self.api.check(self.api.primary_release(self.source_device),
                           "cuDevicePrimaryCtxRelease(source)")
            raise
        self.source_stream = ctypes.c_void_p()
        self.destination_stream = ctypes.c_void_p()
        try:
            with self.api.current(self.source_context):
                self.api.check(self.api.cuStreamCreate(ctypes.byref(self.source_stream),
                                                       CU_STREAM_NON_BLOCKING),
                               "cuStreamCreate(source)")
            with self.api.current(self.destination_context):
                self.api.check(self.api.cuStreamCreate(ctypes.byref(self.destination_stream),
                                                       CU_STREAM_NON_BLOCKING),
                               "cuStreamCreate(destination)")
            can_access = ctypes.c_int()
            self.api.check(self.api.cuDeviceCanAccessPeer(
                ctypes.byref(can_access), self.destination_device, self.source_device),
                "cuDeviceCanAccessPeer")
            self.peer_access = bool(can_access.value)
            if self.peer_access:
                try:
                    with self.api.current(self.destination_context):
                        self.api.check(
                            self.api.cuCtxEnablePeerAccess(self.source_context, 0),
                            "cuCtxEnablePeerAccess",
                            allowed=(CUDA_ERROR_PEER_ACCESS_ALREADY_ENABLED,),
                        )
                except CudaTransferError:
                    self.peer_access = False
        except BaseException:
            self.close(abort=True)
            raise

    @property
    def memory_path(self) -> str:
        return "cuda_cross_gpu_peer" if self.peer_access else "cuda_cross_gpu_pinned_bounce"

    def _allocate(self, width: int, height: int, stride: int, bits: int) -> _TransferSurface:
        pointer = ctypes.c_uint64()
        byte_count = int(stride) * (int(height) + (int(height) + 1) // 2)
        with self.api.current(self.destination_context):
            self.api.check(self.api.mem_alloc(ctypes.byref(pointer), byte_count), "cuMemAlloc(cross-GPU surface)")
        host_pointer = ctypes.c_void_p()
        if not self.peer_access:
            self.api.check(self.api.cuMemHostAlloc(
                ctypes.byref(host_pointer), byte_count, CU_MEMHOSTALLOC_PORTABLE),
                "cuMemHostAlloc(cross-GPU bounce)")
        surface = _TransferSurface(
            self, int(pointer.value), int(host_pointer.value or 0),
            int(width), int(height), int(stride), int(bits), references=1,
        )
        self.surfaces.append(surface)
        return surface

    def _acquire(self, width: int, height: int, stride: int, bits: int) -> _TransferSurface:
        deadline = time.monotonic() + self.timeout
        while True:
            if self.controller.cancel.is_set():
                raise Cancelled("Upscale stopped by user.")
            with self.condition:
                for surface in self.surfaces:
                    if (surface.references == 0 and surface.width == width and
                            surface.height == height and surface.stride == stride and surface.bits == bits):
                        surface.references = 1
                        return surface
                if len(self.surfaces) < self.capacity:
                    return self._allocate(width, height, stride, bits)
                self.pool_waits += 1
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CudaTransferError("Cross-GPU CUDA surface pool remained exhausted.")
                self.condition.wait(timeout=min(0.01, remaining))
            gc.collect()

    def transfer(self, frame: av.VideoFrame) -> tuple[av.VideoFrame, dict[str, Any]]:
        if self.closed:
            raise CudaTransferError("Cross-GPU CUDA transfer pool is closed.")
        if frame.format.name != "cuda" or len(frame.planes) < 2:
            raise CudaTransferError("Cross-GPU transfer requires a CUDA NV12/P010 frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        bits = 16 if sw_format in {"p010", "p010le"} else 8 if sw_format == "nv12" else 0
        if not bits:
            raise CudaTransferError(f"Unsupported cross-GPU CUDA format: {sw_format or 'unknown'}.")
        width, height = int(frame.width), int(frame.height)
        source_stride = int(frame.planes[0].line_size)
        if int(frame.planes[1].line_size) != source_stride:
            raise CudaTransferError("CUDA luma and chroma pitches differ; cross-GPU copy cannot be pooled safely.")
        surface = self._acquire(width, height, source_stride, bits)
        y_bytes = source_stride * height
        uv_bytes = source_stride * ((height + 1) // 2)
        source_y = int(frame.planes[0].buffer_ptr)
        source_uv = int(frame.planes[1].buffer_ptr)
        tick = time.perf_counter()
        try:
            if self.peer_access:
                with self.api.current(self.destination_context):
                    for destination, source, count in (
                        (surface.pointer, source_y, y_bytes),
                        (surface.pointer + y_bytes, source_uv, uv_bytes),
                    ):
                        self.api.check(self.api.cuMemcpyPeerAsync(
                            destination, self.destination_context, source, self.source_context,
                            count, self.destination_stream), "cuMemcpyPeerAsync")
                    self.api.check(self.api.cuStreamSynchronize(self.destination_stream),
                                   "cuStreamSynchronize(peer copy)")
                self.peer_copy_bytes += y_bytes + uv_bytes
            else:
                with self.api.current(self.source_context):
                    self.api.check(self.api.copy_dtoh_async(
                        ctypes.c_void_p(surface.host_pointer), source_y, y_bytes, self.source_stream),
                        "cuMemcpyDtoHAsync(luma)")
                    self.api.check(self.api.copy_dtoh_async(
                        ctypes.c_void_p(surface.host_pointer + y_bytes), source_uv, uv_bytes,
                        self.source_stream), "cuMemcpyDtoHAsync(chroma)")
                    self.api.check(self.api.cuStreamSynchronize(self.source_stream),
                                   "cuStreamSynchronize(pinned download)")
                with self.api.current(self.destination_context):
                    self.api.check(self.api.copy_htod_async(
                        surface.pointer, ctypes.c_void_p(surface.host_pointer), y_bytes,
                        self.destination_stream), "cuMemcpyHtoDAsync(luma)")
                    self.api.check(self.api.copy_htod_async(
                        surface.pointer + y_bytes, ctypes.c_void_p(surface.host_pointer + y_bytes),
                        uv_bytes, self.destination_stream), "cuMemcpyHtoDAsync(chroma)")
                    self.api.check(self.api.cuStreamSynchronize(self.destination_stream),
                                   "cuStreamSynchronize(pinned upload)")
                self.pinned_download_bytes += y_bytes + uv_bytes
                self.pinned_upload_bytes += y_bytes + uv_bytes
            elapsed = time.perf_counter() - tick
            self.copy_seconds += elapsed
            output = surface.to_av_frame()
        except BaseException:
            surface.release()
            raise
        output.pts, output.time_base = frame.pts, frame.time_base
        if frame.duration is not None:
            output.duration = frame.duration
        return output, {
            "memory_path": self.memory_path,
            "copy_ms": elapsed * 1000.0,
            "peer_copy_bytes": y_bytes + uv_bytes if self.peer_access else 0,
            "pinned_download_bytes": 0 if self.peer_access else y_bytes + uv_bytes,
            "pinned_upload_bytes": 0 if self.peer_access else y_bytes + uv_bytes,
        }

    def diagnostics(self) -> dict[str, Any]:
        return {
            "memory_path": self.memory_path,
            "peer_access": self.peer_access,
            "peer_copy_bytes": self.peer_copy_bytes,
            "pinned_download_bytes": self.pinned_download_bytes,
            "pinned_upload_bytes": self.pinned_upload_bytes,
            "copy_seconds": self.copy_seconds,
            "surface_pool_capacity": self.capacity,
            "surface_pool_allocated": len(self.surfaces),
            "surface_pool_waits": self.pool_waits,
        }

    def close(self, *, abort: bool = False) -> None:
        if self.closed:
            return
        deadline = time.monotonic() + (2.0 if abort else self.timeout)
        with self.condition:
            while any(surface.references for surface in self.surfaces):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if abort:
                        # The encoder may still own a DLPack pointer after a failed
                        # encode. Leaking those few allocations is safer than a
                        # use-after-free in the NVIDIA driver during unwinding.
                        self.closed = True
                        return
                    raise CudaTransferError("Encoder retained cross-GPU surfaces past shutdown.")
                self.condition.wait(timeout=min(0.05, remaining))
        try:
            with self.api.current(self.destination_context):
                if self.destination_stream.value:
                    self.api.check(self.api.cuStreamSynchronize(self.destination_stream),
                                   "cuStreamSynchronize(destination close)")
                for surface in self.surfaces:
                    if surface.pointer:
                        self.api.check(self.api.mem_free(surface.pointer), "cuMemFree(cross-GPU surface)")
                        surface.pointer = 0
                    if surface.host_pointer:
                        self.api.check(self.api.cuMemFreeHost(ctypes.c_void_p(surface.host_pointer)),
                                       "cuMemFreeHost(cross-GPU bounce)")
                        surface.host_pointer = 0
                if self.destination_stream.value:
                    self.api.check(self.api.stream_destroy(self.destination_stream),
                                   "cuStreamDestroy(destination)")
                    self.destination_stream = ctypes.c_void_p()
            with self.api.current(self.source_context):
                if self.source_stream.value:
                    self.api.check(self.api.cuStreamSynchronize(self.source_stream),
                                   "cuStreamSynchronize(source close)")
                    self.api.check(self.api.stream_destroy(self.source_stream),
                                   "cuStreamDestroy(source)")
                    self.source_stream = ctypes.c_void_p()
        finally:
            self.api.check(self.api.primary_release(self.destination_device),
                           "cuDevicePrimaryCtxRelease(destination)")
            self.api.check(self.api.primary_release(self.source_device),
                           "cuDevicePrimaryCtxRelease(source)")
            self.closed = True

    def __enter__(self) -> "CudaTransferPool":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close(abort=exc_type is not None)
