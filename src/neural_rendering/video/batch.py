from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from ...core.batch_progress import BatchItemUpdate, BatchProgress
from ...core import app_log
from ...core.disk_paths import StreamingMediaArchive, prepare_output_dir
from ...core.jobs import Cancelled, JobController, active_job
from ...core.runtime import prepare_runtime
from .models import ConversionOptions, VideoBatchResult, VideoConversionFailure, VideoConversionSuccess
from . import processor


def convert_videos(
    input_paths: Iterable[str | os.PathLike[str]],
    options: ConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
    *, output_dir: str | os.PathLike[str] | None = None,
    controller: JobController | None = None,
    on_item_update: Callable[[BatchItemUpdate], None] | None = None,
    create_archive: bool = False, same_as_input: bool = False,
) -> VideoBatchResult:
    """Render in input order, publishing a completed row before starting the next file."""
    options = replace(options) if options else ConversionOptions()
    paths = [Path(path).resolve() for path in input_paths]
    if not paths:
        raise ValueError("Choose at least one video.")
    controller = controller or JobController()
    reporter = BatchProgress(paths, on_item_update, progress)
    successes, failures = [], []
    archive: StreamingMediaArchive | None = None
    archive_path: str | None = None
    archive_error = ""
    try:
        destination = prepare_output_dir(output_dir, default=processor.OUTPUTS) if not same_as_input or create_archive else None
        if create_archive and len(paths) > 1:
            archive = StreamingMediaArchive(
                destination, "DLSS5_VIDEO_BATCH", controller=controller
            )
        with active_job(controller):
            if controller.cancel.is_set():
                raise Cancelled("Stopped before rendering.")
            prepared = prepare_runtime()
            processor._BATCH_CONTEXT.controller = controller
            processor._BATCH_CONTEXT.prepared_runtime = prepared
            try:
                for index, path in enumerate(paths):
                    if controller.cancel.is_set():
                        break
                    reporter.advance(index)
                    try:
                        result = processor.convert_video(
                            path, options, lambda value, message, i=index: reporter.advance(i, value, message),
                            output_dir=prepare_output_dir(path.parent) if same_as_input else destination,
                        )
                    except Exception as exc:
                        cancelled = isinstance(exc, Cancelled) or controller.cancel.is_set()
                        failures.append(VideoConversionFailure(index, str(path), str(exc), cancelled))
                        reporter.fail(index, exc, cancelled=cancelled)
                        if cancelled:
                            controller.stop()
                            break
                    else:
                        successes.append(VideoConversionSuccess(index, str(path), result))
                        reporter.complete(index, result.output_path, f"{result.frames} frames; report: {result.report_path}")
                        if archive is not None and not archive.add(result.output_path):
                            if archive.error is not None and not archive_error:
                                archive_error = str(archive.error)
            finally:
                del processor._BATCH_CONTEXT.controller
                del processor._BATCH_CONTEXT.prepared_runtime
        cancelled = controller.cancel.is_set()
        if cancelled:
            for item in reporter.items:
                if item.state == "Queued":
                    failures.append(VideoConversionFailure(item.index, item.input_path, "Cancelled before rendering.", True))
            reporter.skip_from(0)
        if archive is not None:
            try:
                archive_path = archive.close(publish=len(successes) > 1)
            except Exception as exc:
                archive_error = str(exc)
                archive_path = None
        manifest = app_log.record_batch_summary(
            "video-batch", len(successes), len(failures), cancelled,
        )
        reporter.finish(cancelled=cancelled, manifest_path=manifest)
        return VideoBatchResult(
            successes, failures, cancelled, manifest, archive_path, archive_error
        )
    except BaseException as exc:
        if archive is not None:
            try:
                archive.abort()
            except Exception:
                pass
        reporter.finish(cancelled=controller.cancel.is_set(), error=str(exc))
        raise
