from __future__ import annotations

"""Sample total adapter VRAM during a render through NVIDIA's NVML API."""

import ctypes
import threading


class _MemoryInfo(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


class GPUVramTracker:
    """Observe device use; the delta includes other processes on the adapter."""

    def __init__(self, index: int) -> None:
        self._nvml = None
        self._device = ctypes.c_void_p()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._baseline = 0
        self._peak = 0
        self._samples = 0
        try:
            nvml = ctypes.WinDLL("nvml.dll")
            nvml.nvmlInit_v2.restype = ctypes.c_int
            nvml.nvmlShutdown.restype = ctypes.c_int
            nvml.nvmlDeviceGetHandleByIndex_v2.argtypes = [
                ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)
            ]
            nvml.nvmlDeviceGetHandleByIndex_v2.restype = ctypes.c_int
            nvml.nvmlDeviceGetMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(_MemoryInfo)
            ]
            nvml.nvmlDeviceGetMemoryInfo.restype = ctypes.c_int
            if nvml.nvmlInit_v2():
                return
            if nvml.nvmlDeviceGetHandleByIndex_v2(int(index), ctypes.byref(self._device)):
                nvml.nvmlShutdown()
                return
            self._nvml = nvml
            self._sample()
            self._thread = threading.Thread(
                target=self._run, name="dlss5-vram-sampler", daemon=True
            )
            self._thread.start()
        except (AttributeError, OSError):
            self._nvml = None

    def _sample(self) -> None:
        if self._nvml is None:
            return
        info = _MemoryInfo()
        if self._nvml.nvmlDeviceGetMemoryInfo(self._device, ctypes.byref(info)):
            return
        with self._lock:
            used = int(info.used)
            if not self._samples:
                self._baseline = used
            self._peak = max(self._peak, used)
            self._samples += 1

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            self._sample()

    def snapshot(self) -> dict[str, object]:
        self._sample()
        with self._lock:
            if not self._samples:
                return {"status": "unavailable"}
            mib = 1024 * 1024
            return {
                "status": "sampled",
                "scope": "total_adapter_used_memory",
                "baseline_mib": round(self._baseline / mib, 1),
                "peak_mib": round(self._peak / mib, 1),
                "peak_delta_mib": round(max(0, self._peak - self._baseline) / mib, 1),
                "samples": self._samples,
            }

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            self._thread = None
        self._sample()
        if self._nvml is not None:
            self._nvml.nvmlShutdown()
            self._nvml = None
