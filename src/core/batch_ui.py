"""Shared Gradio disk controls and a bounded, session-owned streaming batch UI."""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

import gradio as gr

from .batch_progress import BatchProgress
from .disk_paths import direct_disk_mode, prepare_output_dir, resolve_inputs
from .jobs import JobController, use_job_controller

BATCH_HEADERS = ["File", "State", "Progress", "Elapsed", "Output path", "Details"]


def build_path_controls():
    source = gr.Textbox(
        label="Input path (Optional)", placeholder=r"D:\Render\inputs",
        info="Absolute file or folder path. Overrides uploads; folder contents only, sorted by filename.",
    )
    destination = gr.Textbox(
        label="Output path (Optional)", placeholder=r"D:\Render\outputs",
        info="Output folder; blank uses the app's outputs folder. Either path disables previews and downloads.",
    )
    return source, destination


def build_media_select_button(label: str, file_types: list[str], elem_id: str):
    """Build the replacement picker shown below an input preview."""
    return gr.UploadButton(
        label,
        file_count="multiple",
        file_types=file_types,
        type="filepath",
        visible=True,
        size="lg",
        scale=1,
        min_width=0,
        elem_id=elem_id,
        elem_classes=["media-select-button"],
    )


def build_media_clear_button(elem_id: str):
    """Build the clear action displayed to the right of a replacement picker."""
    return gr.Button(
        "Clear",
        visible=True,
        size="lg",
        scale=1,
        min_width=0,
        elem_id=elem_id,
        elem_classes=["media-clear-button"],
    )


def input_surface_updates(show_preview: bool, input_path: str | None = None):
    """Swap the drop zone for its preview while uploads are the active input mode."""
    upload_enabled = not bool(str(input_path or "").strip())
    return (
        gr.update(visible=not show_preview, interactive=upload_enabled),
        gr.update(visible=show_preview and upload_enabled),
    )


class BatchRun:
    def __init__(self, paths):
        self.controller = JobController()
        self.progress = BatchProgress(paths)
        self.done = threading.Event()
        self.result = None
        self.error = ""
        self.thread = None
        self.is_preview = False

    def start(self, operation):
        def work():
            try:
                with use_job_controller(self.controller):
                    self.result = operation(self)
            except BaseException as exc:
                self.error = str(exc)
                self.progress.finish(cancelled=self.controller.cancel.is_set(), error=str(exc))
            finally:
                self.done.set()
        self.thread = threading.Thread(target=work, name="dlss-batch-ui", daemon=True)
        self.thread.start()

    def close(self):
        if not self.done.is_set():
            self.controller.stop()
        if self.thread is not None:
            self.thread.join()


@dataclass
class _View:
    lock: threading.RLock = field(default_factory=threading.RLock)
    job: BatchRun | None = None
    revision: int = 0
    disk: bool = False


_views: dict[str, _View] = {}
_views_lock = threading.Lock()


def _view(key):
    with _views_lock:
        return _views.setdefault(key, _View())


def release_view(key):
    with _views_lock:
        view = _views.pop(key, None)
    if view and view.job:
        view.job.controller.stop()


def bind_batch_ui(tab, render_function, *, kind, preview_mode, preview_actions=(), preview_controls=(),
                   preview_result_state=None):
    """Media values are only returned in upload mode, once a batch is finished.

    `preview_controls` are non-button preview components (e.g. the shared
    duration dropdown from `core.preview_duration`) that should show/hide and
    enable/disable in lockstep with the preview buttons, without themselves
    having a `.click()` handler wired up.

    `preview_result_state`, if given, is a `gr.State` that gets the most
    recent successful preview's output video path -- letting "Send to
    Comparison" offer the preview itself as a candidate, not just a
    completed full render from `tab.results`."""
    tab.job_state = gr.State(value=lambda: uuid.uuid4().hex, delete_callback=release_view)
    is_image = kind == "image"
    input_media = tab.input_gallery if is_image else tab.input_preview
    media = [tab.output_gallery] if is_image else [tab.output_video]
    archive_download = getattr(tab, "zip_download", None)
    if archive_download is not None:
        media.append(tab.zip_download)
    preview_buttons = [button for button, _fn in preview_actions]
    preview_visibility = [*preview_buttons, *preview_controls]
    controls = [
        tab.sources, tab.select_source, tab.clear_source, tab.input_path, tab.output_path,
        tab.render, tab.reset, *preview_visibility,
    ]
    outputs = [*media, tab.results, tab.status, *controls]
    path_inputs = [tab.job_state, tab.input_path, tab.output_path]

    def empty_media(disk):
        values = [gr.update(value=None, visible=not disk)]
        if archive_download is not None:
            values.append(gr.update(value=None, visible=False))
        return values

    def completed_media(result, disk):
        if result is None or disk:
            return empty_media(disk)
        values = list(result[:len(media)])
        if archive_download is not None:
            archive_path = values[-1]
            values[-1] = gr.update(value=archive_path, visible=bool(archive_path))
        return values

    def control_updates(busy, input_path):
        upload_enabled = not busy and not bool(str(input_path or "").strip())
        return [
            gr.update(interactive=upload_enabled),
            gr.update(interactive=upload_enabled),
            gr.update(interactive=upload_enabled),
            *[gr.update(interactive=not busy) for _ in controls[3:]],
        ]

    def refresh(key, input_path, output_path, *args):
        view = _view(key)
        disk = direct_disk_mode(input_path, output_path)
        with view.lock:
            view.revision += 1
            revision = view.revision
            view.disk = disk
            busy = view.job is not None and not view.job.done.is_set()
        if busy and disk and view.job is not None and view.job.is_preview:
            view.job.controller.stop()
        if busy and not disk:
            return [gr.skip()] * (1 + len(media) + len(preview_visibility) + 2)
        show_input_media = False
        if disk:
            values = [gr.update(value=None, visible=False), *empty_media(True),
                      *[gr.update(visible=False) for _ in preview_visibility]]
        elif is_image:
            previews = preview_mode(args[0])
            show_input_media = bool(previews)
            values = [gr.update(value=previews, visible=show_input_media), *empty_media(False)]
        else:
            input_update, output_update, *buttons = preview_mode(*args)
            sources = args[0]
            show_input_media = bool([sources] if isinstance(sources, str) else list(sources or []))
            values = [input_update, output_update, *empty_media(False)[1:], *buttons]
        with view.lock:
            if revision != view.revision:
                return [gr.skip()] * (1 + len(media) + len(preview_visibility) + 2)
        source_update, actions_update = input_surface_updates(show_input_media, input_path)
        return [
            *values,
            source_update,
            actions_update,
        ]

    source_args = [tab.sources]
    if hasattr(tab, "target_fps"):
        source_args += [tab.target_fps, tab.engine]
    refresh_outputs = [
        input_media, *media, *preview_visibility,
        tab.sources, tab.input_actions,
    ]
    tab.select_source.upload(
        lambda selected: gr.update(value=selected),
        inputs=tab.select_source,
        outputs=tab.sources,
        queue=False,
        show_progress="hidden",
    )
    tab.clear_source.click(
        lambda: gr.update(value=None),
        outputs=tab.sources,
        queue=False,
        show_progress="hidden",
    )
    for component in (tab.sources, tab.input_path, tab.output_path):
        # Textbox changes also cover pasted paths and programmatic value updates.
        component.change(refresh, inputs=[*path_inputs, *source_args], outputs=refresh_outputs,
                         queue=False, show_progress="hidden")

    def stream(key, input_path, output_path, *args):
        view = _view(key)
        with view.lock:
            if view.job is not None:
                raise gr.Error("A job is already running in this tab.")
            job = BatchRun([])
            view.job = job
            view.disk = direct_disk_mode(input_path, output_path)
            disk = view.disk
        destination = ""
        started = False
        try:
            # Resolve once. Direct paths never become a gr.File/gr.Video/Gallery value.
            paths = resolve_inputs(input_path, args[0], kind)
            destination = str(prepare_output_dir(output_path, user_input=True))
            job.progress = BatchProgress(paths)
            row_values, status = job.progress.display(destination)
            yield (*empty_media(disk), row_values, status, *control_updates(True, input_path))
            def operation(run):
                return render_function(
                    paths, *args[1:], progress=lambda *a, **k: None,
                    output_dir=destination, controller=run.controller,
                    on_item_update=run.progress.apply, direct_disk=disk,
                )
            job.start(operation)
            started = True
            while not job.done.wait(.25):
                with view.lock:
                    if view.job is not job:
                        return
                row_values, status = job.progress.display(destination)
                if job.controller.cancel.is_set():
                    status += "\nStopping — cleaning up the current job. Completed outputs will be kept."
                # No media postprocessing/cache transfer during progress refreshes.
                yield (*[gr.skip() for _ in media], row_values, status,
                       *[gr.skip() for _ in controls])
            job.close()
            row_values, status = job.progress.display(destination)
            final_media = completed_media(job.result, disk)
            if job.error and job.error not in status:
                status += f"\n{job.error}"
            with view.lock:
                if view.job is not job:
                    return
            yield (*final_media, row_values, status, *control_updates(False, input_path))
        except Exception as exc:
            job.progress.finish(cancelled=job.controller.cancel.is_set(), error=str(exc))
            row_values, status = job.progress.display(destination)
            yield (*empty_media(view.disk), row_values, status, *control_updates(False, input_path))
        finally:
            if started:
                job.close()
            with view.lock:
                if view.job is job:
                    view.job = None

    tab.render.click(stream, inputs=[*path_inputs, *tab.render_inputs], outputs=outputs,
                     concurrency_limit=None, trigger_mode="once", show_progress="hidden")

    def stop(key):
        view = _view(key)
        with view.lock:
            job = view.job
        if job is not None and not job.done.is_set():
            job.controller.stop()
        # The streaming callback remains the only writer of batch status.
        return gr.skip()
    tab.stop.click(stop, inputs=tab.job_state, outputs=tab.status, queue=False, show_progress="hidden")

    def _resolve_preview_path(value):
        """Preview functions sometimes wrap their output path in gr.update(...)
        (Upscale Video does, to also set visibility/label) and sometimes
        return the path directly (Neural Rendering Video, Frame
        Interpolation). Normalize either shape to the actual path so it can
        be stored for a later "send this preview to Comparison" action."""
        if hasattr(value, "get"):
            return value.get("value")
        return value

    def guarded_preview(function):
        extra_slots = int(archive_download is not None) + int(preview_result_state is not None)

        def preview(key, input_path, output_path, *args, progress=gr.Progress(track_tqdm=False)):
            def empty_preview(video, status):
                values = (video, status)
                if archive_download is not None:
                    values += (gr.update(value=None, visible=False),)
                if preview_result_state is not None:
                    values += (gr.skip(),)  # a blocked/cancelled preview isn't a new "last preview"
                return values
            view = _view(key)
            if direct_disk_mode(input_path, output_path):
                return empty_preview(gr.update(value=None, visible=False), "Previews are disabled when a path is supplied.")
            with view.lock:
                if view.job is not None:
                    raise gr.Error("A job is already running in this tab.")
                job = BatchRun([])
                job.is_preview = True
                view.job = job
                revision = view.revision
            try:
                with use_job_controller(job.controller):
                    result = function(*args, progress=progress)
                if job.controller.cancel.is_set():
                    return empty_preview(gr.update(value=None, visible=False), "Preview cancelled.")
                with view.lock:
                    if view.revision != revision or view.disk:
                        return (gr.skip(),) * (2 + extra_slots)
                extra = ()
                if archive_download is not None:
                    extra += (gr.update(value=None, visible=False),)
                if preview_result_state is not None:
                    extra += (_resolve_preview_path(result[0]),)
                return (*result, *extra)
            finally:
                job.done.set()
                with view.lock:
                    if view.job is job:
                        view.job = None
        return preview
    for button, function in preview_actions:
        outputs = [tab.output_video, tab.status]
        if archive_download is not None:
            outputs.append(archive_download)
        if preview_result_state is not None:
            outputs.append(preview_result_state)
        button.click(guarded_preview(function), inputs=[*path_inputs, *tab.preview_inputs],
                     outputs=outputs, concurrency_limit=None, show_progress="hidden")
