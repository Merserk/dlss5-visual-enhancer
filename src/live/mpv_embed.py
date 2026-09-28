from __future__ import annotations

"""In-tab MPV hosting for the Live workflow (Windows ``--wid`` embedding).

The Live pipeline keeps producing HLS exactly as before; instead of opening a
separate MPV window, the vendored ``mpv.exe`` is told to attach to a dedicated
child ``QWindow`` that floats over the Live tab's video slot. MPV creates its
own child window covering that container and letterboxes the video itself, so
the Python side only has to keep the container geometry in sync with QML.

Threading: every method here must run on the GUI thread (QML slots and the
bridge run there). The pipeline worker thread never touches this object; the
bridge hands the container HWND + IPC pipe name to the session via
``LiveOptions`` before the worker starts.

Transport (pause/mute/volume) goes through MPV's JSON IPC named pipe on a
short-lived daemon thread so the GUI never blocks. Every command reads
MPV's reply and only then updates local state, so rejections surface via
``embedError`` instead of failing silently. Fullscreen is video-only while
embedded (MPV owns no top-level window under ``--wid``): the container is
promoted to a top-level fullscreen window, leaving the app window alone.
Playback telemetry still comes from the existing ``player_status.lua``
state file (see ``player.py``).
"""

import ctypes
import itertools
import json
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot, Property, Qt

try:
    from PySide6.QtGui import QRasterWindow as _QRasterWindowBase
except Exception:
    _QRasterWindowBase = None  # type: ignore[assignment, misc]


_request_ids = itertools.count(1)
_request_lock = threading.Lock()


def _kernel32():
    """kernel32 with explicit prototypes (never truncate 64-bit HANDLEs)."""
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    try:
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.WriteFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        kernel32.WriteFile.restype = ctypes.c_bool
        kernel32.ReadFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p,
        ]
        kernel32.ReadFile.restype = ctypes.c_bool
        kernel32.PeekNamedPipe.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.PeekNamedPipe.restype = ctypes.c_bool
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool
    except Exception:
        pass
    return kernel32


def _read_reply(kernel32: Any, handle: Any, request_id: int, *, timeout: float = 3.0) -> dict:
    """Read newline-delimited JSON until our command reply arrives."""
    deadline = time.monotonic() + max(0.5, timeout)
    pending = b""
    while time.monotonic() < deadline:
        available = ctypes.c_ulong(0)
        try:
            peeked = kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None)
        except Exception:
            peeked = False
        if not peeked:
            raise OSError(f"Lost MPV IPC pipe while reading ({ctypes.get_last_error()}).")
        if not available.value:
            time.sleep(0.02)
            continue
        chunk = ctypes.create_string_buffer(min(65536, available.value + 16))
        got = ctypes.c_ulong(0)
        if not kernel32.ReadFile(handle, chunk, len(chunk) - 1, ctypes.byref(got), None):
            err = ctypes.get_last_error()
            if err != 234 or not got.value:  # 234 = MORE_DATA, keep draining
                raise OSError(f"Could not read MPV IPC reply ({err}).")
        pending += chunk.raw[: got.value]
        while b"\n" in pending:
            raw, pending = pending.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("request_id") == request_id or (
                request_id == 0 and "error" in message
            ):
                return message
            # Anything else is an unsolicited event; keep waiting for ours.
    raise TimeoutError("Timed out waiting for MPV IPC reply.")


def _ipc_roundtrip(
    pipe: str,
    command: list,
    *,
    tries: int = 8,
    open_delay: float = 0.15,
    reply_timeout: float = 3.0,
) -> dict:
    """Send one JSON IPC command and return MPV's reply object.

    Raises with MPV's own error text when the command is rejected, so
    transport failures are diagnosable instead of silent.
    """
    with _request_lock:
        request_id = next(_request_ids)
    payload = (
        json.dumps({"command": command, "request_id": request_id}) + "\n"
    ).encode("utf-8")
    kernel32 = _kernel32()
    GENERIC_READ_WRITE = 0xC0000000
    OPEN_EXISTING = 3
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    def _usable(handle: Any) -> bool:
        try:
            value = handle.value if isinstance(handle, ctypes.c_void_p) else handle
        except Exception:
            return False
        return value is not None and value not in (0, INVALID_HANDLE)

    last_error: Exception | None = None
    for _ in range(max(1, tries)):
        handle = None
        try:
            handle = kernel32.CreateFileW(pipe, GENERIC_READ_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if not _usable(handle):
                raise OSError(f"Could not open MPV IPC pipe ({ctypes.get_last_error()}).")
            written = ctypes.c_ulong(0)
            if not kernel32.WriteFile(handle, payload, len(payload), ctypes.byref(written), None):
                raise OSError(f"Could not write MPV IPC pipe ({ctypes.get_last_error()}).")
            if written.value != len(payload):
                raise OSError("Short write to MPV IPC pipe.")
            reply = _read_reply(kernel32, handle, request_id, timeout=reply_timeout)
            if reply.get("error") not in (None, "success"):
                raise RuntimeError(f"MPV rejected command {command!r}: {reply.get('error')}")
            return reply
        except (OSError, TimeoutError) as exc:  # pipe not ready yet; retry briefly
            last_error = exc
            time.sleep(open_delay)
        finally:
            if _usable(handle):
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
    if last_error is not None:
        raise last_error
    raise OSError("Could not reach MPV IPC pipe.")


class _FsExitKeyHook:
    """WH_KEYBOARD_LL hook: Esc/F11 exits video-only fullscreen.

    Installed on the GUI thread for the duration of video-only
    fullscreen and removed afterwards. The QML Escape shortcut cannot
    fire there (it belongs to the app window, which does not own keyboard
    focus) and MPV ignores keys, so a thread hook is the only
    focus-proof path. Acts only when the foreground window is our video
    (the container, its MPV child, or the exit pill), so Esc pressed
    anywhere else is never disturbed. Windows-only; ``install`` returns
    False everywhere else (the QML shortcut remains as backup).
    """

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    VK_ESCAPE = 0x1B
    VK_F11 = 0x7A

    class _KbdStruct(ctypes.Structure):
        _fields_ = [
            ("vkCode", ctypes.c_uint),
            ("scanCode", ctypes.c_uint),
            ("flags", ctypes.c_uint),
            ("time", ctypes.c_uint),
            ("dwExtraInfo", ctypes.c_void_p),
        ]

    def __init__(self) -> None:
        self._hook: Any = None
        self._proc: Any = None
        self._user32: Any = None
        self._on_exit: Any = None

    def install(self, on_exit: Any, container_hwnd: int, extra_hwnds: tuple = ()) -> bool:
        """Install the hook. Returns False when unavailable."""
        try:
            self.uninstall()
        except Exception:
            pass
        if sys.platform != "win32" or not callable(on_exit):
            return False
        try:
            container = int(container_hwnd or 0)
        except Exception:
            container = 0
        if container <= 0:
            return False
        try:
            allowed = {container}
            for extra in extra_hwnds or ():
                try:
                    number = int(extra or 0)
                except Exception:
                    continue
                if number > 0:
                    allowed.add(number)
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.IsChild.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.IsChild.restype = ctypes.c_bool
            user32.CallNextHookEx.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
            ]
            user32.CallNextHookEx.restype = ctypes.c_longlong
            user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
            user32.UnhookWindowsHookEx.restype = ctypes.c_bool
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ]
            user32.SetWindowsHookExW.restype = ctypes.c_void_p
            self._on_exit = on_exit
            struct_type = self._KbdStruct
            exit_keys = (self.VK_ESCAPE, self.VK_F11)
            down_msgs = (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)

            def _proc(nCode: int, wParam: int, lParam: int) -> int:
                try:
                    if nCode == 0 and wParam in down_msgs and lParam:
                        try:
                            vk = struct_type.from_address(int(lParam)).vkCode
                        except Exception:
                            vk = 0
                        if vk in exit_keys:
                            try:
                                foreground = int(user32.GetForegroundWindow() or 0)
                            except Exception:
                                foreground = 0
                            if foreground and (
                                foreground in allowed
                                or bool(user32.IsChild(container, foreground))
                            ):
                                try:
                                    if callable(self._on_exit):
                                        self._on_exit()
                                except Exception:
                                    pass
                                return 1  # swallow: our video owns the screen
                    return int(user32.CallNextHookEx(None, nCode, wParam, lParam))
                except Exception:
                    try:
                        return int(user32.CallNextHookEx(None, nCode, wParam, lParam))
                    except Exception:
                        return 0

            proc_type = ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
                ctypes.c_longlong, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
            )
            self._proc = proc_type(_proc)
            hook = user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._proc, None, 0)
            try:
                valid = int(hook or 0) > 0
            except Exception:
                valid = False
            if not valid:
                self._proc = None
                self._on_exit = None
                return False
            self._hook = hook
            self._user32 = user32
            return True
        except Exception:
            self._proc = None
            self._on_exit = None
            return False

    def uninstall(self) -> None:
        """Remove the hook. Idempotent; never raises."""
        hook, self._hook = self._hook, None
        self._on_exit = None
        if hook:
            try:
                user32 = self._user32 or ctypes.windll.user32  # type: ignore[attr-defined]
                user32.UnhookWindowsHookEx(hook)
            except Exception:
                pass
        # NOTE: self._proc is deliberately kept alive so a stray OS
        # callback can never hit a garbage-collected function pointer.


if _QRasterWindowBase is not None:

    class _FsExitPill(_QRasterWindowBase):
        """Clickable 'Exit fullscreen' pill above the fullscreen video.

        QML cannot render here (the native video paints above the whole
        Qt Quick scene), so this is a tiny native child of the promoted
        container: it travels with it automatically, needs no screen/DPI
        math beyond the parent size, and dies with the session. Opaque
        Theme-matching colors (translucency is unreliable for native
        child windows).
        """

        PILL_W = 252
        PILL_H = 40
        MARGIN = 16

        def __init__(self, parent: Any = None) -> None:
            super().__init__(parent)
            self._on_clicked: Any = None
            try:
                self.setTitle("Exit fullscreen")
            except Exception:
                pass
            try:
                self.resize(self.PILL_W, self.PILL_H)
            except Exception:
                pass
            try:
                self.setCursor(Qt.PointingHandCursor)
            except Exception:
                pass

        def set_clicked(self, callback: Any) -> None:
            self._on_clicked = callback

        def place_top_right(self, parent_w: int, parent_h: int) -> None:
            try:
                width = max(2, int(parent_w))
            except Exception:
                return
            try:
                self.setX(max(0, width - self.PILL_W - self.MARGIN))
                self.setY(self.MARGIN)
                self.resize(self.PILL_W, self.PILL_H)
            except Exception:
                pass

        def paintEvent(self, event: Any) -> None:
            try:
                from PySide6.QtGui import QPainter, QColor

                painter = QPainter(self)
                try:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setBrush(QColor(27, 27, 30))
                    painter.setPen(QColor(69, 69, 75))
                    painter.drawRoundedRect(
                        0, 0, self.PILL_W - 1, self.PILL_H - 1, 10, 10
                    )
                    font = self.font()
                    try:
                        font.setPixelSize(13)
                    except Exception:
                        pass
                    painter.setFont(font)
                    painter.setPen(QColor(241, 245, 249))
                    painter.drawText(
                        painter.viewport(),
                        int(Qt.AlignCenter),
                        "×  Exit fullscreen  (Esc)",
                    )
                finally:
                    painter.end()
            except Exception:
                pass

        def mousePressEvent(self, event: Any) -> None:
            try:
                if event is not None and event.button() == Qt.LeftButton:
                    if callable(self._on_clicked):
                        self._on_clicked()
                    return
            except Exception:
                pass

else:
    _FsExitPill = None  # type: ignore[assignment, misc]


class MpvEmbedController(QObject):
    """Owns the in-tab MPV container window and its IPC transport."""

    embedError = Signal(str)
    pausedChanged = Signal()
    mutedChanged = Signal()
    volumeChanged = Signal()
    activeChanged = Signal()
    tabActiveChanged = Signal()
    dialogOpenChanged = Signal()
    fullscreenChanged = Signal()

    MPV_DEFAULT_VOLUME = 100
    MPV_MAX_VOLUME = 130
    VOLUME_STEP = 5

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._main_window: Any | None = None
        self._container: Any | None = None
        self._session_active = False
        self._ipc_pipe = ""
        self._paused = False
        self._muted = False
        self._volume = self.MPV_DEFAULT_VOLUME
        self._fullscreen = False
        # Whether the Live tab is currently visible. The embed container is
        # a native child window that always paints above the Qt Quick scene
        # (QML clip/StackLayout cannot clip it), so it must be hidden
        # whenever the user switches to another tab — while the session
        # (audio/pipeline) keeps running in the background.
        self._tab_visible = True
        # True while a modal app dialog (e.g. the "Operation still active"
        # exit prompt) is open. Dialogs live in the Qt Quick scene, so the
        # native container would otherwise paint over them and steal clicks.
        self._dialog_open = False
        # Native HWND handed to MPV via --wid (0 = detached fallback where
        # MPV owns its own window and IPC fullscreen genuinely works).
        self._wid = 0
        # True while the video-only fullscreen mode is active (container
        # promoted to a top-level fullscreen window; the app window is
        # never touched). MPV cannot fullscreen itself when embedded via
        # --wid (it owns no top-level window), so the container itself
        # goes fullscreen instead. Only moved/reparented natively with
        # the same HWND and no flag changes, so MPV's attachment stays
        # valid.
        self._qt_fs = False
        # Fullscreen exit UI: native pill button above the video + a
        # foreground-gated Esc/F11 hook (see _show_fs_ui). Both funnel
        # into _fs_exit_requested; both are inert outside fullscreen.
        self._fs_pill: Any = None
        self._fs_keys = _FsExitKeyHook()
        # Last QML-reported geometry (parent-relative DIPs). Re-applied when
        # the session activates so early reports are not lost.
        self._geom: tuple[float, float, float, float] | None = None

    # -- window ---------------------------------------------------------
    def attach_main_window(self, window: Any) -> None:
        """Remember the QML root window; must run on the GUI thread."""
        self._main_window = window

    def _ensure_container(self) -> bool:
        """Create the (hidden) container child window. Returns success."""
        if self._container is not None:
            return True
        if self._main_window is None:
            return False
        try:
            from PySide6.QtGui import QWindow

            container = QWindow(self._main_window)
            container.setFlags(Qt.FramelessWindowHint)
            container.setTitle("DLSS 5 Live")
            container.resize(2, 2)
            container.setVisible(False)
            self._container = container
            return True
        except Exception as exc:
            self.embedError.emit(f"Could not create Live video slot: {exc}")
            return False

    def prepare(self, ipc_pipe: str) -> tuple[int, str]:
        """Create the container (hidden) and return its HWND + IPC pipe.

        Called from ``startLive`` on the GUI thread *before* the session
        worker starts, so the pipeline can pass both straight to MPV.
        Falls back to ``(0, pipe)`` (detached window) when unavailable.
        """
        self._ipc_pipe = ipc_pipe or ""
        self._paused = False
        self._muted = False
        self._volume = self.MPV_DEFAULT_VOLUME
        self._fullscreen = False
        self._qt_fs = False
        self._wid = 0
        for signal in (self.pausedChanged, self.mutedChanged, self.volumeChanged,
                       self.fullscreenChanged):
            try:
                signal.emit()
            except Exception:
                pass
        if not self._ensure_container():
            return 0, self._ipc_pipe
        try:
            assert self._container is not None
            self._container.setVisible(False)
            hwnd = int(self._container.winId())
            self._wid = int(hwnd or 0)
            return (hwnd if hwnd else 0), self._ipc_pipe
        except Exception as exc:
            self._wid = 0
            self.embedError.emit(f"Could not query Live video slot handle: {exc}")
            return 0, self._ipc_pipe

    @Slot(float, float, float, float)
    def setGeometry(self, x: float, y: float, w: float, h: float) -> None:
        """Move/resize the container (client-relative DIPs, from QML)."""
        try:
            self._geom = (float(x), float(y), float(w), float(h))
        except Exception:
            return
        if self._container is None:
            return
        self._apply_visibility()

    @Slot(bool)
    def setTabActive(self, active: bool) -> None:
        """Show/hide the native container for Live-tab visibility.

        Called from the ``activeTab`` setter on the GUI thread whenever the
        user switches tabs. The Live session (pipeline + audio) keeps
        running; only the native overlay window is hidden so it can never
        paint over Upscale / Neural Rendering / other tabs. Returning to
        the Live tab re-shows it at the last reported geometry.
        """
        try:
            active = bool(active)
        except Exception:
            return
        if active == self._tab_visible:
            return
        self._tab_visible = active
        try:
            self.tabActiveChanged.emit()
        except Exception:
            pass
        if not active and self._fullscreen:
            self._exit_fullscreen_if_needed()
        self._apply_visibility()

    @Slot(bool)
    def setDialogOpen(self, open: bool) -> None:
        """Hide the native container while a modal dialog is open.

        QML popups/dialogs (including the exit prompt) live in the Qt Quick
        scene, which a native child window always paints above. Hiding here
        keeps the dialog clickable; the session keeps running and the cached
        geometry re-shows the video as soon as the dialog closes.
        """
        try:
            open = bool(open)
        except Exception:
            return
        if open == self._dialog_open:
            return
        self._dialog_open = open
        try:
            self.dialogOpenChanged.emit()
        except Exception:
            pass
        if open:
            self._exit_fullscreen_if_needed()
        self._apply_visibility()

    def _is_embedded(self) -> bool:
        """True when MPV renders into our container (vs detached window)."""
        try:
            return bool(self._wid) and self._container is not None
        except Exception:
            return False

    def _exit_fullscreen_if_needed(self) -> None:
        """Leave fullscreen so no orphan overlay survives a hide/stop.

        Video-only mode (embedded): dock the container back into the app
        window synchronously. Detached fallback: ask MPV to leave
        fullscreen via IPC (it owns its window there, so IPC works).
        """
        if not self._fullscreen:
            self._qt_fs = False
            return
        if self._qt_fs:
            try:
                self._hide_fs_ui()
            except Exception:
                pass
            try:
                self._restore_video_slot()
            except Exception:
                pass
            self._qt_fs = False
            self._fullscreen = False
            try:
                self.fullscreenChanged.emit()
            except Exception:
                pass
            return
        try:
            self._send(
                ["set", "fullscreen", "no"],
                lambda _reply: (setattr(self, "_fullscreen", False),
                                self.fullscreenChanged.emit()),
            )
        except Exception:
            pass

    def _enter_qt_fullscreen(self) -> None:
        """Put only the video on screen; the app window stays as it is.

        The container is promoted to a top-level fullscreen window (pure
        native reparent, same HWND, no flags touched), so MPV's ``--wid``
        attachment keeps rendering and its child window covers the new
        fullscreen parent automatically, letterboxing the video itself.
        """
        if self._main_window is None or self._container is None:
            raise RuntimeError("Live video slot is unavailable.")
        try:
            screen = self._main_window.screen()
        except Exception:
            screen = None
        try:
            if screen is not None:
                self._container.setScreen(screen)
        except Exception:
            pass
        try:
            self._container.setParent(None)
        except Exception as exc:
            raise RuntimeError(f"Could not enter fullscreen: {exc}.")
        try:
            actual_wid = int(self._container.winId())
        except Exception:
            actual_wid = 0
        if self._wid and actual_wid and actual_wid != self._wid:
            # Native window was recreated: MPV is still attached to the
            # dead HWND. Dock back immediately so the session keeps
            # playing in its slot instead of going black.
            try:
                self._container.setParent(self._main_window)
            except Exception:
                pass
            try:
                self._apply_visibility()
            except Exception:
                pass
            raise RuntimeError(
                "Fullscreen is not supported on this setup "
                "(video slot was recreated); playing in-slot."
            )
        try:
            self._container.showFullScreen()
        except Exception as exc:
            try:
                self._container.setParent(self._main_window)
            except Exception:
                pass
            raise RuntimeError(f"Could not enter fullscreen: {exc}.")
        try:
            self._container.raise_()
        except Exception:
            pass
        self._qt_fs = True
        self._fullscreen = True
        try:
            self.fullscreenChanged.emit()
        except Exception:
            pass
        try:
            self._show_fs_ui()
        except Exception:
            pass

    def _fs_exit_requested(self) -> None:
        """Exit fullscreen from the pill/hook. No-op when not fullscreen."""
        try:
            if self._fullscreen:
                self.toggleFullscreen()
        except Exception:
            pass

    def _show_fs_ui(self) -> None:
        """Show the exit pill above the fs video and arm the Esc hook."""
        if self._container is None:
            return
        try:
            if _FsExitPill is not None:
                if self._fs_pill is None:
                    self._fs_pill = _FsExitPill(self._container)
                    self._fs_pill.set_clicked(self._fs_exit_requested)
                try:
                    screen = self._container.screen()
                    if screen is not None:
                        size = screen.size()
                        parent_w, parent_h = int(size.width()), int(size.height())
                    else:
                        parent_w, parent_h = 0, 0
                except Exception:
                    parent_w, parent_h = 0, 0
                if parent_w < 2 or parent_h < 2:
                    try:
                        parent_w = int(self._container.width())
                        parent_h = int(self._container.height())
                    except Exception:
                        parent_w, parent_h = 0, 0
                if parent_w >= 2 and parent_h >= 2:
                    self._fs_pill.place_top_right(parent_w, parent_h)
                try:
                    self._fs_pill.raise_()
                except Exception:
                    pass
                try:
                    if not self._fs_pill.isVisible():
                        self._fs_pill.setVisible(True)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            pill_hwnd = 0
            if self._fs_pill is not None:
                try:
                    pill_hwnd = int(self._fs_pill.winId() or 0)
                except Exception:
                    pill_hwnd = 0
            self._fs_keys.install(
                self._fs_exit_requested,
                int(self._wid or 0),
                (pill_hwnd,) if pill_hwnd > 0 else (),
            )
        except Exception:
            pass

    def _hide_fs_ui(self) -> None:
        """Hide the exit pill and disarm the Esc hook. Idempotent."""
        try:
            self._fs_keys.uninstall()
        except Exception:
            pass
        try:
            if self._fs_pill is not None and self._fs_pill.isVisible():
                self._fs_pill.setVisible(False)
        except Exception:
            pass

    def _restore_video_slot(self) -> None:
        """Dock the container back into the app window's video slot."""
        if self._container is None:
            return
        try:
            self._container.showNormal()
        except Exception:
            pass
        if self._main_window is not None:
            try:
                self._container.setParent(self._main_window)
            except Exception:
                pass

    def _apply_visibility(self) -> None:
        """Central visibility gate: session AND tab AND no dialog AND geom.

        While video-only fullscreen is active the container is a top-level
        fullscreen window: slot geometry syncs must NOT touch it (they
        would drag the fullscreen window around), so this is a no-op that
        only ensures it stays visible. Slot geometry is preserved for the
        restore.
        """
        if self._container is None:
            return
        try:
            if not self._session_active or not self._tab_visible or self._dialog_open:
                try:
                    if self._container.isVisible():
                        self._container.setVisible(False)
                except Exception:
                    try:
                        self._container.setVisible(False)
                    except Exception:
                        pass
                return
            if self._qt_fs and self._fullscreen and self._is_embedded():
                try:
                    if not self._container.isVisible():
                        self._container.setVisible(True)
                except Exception:
                    pass
                return
            if self._geom is None:
                return
            x, y, w, h = self._geom
            if w < 2 or h < 2:
                self._container.setVisible(False)
                return
            self._container.setX(int(round(x)))
            self._container.setY(int(round(y)))
            self._container.resize(max(2, int(round(w))), max(2, int(round(h))))
            if not self._container.isVisible():
                self._container.setVisible(True)
        except Exception:
            pass

    def set_session_active(self, active: bool) -> None:
        """Show/hide the container for a Live session (GUI thread)."""
        self._session_active = bool(active)
        if not active:
            # Dock the video back first: stopping mid-fullscreen must
            # never strand a fullscreen video window with no session.
            try:
                self._exit_fullscreen_if_needed()
            except Exception:
                pass
            self._paused = False
            self._muted = False
            self._volume = self.MPV_DEFAULT_VOLUME
            self._fullscreen = False
            self._qt_fs = False
            for signal in (self.pausedChanged, self.mutedChanged, self.volumeChanged,
                           self.fullscreenChanged):
                try:
                    signal.emit()
                except Exception:
                    pass
        try:
            self.activeChanged.emit()
        except Exception:
            pass
        self._apply_visibility()

    @Property(bool, notify=activeChanged)
    def isActive(self) -> bool:
        return self._session_active

    @Property(bool, notify=tabActiveChanged)
    def tabActive(self) -> bool:
        return self._tab_visible

    @Property(bool, notify=dialogOpenChanged)
    def dialogOpen(self) -> bool:
        return self._dialog_open

    def shutdown(self) -> None:
        """Hide and release the container (app exit, GUI thread)."""
        try:
            self._hide_fs_ui()
        except Exception:
            pass
        self._session_active = False
        self._tab_visible = True
        self._dialog_open = False
        self._qt_fs = False
        self._fs_pill = None
        self._wid = 0
        self._geom = None
        try:
            if self._container is not None:
                self._container.setVisible(False)
        except Exception:
            pass
        self._container = None
        self._main_window = None

    # -- transport (mpv JSON IPC, checked replies on a daemon thread) ----
    def _send(self, command: list, apply: Any) -> None:
        """Run one IPC round-trip; ``apply(reply)`` only on MPV success."""
        pipe = self._ipc_pipe
        if not pipe:
            self.embedError.emit("Live player control is unavailable (no session).")
            return

        def _run() -> None:
            try:
                reply = _ipc_roundtrip(pipe, command)
            except Exception as exc:
                try:
                    self.embedError.emit(f"Live player control failed: {exc}")
                except Exception:
                    pass
                return
            try:
                apply(reply)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True, name="dlss5-live-ipc").start()

    @Slot()
    def togglePause(self) -> None:
        """Toggle MPV pause; flips the label only on MPV success."""
        def _apply(_reply: dict) -> None:
            self._paused = not self._paused
            self.pausedChanged.emit()

        self._send(["cycle", "pause"], _apply)

    @Slot()
    def toggleMute(self) -> None:
        """Toggle MPV mute; flips the label only on MPV success."""
        def _apply(_reply: dict) -> None:
            self._muted = not self._muted
            self.mutedChanged.emit()

        self._send(["cycle", "mute"], _apply)

    def _bump_volume(self, delta: int) -> None:
        """Step MPV volume relatively, then read back the true value."""
        pipe = self._ipc_pipe
        if not pipe:
            self.embedError.emit("Live player control is unavailable (no session).")
            return

        def _run() -> None:
            try:
                _ipc_roundtrip(pipe, ["add", "volume", int(delta)])
                reply = _ipc_roundtrip(pipe, ["get_property", "volume"])
                value = int(round(float(reply.get("data", self._volume))))
            except Exception as exc:
                try:
                    self.embedError.emit(f"Live volume control failed: {exc}")
                except Exception:
                    pass
                return
            try:
                self._volume = max(0, min(self.MPV_MAX_VOLUME, value))
                self.volumeChanged.emit()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True, name="dlss5-live-ipc").start()

    @Slot()
    def volumeUp(self) -> None:
        """Raise MPV volume one step; updates the label on success."""
        self._bump_volume(self.VOLUME_STEP)

    @Slot()
    def volumeDown(self) -> None:
        """Lower MPV volume one step; updates the label on success."""
        self._bump_volume(-self.VOLUME_STEP)

    @Slot()
    def toggleFullscreen(self) -> None:
        """Toggle video-only fullscreen (Qt-side when embedded, IPC if detached).

        MPV cannot fullscreen itself while embedded via ``--wid`` (it owns
        no top-level window; ``cycle fullscreen`` succeeds with zero visual
        effect). The embedded path therefore promotes the container itself
        to a top-level fullscreen window — only the video takes the
        screen, the app window stays exactly as it is. The detached
        fallback keeps the original IPC command, which works there.
        """
        if self._fullscreen:
            try:
                self._exit_fullscreen_if_needed()
            except Exception as exc:
                self.embedError.emit(f"Could not leave fullscreen: {exc}")
                return
            self._apply_visibility()
            return
        if self._is_embedded():
            if not self._session_active:
                self.embedError.emit("Start a Live session before using Full Screen.")
                return
            try:
                self._enter_qt_fullscreen()
            except Exception as exc:
                self.embedError.emit(f"Could not enter fullscreen: {exc}")
                return
            self._apply_visibility()
            return

        def _apply(_reply: dict) -> None:
            self._fullscreen = not self._fullscreen
            self.fullscreenChanged.emit()

        self._send(["cycle", "fullscreen"], _apply)

    @Property(bool, notify=fullscreenChanged)
    def fullscreen(self) -> bool:
        return self._fullscreen

    @Property(bool, notify=pausedChanged)
    def paused(self) -> bool:
        return self._paused

    @Property(bool, notify=mutedChanged)
    def muted(self) -> bool:
        return self._muted

    @Property(int, notify=volumeChanged)
    def volume(self) -> int:
        return self._volume
