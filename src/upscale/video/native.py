from __future__ import annotations

"""Persistent in-process CUDA bridge for RTX Video VSR and TrueHDR.

The legacy D3D11 worker remains packaged as an explicit developer A/B probe,
but production frames never fall back to it. Host callers cross PCIe once in
each direction; CUDA video callers stay on the selected adapter through DLPack.
"""

import collections
import ctypes
import gc
import json
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from ...core.gpu_detection import detect_gpus
from ...core.gpu_selection import resolve_ai_gpu
from ...core.jobs import Cancelled, JobController, current_job_controller
from ...core.neural_bridge import _DLPackPlane
from ...core.ngx_runtime import NGX_RUNTIME_LOCK
from ...core.paths import ROOT, RUNTIME
from .models import UpscaleCapabilities, UpscaleOptions


RUNTIME_DIR = RUNTIME / "rtx_video"
BRIDGE = RUNTIME_DIR / "neuroframe_engine.dll"
WORKER = (ROOT / "native (dev)" / "Upscale" / "RTX Video" / "rtx_video" /
          "build" / "legacy" / "rtx-video-worker.exe")
BRIDGE_ABI_VERSION = 1
MEMORY_HOST, MEMORY_CUDA = 1, 2
FORMAT_RGBA8, FORMAT_R10, FORMAT_RGBA16F, FORMAT_NV12, FORMAT_P010, FORMAT_YUV422P10 = 1, 2, 3, 4, 5, 6


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
        ("input_width", ctypes.c_uint32), ("input_height", ctypes.c_uint32),
        ("output_width", ctypes.c_uint32), ("output_height", ctypes.c_uint32),
        ("input_format", ctypes.c_uint32), ("output_format", ctypes.c_uint32),
        ("vsr_enabled", ctypes.c_uint32), ("vsr_quality", ctypes.c_uint32),
        ("hdr_enabled", ctypes.c_uint32), ("hdr_contrast", ctypes.c_uint32),
        ("hdr_saturation", ctypes.c_uint32), ("hdr_middle_gray", ctypes.c_uint32),
        ("hdr_peak_luminance", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 5),
    ]


class FrameResultV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32), ("abi_version", ctypes.c_uint32),
        ("vsr_result", ctypes.c_uint32), ("hdr_result", ctypes.c_uint32),
        ("cuda_result", ctypes.c_uint32), ("reserved0", ctypes.c_uint32),
        ("upload_bytes", ctypes.c_uint64), ("download_bytes", ctypes.c_uint64),
        ("input_milliseconds", ctypes.c_double), ("ngx_milliseconds", ctypes.c_double),
        ("output_milliseconds", ctypes.c_double), ("total_milliseconds", ctypes.c_double),
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
        ("vsr_init_result", ctypes.c_uint32), ("hdr_init_result", ctypes.c_uint32),
        ("vsr_min_driver_major", ctypes.c_uint32),
        ("vsr_min_driver_minor", ctypes.c_uint32),
        ("hdr_min_driver_major", ctypes.c_uint32),
        ("hdr_min_driver_minor", ctypes.c_uint32),
        ("bridge_version", ctypes.c_char * 32), ("gpu_name", ctypes.c_char * 128),
        ("last_error", ctypes.c_char * 512),
    ]

    @classmethod
    def empty(cls) -> "BridgeStatusV1":
        value = cls()
        value.struct_size = ctypes.sizeof(cls)
        value.abi_version = BRIDGE_ABI_VERSION
        return value


class RTXVideoBridgeError(RuntimeError):
    pass


class RTXVideoBridgePoisonedError(RTXVideoBridgeError):
    pass


def gpu_luid(gpu: dict) -> str:
    driver = ctypes.WinDLL("nvcuda.dll")
    device, mask = ctypes.c_int(), ctypes.c_uint()
    luid = (ctypes.c_char * 8)()
    if (driver.cuInit(0) or driver.cuDeviceGet(ctypes.byref(device), int(gpu["cuda_ordinal"])) or
            driver.cuDeviceGetLuid(luid, ctypes.byref(mask), device)):
        raise RuntimeError("Cannot match the selected NVIDIA GPU to its DirectX adapter.")
    return bytes(luid).hex()


def runtime_files(*, legacy_probe: bool = False) -> None:
    required = [BRIDGE, RUNTIME_DIR / "nvngx_vsr.dll", RUNTIME_DIR / "nvngx_truehdr.dll"]
    if legacy_probe:
        required.append(WORKER)
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"RTX Video runtime is missing: {path}. Rebuild or restore the RTX Video runtime.")


class _BridgeManager:
    def __init__(self) -> None:
        self.library: Any | None = None
        self.gpu_ordinal: int | None = None
        self.poisoned_reason = ""
        self.ffmpeg_devices: dict[int, tuple[Any, ctypes.c_void_p]] = {}

    def _ensure_ffmpeg_device(self, ordinal: int) -> None:
        """Create and retain FFmpeg's primary CUDA context before NGX loads.

        Some driver releases reject the first av_hwdevice_ctx_create after NGX
        has initialized CUDA.  A retained generic device makes subsequent
        PyAV decoder and encoder contexts cheap references to the same primary
        context, even when capability probing happens before a video is open.
        """
        if ordinal in self.ffmpeg_devices:
            return
        import av

        av_root = Path(av.__file__).resolve().parent.parent
        candidates = sorted((av_root / "av.libs").glob("avutil-*.dll"))
        if not candidates:
            raise RTXVideoBridgeError("PyAV's avutil runtime is missing; CUDA video contexts cannot be prepared.")
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
            raise RTXVideoBridgeError("FFmpeg CUDA primary-context options could not be prepared.")
        try:
            result = int(avutil.av_hwdevice_ctx_create(
                ctypes.byref(reference), kind, str(int(ordinal)).encode("ascii"), options, 0))
        finally:
            avutil.av_dict_free(ctypes.byref(options))
        if result < 0 or not reference.value:
            raise RTXVideoBridgeError(
                f"FFmpeg could not create CUDA primary context {ordinal} before NGX (error {result})."
            )
        # Deliberately process-lifetime: releasing this before NGX can recreate
        # the driver-ordering hang that this guard prevents.
        self.ffmpeg_devices[ordinal] = (avutil, reference)

    def _load(self) -> Any:
        if self.library is not None:
            return self.library
        runtime_files()
        try:
            library = ctypes.WinDLL(str(BRIDGE))
        except OSError as exc:
            raise RTXVideoBridgeError(f"Could not load the RTX Video CUDA bridge: {exc}") from exc
        library.rtxv_abi_version.restype = ctypes.c_uint32
        library.rtxv_version.restype = ctypes.c_char_p
        library.rtxv_init.argtypes = [ctypes.c_int, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_int]
        library.rtxv_init.restype = ctypes.c_int
        library.rtxv_status.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.rtxv_status.restype = ctypes.c_int
        library.rtxv_get_status_v1.argtypes = [ctypes.POINTER(BridgeStatusV1)]
        library.rtxv_get_status_v1.restype = ctypes.c_int
        library.rtxv_session_create.argtypes = [ctypes.POINTER(SessionDescriptorV1), ctypes.c_void_p, ctypes.c_int]
        library.rtxv_session_create.restype = ctypes.c_void_p
        library.rtxv_session_release.argtypes = [ctypes.c_void_p]
        library.rtxv_surface_acquire.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        library.rtxv_surface_acquire.restype = ctypes.c_void_p
        library.rtxv_surface_frame_desc.argtypes = [ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1)]
        library.rtxv_surface_frame_desc.restype = ctypes.c_int
        library.rtxv_surface_retain.argtypes = [ctypes.c_void_p]
        library.rtxv_surface_release.argtypes = [ctypes.c_void_p]
        library.rtxv_process_frame_v1.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1), ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameResultV1), ctypes.c_void_p, ctypes.c_int,
        ]
        library.rtxv_process_frame_v1.restype = ctypes.c_int
        library.rtxv_convert_input_v1.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(FrameDescriptorV1),
            ctypes.POINTER(FrameDescriptorV1), ctypes.c_void_p, ctypes.c_int,
        ]
        library.rtxv_convert_input_v1.restype = ctypes.c_int
        if int(library.rtxv_abi_version()) != BRIDGE_ABI_VERSION:
            raise RTXVideoBridgeError("RTX Video CUDA bridge ABI does not match this application.")
        self.library = library
        return library

    def guard(self) -> None:
        if self.poisoned_reason:
            raise RTXVideoBridgePoisonedError(
                f"The RTX Video CUDA engine is poisoned: {self.poisoned_reason}. "
                "Restart the application before using RTX Video again."
            )

    def initialize(self, ordinal: int) -> dict[str, Any]:
        with NGX_RUNTIME_LOCK:
            self.guard()
            if self.gpu_ordinal is not None and self.gpu_ordinal != ordinal:
                raise RTXVideoBridgeError(
                    "RTX Video is already bound to another AI GPU. Restart the application "
                    "before changing the RTX Video AI adapter."
                )
            self._ensure_ffmpeg_device(int(ordinal))
            library = self._load()
            error = ctypes.create_string_buffer(4096)
            if not library.rtxv_init(int(ordinal), str(RUNTIME_DIR), error, len(error)):
                detail = error.value.decode("utf-8", "replace") or "unknown initialization failure"
                if "poison" in detail.casefold() or "access violation" in detail.casefold():
                    self.poisoned_reason = detail
                    self.guard()
                raise RTXVideoBridgeError(f"RTX Video CUDA initialization failed: {detail}")
            self.gpu_ordinal = int(ordinal)
            return self.status()

    def status(self) -> dict[str, Any]:
        library = self._load()
        record = BridgeStatusV1.empty()
        if not library.rtxv_get_status_v1(ctypes.byref(record)):
            raise RTXVideoBridgeError("RTX Video CUDA bridge returned an invalid status record.")
        flags = int(record.flags)
        return {
            "abi_version": int(record.abi_version),
            "bridge_version": bytes(record.bridge_version).split(b"\0", 1)[0].decode("utf-8", "replace"),
            "initialized": bool(flags & 1), "poisoned": bool(flags & 2),
            "gpu_ordinal": int(record.gpu_ordinal),
            "gpu_name": bytes(record.gpu_name).split(b"\0", 1)[0].decode("utf-8", "replace"),
            "vsr_available": bool(flags & 4), "hdr_available": bool(flags & 8),
            "vsr_min_driver": f"{record.vsr_min_driver_major}.{record.vsr_min_driver_minor}",
            "hdr_min_driver": f"{record.hdr_min_driver_major}.{record.hdr_min_driver_minor}",
            "vsr_init_result": int(record.vsr_init_result),
            "hdr_init_result": int(record.hdr_init_result),
            "last_error": bytes(record.last_error).split(b"\0", 1)[0].decode("utf-8", "replace"),
        }

    def call(self, label: str, function, keepalive: tuple[Any, ...], timeout: float) -> Any:
        """Run a potentially wedged native call without releasing its arguments."""
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

        thread = threading.Thread(target=invoke, name=f"rtx-video-{label}", daemon=True)
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


def _feature_record(status: dict[str, Any], prefix: str) -> dict[str, Any]:
    version = str(status.get(f"{prefix}_min_driver") or "0.0").split(".", 1)
    return {
        "available": bool(status.get(f"{prefix}_available")),
        "min_driver_major": int(version[0] or 0),
        "min_driver_minor": int(version[1] or 0) if len(version) > 1 else 0,
        "init_result": int(status.get(f"{prefix}_init_result") or 0),
    }


def probe_capabilities(gpu_uuid: str = "auto", *, controller=None) -> UpscaleCapabilities:
    gpu = resolve_ai_gpu(detect_gpus(), gpu_uuid)
    if controller is not None and controller.cancel.is_set():
        raise Cancelled("Stopped before checking RTX Video capabilities.")
    status = _MANAGER.initialize(int(gpu["cuda_ordinal"]))
    return UpscaleCapabilities(
        gpu=gpu, luid=gpu_luid(gpu), vsr=_feature_record(status, "vsr"),
        hdr=_feature_record(status, "hdr"), sdk_version="1.1.0",
        worker_version="developer-probe-only",
        bridge_version=str(status.get("bridge_version") or "unknown"),
        bridge_status=status,
    )


@lru_cache(maxsize=8)
def probe_legacy_capabilities(luid: str) -> dict[str, Any]:
    """Explicit developer-only D3D11 subprocess capability probe."""
    runtime_files(legacy_probe=True)
    controller = current_job_controller() or JobController()
    process = subprocess.Popen(
        [str(WORKER), "--probe", "--gpu-luid", luid], cwd=WORKER.parent,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    controller.register(process)
    try:
        stdout, stderr = process.communicate(timeout=90)
    finally:
        if process.poll() is None:
            process.kill()
        controller.unregister(process)
    if process.returncode:
        raise RuntimeError(stderr.decode("utf-8", "replace")[-3000:])
    return json.loads(stdout)


class RTXVideoCudaSurface:
    def __init__(self, library: Any, handle: int, descriptor: FrameDescriptorV1, ordinal: int) -> None:
        self.library, self.handle = library, int(handle)
        self.descriptor, self.ordinal = descriptor, int(ordinal)
        self.closed = False

    def retain(self) -> None:
        self.library.rtxv_surface_retain(ctypes.c_void_p(self.handle))

    def release(self) -> None:
        self.library.rtxv_surface_release(ctypes.c_void_p(self.handle))

    def close(self) -> None:
        if not self.closed:
            self.release()
            self.closed = True

    def to_av_frame(self):
        import av

        descriptor = self.descriptor
        bits = 16 if descriptor.pixel_format == FORMAT_P010 else 8
        item_size = bits // 8
        y_plane = _DLPackPlane(
            pointer=int(descriptor.planes[0]), shape=(int(descriptor.height), int(descriptor.width)),
            strides=(int(descriptor.strides[0]) // item_size, 1), bits=bits,
            device_id=self.ordinal, retain=self.retain, release=self.release,
        )
        uv_plane = _DLPackPlane(
            pointer=int(descriptor.planes[1]),
            shape=((int(descriptor.height) + 1) // 2, (int(descriptor.width) + 1) // 2, 2),
            strides=(int(descriptor.strides[1]) // item_size, 2, 1), bits=bits,
            device_id=self.ordinal, retain=self.retain, release=self.release,
        )
        try:
            frame = av.VideoFrame.from_dlpack(
                [y_plane, uv_plane], format="p010le" if bits == 16 else "nv12",
                width=int(descriptor.width), height=int(descriptor.height),
                device_id=self.ordinal, primary_ctx=True,
            )
        except BaseException:
            self.close()
            raise
        self.close()
        return frame


class RTXVideoSession:
    """Persistent RTX Video session supporting host and CUDA-frame callers."""

    def __init__(self, width: int, height: int, output_width: int, output_height: int,
                 options: UpscaleOptions, input_format: int, capabilities: UpscaleCapabilities,
                 controller: JobController, *, timeout: float = 180,
                 image_srgb: bool = False):
        options.validate()
        for enabled, cap, name in ((options.vsr_enabled, capabilities.vsr, "VSR"),
                                   (options.hdr_enabled, capabilities.hdr, "HDR")):
            if enabled and not cap.get("available"):
                raise RTXVideoBridgeError(
                    f"RTX Video {name} is unavailable on {capabilities.gpu['name']}. "
                    f"Minimum driver {cap.get('min_driver_major', 0)}.{cap.get('min_driver_minor', 0)}; "
                    f"initialization result 0x{cap.get('init_result', 0):08X}."
                )
        self.library = _MANAGER._load()
        self.controller, self.timeout = controller, float(timeout)
        self.ordinal = int(capabilities.gpu["cuda_ordinal"])
        self.width, self.height = int(width), int(height)
        self.output_width, self.output_height = int(output_width), int(output_height)
        self.input_format = int(input_format)
        self.output_format = (FORMAT_RGBA16F if options.hdr_precision == "FP16" else FORMAT_R10) \
            if options.hdr_enabled else self.input_format
        self.input_bytes = self.width * self.height * 4
        self.output_bytes = self.output_width * self.output_height * (8 if self.output_format == FORMAT_RGBA16F else 4)
        self.logs = collections.deque(maxlen=150)
        self.completed_frames = 0
        self.last_results = (0, 0)
        self.last_timing: dict[str, float] = {}
        self.upload_bytes = self.download_bytes = self.pool_waits = 0
        self.closed = False
        descriptor = SessionDescriptorV1()
        descriptor.struct_size, descriptor.abi_version = ctypes.sizeof(descriptor), BRIDGE_ABI_VERSION
        descriptor.input_width, descriptor.input_height = self.width, self.height
        descriptor.output_width, descriptor.output_height = self.output_width, self.output_height
        descriptor.input_format, descriptor.output_format = self.input_format, self.output_format
        descriptor.vsr_enabled, descriptor.vsr_quality = int(options.vsr_enabled), int(options.vsr_quality)
        descriptor.hdr_enabled = int(options.hdr_enabled)
        descriptor.hdr_contrast, descriptor.hdr_saturation = int(options.hdr_contrast), int(options.hdr_saturation)
        descriptor.hdr_middle_gray, descriptor.hdr_peak_luminance = int(options.hdr_middle_gray), int(options.hdr_peak_luminance)
        descriptor.reserved[0] = int(bool(image_srgb))
        error = ctypes.create_string_buffer(4096)
        with NGX_RUNTIME_LOCK:
            _MANAGER.guard()
            self.handle = int(self.library.rtxv_session_create(ctypes.byref(descriptor), error, len(error)) or 0)
        if not self.handle:
            detail = error.value.decode("utf-8", "replace") or "unknown session creation failure"
            raise RTXVideoBridgeError(f"Could not create RTX Video CUDA session: {detail}")

    def _check(self) -> None:
        if self.closed:
            raise RTXVideoBridgeError("RTX Video session is closed.")
        if self.controller.cancel.is_set():
            raise Cancelled("Upscale stopped by user.")
        _MANAGER.guard()

    @staticmethod
    def _host_pointer(buffer: Any, expected: int) -> tuple[Any, int]:
        view = memoryview(buffer).cast("B")
        if view.nbytes != expected:
            raise ValueError("Incorrect RTX Video frame byte count.")
        if not view.contiguous:
            raise ValueError("RTX Video host frames must be contiguous.")
        if view.readonly:
            owner: Any = bytearray(view)
            view = memoryview(owner)
        else:
            owner = buffer
        return owner, ctypes.addressof(ctypes.c_ubyte.from_buffer(view))

    def _record(self, result: FrameResultV1) -> dict[str, Any]:
        self.completed_frames += 1
        self.last_results = (int(result.vsr_result), int(result.hdr_result))
        self.upload_bytes += int(result.upload_bytes)
        self.download_bytes += int(result.download_bytes)
        self.last_timing = {
            "input_ms": float(result.input_milliseconds), "ngx_ms": float(result.ngx_milliseconds),
            "output_ms": float(result.output_milliseconds), "total_ms": float(result.total_milliseconds),
        }
        return {
            **self.last_timing, "vsr_result": f"0x{int(result.vsr_result):08X}",
            "hdr_result": f"0x{int(result.hdr_result):08X}", "cuda_result": int(result.cuda_result),
            "upload_bytes": int(result.upload_bytes), "download_bytes": int(result.download_bytes),
        }

    def _evaluate(self, source: FrameDescriptorV1, destination: FrameDescriptorV1,
                  keepalive: tuple[Any, ...]) -> dict[str, Any]:
        result, error = FrameResultV1.empty(), ctypes.create_string_buffer(4096)
        ok = _MANAGER.call(
            "frame evaluation",
            lambda: self.library.rtxv_process_frame_v1(
                ctypes.c_void_p(self.handle), ctypes.byref(source), ctypes.byref(destination),
                ctypes.byref(result), error, len(error)),
            (*keepalive, source, destination, result, error), self.timeout,
        )
        if not ok:
            detail = error.value.decode("utf-8", "replace") or "unknown frame evaluation failure"
            if _MANAGER.status().get("poisoned"):
                _MANAGER.poisoned_reason = detail
                _MANAGER.guard()
            raise RTXVideoBridgeError(f"RTX Video CUDA evaluation failed: {detail}")
        return self._record(result)

    def process_frame(self, pixels) -> bytearray:
        """Compatibility route for images/software decode: one upload/download."""
        self._check()
        source_owner, source_pointer = self._host_pointer(pixels, self.input_bytes)
        output = bytearray(self.output_bytes)
        _, destination_pointer = self._host_pointer(output, self.output_bytes)
        source, destination = FrameDescriptorV1.empty(), FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_HOST, self.input_format
        source.width, source.height, source.planes[0], source.strides[0] = self.width, self.height, source_pointer, self.width * 4
        destination.memory_type, destination.pixel_format = MEMORY_HOST, self.output_format
        destination.width, destination.height = self.output_width, self.output_height
        destination.planes[0] = destination_pointer
        destination.strides[0] = self.output_width * (8 if self.output_format == FORMAT_RGBA16F else 4)
        self._evaluate(source, destination, (source_owner, pixels, output))
        return output

    def _acquire_surface(self, pixel_format: int) -> RTXVideoCudaSurface:
        deadline, error = time.monotonic() + self.timeout, ctypes.create_string_buffer(4096)
        while True:
            self._check()
            with NGX_RUNTIME_LOCK:
                handle = int(self.library.rtxv_surface_acquire(
                    ctypes.c_void_p(self.handle), pixel_format, error, len(error)) or 0)
            if handle:
                descriptor = FrameDescriptorV1.empty()
                if not self.library.rtxv_surface_frame_desc(ctypes.c_void_p(handle), ctypes.byref(descriptor)):
                    self.library.rtxv_surface_release(ctypes.c_void_p(handle))
                    raise RTXVideoBridgeError("RTX Video returned an invalid CUDA surface descriptor.")
                return RTXVideoCudaSurface(self.library, handle, descriptor, self.ordinal)
            detail = error.value.decode("utf-8", "replace")
            if "pool is exhausted" not in detail:
                raise RTXVideoBridgeError(f"Could not acquire RTX Video CUDA output surface: {detail}")
            self.pool_waits += 1
            gc.collect()
            if time.monotonic() >= deadline:
                raise RTXVideoBridgeError("RTX Video CUDA output surface pool remained exhausted.")
            time.sleep(0.001)

    def process_cuda_frame(self, frame: Any, *, color_matrix: int = 1, color_range: int = 0,
                           color_primaries: int = 1, color_transfer: int = 0,
                           chroma_location: int = 0, rotation: int = 0,
                           output_p010: bool = False) -> tuple[Any, dict[str, Any]]:
        self._check()
        if str(getattr(getattr(frame, "format", None), "name", "")) != "cuda":
            raise RTXVideoBridgeError("RTX Video CUDA input must be a PyAV CUDA frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        pixel_format = {"nv12": FORMAT_NV12, "p010": FORMAT_P010, "p010le": FORMAT_P010}.get(sw_format)
        if pixel_format is None or len(frame.planes) < 2:
            raise RTXVideoBridgeError(f"Unsupported NVDEC surface format: {sw_format or 'unknown'}.")
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_CUDA, pixel_format
        source.width, source.height = int(frame.width), int(frame.height)
        source.planes[0], source.planes[1] = int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr)
        source.strides[0], source.strides[1] = int(frame.planes[0].line_size), int(frame.planes[1].line_size)
        source.color_matrix, source.color_range = int(color_matrix), int(color_range)
        source.color_primaries, source.color_transfer = int(color_primaries), int(color_transfer)
        source.rotation = int(rotation) % 360
        source.reserved[0] = int(chroma_location)
        surface = self._acquire_surface(FORMAT_P010 if output_p010 else FORMAT_NV12)
        try:
            details = self._evaluate(source, surface.descriptor, (frame, surface))
            output = surface.to_av_frame()
        except BaseException:
            surface.close()
            raise
        output.pts, output.time_base = getattr(frame, "pts", None), getattr(frame, "time_base", None)
        if getattr(frame, "duration", None) is not None:
            output.duration = frame.duration
        details.update(input_format=sw_format, output_format="p010le" if output_p010 else "nv12")
        return output, details

    def process_cuda_to_host_for_test(
        self, frame: Any, *, color_matrix: int = 1, color_range: int = 0,
        color_primaries: int = 1, color_transfer: int = 0,
        chroma_location: int = 0, rotation: int = 0,
    ) -> tuple[bytearray, dict[str, Any]]:
        """Evaluate a CUDA input into packed host output for lossless A/B tests.

        Production video uses :meth:`process_cuda_frame`; this explicit test
        route intentionally downloads the result so legacy and CUDA engines can
        be compared before a lossy delivery encode.
        """
        self._check()
        if str(getattr(getattr(frame, "format", None), "name", "")) != "cuda":
            raise RTXVideoBridgeError("RTX Video CUDA input must be a PyAV CUDA frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        pixel_format = {"nv12": FORMAT_NV12, "p010": FORMAT_P010,
                        "p010le": FORMAT_P010}.get(sw_format)
        if pixel_format is None or len(frame.planes) < 2:
            raise RTXVideoBridgeError(f"Unsupported NVDEC surface format: {sw_format or 'unknown'}.")
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_CUDA, pixel_format
        source.width, source.height = int(frame.width), int(frame.height)
        source.planes[0], source.planes[1] = (
            int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr))
        source.strides[0], source.strides[1] = (
            int(frame.planes[0].line_size), int(frame.planes[1].line_size))
        source.color_matrix, source.color_range = int(color_matrix), int(color_range)
        source.color_primaries, source.color_transfer = int(color_primaries), int(color_transfer)
        source.rotation, source.reserved[0] = int(rotation) % 360, int(chroma_location)
        output = bytearray(self.output_bytes)
        _, destination_pointer = self._host_pointer(output, self.output_bytes)
        destination = FrameDescriptorV1.empty()
        destination.memory_type, destination.pixel_format = MEMORY_HOST, self.output_format
        destination.width, destination.height = self.output_width, self.output_height
        destination.planes[0] = destination_pointer
        destination.strides[0] = self.output_width * (
            8 if self.output_format == FORMAT_RGBA16F else 4)
        details = self._evaluate(source, destination, (frame, output))
        details.update(input_format=sw_format, output_format={
            FORMAT_RGBA8: "rgba8", FORMAT_R10: "r10", FORMAT_RGBA16F: "rgba16f",
        }[self.output_format])
        return output, details

    def _planar_destination(self, pixel_format: int, plane_pointers: tuple[int, ...],
                            strides: tuple[int, ...]) -> FrameDescriptorV1:
        required = 3 if pixel_format == FORMAT_YUV422P10 else 2
        if pixel_format not in {FORMAT_NV12, FORMAT_P010, FORMAT_YUV422P10}:
            raise ValueError("Pinned host output must be NV12, P010, or YUV422P10.")
        if len(plane_pointers) != required or len(strides) != required:
            raise ValueError(f"Pinned host output requires {required} planes.")
        if any(int(pointer) <= 0 for pointer in plane_pointers) or any(int(stride) <= 0 for stride in strides):
            raise ValueError("Pinned host output planes and strides must be positive.")
        destination = FrameDescriptorV1.empty()
        destination.memory_type, destination.pixel_format = MEMORY_HOST, int(pixel_format)
        destination.width, destination.height = self.output_width, self.output_height
        for index, (pointer, stride) in enumerate(zip(plane_pointers, strides)):
            destination.planes[index], destination.strides[index] = int(pointer), int(stride)
        return destination

    def process_cuda_to_host_planar(
        self, frame: Any, *, output_format: int,
        plane_pointers: tuple[int, ...], strides: tuple[int, ...],
        color_matrix: int = 1, color_range: int = 0,
        color_primaries: int = 1, color_transfer: int = 0,
        chroma_location: int = 0, rotation: int = 0,
    ) -> dict[str, Any]:
        """Evaluate a CUDA source into caller-owned pinned planar host memory."""
        self._check()
        if str(getattr(getattr(frame, "format", None), "name", "")) != "cuda":
            raise RTXVideoBridgeError("RTX Video CUDA input must be a PyAV CUDA frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        pixel_format = {"nv12": FORMAT_NV12, "p010": FORMAT_P010,
                        "p010le": FORMAT_P010}.get(sw_format)
        if pixel_format is None or len(frame.planes) < 2:
            raise RTXVideoBridgeError(f"Unsupported NVDEC surface format: {sw_format or 'unknown'}.")
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_CUDA, pixel_format
        source.width, source.height = int(frame.width), int(frame.height)
        source.planes[0], source.planes[1] = (
            int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr))
        source.strides[0], source.strides[1] = (
            int(frame.planes[0].line_size), int(frame.planes[1].line_size))
        source.color_matrix, source.color_range = int(color_matrix), int(color_range)
        source.color_primaries, source.color_transfer = int(color_primaries), int(color_transfer)
        source.rotation, source.reserved[0] = int(rotation) % 360, int(chroma_location)
        destination = self._planar_destination(output_format, plane_pointers, strides)
        details = self._evaluate(source, destination, (frame,))
        details.update(input_format=sw_format, output_format={
            FORMAT_NV12: "nv12", FORMAT_P010: "p010le", FORMAT_YUV422P10: "yuv422p10le",
        }[output_format])
        return details

    def process_host_to_host_planar(
        self, pixels: Any, *, output_format: int,
        plane_pointers: tuple[int, ...], strides: tuple[int, ...],
    ) -> dict[str, Any]:
        """One host upload and one pinned planar download for software decoders."""
        self._check()
        source_owner, source_pointer = self._host_pointer(pixels, self.input_bytes)
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_HOST, self.input_format
        source.width, source.height = self.width, self.height
        source.planes[0], source.strides[0] = source_pointer, self.width * 4
        destination = self._planar_destination(output_format, plane_pointers, strides)
        details = self._evaluate(source, destination, (source_owner, pixels))
        details.update(input_format="host-packed", output_format={
            FORMAT_NV12: "nv12", FORMAT_P010: "p010le", FORMAT_YUV422P10: "yuv422p10le",
        }[output_format])
        return details

    def convert_cuda_input_for_test(
        self, frame: Any, *, color_matrix: int = 1, color_range: int = 0,
        color_primaries: int = 1, color_transfer: int = 0,
        chroma_location: int = 0, rotation: int = 0,
    ) -> bytearray:
        """Return the production pre-NGX packed input for deterministic tests."""
        self._check()
        if str(getattr(getattr(frame, "format", None), "name", "")) != "cuda":
            raise RTXVideoBridgeError("RTX Video CUDA input must be a PyAV CUDA frame.")
        sw_format = str(getattr(getattr(frame, "sw_format", None), "name", ""))
        pixel_format = {"nv12": FORMAT_NV12, "p010": FORMAT_P010, "p010le": FORMAT_P010}.get(sw_format)
        if pixel_format is None or len(frame.planes) < 2:
            raise RTXVideoBridgeError(f"Unsupported NVDEC surface format: {sw_format or 'unknown'}.")
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_CUDA, pixel_format
        source.width, source.height = int(frame.width), int(frame.height)
        source.planes[0], source.planes[1] = int(frame.planes[0].buffer_ptr), int(frame.planes[1].buffer_ptr)
        source.strides[0], source.strides[1] = int(frame.planes[0].line_size), int(frame.planes[1].line_size)
        source.color_matrix, source.color_range = int(color_matrix), int(color_range)
        source.color_primaries, source.color_transfer = int(color_primaries), int(color_transfer)
        source.rotation, source.reserved[0] = int(rotation) % 360, int(chroma_location)
        output = bytearray(self.input_bytes)
        _, pointer = self._host_pointer(output, self.input_bytes)
        destination = FrameDescriptorV1.empty()
        destination.memory_type, destination.pixel_format = MEMORY_HOST, self.input_format
        destination.width, destination.height = self.width, self.height
        destination.planes[0], destination.strides[0] = pointer, self.width * 4
        error = ctypes.create_string_buffer(4096)
        ok = _MANAGER.call(
            "input-conversion validation",
            lambda: self.library.rtxv_convert_input_v1(
                ctypes.c_void_p(self.handle), ctypes.byref(source), ctypes.byref(destination),
                error, len(error),
            ),
            (frame, output, source, destination, error), self.timeout,
        )
        if not ok:
            detail = error.value.decode("utf-8", "replace") or "unknown input-conversion failure"
            raise RTXVideoBridgeError(f"RTX Video CUDA input conversion failed: {detail}")
        return output

    def process_host_to_cuda_frame(self, pixels: Any, *, output_p010: bool = False,
                                   pts: int | None = None, time_base: Any = None,
                                   duration: int | None = None) -> tuple[Any, dict[str, Any]]:
        """Upload a normalized software-decoded frame once and return CUDA YUV."""
        self._check()
        source_owner, source_pointer = self._host_pointer(pixels, self.input_bytes)
        source = FrameDescriptorV1.empty()
        source.memory_type, source.pixel_format = MEMORY_HOST, self.input_format
        source.width, source.height = self.width, self.height
        source.planes[0], source.strides[0] = source_pointer, self.width * 4
        surface = self._acquire_surface(FORMAT_P010 if output_p010 else FORMAT_NV12)
        try:
            details = self._evaluate(source, surface.descriptor, (source_owner, pixels, surface))
            output = surface.to_av_frame()
        except BaseException:
            surface.close()
            raise
        output.pts = pts
        if time_base is not None:
            output.time_base = time_base
        if duration is not None:
            output.duration = duration
        details.update(input_format="host-r10" if self.input_format == FORMAT_R10 else "host-rgba8",
                       output_format="p010le" if output_p010 else "nv12")
        return output, details

    def structured_status(self, *, decode_backend: str = "software", encode_backend: str = "") -> dict[str, Any]:
        return {
            **_MANAGER.status(),
            "memory_path": "cuda_zero_copy" if self.upload_bytes == 0 and self.download_bytes == 0 else "host_staging",
            "decode_backend": decode_backend, "encode_backend": encode_backend,
            "frames": self.completed_frames,
            "transfers": {"upload_bytes": self.upload_bytes, "download_bytes": self.download_bytes},
            "last_timing": dict(self.last_timing),
            "surface_pool": {"capacity": 8, "backpressure_waits": self.pool_waits},
        }

    def close(self, *, abort: bool = False) -> None:
        del abort
        if self.closed:
            return
        self.closed = True
        handle, self.handle = self.handle, 0
        with NGX_RUNTIME_LOCK:
            self.library.rtxv_session_release(ctypes.c_void_p(handle))

    def __enter__(self) -> "RTXVideoSession":
        return self

    def __exit__(self, exc_type, *_args) -> None:
        self.close(abort=exc_type is not None)
