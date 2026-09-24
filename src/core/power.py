"""Windows power-management helpers for notebook GPUs.

Laptops (for example RTX 50-series Laptop GPUs behind NVIDIA Optimus) are
aggressive about saving power: Windows 11 applies EcoQoS to processes whose
window is not in the foreground, which slows the CPU threads that decode,
stage, and encode frames for the GPU, and an idle-sleep timer can suspend the
machine in the middle of a long render. These helpers opt the application out
of both while it is doing GPU work. Every function is a no-op elsewhere and
never raises.
"""

from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager
from typing import Iterator

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("ControlMask", ctypes.c_ulong),
        ("StateMask", ctypes.c_ulong),
    ]


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def disable_power_throttling() -> bool:
    """Opt this process out of EcoQoS execution-speed throttling."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        state = _PowerThrottlingState(
            _PROCESS_POWER_THROTTLING_CURRENT_VERSION,
            _PROCESS_POWER_THROTTLING_EXECUTION_SPEED,
            0,  # ControlMask set + StateMask clear = never throttle.
        )
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
        ]
        kernel32.SetProcessInformation.restype = ctypes.c_int
        return bool(kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING,
            ctypes.byref(state), ctypes.sizeof(state),
        ))
    except Exception:
        return False


def on_battery() -> bool | None:
    """Return True on battery, False on AC power, None when unknown."""
    if sys.platform != "win32":
        return None
    try:
        status = _SystemPowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return None
        if status.ACLineStatus == 0:
            return True
        if status.ACLineStatus == 1:
            return False
    except Exception:
        pass
    return None


@contextmanager
def keep_system_awake() -> Iterator[None]:
    """Prevent idle sleep on the calling thread for the duration of a render."""
    set_state = None
    if sys.platform == "win32":
        try:
            set_state = ctypes.windll.kernel32.SetThreadExecutionState
            set_state.argtypes = [ctypes.c_uint32]
            set_state.restype = ctypes.c_uint32
            set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        except Exception:
            set_state = None
    try:
        yield
    finally:
        if set_state is not None:
            try:
                set_state(_ES_CONTINUOUS)
            except Exception:
                pass
