"""Shared Gradio disk controls and a bounded, session-owned streaming batch UI."""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import gradio as gr

from .i18n import t, translator
from .batch_progress import BatchProgress
from .disk_paths import create_media_archive, direct_disk_mode, prepare_output_dir, resolve_inputs
from .jobs import JobController, use_job_controller
from .paths import GRADIO_TEMP

BATCH_HEADERS = ["File", "State", "Progress", "Elapsed", "Output path", "Details"]


def batch_headers(language: str | None = None) -> list[str]:
    ui_t = translator(language).t
    return [
        ui_t("batch.header.file"),
        ui_t("batch.header.state"),
        ui_t("batch.header.progress"),
        ui_t("batch.header.elapsed"),
        ui_t("batch.header.output_path"),
        ui_t("batch.header.details"),
    ]


def build_path_controls():
    # Keep the two disk-path fields in their own layout scope.  Without an
    # explicit boundary Gradio may merge adjacent encoding/output controls
    # into the same generated Form, producing inconsistent visual grouping
    # between workflows.  A default Column is structural only (no panel
    # styling), so the path fields retain their existing native appearance.
    with gr.Column():
        source = gr.Textbox(
            label=t("batch.path.input"), placeholder=r"D:\Render\inputs",
        )
        destination = gr.Textbox(
            label=t("batch.path.output"), placeholder=r"D:\Render\outputs",
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
        t("common.button.clear"),
        visible=True,
        size="lg",
        scale=1,
        min_width=0,
        elem_id=elem_id,
        elem_classes=["media-clear-button"],
    )


def build_save_controls(kind: str, elem_id: str):
    """Build direct-save and lazy-archive controls for an image/video batch tab."""
    if kind not in {"image", "video"}:
        raise ValueError(t("batch.error.unknown_media_type", kind=kind))
    direct = gr.DownloadButton(
        t(f"batch.save.{kind}"), value=None, visible=False, elem_id=f"{elem_id}-direct-save"
    )
    archive = gr.Button(t("batch.save.zip"), visible=False, elem_id=f"{elem_id}-save-zip")
    # Keep the internal download target permanently rendered so the chained JS
    # action can click it after lazy ZIP creation.  It is hidden exclusively by
    # application CSS instead of Gradio's visibility state; tab remounts can
    # otherwise leak a `visible="hidden"` DownloadButton into the normal UI.
    archive_download = gr.DownloadButton(
        t("batch.download.zip"),
        value=None,
        visible=True,
        elem_id=f"{elem_id}-zip-download",
        elem_classes=["internal-zip-download"],
    )
    return direct, archive, archive_download


def _stable_visible(show: bool):
    """Keep stateful media components mounted even when they are not displayed."""
    return True if show else "hidden"


def input_surface_updates(show_preview: bool, input_path: str | None = None):
    """Atomically swap upload/preview actions without unmounting either tree."""
    upload_enabled = not bool(str(input_path or "").strip())
    return (
        gr.update(visible=_stable_visible(not show_preview), interactive=upload_enabled),
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
        self.is_auto_preview = False

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


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    path: str
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(slots=True)
class _DownloadState:
    kind: str
    input_count: int
    paths: tuple[str, ...]
    identities: tuple[_FileIdentity, ...]
    output_dir: str
    archive_prefix: str
    archive_path: str | None = None
    archive_lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class _View:
    lock: threading.RLock = field(default_factory=threading.RLock)
    job: BatchRun | None = None
    revision: int = 0
    disk: bool = False
    download: _DownloadState | None = None
    # Input media preparation can be slower than navigation (RAW/HEIF/image
    # thumbnails, video inspection).  Track readiness independently so a tab
    # reactivation can hide stale construction-time controls until the accepted
    # refresh has completed.
    input_ready: bool = True
    auto_revision: int = 0
    auto_preview_path: str | None = None


_views: dict[str, _View] = {}
_views_lock = threading.Lock()


def _view(key):
    with _views_lock:
        return _views.setdefault(key, _View())


def _discard_auto_preview(path: str | None) -> None:
    """Remove only previews owned by the app's managed Gradio temp tree."""
    if not path:
        return
    try:
        candidate = Path(path).resolve()
        root = GRADIO_TEMP.resolve()
        if candidate.is_relative_to(root):
            candidate.unlink(missing_ok=True)
            try:
                candidate.parent.rmdir()
            except OSError:
                pass
    except (OSError, RuntimeError, ValueError):
        pass


def _managed_preview_path(media) -> str | None:
    """Find a generated media path inside a Gradio update/value structure."""
    if isinstance(media, (str, Path)):
        try:
            candidate = Path(media).resolve()
            if candidate.is_file() and candidate.is_relative_to(GRADIO_TEMP.resolve()):
                return str(candidate)
        except (OSError, RuntimeError, ValueError):
            return None
        return None
    if isinstance(media, dict):
        return _managed_preview_path(media.get("value"))
    if isinstance(media, (list, tuple)):
        for item in media:
            if path := _managed_preview_path(item):
                return path
    return None


def release_view(key):
    with _views_lock:
        view = _views.pop(key, None)
    if view and view.job:
        view.job.controller.stop()
    if view:
        _discard_auto_preview(view.auto_preview_path)


def _uploaded_source_count(value) -> int:
    if isinstance(value, str):
        return 1 if value.strip() else 0
    try:
        return len(list(value or []))
    except TypeError:
        return 1 if value else 0


def _has_uploaded_sources(value) -> bool:
    return _uploaded_source_count(value) > 0


def bind_input_surface_reactivation(trigger, tab, *, kind: str, event_name: str | None = None):
    """Re-assert one tab's upload/preview visibility after navigation.

    Gradio may restore a child component's construction-time visibility when a
    parent Tab/Column is hidden and shown again.  Uploaded values themselves are
    still correct, so this callback intentionally changes *visibility only*: it
    never regenerates thumbnails, clears outputs, invalidates downloads, or
    mutates render state.  If an input refresh is still running, both the upload
    and preview are kept hidden until that accepted refresh publishes its final
    state.
    """
    if kind not in {"image", "video"}:
        raise ValueError(t("batch.error.unknown_media_type", kind=kind))
    input_media = tab.input_gallery if kind == "image" else tab.input_preview
    preview_buttons = [
        component for name in ("preview_frame", "preview")
        if (component := getattr(tab, name, None)) is not None
    ]

    def reconcile(key, input_path, output_path, sources):
        view = _view(key)
        disk = direct_disk_mode(input_path, output_path)
        source_count = _uploaded_source_count(sources)
        has_sources = source_count > 0
        with view.lock:
            ready = view.input_ready

        # Direct-disk mode intentionally uses the original upload/path surface
        # rules and suppresses browser previews/downloads.
        if disk:
            return (
                gr.update(visible=True),
                gr.update(visible="hidden"),
                gr.update(visible=False),
                *[gr.update(visible=False) for _ in preview_buttons],
            )
        if not has_sources:
            return (
                gr.update(visible=True),
                gr.update(visible="hidden"),
                gr.update(visible=False),
                *[gr.update(visible=False) for _ in preview_buttons],
            )
        if not ready:
            # The source exists but its preview is still being prepared.  Never
            # flash the upload box as if the workspace were empty.
            return (
                gr.update(visible="hidden"),
                gr.update(visible="hidden"),
                gr.update(visible=False),
                *[gr.update(visible=False) for _ in preview_buttons],
            )
        return (
            gr.update(visible="hidden"),
            gr.update(visible=True),
            gr.update(visible=True),
            *[gr.update(visible=source_count == 1) for _ in preview_buttons],
        )

    inputs = [tab.job_state, tab.input_path, tab.output_path, tab.sources]
    outputs = [tab.sources, input_media, tab.input_actions, *preview_buttons]
    kwargs = dict(
        inputs=inputs, outputs=outputs, queue=False, show_progress="hidden",
        trigger_mode="always_last",
    )
    if event_name is None:
        return trigger.then(reconcile, **kwargs)
    return getattr(trigger, event_name)(reconcile, **kwargs)


def _identity(path: str) -> _FileIdentity:
    source = Path(path).resolve()
    stat = source.stat()
    if not source.is_file():
        raise ValueError(f"Rendered output is no longer a file: {source}")
    return _FileIdentity(
        str(source), int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns)
    )


def _validate_download_state(state: _DownloadState) -> None:
    current = tuple(_identity(path) for path in state.paths)
    if current != state.identities:
        raise ValueError(t("batch.error.output_changed"))


def _archive_trigger_js(elem_id: str) -> str:
    encoded = json.dumps(elem_id)
    return f"""() => {{
        const root = document.getElementById({encoded});
        const target = root && (root.matches('a,button') ? root : root.querySelector('a,button'));
        if (target) target.click();
    }}"""


def bind_batch_ui(
    tab, render_function, *, kind, preview_mode, archive_prefix: str, preview_actions=(),
    realtime_preview=None, realtime_components=(),
):
    """Bind one batch tab, including direct single-file and lazy multi-file downloads.

    Render callbacks return four values: display media, successful master output
    paths, batch rows, and status text.  The archive is intentionally *not* part
    of rendering; it is created only when the user clicks Save as ZIP.
    """
    tab.job_state = gr.State(value=lambda: uuid.uuid4().hex, delete_callback=release_view)
    is_image = kind == "image"
    input_media = tab.input_gallery if is_image else tab.input_preview
    display_media = tab.output_gallery if is_image else tab.output_video
    direct_save = tab.save_download
    archive_button = tab.zip_button
    archive_download = tab.zip_download
    media_outputs = [display_media, direct_save, archive_button, archive_download]
    preview_buttons = [button for button, _fn in preview_actions]
    controls = [
        tab.sources, tab.select_source, tab.clear_source, tab.input_path, tab.output_path,
        tab.render, tab.reset, *preview_buttons,
    ]
    outputs = [*media_outputs, tab.results, tab.status, *controls]
    path_inputs = [tab.job_state, tab.input_path, tab.output_path]

    def empty_save_controls():
        return (
            gr.update(value=None, visible=False),
            gr.update(visible=False),
            gr.update(value=None),
        )

    def empty_media(disk):
        return [
            gr.update(value=None, visible=not disk),
            *empty_save_controls(),
        ]

    def completed_media(result, disk, *, input_count: int, destination: str, view: _View):
        if result is None or disk:
            with view.lock:
                view.download = None
            return empty_media(disk)

        display_value, successful_paths, _rows, _status = result
        successful_paths = tuple(str(Path(path).resolve()) for path in (successful_paths or []))
        if not successful_paths:
            with view.lock:
                view.download = None
            return [display_value, *empty_save_controls()]

        try:
            identities = tuple(_identity(path) for path in successful_paths)
        except Exception:
            # Rendering reported a success that cannot be downloaded anymore.
            # Keep the media preview/result rows, but never expose a stale save action.
            with view.lock:
                view.download = None
            return [display_value, *empty_save_controls()]

        state = _DownloadState(
            kind=kind,
            input_count=input_count,
            paths=successful_paths,
            identities=identities,
            output_dir=destination,
            archive_prefix=archive_prefix,
        )
        with view.lock:
            view.download = state

        if input_count == 1:
            return [
                display_value,
                gr.update(value=successful_paths[0], visible=True, label=t(f"batch.save.{kind}")),
                gr.update(visible=False),
                gr.update(value=None),
            ]
        return [
            display_value,
            gr.update(value=None, visible=False),
            gr.update(visible=True),
            gr.update(value=None),
        ]

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
            view.download = None
            view.input_ready = False
            busy = view.job is not None and not view.job.done.is_set()
        if busy and disk and view.job is not None and view.job.is_preview:
            view.job.controller.stop()
        if busy and not disk:
            with view.lock:
                if revision == view.revision:
                    view.input_ready = True
            return [gr.skip()] * (1 + len(media_outputs) + len(preview_buttons) + 2)
        show_input_media = False
        try:
            if disk:
                values = [gr.update(value=None, visible="hidden"), *empty_media(True),
                          *[gr.update(visible=False) for _ in preview_buttons]]
            elif is_image:
                previews = preview_mode(args[0])
                show_input_media = bool(previews)
                source_count = _uploaded_source_count(args[0])
                values = [
                    gr.update(value=previews, visible=_stable_visible(show_input_media)),
                    *empty_media(False),
                    *[gr.update(visible=source_count == 1) for _ in preview_buttons],
                ]
            else:
                input_update, output_update, *buttons = preview_mode(*args)
                sources = args[0]
                show_input_media = bool([sources] if isinstance(sources, str) else list(sources or []))
                values = [input_update, output_update, *empty_save_controls(), *buttons]
        except BaseException:
            with view.lock:
                if revision == view.revision:
                    view.input_ready = True
            raise
        with view.lock:
            if revision != view.revision:
                return [gr.skip()] * (1 + len(media_outputs) + len(preview_buttons) + 2)
            view.input_ready = True
        source_update, actions_update = input_surface_updates(show_input_media, input_path)
        return [*values, source_update, actions_update]

    source_args = [tab.sources]
    if hasattr(tab, "target_fps"):
        source_args += [tab.target_fps, tab.engine]
    refresh_outputs = [
        input_media, *media_outputs, *preview_buttons,
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
        component.change(
            refresh,
            inputs=[*path_inputs, *source_args],
            outputs=refresh_outputs,
            queue=False,
            show_progress="hidden",
            trigger_mode="always_last",
        )

    def stream(key, input_path, output_path, *args):
        view = _view(key)
        with view.lock:
            if view.job is not None:
                raise gr.Error(t("batch.error.job_running"))
            job = BatchRun([])
            view.job = job
            view.download = None
            view.disk = direct_disk_mode(input_path, output_path)
            disk = view.disk
        destination = ""
        started = False
        paths = []
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
                    status += "\n" + t("batch.status.stopping")
                # No media postprocessing/cache transfer during progress refreshes.
                yield (*[gr.skip() for _ in media_outputs], row_values, status,
                       *[gr.skip() for _ in controls])
            job.close()
            row_values, status = job.progress.display(destination)
            final_media = completed_media(
                job.result, disk, input_count=len(paths), destination=destination, view=view
            )
            if job.error and job.error not in status:
                status += f"\n{job.error}"
            with view.lock:
                if view.job is not job:
                    return
            yield (*final_media, row_values, status, *control_updates(False, input_path))
        except Exception as exc:
            job.progress.finish(cancelled=job.controller.cancel.is_set(), error=str(exc))
            row_values, status = job.progress.display(destination)
            with view.lock:
                view.download = None
            yield (*empty_media(view.disk), row_values, status, *control_updates(False, input_path))
        finally:
            if started:
                job.close()
            with view.lock:
                if view.job is job:
                    view.job = None

    tab.render.click(
        stream,
        inputs=[*path_inputs, *tab.render_inputs],
        outputs=outputs,
        concurrency_limit=None,
        trigger_mode="once",
        show_progress="hidden",
    )

    def stop(key):
        view = _view(key)
        with view.lock:
            job = view.job
        if job is not None and not job.done.is_set():
            job.controller.stop()
        # The streaming callback remains the only writer of batch status.
        return gr.skip()

    tab.stop.click(stop, inputs=tab.job_state, outputs=tab.status, queue=False, show_progress="hidden")

    def create_lazy_archive(key):
        view = _view(key)
        with view.lock:
            state = view.download
            busy = view.job is not None and not view.job.done.is_set()
        if busy:
            raise gr.Error(t("batch.error.wait_zip"))
        if state is None or state.input_count < 2 or not state.paths:
            raise gr.Error(t("batch.error.no_zip_batch"))
        with state.archive_lock:
            try:
                _validate_download_state(state)
                if state.archive_path and Path(state.archive_path).is_file():
                    archive_path = state.archive_path
                else:
                    archive_path = create_media_archive(
                        state.paths, state.output_dir, state.archive_prefix
                    )
                    if not archive_path:
                        raise RuntimeError("The ZIP archive was not created.")
                    state.archive_path = archive_path
                return gr.update(value=archive_path)
            except gr.Error:
                raise
            except Exception as exc:
                raise gr.Error(t("batch.error.create_zip", error=exc)) from exc

    archive_event = archive_button.click(
        create_lazy_archive,
        inputs=tab.job_state,
        outputs=archive_download,
        concurrency_limit=1,
        trigger_mode="once",
        show_progress="minimal",
    )
    archive_event.success(
        fn=None,
        js=_archive_trigger_js(archive_download.elem_id),
        queue=False,
        show_progress="hidden",
    )

    def guarded_preview(function):
        def preview(key, input_path, output_path, *args, progress=gr.Progress(track_tqdm=False)):
            def empty_preview(media, status):
                return (media, status, *empty_save_controls())

            view = _view(key)
            if direct_disk_mode(input_path, output_path):
                with view.lock:
                    view.download = None
                yield empty_preview(
                    gr.update(value=None, visible=False),
                    t("batch.status.preview_disabled_path"),
                )
                return
            with view.lock:
                if view.job is not None:
                    raise gr.Error(t("batch.error.job_running"))
                job = BatchRun([])
                job.is_preview = True
                view.job = job
                view.download = None
                revision = view.revision
            # Hide every save action before expensive preview work starts so a
            # previous completed render can never be downloaded as if it were
            # the new preview/result.
            yield (gr.skip(), gr.skip(), *empty_save_controls())
            try:
                with use_job_controller(job.controller):
                    result = function(*args, progress=progress)
                if job.controller.cancel.is_set():
                    yield empty_preview(gr.update(value=None, visible=False), t("batch.status.preview_cancelled"))
                    return
                with view.lock:
                    stale = view.revision != revision or view.disk
                if stale:
                    yield (gr.skip(),) * 5
                    return
                yield (*result, *empty_save_controls())
            finally:
                job.done.set()
                with view.lock:
                    if view.job is job:
                        view.job = None
        return preview

    for button, function in preview_actions:
        button.click(
            guarded_preview(function),
            inputs=[*path_inputs, *tab.preview_inputs],
            outputs=[display_media, tab.status, direct_save, archive_button, archive_download],
            concurrency_limit=None,
            show_progress="hidden",
        )

    if realtime_preview is None:
        return

    # User input invalidates the current preview synchronously, even while its
    # queued backend call is still evaluating. Slider previews trigger on
    # release so dragging never launches one GPU job per intermediate value;
    # dropdown/radio changes trigger immediately.
    auto_token = gr.State(0)

    def invalidate_auto_preview(key, sources):
        view = _view(key)
        old_path = None
        with view.lock:
            view.auto_revision += 1
            token = view.auto_revision
            job = view.job
            if job is not None and job.is_preview and not job.done.is_set():
                job.controller.stop()
            if _uploaded_source_count(sources) != 1:
                old_path = view.auto_preview_path
                view.auto_preview_path = None
        _discard_auto_preview(old_path)
        return token

    def automatic_preview(
        key, token, input_path, output_path, *args,
        progress=gr.Progress(track_tqdm=False),
    ):
        unchanged = (gr.skip(),) * 5
        view = _view(key)
        try:
            token = int(token)
        except (TypeError, ValueError):
            yield unchanged
            return
        if direct_disk_mode(input_path, output_path) or _uploaded_source_count(args[0]) != 1:
            yield unchanged
            return

        with view.lock:
            stale = token != view.auto_revision
            existing = view.job
        if stale:
            yield unchanged
            return
        if existing is not None and not existing.done.is_set():
            if not existing.is_preview:
                # Production output owns the tab and the process-wide GPU slot.
                yield unchanged
                return
            existing.controller.stop()
            if not existing.done.wait(5.0):
                yield unchanged
                return

        with view.lock:
            stale = token != view.auto_revision or view.disk
            busy = view.job is not None and not view.job.done.is_set()
            if not stale and not busy:
                job = BatchRun([])
                job.is_preview = True
                job.is_auto_preview = True
                view.job = job
                view.download = None
                source_revision = view.revision
        if stale or busy:
            yield unchanged
            return

        # Preserve the current visual until its replacement is ready, but hide
        # save actions immediately because this is an ephemeral preview.
        yield (gr.skip(), gr.skip(), *empty_save_controls())
        preview_dir = GRADIO_TEMP / "neural-realtime" / str(key)
        preview_dir.mkdir(parents=True, exist_ok=True)
        try:
            # A short quiet window coalesces keyboard/radio changes that arrive
            # together (for example a preset selection updating several fields).
            if job.controller.cancel.wait(0.18):
                yield unchanged
                return
            with view.lock:
                stale = token != view.auto_revision
            if stale:
                yield unchanged
                return
            with use_job_controller(job.controller):
                result = realtime_preview(
                    *args,
                    progress=progress,
                    output_dir=preview_dir,
                    controller=job.controller,
                    ephemeral_preview=True,
                )
            with view.lock:
                stale = (
                    job.controller.cancel.is_set()
                    or token != view.auto_revision
                    or source_revision != view.revision
                    or view.disk
                )
            if stale:
                _discard_auto_preview(_managed_preview_path(result[0]))
                yield unchanged
                return

            managed_path = _managed_preview_path(result[0])
            with view.lock:
                old_path = view.auto_preview_path
                view.auto_preview_path = managed_path
            if old_path != managed_path:
                _discard_auto_preview(old_path)
            yield (*result, *empty_save_controls())
        except Exception as exc:
            with view.lock:
                stale = job.controller.cancel.is_set() or token != view.auto_revision
            if stale:
                yield unchanged
            else:
                yield (
                    gr.skip(),
                    t("batch.status.auto_preview_failed", error=exc),
                    *empty_save_controls(),
                )
        finally:
            job.done.set()
            with view.lock:
                if view.job is job:
                    view.job = None

    triggers = [tab.sources.change]
    triggers.extend(
        component.release if isinstance(component, gr.Slider) else component.input
        for component in realtime_components
    )
    invalidated = gr.on(
        triggers=triggers,
        fn=invalidate_auto_preview,
        inputs=[tab.job_state, tab.sources],
        outputs=auto_token,
        queue=False,
        show_progress="hidden",
        trigger_mode="always_last",
        api_visibility="private",
    )
    invalidated.then(
        automatic_preview,
        inputs=[
            tab.job_state, auto_token, tab.input_path, tab.output_path,
            *tab.preview_inputs,
        ],
        outputs=[display_media, tab.status, direct_save, archive_button, archive_download],
        concurrency_limit=1,
        concurrency_id="neural-realtime-preview",
        trigger_mode="always_last",
        show_progress="hidden",
        api_visibility="private",
    )

    def refresh_realtime_preview_after(trigger):
        """Regenerate this tab's preview after a successful programmatic update."""
        reset_invalidated = trigger.success(
            invalidate_auto_preview,
            inputs=[tab.job_state, tab.sources],
            outputs=auto_token,
            queue=False,
            show_progress="hidden",
            trigger_mode="always_last",
            api_visibility="private",
        )
        return reset_invalidated.then(
            automatic_preview,
            inputs=[
                tab.job_state, auto_token, tab.input_path, tab.output_path,
                *tab.preview_inputs,
            ],
            outputs=[display_media, tab.status, direct_save, archive_button, archive_download],
            concurrency_limit=1,
            concurrency_id="neural-realtime-preview",
            trigger_mode="always_last",
            show_progress="hidden",
            api_visibility="private",
        )

    return refresh_realtime_preview_after
