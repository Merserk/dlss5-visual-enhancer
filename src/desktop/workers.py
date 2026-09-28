from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from ..core.jobs import JobController, Cancelled, use_job_controller
from ..core.batch_progress import BatchItemUpdate


class WorkerSignals(QObject):
    """Signals for background task execution."""
    started = Signal()
    progress = Signal(float, str)
    itemUpdated = Signal(int, str, float, str, str, float)
    metadataReady = Signal(str, object)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class JobWorker(QRunnable):
    """Execute a cancellable task without blocking the Qt GUI thread.

    Backend jobs use ``controller``, ``progress`` and ``on_item_update`` callback
    names, while desktop-only helper jobs often do not.  Callback injection is
    therefore signature-aware instead of blindly passing keywords to every
    callable.
    """

    def __init__(
        self,
        task_fn: Callable[..., Any],
        *args: Any,
        controller: JobController | None = None,
        inject_callbacks: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.task_fn = task_fn
        self.args = args
        self.kwargs = kwargs
        self.controller = controller or JobController()
        self.inject_callbacks = inject_callbacks
        self.signals = WorkerSignals()
        self.setAutoDelete(True)

    def on_batch_item_update(self, update: BatchItemUpdate) -> None:
        idx = update.index if update.index is not None else -1
        self.signals.itemUpdated.emit(
            idx,
            update.state,
            update.progress,
            update.detail,
            update.output_path,
            update.elapsed_seconds,
        )

    def on_progress(self, fraction: float, message: str) -> None:
        self.signals.progress.emit(max(0.0, min(1.0, float(fraction))), str(message))

    def _accepted_kwargs(self) -> tuple[set[str], bool]:
        try:
            signature = inspect.signature(self.task_fn)
        except (TypeError, ValueError):
            return set(), True
        names: set[str] = set()
        var_kwargs = False
        for parameter in signature.parameters.values():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                var_kwargs = True
            elif parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                names.add(parameter.name)
        return names, var_kwargs

    @Slot()
    def run(self) -> None:
        self.signals.started.emit()
        try:
            with use_job_controller(self.controller):
                kwargs = dict(self.kwargs)
                if self.inject_callbacks:
                    accepted, var_kwargs = self._accepted_kwargs()
                    optional = {
                        "controller": self.controller,
                        "progress": self.on_progress,
                        "on_item_update": self.on_batch_item_update,
                    }
                    for name, value in optional.items():
                        if name not in kwargs and (var_kwargs or name in accepted):
                            kwargs[name] = value

                result = self.task_fn(*self.args, **kwargs)
                if self.controller.cancel.is_set():
                    self.signals.cancelled.emit()
                else:
                    self.signals.finished.emit(result)
        except Cancelled:
            self.signals.cancelled.emit()
        except Exception as exc:
            traceback.print_exc()
            self.signals.failed.emit(str(exc))
