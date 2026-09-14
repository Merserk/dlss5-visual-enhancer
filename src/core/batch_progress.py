"""Structured, monotonic batch progress shared by processors and the UI."""
from __future__ import annotations

import math
import logging
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable

from .i18n import batch_state_label, t

TERMINAL = {"Complete", "Failed", "Cancelled", "Skipped"}


@dataclass(frozen=True, slots=True)
class BatchItemUpdate:
    index: int | None
    state: str
    progress: float = 0.0
    detail: str = ""
    output_path: str = ""
    elapsed_seconds: float = 0.0
    manifest_path: str = ""


@dataclass(slots=True)
class BatchItem:
    index: int
    input_path: str
    state: str = "Queued"
    progress: float = 0.0
    detail: str = ""
    output_path: str = ""
    elapsed_seconds: float = 0.0
    started: float | None = None


class BatchProgress:
    def __init__(self, paths, callback: Callable[[BatchItemUpdate], None] | None = None, progress=None):
        self.items = [BatchItem(i, str(path)) for i, path in enumerate(paths)]
        self.callback = callback
        self.legacy_progress = progress
        self.started = time.monotonic()
        self.finished: float | None = None
        self.state = "Running"
        self.detail = ""
        self.manifest_path = ""
        self.lock = threading.RLock()

    def apply(self, update: BatchItemUpdate) -> None:
        with self.lock:
            if self.finished is not None:
                return
            if update.index is None:
                self.finished = time.monotonic()
                self.state, self.detail = update.state, update.detail
                self.manifest_path = update.manifest_path
                return
            item = self.items[update.index]
            if item.state in TERMINAL:
                return
            if update.state == "Running" and item.started is None:
                item.started = time.monotonic()
            value = update.progress if math.isfinite(update.progress) else item.progress
            item.progress = (1.0 if update.state == "Complete" else
                             0.0 if update.state == "Skipped" else
                             min(.99, max(item.progress, value)))
            item.state = update.state
            item.detail = update.detail
            item.output_path = update.output_path or item.output_path
            item.elapsed_seconds = max(item.elapsed_seconds, update.elapsed_seconds)

    def emit(self, update: BatchItemUpdate) -> None:
        self.apply(update)
        if self.callback:
            try:
                self.callback(update)
            except Exception:
                logging.getLogger(__name__).exception("Batch progress listener failed; rendering continues")
                self.callback = None
        if self.legacy_progress:
            try:
                self.legacy_progress(self.statistics()["progress"], update.detail or update.state)
            except Exception:
                self.legacy_progress = None

    def advance(self, index: int, value: float = 0.0, detail: str | None = None) -> None:
        if detail is None:
            detail = t("batch.detail.preparing")
        self.emit(BatchItemUpdate(index, "Running", value, detail, elapsed_seconds=self.elapsed(index)))

    def elapsed(self, index: int) -> float:
        item = self.items[index]
        return time.monotonic() - item.started if item.started is not None else 0.0

    def complete(self, index: int, output_path: str, detail: str | None = None) -> None:
        if detail is None:
            detail = t("batch.detail.saved_verified")
        self.emit(BatchItemUpdate(index, "Complete", 1.0, detail, str(output_path), self.elapsed(index)))

    def fail(self, index: int, error, *, cancelled: bool = False) -> None:
        self.emit(BatchItemUpdate(index, "Cancelled" if cancelled else "Failed",
                                 self.items[index].progress, str(error), elapsed_seconds=self.elapsed(index)))

    def skip_from(self, index: int, detail: str | None = None) -> None:
        if detail is None:
            detail = t("batch.detail.cancelled_before_rendering")
        for item in self.items[index:]:
            if item.state == "Queued":
                self.emit(BatchItemUpdate(item.index, "Skipped", detail=detail))

    def finish(self, *, cancelled: bool = False, error: str = "", manifest_path: str = "") -> None:
        if self.finished is not None:
            return
        if cancelled or error:
            for item in self.items:
                if item.state == "Running":
                    self.fail(item.index, error or t("batch.detail.stopped_by_user"), cancelled=cancelled)
            self.skip_from(
                0,
                t("batch.detail.cancelled_before_rendering")
                if cancelled
                else t("batch.detail.batch_could_not_continue"),
            )
        failures = sum(item.state == "Failed" for item in self.items)
        successes = sum(item.state == "Complete" for item in self.items)
        state = ("Cancelled" if cancelled else "Failed" if error or (failures and not successes)
                 else "Complete with errors" if failures else "Complete")
        self.emit(BatchItemUpdate(None, state, detail=error, manifest_path=manifest_path))

    def statistics(self) -> dict:
        with self.lock:
            counts = {state.lower(): sum(item.state == state for item in self.items)
                      for state in ("Queued", "Running", "Complete", "Failed", "Cancelled", "Skipped")}
            progress = sum(1.0 if item.state in {"Complete", "Failed"} else item.progress
                           for item in self.items) / max(1, len(self.items))
            if self.finished is None:
                progress = min(.99, progress)
            return {"state": self.state, "total": len(self.items), **counts,
                    "progress": progress, "elapsed_seconds": (self.finished or time.monotonic()) - self.started}

    def diagnostics(self, *, final: bool = False) -> dict:
        with self.lock:
            statistics = self.statistics()
            if final:
                statistics["state"] = ("Cancelled" if statistics["cancelled"] or statistics["skipped"] else
                                       "Failed" if statistics["failed"] and not statistics["complete"] else
                                       "Complete with errors" if statistics["failed"] else "Complete")
                if statistics["state"] != "Cancelled":
                    statistics["progress"] = 1.0
            return {"items": [{k: v for k, v in asdict(item).items() if k != "started"} for item in self.items],
                    "statistics": statistics}

    def display(self, output_dir: str = "") -> tuple[list[list[str]], str]:
        with self.lock:
            items = [replace(item) for item in self.items]
            stats = self.statistics()
            detail, manifest = self.detail, self.manifest_path
        rows = [[Path(item.input_path).name, batch_state_label(item.state),
                 f"{min(99, int(item.progress * 100)) if item.state != 'Complete' else 100}%",
                 f"{self.elapsed(item.index) if item.state == 'Running' else item.elapsed_seconds:.1f}s",
                 item.output_path, (item.detail if len(item.detail) <= 600 else
                                    item.detail[:600] + t("batch.detail.full_report"))] for item in items]
        current = next((f"{item.index + 1}/{len(items)} — {Path(item.input_path).name}" for item in items
                        if item.state == "Running"), t("batch.current.none"))
        status = t(
            "batch.status.summary",
            state=batch_state_label(stats["state"]),
            progress=f"{stats['progress']:.0%}",
            total=stats["total"],
            complete=stats["complete"],
            failed=stats["failed"],
            cancelled=stats["cancelled"],
            skipped=stats["skipped"],
            queued=stats["queued"],
            current=current,
            elapsed=stats["elapsed_seconds"],
        )
        if output_dir:
            status += "\n" + t("batch.status.output_folder", path=output_dir)
        if detail:
            status += f"\n{detail}"
        if manifest:
            status += "\n" + t("batch.status.report", path=manifest)
        return rows, status
