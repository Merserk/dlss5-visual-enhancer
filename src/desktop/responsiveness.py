"""Capture local thread stacks if the desktop event loop stops responding."""
from __future__ import annotations

import faulthandler
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Slot


class UiResponsivenessMonitor(QObject):
    def __init__(self, report_path: Path, *, context: Callable[[], str],
                 stall_seconds: float = 10.0, heartbeat_ms: int = 500,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._report_path = Path(report_path)
        self._context = context
        self._stall_seconds = stall_seconds
        self._last_heartbeat = time.monotonic()
        self._context_snapshot = ""
        self._stop = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(heartbeat_ms)
        self._timer.timeout.connect(self._heartbeat)
        self._heartbeat()
        self._timer.start()
        self._thread = threading.Thread(target=self._watch, name="ui-responsiveness", daemon=True)
        self._thread.start()

    @Slot()
    def _heartbeat(self) -> None:
        # Read Qt/application state only on the GUI thread. The watcher uses
        # this plain-text snapshot and never calls Qt or the session logger.
        self._context_snapshot = str(self._context())
        self._last_heartbeat = time.monotonic()

    def _watch(self) -> None:
        reported_heartbeat = None
        while not self._stop.wait(min(0.5, self._stall_seconds / 4)):
            heartbeat = self._last_heartbeat
            stalled_for = time.monotonic() - heartbeat
            if stalled_for < self._stall_seconds or heartbeat == reported_heartbeat:
                continue
            reported_heartbeat = heartbeat
            try:
                self._report_path.parent.mkdir(parents=True, exist_ok=True)
                with self._report_path.open("a", encoding="utf-8", errors="replace") as report:
                    report.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                                 f"GUI heartbeat absent for {stalled_for:.1f}s\n"
                                 f"{self._context_snapshot}\n")
                    report.flush()
                    faulthandler.dump_traceback(file=report, all_threads=True)
            except (OSError, RuntimeError):
                # Diagnostics must never disrupt the application.
                pass

    @Slot()
    def stop(self) -> None:
        self._timer.stop()
        self._stop.set()
        # Do not wait for a diagnostic write on the GUI thread.
