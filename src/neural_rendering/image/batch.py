from __future__ import annotations

import os
import time
from contextlib import suppress
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np

from ...core.batch_progress import BatchItemUpdate, BatchProgress
from ...core.disk_paths import OutputFile, prepare_output_dir
from ...core.dlss_modes import DLSS_MODES, validate_dlss
from ...core.gpu_selection import resolve_runtime_ai_gpu
from ...core.jobs import Cancelled, JobController, active_job
from ...core.render_metadata import prepare_render_note
from ...core.naming import output_filename, unique_output_path, validate_rename
from ...core.paths import OUTPUTS
from ...core.runtime import (
    DLSSFrameSession, prepare_runtime, resize_fit, resolve_native_settings,
    resolve_output_size, resolve_upscaling_mode, verify_feature_18, write_failure_report,
)
from .decoder import _DecodedImage, decode_image
from .encoder import _encode_image, take_image_preview
from .models import (
    IMAGE_EXTENSIONS, IMAGE_FORMATS, ImageBatchResult, ImageConversionFailure,
    ImageConversionOptions, ImageConversionResult,
)
from .reports import (
    IncrementalImageArchive, _ImageReportData, _SessionReportData, _build_manifest,
    _report_data, _write_report, snapshot_session, update_report_performance,
)


@dataclass(slots=True)
class _OutputTask:
    index: int
    path: Path
    output: Path
    processed: np.ndarray
    metadata: dict[str, object]
    report_data: _ImageReportData
    session_data: _SessionReportData
    evidence: dict[str, object]
    gpu: dict[str, object]
    input_width: int
    input_height: int
    render_width: int
    render_height: int
    output_width: int
    output_height: int
    resize_method: str
    memory_path: str
    bridge_status: dict[str, object]
    source_warnings: list[str]
    has_transparency: bool
    timings: dict[str, float]


def _validate_options(options: ImageConversionOptions) -> ImageConversionOptions:
    if options.scale_method not in {"Standard", "DLSS"}:
        raise ValueError("Unknown Neural Rendering scale method.")
    validate_dlss(options.dlss_mode, options.dlss_preset)
    if options.output_format not in IMAGE_FORMATS:
        raise ValueError(f"Unknown image output format: {options.output_format!r}.")
    if isinstance(options.quality, bool):
        raise ValueError("Image quality must be an integer from 1 to 100.")
    try:
        quality = int(options.quality)
    except (TypeError, ValueError) as exc:
        raise ValueError("Image quality must be an integer from 1 to 100.") from exc
    if quality != options.quality or not 1 <= quality <= 100:
        raise ValueError("Image quality must be an integer from 1 to 100.")
    validate_rename(options.rename_mode, options.custom_suffix)
    resolve_native_settings(options)
    resolve_upscaling_mode(options.upscaling_factor)
    return options


def _output_path(source, output_format, stamp, index, rename_mode, custom_suffix, *, output_dir=None):
    safe_stem = source.stem.strip().rstrip(".") or "image"
    return (output_dir or OUTPUTS) / output_filename(
        source, IMAGE_EXTENSIONS[output_format], rename_mode, custom_suffix,
        f"{safe_stem}_DLSS5_IMAGE_{stamp}-{index + 1:04d}",
    )


def _timed_decode(path: Path) -> tuple[_DecodedImage, float]:
    started = time.monotonic()
    decoded = decode_image(path)
    return decoded, time.monotonic() - started


def _finalize_output(
    task: _OutputTask,
    options: ImageConversionOptions,
    reporter: BatchProgress,
    controller: JobController,
    generate_previews: bool,
    archive: IncrementalImageArchive | None,
) -> ImageConversionResult:
    """CPU/I/O stage. Runs on exactly one bounded output worker."""
    output_file = None
    try:
        if controller.cancel.is_set():
            raise Cancelled("Image rendering stopped by user.")
        reporter.advance(task.index, .80, "Encoding image")
        output_file = OutputFile(task.output)
        metadata_diagnostics: dict = {}
        render_note = prepare_render_note(options, metadata_diagnostics)
        warnings = _encode_image(
            output_file.temporary,
            task.processed,
            options,
            task.metadata,
            generate_preview=generate_previews,
            preview_path=task.output,
            render_note=render_note,
            metadata_diagnostics=metadata_diagnostics,
            controller=controller,
            has_transparency=task.has_transparency,
            timings=task.timings,
        )
        warning = metadata_diagnostics.get("warning")
        if warning and warning not in warnings:
            warnings.append(warning)
        if controller.cancel.is_set():
            raise Cancelled("Image rendering stopped by user.")

        reporter.advance(task.index, .95, "Writing diagnostic report")
        result = ImageConversionResult(
            input_path=str(task.path),
            output_path=str(task.output),
            report_path="",
            elapsed_seconds=reporter.elapsed(task.index),
            gpu=str(task.gpu["display_name"]),
            input_width=task.input_width,
            input_height=task.input_height,
            render_width=task.render_width,
            render_height=task.render_height,
            output_width=task.output_width,
            output_height=task.output_height,
            upscaling_factor=(float(DLSS_MODES[options.dlss_mode][0])
                              if options.scale_method == "DLSS" else float(options.upscaling_factor)),
            output_format=options.output_format,
            neural_dimensions={"width": task.render_width, "height": task.render_height},
            resize_method=task.resize_method,
            memory_path=task.memory_path,
            bridge_status=task.bridge_status,
            warnings=[*task.source_warnings, *warnings],
            timings=task.timings,
        )
        # Keep the externally reported GPU name identical to the selected GPU.
        # The batch caller replaces the temporary value below before report write.
        report_started = time.monotonic()
        result.report_path = _write_report(
            result, options, task.report_data,
            task.gpu,
            task.session_data, task.evidence,
            metadata_diagnostics=metadata_diagnostics,
        )
        task.timings["report"] = time.monotonic() - report_started
        if controller.cancel.is_set():
            raise Cancelled("Image rendering stopped by user.")

        publish_started = time.monotonic()
        output_file.publish()
        task.timings["publish"] = time.monotonic() - publish_started

        # Archive work is deliberately after publication: cancellation or archive
        # failure must never roll back a valid completed image.
        if archive is not None:
            archive_started = time.monotonic()
            try:
                archive.add(task.output)
            except Cancelled:
                pass
            task.timings["archive"] = time.monotonic() - archive_started

        result.elapsed_seconds = reporter.elapsed(task.index)
        task.timings["total"] = result.elapsed_seconds
        with suppress(Exception):
            update_report_performance(result)
        reporter.complete(
            task.index, str(task.output),
            "; ".join([*result.warnings, f"Report: {result.report_path}"]),
        )
        return result
    except Exception:
        if output_file is not None:
            output_file.cleanup(rollback=True)
        preview = take_image_preview(task.output)
        if preview is not None:
            preview.close()
        raise
    finally:
        if output_file is not None:
            output_file.cleanup()


def convert_images(
    input_paths: Iterable[str | os.PathLike[str]],
    options: ImageConversionOptions | None = None,
    progress: Callable[[float, str], None] | None = None,
    *, output_dir: str | os.PathLike[str] | None = None,
    controller: JobController | None = None,
    on_item_update: Callable[[BatchItemUpdate], None] | None = None,
    generate_previews: bool = True, create_zip: bool = True,
    same_as_input: bool = False,
) -> ImageBatchResult:
    options = _validate_options(replace(options) if options else ImageConversionOptions())
    paths = [Path(path).resolve() for path in input_paths]
    if not paths:
        raise ValueError("Choose at least one image.")
    controller = controller or JobController()
    reporter = BatchProgress(paths, on_item_update, progress)
    successes: list[ImageConversionResult] = []
    failures: list[ImageConversionFailure] = []
    session: DLSSFrameSession | None = None
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    archive: IncrementalImageArchive | None = None
    pending: tuple[int, Path, Path, Future[ImageConversionResult]] | None = None
    reserved_outputs: set[Path] = set()

    def collect_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        index, path, output, future = pending
        pending = None
        try:
            result = future.result()
        except Exception as exc:
            cancelled_item = isinstance(exc, Cancelled) or controller.cancel.is_set()
            failures.append(ImageConversionFailure(str(path), str(exc)))
            reporter.fail(index, exc, cancelled=cancelled_item)
            if cancelled_item:
                controller.stop()
        else:
            # Output tasks are submitted in input order and only one may be
            # outstanding, so appending here preserves stable result ordering.
            successes.append(result)
        finally:
            reserved_outputs.discard(output)

    try:
        destination = prepare_output_dir(output_dir, default=OUTPUTS) if not same_as_input or create_zip else None
        if create_zip:
            archive = IncrementalImageArchive(
                destination / f"DLSS5_IMAGE_BATCH_{stamp}.zip", controller,
            )
        with (
            active_job(controller),
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlss5-image-decode") as decoder,
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="dlss5-image-output") as output_worker,
        ):
            if controller.cancel.is_set():
                raise Cancelled("Stopped before rendering.")
            prepared = prepare_runtime()
            gpu = resolve_runtime_ai_gpu(prepared.gpus, prepared.runtime_bundle, options.ai_gpu_uuid)
            factor, mode = resolve_upscaling_mode(
                1.0 if options.scale_method == "DLSS" else options.upscaling_factor)
            native = resolve_native_settings(options)
            next_decode: Future[tuple[_DecodedImage, float]] | None = None
            session_size: tuple[int, int] | None = None
            session_evidence: dict[str, object] | None = None
            sent = 0
            try:
                for index, path in enumerate(paths):
                    if controller.cancel.is_set():
                        break
                    reporter.advance(index, 0.0, "Decoding image")
                    item_destination = prepare_output_dir(path.parent) if same_as_input else destination
                    output = unique_output_path(_output_path(
                        path, options.output_format, stamp, index,
                        options.rename_mode, options.custom_suffix, output_dir=item_destination,
                    ), reserved_outputs)
                    decoded = None
                    render_rgba = None
                    processed = None
                    timings: dict[str, float] = {}
                    try:
                        future = next_decode or decoder.submit(_timed_decode, path)
                        next_decode = None
                        while True:
                            if controller.cancel.is_set():
                                future.cancel()
                                raise Cancelled("Image rendering stopped by user.")
                            try:
                                decoded, timings["decode"] = future.result(timeout=.1)
                                break
                            except TimeoutError:
                                continue
                        del future
                        # Decode only one future image ahead to keep memory bounded.
                        if index + 1 < len(paths):
                            next_decode = decoder.submit(_timed_decode, paths[index + 1])

                        height, width = decoded.rgba.shape[:2]
                        if width < 64 or height < 64:
                            raise ValueError(
                                f"{path.name} is {width}×{height}; DLSS requires at least 64×64."
                            )
                        dlss_details = None
                        if options.scale_method == "DLSS":
                            from ...core.dlss_bridge import process_image as process_dlss_image
                            dlss_started = time.monotonic()
                            dlss_rgba, dlss_details = process_dlss_image(
                                decoded.rgba, options.dlss_mode, options.dlss_preset,
                                gpu_uuid=options.ai_gpu_uuid, controller=controller)
                            timings["dlss_super_resolution"] = time.monotonic() - dlss_started
                            output_height, output_width = dlss_rgba.shape[:2]
                        else:
                            dlss_rgba = None
                            output_width, output_height = resolve_output_size(
                                width, height, options.upscaling_factor)
                        dimensions = (output_width, output_height)
                        if session is not None and dimensions != session_size:
                            close_started = time.monotonic()
                            session.close()
                            timings["previous_session_close"] = time.monotonic() - close_started
                            session = None
                            session_evidence = None

                        if session is None:
                            reporter.advance(index, .10, "Starting D3D12/NGX bridge")
                            setup_started = time.monotonic()
                            session = DLSSFrameSession(
                                input_width=output_width if dlss_rgba is not None else width,
                                input_height=output_height if dlss_rgba is not None else height,
                                output_width=output_width,
                                output_height=output_height,
                                frame_count=None,
                                warmup_frames=options.warmup_frames,
                                factor=factor,
                                mode=mode,
                                native_settings=native,
                                composition_mask=options.nr_mask,
                                gpu=gpu,
                                runtime_bundle=prepared.runtime_bundle,
                                controller=controller,
                            )
                            timings["dlss_setup"] = time.monotonic() - setup_started
                            session_size, sent = dimensions, 0
                            session_evidence = None

                        reporter.advance(index, .25, "Processing image")
                        prep_started = time.monotonic()
                        render_rgba = resize_fit(
                            dlss_rgba if dlss_rgba is not None else decoded.rgba,
                            session.render_width, session.render_height)
                        session.diagnostics.source_format = (
                            f"{decoded.metadata['source_format']}/{decoded.metadata['source_bit_depth']}"
                        )
                        timings["render_prepare"] = time.monotonic() - prep_started

                        dlss_started = time.monotonic()
                        processed, _pts = session.process(
                            index=sent, rgba=render_rgba, reset=True, pts=sent,
                        )
                        timings["dlss_evaluate"] = time.monotonic() - dlss_started
                        sent += 1

                        if session_evidence is None:
                            evidence_started = time.monotonic()
                            # Signed-runtime/setup evidence is session-invariant;
                            # direct process() success still validates every image.
                            session_evidence = verify_feature_18(
                                session.bridge_logs, session.structured_status(),
                            )
                            timings["feature18_verify"] = time.monotonic() - evidence_started
                        else:
                            timings["feature18_verify"] = 0.0

                        alpha_started = time.monotonic()
                        if decoded.alpha is None:
                            processed[..., 3] = np.iinfo(processed.dtype).max
                        elif decoded.alpha.shape == dimensions[::-1]:
                            processed[..., 3] = decoded.alpha
                        else:
                            processed[..., 3] = cv2.resize(
                                decoded.alpha, dimensions, interpolation=cv2.INTER_LANCZOS4,
                            )
                        timings["alpha"] = time.monotonic() - alpha_started
                        output_depth = 8
                        if processed.dtype == np.uint16:
                            if options.output_format in {"PNG", "TIFF"}:
                                output_depth = 16
                            elif options.output_format == "AVIF":
                                output_depth = (
                                    12 if int(decoded.metadata["source_bit_depth"]) > 10 else 10
                                )
                        session.diagnostics.output_format = f"{options.output_format}/{output_depth}"

                        session_data = snapshot_session(session)
                        evidence_snapshot = dict(session_evidence)
                        render_width = session.render_width
                        render_height = session.render_height
                        resize_method = "dlss" if dlss_details else ("none" if factor == 1.0 else "lanczos")
                        memory_path = session.diagnostics.memory_path
                        bridge_status = session.structured_status()
                        if dlss_details:
                            bridge_status = {**bridge_status, "dlss_super_resolution": dlss_details}

                        # Preserve the old invariant that the final image is not
                        # published if native streaming completion fails.
                        if index == len(paths) - 1:
                            close_started = time.monotonic()
                            session.close()
                            timings["session_close"] = time.monotonic() - close_started
                            session = None
                            session_size = None
                            session_evidence = None

                        # This wait happens only after the current GPU evaluation,
                        # overlapping image N CPU/I/O with image N+1 DLSS work while
                        # bounding retained full-resolution outputs to one pending item.
                        collect_pending()
                        if controller.cancel.is_set():
                            raise Cancelled("Image rendering stopped by user.")

                        task = _OutputTask(
                            index=index,
                            path=path,
                            output=output,
                            processed=processed,
                            metadata=decoded.metadata,
                            report_data=_report_data(decoded),
                            session_data=session_data,
                            evidence=evidence_snapshot,
                            gpu=dict(gpu),
                            input_width=width,
                            input_height=height,
                            render_width=render_width,
                            render_height=render_height,
                            output_width=output_width,
                            output_height=output_height,
                            resize_method=resize_method,
                            memory_path=memory_path,
                            bridge_status=bridge_status,
                            source_warnings=list(decoded.warnings),
                            has_transparency=decoded.alpha is not None,
                            timings=timings,
                        )
                        pending = (
                            index,
                            path,
                            output,
                            output_worker.submit(
                                _finalize_output, task, options, reporter, controller,
                                generate_previews, archive,
                            ),
                        )
                        reserved_outputs.add(output)
                        # Ownership of the full output moves to the worker.
                        processed = None
                    except Exception as exc:
                        cancelled_item = isinstance(exc, Cancelled) or controller.cancel.is_set()
                        if session is not None:
                            session.abort()
                            if not cancelled_item:
                                with suppress(Exception):
                                    report = write_failure_report(
                                        operation="image-render",
                                        source=str(path),
                                        error=exc,
                                        gpu=gpu,
                                        runtime_bundle=prepared.runtime_bundle,
                                        bridge_status=session.structured_status(),
                                        bridge_log=session.bridge_logs,
                                    )
                                    exc = RuntimeError(f"{exc}\nDetails: {report}")
                            session = None
                            session_size = None
                            session_evidence = None
                        failures.append(ImageConversionFailure(str(path), str(exc)))
                        reporter.fail(index, exc, cancelled=cancelled_item)
                        if cancelled_item:
                            controller.stop()
                            break
                    finally:
                        decoded = None
                        render_rgba = None
                        processed = None

                # Drain the final output task. This does not reduce GPU overlap
                # because there is no subsequent image to process.
                collect_pending()
                if session is not None and not session.closed:
                    if controller.cancel.is_set():
                        session.abort()
                    else:
                        session.close()
            finally:
                if next_decode is not None:
                    next_decode.cancel()
                if session is not None and not session.closed:
                    session.abort()
                # ThreadPoolExecutor waits for a running output task; collect it
                # before leaving so its success/failure reaches the manifest.
                collect_pending()

        cancelled = controller.cancel.is_set()
        if cancelled:
            for item in reporter.items:
                if item.state == "Queued":
                    failures.append(ImageConversionFailure(item.input_path, "Cancelled before rendering."))
            reporter.skip_from(0)

        zip_path = archive.finish(cancelled=cancelled) if archive is not None else None
        archive_error = archive.error if archive is not None else None
        manifest = _build_manifest(
            stamp, options, successes, failures, cancelled,
            output_dir=destination,
            batch_diagnostics=reporter.diagnostics(final=True),
            archive_error=archive_error,
        )
        reporter.finish(cancelled=cancelled, manifest_path=manifest)
        return ImageBatchResult(successes, failures, cancelled, manifest, zip_path)
    except BaseException as exc:
        if archive is not None and not archive.closed:
            archive.abort()
        reporter.finish(cancelled=controller.cancel.is_set(), error=str(exc))
        raise
