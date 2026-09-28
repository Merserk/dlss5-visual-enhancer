from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any


# Laptop dGPUs (NVIDIA Optimus / Advanced Optimus) are often powered down while
# idle. The first nvidia-smi query has to wake the GPU from D3cold, which can
# take well over ten seconds on some notebooks, so detection gets a longer
# budget than a desktop card would need.
NVIDIA_SMI_TIMEOUT_SECONDS = 45
# GPUs at or below this frame-buffer size are reported as low-VRAM so the UI
# can steer users toward settings that fit (e.g. 8 GB RTX 5050 Laptop GPU).
LOW_VRAM_THRESHOLD_MB = 8 * 1024 + 512
_LAPTOP_NAME_MARKERS = ("LAPTOP", "MOBILE", "MAX-Q", "NOTEBOOK")


def is_laptop_gpu_name(name: str) -> bool:
    upper = str(name or "").upper()
    return any(marker in upper for marker in _LAPTOP_NAME_MARKERS)


def _nvidia_smi_executable() -> str:
    """Locate nvidia-smi even when it is not on the (embedded) PATH."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    candidates: list[Path] = []
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        candidates.append(Path(system_root) / "System32" / "nvidia-smi.exe")
    program_files = os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles")
    if program_files:
        candidates.append(Path(program_files) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe")
    if system_root:
        # DCH drivers install nvidia-smi into the driver store.
        store = Path(system_root) / "System32" / "DriverStore" / "FileRepository"
        try:
            candidates.extend(sorted(store.glob("nv*.inf_amd64_*/nvidia-smi.exe"), reverse=True))
        except OSError:
            pass
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return "nvidia-smi"


def _classify(device: dict[str, Any]) -> dict[str, Any]:
    """Attach laptop / low-VRAM hints (and the VRAM key the desktop UI reads)."""
    memory_mb = int(device.get("memory_mb", 0) or 0)
    device["memory_total_mb"] = memory_mb
    device["is_laptop"] = is_laptop_gpu_name(str(device.get("name", "")))
    device["low_vram"] = 0 < memory_mb <= LOW_VRAM_THRESHOLD_MB
    return device


def _normalize_pci_bus_id(value: str) -> str:
    match = re.fullmatch(
        r"(?:([0-9A-Fa-f]{4,8}):)?([0-9A-Fa-f]{2}):([0-9A-Fa-f]{2})\.([0-7])",
        value.strip(),
    )
    if match is None:
        return value.strip().upper()
    domain = int(match.group(1) or "0", 16)
    return f"{domain:04X}:{match.group(2).upper()}:{match.group(3).upper()}.{match.group(4)}"


def _cuda_device_identities() -> dict[str, dict[str, Any]]:
    """Map normalized PCI bus IDs to CUDA ordinals for optional NVENC selection."""
    try:
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        cuda = loader("nvcuda.dll")
    except (AttributeError, OSError):
        return {}

    c_int_p = ctypes.POINTER(ctypes.c_int)
    cuda.cuInit.argtypes = [ctypes.c_uint]
    cuda.cuInit.restype = ctypes.c_int
    cuda.cuDeviceGetCount.argtypes = [c_int_p]
    cuda.cuDeviceGetCount.restype = ctypes.c_int
    cuda.cuDeviceGet.argtypes = [c_int_p, ctypes.c_int]
    cuda.cuDeviceGet.restype = ctypes.c_int
    cuda.cuDeviceGetPCIBusId.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    cuda.cuDeviceGetPCIBusId.restype = ctypes.c_int
    if cuda.cuInit(0) != 0:
        return {}
    count = ctypes.c_int()
    if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
        return {}
    identities: dict[str, dict[str, Any]] = {}
    for ordinal in range(max(0, count.value)):
        device = ctypes.c_int()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        bus_buffer = ctypes.create_string_buffer(32)
        if cuda.cuDeviceGetPCIBusId(bus_buffer, len(bus_buffer), device.value) != 0:
            continue
        identities[_normalize_pci_bus_id(bus_buffer.value.decode("ascii", "replace"))] = {
            "cuda_ordinal": ordinal,
        }
    return identities


def _cuda_driver_gpus() -> tuple[dict[str, Any], ...]:
    """Enumerate NVIDIA GPUs through nvcuda.dll when nvidia-smi is unusable."""
    try:
        loader = getattr(ctypes, "WinDLL", ctypes.CDLL)
        cuda = loader("nvcuda.dll")
    except (AttributeError, OSError):
        return ()
    c_int_p = ctypes.POINTER(ctypes.c_int)
    try:
        cuda.cuInit.argtypes = [ctypes.c_uint]
        cuda.cuDeviceGetCount.argtypes = [c_int_p]
        cuda.cuDeviceGet.argtypes = [c_int_p, ctypes.c_int]
        cuda.cuDeviceGetName.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        cuda.cuDeviceTotalMem_v2.argtypes = [ctypes.POINTER(ctypes.c_size_t), ctypes.c_int]
        cuda.cuDeviceGetPCIBusId.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        cuda.cuDriverGetVersion.argtypes = [c_int_p]
        get_uuid = getattr(cuda, "cuDeviceGetUuid_v2", None) or getattr(cuda, "cuDeviceGetUuid", None)
        if get_uuid is not None:
            get_uuid.argtypes = [ctypes.c_char_p, ctypes.c_int]
    except AttributeError:
        return ()
    if cuda.cuInit(0) != 0:
        return ()
    count = ctypes.c_int()
    if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0:
        return ()
    driver_version = ctypes.c_int()
    driver = "unknown"
    if cuda.cuDriverGetVersion(ctypes.byref(driver_version)) == 0 and driver_version.value:
        driver = f"CUDA {driver_version.value // 1000}.{(driver_version.value % 1000) // 10}"
    devices: list[dict[str, Any]] = []
    for ordinal in range(max(0, count.value)):
        device = ctypes.c_int()
        if cuda.cuDeviceGet(ctypes.byref(device), ordinal) != 0:
            continue
        name_buffer = ctypes.create_string_buffer(256)
        if cuda.cuDeviceGetName(name_buffer, len(name_buffer), device.value) != 0:
            continue
        name = name_buffer.value.decode("utf-8", "replace").strip() or "NVIDIA GPU"
        total = ctypes.c_size_t()
        memory_mb = 0
        if cuda.cuDeviceTotalMem_v2(ctypes.byref(total), device.value) == 0:
            memory_mb = int(total.value // (1024 * 1024))
        bus_buffer = ctypes.create_string_buffer(32)
        pci = ""
        if cuda.cuDeviceGetPCIBusId(bus_buffer, len(bus_buffer), device.value) == 0:
            pci = _normalize_pci_bus_id(bus_buffer.value.decode("ascii", "replace"))
        uuid = f"cuda:{ordinal}"
        if get_uuid is not None:
            raw = ctypes.create_string_buffer(16)
            if get_uuid(raw, device.value) == 0:
                h = bytes(raw.raw).hex()
                uuid = f"GPU-{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        is_rtx = "RTX" in name.upper()
        devices.append(_classify({
            "index": ordinal,
            "uuid": uuid,
            "pci_bus_id": pci,
            "name": name,
            "display_name": name,
            "driver": driver,
            "memory_mb": memory_mb,
            "beta": False,
            "ai_compatible": is_rtx,
            "compatibility_error": "" if is_rtx else "The device name does not identify an RTX GPU.",
            "cuda_ordinal": ordinal,
        }))
    return tuple(devices)


@lru_cache(maxsize=1)
def detect_gpus() -> tuple[dict[str, Any], ...]:
    try:
        return _detect_gpus_nvidia_smi()
    except RuntimeError as smi_error:
        # nvidia-smi can be missing from PATH, blocked, or time out while a
        # laptop dGPU wakes up. The CUDA driver API gives the same identity.
        fallback = _cuda_driver_gpus()
        if fallback:
            return fallback
        raise smi_error


def _detect_gpus_nvidia_smi() -> tuple[dict[str, Any], ...]:
    command = [
        _nvidia_smi_executable(),
        "--query-gpu=index,uuid,pci.bus_id,name,driver_version,memory.total",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=NVIDIA_SMI_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "NVIDIA driver tools are unavailable; an RTX GPU and current driver are required."
        ) from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        message = "nvidia-smi failed while detecting an RTX GPU"
        raise RuntimeError(f"{message}: {detail}" if detail else f"{message}.")
    cuda_identities = _cuda_device_identities()
    devices: list[dict[str, Any]] = []
    for fallback_index, line in enumerate(result.stdout.splitlines()):
        parts = [part.strip() for part in line.split(",")]
        if not parts or not any(parts):
            continue
        if len(parts) == 4:
            legacy_row = True
            name, driver, memory, _legacy_capability = parts
            index, uuid, pci_bus_id = str(fallback_index), f"index:{fallback_index}", ""
        elif len(parts) == 6:
            legacy_row = False
            index, uuid, pci_bus_id, name, driver, memory = parts
        else:
            name = parts[3] if len(parts) > 3 else parts[0] or "NVIDIA GPU"
            raise RuntimeError(
                f"{name} returned incomplete nvidia-smi data; expected name, driver, "
                "memory, UUID, and PCI bus ID."
            )
        if any(not value for value in (name, driver, memory)):
            raise RuntimeError(
                f"{name or 'NVIDIA GPU'} returned incomplete nvidia-smi data; expected name, "
                "driver, and memory capacity."
            )
        if memory.strip().upper() in {"[N/A]", "N/A"}:
            # Some notebook drivers cannot report frame-buffer size while the
            # dGPU is in a low-power state; treat it as unknown, not fatal.
            memory = "0"
        try:
            memory_mb = int(float(memory))
            smi_index = int(index)
        except ValueError as exc:
            raise RuntimeError(
                f"{name} reported malformed index or memory capacity through nvidia-smi."
            ) from exc
        normalized_pci = _normalize_pci_bus_id(pci_bus_id) if pci_bus_id else ""
        identity = cuda_identities.get(normalized_pci, {})
        is_rtx = "RTX" in name.upper()
        compatibility_error = ""
        if not is_rtx:
            compatibility_error = "The device name does not identify an RTX GPU."
        devices.append(_classify(
            {
                "index": smi_index,
                "uuid": uuid,
                "pci_bus_id": normalized_pci,
                "name": name,
                "display_name": name,
                "driver": driver,
                "memory_mb": memory_mb,
                "beta": False,
                "ai_compatible": is_rtx,
                "compatibility_error": compatibility_error,
                "cuda_ordinal": (
                    smi_index if legacy_row else identity.get("cuda_ordinal", smi_index)
                ),
            }
        ))
    if not devices:
        raise RuntimeError("No NVIDIA GPU was detected.")
    return tuple(devices)


def clear_gpu_detection_cache() -> None:
    detect_gpus.cache_clear()
