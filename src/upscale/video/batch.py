from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ...core import app_log
from ...core.batch_progress import BatchProgress
from ...core.disk_paths import prepare_output_dir
from ...core.jobs import Cancelled, JobController, active_job
from .models import UpscaleOptions, UpscaleBatchResult, UpscaleSuccess, UpscaleFailure
from .native import probe_capabilities
from .processor import upscale_video


def upscale_videos(input_paths, options=None, progress=None, *, output_dir=None, controller=None,
                   on_item_update=None, same_as_input=False):
    options = replace(options) if options else UpscaleOptions()
    options.validate()
    paths = [Path(p).resolve() for p in input_paths]
    if not paths:
        raise ValueError("Choose at least one video.")
    controller = controller or JobController()
    reporter = BatchProgress(paths, on_item_update, progress)
    successes, failures = [], []
    try:
        destination = None if same_as_input else prepare_output_dir(output_dir)
        with active_job(controller):
            if controller.cancel.is_set():
                raise Cancelled("Stopped before rendering.")
            caps = (probe_capabilities(options.ai_gpu_uuid, controller=controller)
                    if options.engine != "DLSS" or options.hdr_enabled else None)
            for i, path in enumerate(paths):
                if controller.cancel.is_set():
                    break
                reporter.advance(i)
                try:
                    result = upscale_video(path, options, lambda v, m, i=i: reporter.advance(i, v, m),
                                           output_dir=prepare_output_dir(path.parent) if same_as_input else destination,
                                           controller=controller, _owns_slot=True, _capabilities=caps)
                except Exception as exc:
                    cancelled = controller.cancel.is_set() or isinstance(exc, Cancelled)
                    failures.append(UpscaleFailure(i, str(path), str(exc), cancelled))
                    reporter.fail(i, exc, cancelled=cancelled)
                    if cancelled:
                        controller.stop(); break
                else:
                    successes.append(UpscaleSuccess(i, str(path), result))
                    reporter.complete(i, result.output_path, f"{result.frames} frames; report: {result.report_path}")
        if controller.cancel.is_set():
            for item in reporter.items:
                if item.state == "Queued":
                    failures.append(UpscaleFailure(item.index, item.input_path, "Cancelled before rendering.", True))
            reporter.skip_from(0)
        status = "cancelled" if controller.cancel.is_set() else ("partial" if failures else "success")
        app_log.info("upscale-batch", f"{status} ok={len(successes)} failed={len(failures)}")
        manifest = app_log.session_path()
        result = UpscaleBatchResult(successes, failures, controller.cancel.is_set(), manifest)
        reporter.finish(cancelled=result.cancelled, manifest_path=manifest)
        return result
    except BaseException as exc:
        reporter.finish(cancelled=controller.cancel.is_set(), error=str(exc))
        raise
