"""Debounced effect snapshots for the serialized in-process bridge."""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, fields

from ..core.jobs import Cancelled, JobController
from ..core.runtime import resolve_native_settings
from ..core.nr_control_mask import mask_selection, report_options
from ..core.ui_messages import UiMessage

EFFECT_DEBOUNCE_SECONDS = 0.5
EFFECT_REPLACEMENT_TIMEOUT = 15.0

@dataclass(frozen=True, slots=True)
class EffectSettings:
    nr_style: str
    nr_intensity: float
    nr_passes: int
    local_tone_strength: float
    local_structure_strength: float
    skin_structure_strength: float
    nr_mask: object | None
    automatic_mask: bool
    nr_optical_flow_quality: str = "High"

    @classmethod
    def from_options(cls, options) -> EffectSettings:
        values = {field.name: getattr(options, field.name) for field in fields(cls)}
        values["nr_mask"] = mask_selection(values["nr_mask"])
        result = cls(**values)
        resolve_native_settings(result)
        return result

@dataclass(frozen=True, slots=True)
class EffectRequest:
    revision: int
    settings: EffectSettings
    requested_at: float
    due_at: float

class EffectUpdates:
    """One pending update per session; no timer thread or FIFO of slider edits."""

    def __init__(self, options, *, clock=time.monotonic) -> None:
        self.initial = self.applied = self.requested = EffectSettings.from_options(options)
        self._clock = clock
        self._lock = threading.Lock()
        self._revision = self.applied_revision = 0
        self._pending: EffectRequest | None = None
        self._applying: EffectRequest | None = None
        self._accepting = True
        self._status = UiMessage("Initial settings")
        self._error = ""
        self._applied_at_pts: int | None = None
        self._successes = self._failures = 0
        self._history: deque[dict] = deque(maxlen=128)

    def submit(self, options) -> bool:
        settings = EffectSettings.from_options(options)
        with self._lock:
            if settings == self.requested:
                return False
            self.requested = settings
            if not self._accepting:
                self._status = UiMessage("Processing finished or stopped; changes apply on the next Start.")
                return False
            self._revision += 1
            if settings == self.applied and self._applying is None:
                self._pending = None
                return False
            now = self._clock()
            self._pending = EffectRequest(self._revision, settings, now, now + EFFECT_DEBOUNCE_SECONDS)
            return True

    def take_due(self) -> EffectRequest | None:
        with self._lock:
            if (not self._accepting or self._applying is not None or self._pending is None
                    or self._clock() < self._pending.due_at):
                return None
            self._applying, self._pending = self._pending, None
            return self._applying

    def complete(self, request: EffectRequest, *, pts: int, milliseconds: float,
                 error: str = "", restored: bool = True) -> None:
        with self._lock:
            changed = [field.name for field in fields(EffectSettings)
                       if getattr(self.applied, field.name) != getattr(request.settings, field.name)]
            self._history.append({"revision": request.revision, "settings": report_options(request.settings),
                "changed": changed, "pts": pts, "refresh_ms": milliseconds,
                "request_to_frame_ms": (self._clock() - request.requested_at) * 1000,
                 "error": error, "restored": bool(error) and restored})
            self._applying = None
            self._error = error
            if error:
                self._failures += 1
                self._status = (UiMessage("Update failed—previous settings restored") if restored
                                else UiMessage("Update failed; recovery failed"))
            else:
                self.applied = request.settings
                self.applied_revision = request.revision
                self._applied_at_pts = pts
                self._successes += 1
                self._status = UiMessage("Applied to processing")
            # An edit back to A while A->B was failing needs no second restart.
            if self._pending and self._pending.settings == self.applied:
                self._pending = None

    def finish(self) -> None:
        with self._lock:
            self._accepting = False
            if self._pending or self._applying:
                self._status = UiMessage("Processing finished or stopped; changes apply on the next Start.")
            self._pending = None

    def snapshot(self) -> dict:
        with self._lock:
            status = self._status
            if self._accepting and self._applying:
                status = UiMessage("Applying; newer changes pending") if self._pending else UiMessage("Applying")
            elif self._accepting and self._pending:
                status = UiMessage("Pending")
            return {"effects_status": status, "effects_error": self._error,
                    "pending_revision": self._pending.revision if self._pending else None,
                    "applied_revision": self.applied_revision, "applied_at_pts": self._applied_at_pts,
                    "effect_updates": self._successes, "effect_update_failures": self._failures}

    def report(self) -> dict:
        with self._lock:
            return {"initial": report_options(self.initial), "applied": report_options(self.applied),
                    "requested": report_options(self.requested), "requests": self._revision,
                    "applied_count": self._successes, "failed_count": self._failures,
                    "history": list(self._history)}

class NativeDeadline(JobController):
    """Bound an effect-session replacement; Stop still cancels the whole job."""

    def __init__(self, parent: JobController, seconds: float) -> None:
        super().__init__()
        self.parent = parent
        self.cancel = parent.cancel
        self.seconds = seconds
        self.expired = threading.Event()
        self._timer = threading.Timer(seconds, self._expire)
        self._timer.daemon = True

    def register(self, process: subprocess.Popen) -> None:
        super().register(process)
        self.parent.register(process)
        if self.expired.is_set() and process.poll() is None:
            process.kill()

    def unregister(self, process: subprocess.Popen) -> None:
        super().unregister(process)
        self.parent.unregister(process)

    def _expire(self) -> None:
        self.expired.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass

    def __enter__(self) -> NativeDeadline:
        if self.cancel.is_set():
            raise Cancelled("Live stopped.")
        self._timer.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._timer.cancel()
        self._timer.join()
        if exc_type or self.expired.is_set() or self.cancel.is_set():
            # Also cover constructors that failed before returning an object.
            with self._lock:
                processes = list(self._processes)
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                self.unregister(process)
            if self.cancel.is_set():
                raise Cancelled("Live stopped.") from exc
            if self.expired.is_set():
                raise TimeoutError(f"Bridge initialization and verification exceeded {self.seconds:g} seconds.") from exc
