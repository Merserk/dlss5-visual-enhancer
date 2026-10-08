from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

from ..portable import configure_portable_environment

configure_portable_environment()

from PySide6.QtCore import QRect, QUrl, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

from ..core.paths import ROOT, OUTPUTS, LOGS, LIVE_DIR, JOBS
from ..core.cache_cleanup import cleanup_old_caches
from ..core import app_log
from .image_provider import IconImageProvider, PreviewImageProvider
from .bridge import AppBridge
from .preview_player import PreviewPlayer
from .responsiveness import UiResponsivenessMonitor
from .window_geometry import restore_window_geometry
from .window_focus import ClickFocusFilter
from .win_frameless import install_win_chrome, remove_win_chrome, ensure_win_style, log_native_chrome


def hide_inherited_console() -> None:
    """Detach any console inherited when launched through the GUI launcher.

    pythonw.exe does not allocate a console. If launched from a terminal,
    detach our process without hiding the caller's terminal window.
    """
    if os.environ.get("DLSS5_WINDOWLESS") != "1":
        return
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            kernel32.FreeConsole()
    except Exception:
        pass


def apply_windows_dark_titlebar(win_id: int) -> None:
    """Enable Windows 10/11 dark titlebar styling when native chrome is used."""
    try:
        value = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(win_id), ctypes.c_uint(20), ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass


def enable_dpi_awareness() -> str:
    """Declare per-monitor DPI awareness before Qt initializes.

    The native frame math (maximized clamp, hit-test mapping) mixes physical
    pixels with Qt DIPs; without PMv2 awareness Windows scales one side and
    maximized geometry lands with a large offset. One-shot API: must run
    before QGuiApplication exists. Never raises.
    """
    if sys.platform != "win32":
        return "non-windows"
    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(0xFFFFFFFFFFFFFFFC)):
                return "per-monitor-v2"
        except Exception:
            pass
        try:
            shcore = ctypes.windll.shcore
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            if shcore.SetProcessDpiAwareness(2) == 0:
                return "per-monitor-v1"
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware.argtypes = []
            user32.SetProcessDPIAware.restype = ctypes.c_bool
            if user32.SetProcessDPIAware():
                return "system-aware"
        except Exception:
            pass
    except Exception:
        pass
    return "unchanged"


def resolve_app_icon() -> Path | None:
    """Locate the deployed multi-size Windows icon, with PNG fallbacks."""
    try:
        candidates = (
            Path(__file__).resolve().parent / "app.ico",
            ROOT / "app.ico",
            ROOT / "native (dev)" / "ui" / "launcher (qt6)" / "app.ico",
            ROOT / "native (dev)" / "assets" / "icon.png",
            ROOT / "icon.png",
            Path(__file__).resolve().parent / "icon.png",
        )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except Exception:
                continue
    except Exception:
        pass
    return None


# Keep the shell identity stable across releases and independent of the
# obsolete v10 taskbar group, whose cached branding can survive icon updates.
APP_USER_MODEL_ID = "Merserk.VisualEnhancer"
APP_DISPLAY_NAME = "Visual Enhancer"


def set_native_taskbar_icons(window) -> bool:
    """Give Windows' window-class fallback the same icons as the Qt window."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_size_t
        user32.CopyIcon.argtypes = [ctypes.c_void_p]
        user32.CopyIcon.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_size_t]
        user32.SetClassLongPtrW.restype = ctypes.c_size_t
        user32.GetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetClassLongPtrW.restype = ctypes.c_size_t
        user32.DestroyIcon.argtypes = [ctypes.c_void_p]
        hwnd = int(window.winId())
        owned = getattr(window, "_taskbar_class_icons", None)
        if owned is None:
            owned = []
            for class_index, icon_size in ((-14, 1), (-34, 0)):
                source = user32.SendMessageW(hwnd, 0x007F, icon_size, 0)
                copied = user32.CopyIcon(source) if source else None
                if not copied:
                    for _, icon, _ in owned:
                        user32.DestroyIcon(icon)
                    return False
                previous = user32.GetClassLongPtrW(hwnd, class_index)
                owned.append((class_index, copied, previous))
            window._taskbar_class_icons = owned

            def release_icons() -> None:
                for class_index, icon, previous in owned:
                    if user32.GetClassLongPtrW(hwnd, class_index) == icon:
                        user32.SetClassLongPtrW(hwnd, class_index, previous)
                    user32.DestroyIcon(icon)
                window._taskbar_class_icons = None

            QGuiApplication.instance().aboutToQuit.connect(release_icons)
        for class_index, icon, _ in owned:
            user32.SetClassLongPtrW(hwnd, class_index, icon)
            if user32.GetClassLongPtrW(hwnd, class_index) != icon:
                return False
        return True
    except Exception:
        return False


def resolve_launcher_exe() -> Path | None:
    """Locate the branded launcher .exe next to the app folder. Never raises."""
    try:
        for name in ("Visual Enhancer.exe", "DLSS 5 Visual Enhancer.exe"):
            candidate = ROOT / name
            if candidate.is_file():
                return candidate
    except Exception:
        pass
    return None


def set_process_appusermodel_id() -> bool:
    """Declare this pythonw.exe process as our app to the taskbar.

    Without an explicit AppUserModelID, Windows groups the window under the
    host executable, so the taskbar shows "Python" with the Python icon.
    Must run before any UI is presented. Never raises.
    """
    if sys.platform != "win32":
        return False
    try:
        shell32 = ctypes.windll.shell32
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.c_wchar_p]
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        return int(shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)) == 0
    except Exception:
        return False


def stamp_taskbar_relaunch(hwnd: int) -> bool:
    """Brand the live taskbar button via window relaunch properties.

    Sets RelaunchCommand/DisplayName/IconResource (pointing at the branded
    launcher .exe) plus the window-level AppID, so the Jump List header shows
    the program name and VE icon, and pinning/relaunch targets the launcher.
    Never raises.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        try:
            ole32.CoInitialize(None)
        except Exception:
            pass

        launcher = resolve_launcher_exe()
        launcher_str = str(launcher) if launcher is not None else ""

        class _GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", ctypes.c_ulong),
                ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class _PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", _GUID), ("pid", ctypes.c_ulong)]

        class _PROPVARIANT(ctypes.Structure):
            # PROPVARIANT is 24 bytes on Win64: the union also contains
            # counted arrays (a count plus an aligned pointer).
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("value", ctypes.c_void_p),
                ("union_tail", ctypes.c_void_p),
            ]

        def _guid(s: str) -> _GUID:
            parts = s.strip("{}").split("-")
            d1 = int(parts[0], 16)
            d2 = int(parts[1], 16)
            d3 = int(parts[2], 16)
            d4 = bytes.fromhex(parts[3] + parts[4])
            return _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))

        def _pkey(fmtid: str, pid: int) -> _PROPERTYKEY:
            return _PROPERTYKEY(_guid(fmtid), pid)

        def _str_var(text: str):
            buf = ctypes.create_unicode_buffer(text)
            var = _PROPVARIANT()
            var.vt = 31  # VT_LPWSTR
            var.value = ctypes.cast(buf, ctypes.c_void_p).value
            return var, buf  # keep buffer alive for the call

        IID_PPV = _guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")  # IID_IPropertyStore
        APPMODEL_FMTID = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
        PKEY_RelaunchCommand = _pkey(APPMODEL_FMTID, 2)
        PKEY_RelaunchIconResource = _pkey(APPMODEL_FMTID, 3)
        PKEY_RelaunchDisplayName = _pkey(APPMODEL_FMTID, 4)
        PKEY_AppId = _pkey(APPMODEL_FMTID, 5)

        shell32.SHGetPropertyStoreForWindow.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetPropertyStoreForWindow.restype = ctypes.c_long
        store = ctypes.c_void_p()
        if int(shell32.SHGetPropertyStoreForWindow(
                ctypes.c_void_p(hwnd), ctypes.byref(IID_PPV), ctypes.byref(store))) != 0:
            return False
        if not store.value:
            return False
        try:
            vtbl = ctypes.cast(store.value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            set_value_type = ctypes.CFUNCTYPE(
                ctypes.c_long, ctypes.c_void_p,
                ctypes.POINTER(_PROPERTYKEY), ctypes.POINTER(_PROPVARIANT))
            commit_type = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
            release_type = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            set_value = set_value_type(vtbl.contents[6])
            commit = commit_type(vtbl.contents[7])

            keepalive = []
            if launcher_str:
                icon_path = resolve_app_icon()
                icon_resource = ('"' + str(icon_path) + '",0'
                                 if icon_path is not None and icon_path.suffix.lower() == ".ico"
                                 else '"' + launcher_str + '",0')
                for key, text in (
                    (PKEY_RelaunchCommand, '"' + launcher_str + '"'),
                    (PKEY_RelaunchDisplayName, APP_DISPLAY_NAME),
                    (PKEY_RelaunchIconResource, icon_resource),
                ):
                    var, buf = _str_var(text)
                    keepalive.append(buf)
                    if int(set_value(store, ctypes.byref(key), ctypes.byref(var))) != 0:
                        return False
            var, buf = _str_var(APP_USER_MODEL_ID)
            keepalive.append(buf)
            if int(set_value(store, ctypes.byref(PKEY_AppId), ctypes.byref(var))) != 0:
                return False
            return int(commit(store)) == 0
        finally:
            try:
                release = ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                    ctypes.cast(store.value, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents[2])
                release(store)
            except Exception:
                pass
    except Exception:
        return False


def launch_desktop() -> int:
    """Prepare runtime and the first Qt frame before revealing the window."""
    started = time.perf_counter()

    def mark(stage: str) -> None:
        app_log.info("startup", f"{stage}_ms={(time.perf_counter() - started) * 1000:.1f}")

    hide_inherited_console()
    OUTPUTS.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    LIVE_DIR.mkdir(exist_ok=True)
    JOBS.mkdir(exist_ok=True)
    # Startup sweep of stale temp caches (24h+ old): crash orphans, old
    # staged pastes and previews. Queues are in-memory per session, so no
    # live references can exist yet. Finish the sweep before showing the UI.
    try:
        cleanup_old_caches()
    except Exception:
        pass
    log_native_chrome(f"dpi-awareness={enable_dpi_awareness()}")
    # Declare our taskbar identity before any UI exists; otherwise Windows
    # groups the window under the host executable ("Python").
    set_process_appusermodel_id()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    # Use FFmpeg for the original-media player as well as export formats.
    # Cached previews use our direct 10-bit frame player below.
    os.environ["QT_MEDIA_BACKEND"] = "ffmpeg"
    app = QGuiApplication(sys.argv)
    from PySide6.QtQml import qmlRegisterType
    qmlRegisterType(PreviewPlayer, "VisualEnhancer", 1, 0, "PreviewPlayer")
    app.setApplicationName("Visual Enhancer")
    app.setApplicationDisplayName("Visual Enhancer")
    app.setOrganizationName("Merserk")
    icon_path = None
    try:
        icon_path = resolve_app_icon()
        if icon_path is not None:
            app.setWindowIcon(QIcon(str(icon_path)))
    except Exception:
        pass

    image_provider = PreviewImageProvider()
    bridge = AppBridge(image_provider)
    from .export_power import ExportPowerFilter
    export_power = ExportPowerFilter(bridge)
    app.installNativeEventFilter(export_power)
    app.aboutToQuit.connect(bridge.shutdown)
    bridge.initializeRuntime()
    mark("runtime_ready" if bridge.runtimeState == "Ready" else "runtime_failed")

    engine = QQmlApplicationEngine()
    engine.addImageProvider("preview", image_provider)
    engine.addImageProvider("icons", IconImageProvider())
    engine.rootContext().setContextProperty("backend", bridge)
    bridge.languageChanged.connect(engine.retranslate)

    qml_path = Path(__file__).resolve().parent / "qml" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_path)))
    mark("qml_loaded")
    if not engine.rootObjects():
        try:
            if sys.stderr is not None:
                print("Error: Failed to load QML root object.", file=sys.stderr)
        except Exception:
            pass
        bridge.shutdown()
        del engine
        return 1

    root_window = engine.rootObjects()[0]
    focus_filter = ClickFocusFilter(root_window)
    restore_window_geometry(
        root_window,
        QRect(bridge.windowX, bridge.windowY, bridge.windowWidth, bridge.windowHeight),
        has_position=(bridge.windowX, bridge.windowY) != (-1, -1),
    )
    root_window.create()
    # Brand the live taskbar button (name + VE icon + launcher relaunch) and
    # pin the window icon explicitly so pythonw.exe art never leaks through.
    try:
        if icon_path is not None:
            root_window.setIcon(QIcon(str(icon_path)))
            log_native_chrome(f"taskbar-class-icons={set_native_taskbar_icons(root_window)}")
    except Exception:
        pass
    try:
        stamp_taskbar_relaunch(int(root_window.winId()))
    except Exception:
        pass
    # Native OS chrome (snap layouts, Win+arrows, drag-to-snap/restore) for the
    # frameless window. QML keeps working fallbacks where this stays inactive.
    install_win_chrome(root_window)
    # Parent window for the in-tab Live player container (MPV --wid embed).
    try:
        bridge.attachMainWindow(root_window)
    except Exception:
        pass
    # Qt Quick can build its first scene graph while the native window remains
    # hidden. Keep the resulting graphics resources for the visible frame.
    try:
        app.processEvents()
        frame = root_window.grabWindow()
        if frame.isNull():
            app_log.error("startup", "hidden Qt frame was empty")
        del frame
    except Exception as exc:
        app_log.error("startup", f"hidden Qt frame failed: {exc}")
    mark("hidden_frame")

    first_frame_logged = False

    def on_first_frame() -> None:
        nonlocal first_frame_logged
        if not first_frame_logged:
            first_frame_logged = True
            mark("first_visible_frame")

    root_window.frameSwapped.connect(on_first_frame)

    # Always launch maximized, even when the last session was restored or
    # snapped to one side. Saved geometry still supplies the restore size.
    root_window.showMaximized()
    mark("window_shown")

    # Qt re-applies window flags when shown, so restore native chrome once.
    try:
        ensure_win_style(int(root_window.winId()))
    except Exception:
        pass
    try:
        apply_windows_dark_titlebar(int(root_window.winId()))
    except Exception:
        pass

    responsiveness = UiResponsivenessMonitor(
        Path(app_log.session_path()).with_suffix(".ui-hang.err"),
        context=lambda: (f"operation={bridge.operationState}; "
                         f"selected={bridge._context().selected_path}"),
        parent=app,
    )
    app.aboutToQuit.connect(responsiveness.stop)
    try:
        return app.exec()
    finally:
        responsiveness.stop()
        # aboutToQuit normally runs this first. Keep the fallback for exits
        # that bypass the signal, then dismantle QML and the native event
        # filter while the application object is still alive.
        try:
            bridge.shutdown()
        finally:
            try:
                remove_win_chrome(root_window)
            finally:
                del root_window
                del engine
