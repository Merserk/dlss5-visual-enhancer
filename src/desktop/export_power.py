"""Request worker release before Windows freezes the app for system sleep."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QAbstractNativeEventFilter


class ExportPowerFilter(QAbstractNativeEventFilter):
    def __init__(self, bridge):
        super().__init__()
        self.bridge = bridge

    def nativeEventFilter(self, event_type, message):
        if sys.platform == "win32":
            from .win_frameless import _MSG
            notification = ctypes.cast(int(message), ctypes.POINTER(_MSG)).contents
            if notification.message == 0x0218 and notification.wParam in (0x0000, 0x0004):
                # TerminateJobObject is requested synchronously; the render
                # controller completes cleanup/checkpointing on its thread.
                self.bridge.suspendProcessing()
        return False, 0
