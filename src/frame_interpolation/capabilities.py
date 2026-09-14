from __future__ import annotations

import ctypes
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from ..core.gpu_selection import detect_gpu
from .models import FrameInterpolationCapabilities
from .native import BRIDGE, BRIDGE_ABI_VERSION, RUNTIME_DIR, initialize_bridge

DLSSG_RUNTIME = RUNTIME_DIR / "nvngx_dlssg.dll"


def _hags_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers") as key:
            value, _kind = winreg.QueryValueEx(key, "HwSchMode")
        return int(value) == 2
    except (OSError, ValueError):
        return False


def _authenticode_status(path: Path) -> str:
    if os.name != "nt":
        return "Unavailable"
    escaped = str(path.resolve()).replace("'", "''")
    try:
        process = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Import-Module \"$env:WINDIR\\System32\\WindowsPowerShell\\v1.0\\Modules\\"
             "Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1\"; "
             f"[string](Get-AuthenticodeSignature -LiteralPath '{escaped}').Status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "Unavailable"
    return process.stdout.strip() if process.returncode == 0 else "Unavailable"


def _nvof_present() -> bool:
    try:
        ctypes.WinDLL("nvofapi64.dll")
        return True
    except (AttributeError, OSError):
        return False


@lru_cache(maxsize=32)
def probe_frame_interpolation_capabilities(
        ai_gpu_uuid: str = "auto") -> FrameInterpolationCapabilities:
    gpu = detect_gpu(ai_gpu_uuid)
    gpu_name = str(gpu.get("display_name") or gpu.get("name") or "NVIDIA RTX GPU")
    driver = str(gpu.get("driver") or "unknown")
    hags, nvof = _hags_enabled(), _nvof_present()
    signature_status = _authenticode_status(DLSSG_RUNTIME) if DLSSG_RUNTIME.is_file() else "Missing"
    missing = [path for path in (BRIDGE, DLSSG_RUNTIME) if not path.is_file()]
    status: dict = {}
    detail = ""
    if missing:
        detail = "Frame Interpolation runtime is missing: " + ", ".join(map(str, missing))
    else:
        try:
            status = initialize_bridge(int(gpu["cuda_ordinal"]))
        except Exception as exc:
            detail = str(exc)
    generated_max = max(0, int(status.get("multi_frame_count_max", 0)))
    available = bool(status.get("available")) and generated_max >= 1 and nvof
    notes: list[str] = []
    if not nvof:
        notes.append("NVIDIA Optical Flow is unavailable.")
    if not hags:
        notes.append("HAGS is disabled; DLSS Frame Generation may be rejected by the runtime.")
    if signature_status not in {"Valid", "Unavailable"}:
        notes.append(f"DLSSG Authenticode status is {signature_status} (diagnostic only).")
    detail = " ".join(part for part in (detail, *notes) if part)
    return FrameInterpolationCapabilities(
        available=available, gpu=gpu_name, driver=driver, hags_enabled=hags,
        native_generated_frame_max=generated_max,
        native_multiplier=generated_max + 1 if generated_max else 1,
        cascade_available=available and generated_max >= 1,
        runtime_version=str(status.get("runtime_version") or "unknown"),
        bridge_version=str(status.get("bridge_version") or "missing"),
        bridge_abi_version=int(status.get("abi_version") or BRIDGE_ABI_VERSION),
        nvof_available=nvof, cuda_interop=bool(status.get("cuda_interop")),
        signature_status=signature_status, detail=detail,
        gpu_uuid=str(gpu.get("uuid") or ai_gpu_uuid))


def clear_capability_cache() -> None:
    probe_frame_interpolation_capabilities.cache_clear()
