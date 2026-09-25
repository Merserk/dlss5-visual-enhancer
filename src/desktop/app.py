from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine

from ..core.paths import ROOT, OUTPUTS, LOGS, LIVE_DIR
from ..core.cache_cleanup import cleanup_old_caches
from ..core import app_log
from ..core.power import disable_power_throttling
from .image_provider import IconImageProvider, PreviewImageProvider
from .bridge import AppBridge
from .win_frameless import install_win_chrome, remove_win_chrome, ensure_win_style, log_native_chrome


def hide_inherited_console() -> None:
    """Detach/hide any console inherited when launched windowless.

    The GUI launcher sets DLSS5_WINDOWLESS=1 and starts pythonw.exe with
    CREATE_NO_WINDOW. If Windows still attached a console (e.g. launched
    from a console python, or a flashing helper), hide it so double-clicking
    the .exe shows only the Qt window.
    """
    if os.environ.get("DLSS5_WINDOWLESS") != "1":
        return
    if sys.platform != "win32":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            try:
                user32.ShowWindow(hwnd, 0)  # SW_HIDE
            except Exception:
                pass
            try:
                kernel32.FreeConsole()
            except Exception:
                pass
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
    """Locate the canonical app icon (native (dev)/icon.png). Never raises."""
    try:
        candidates = (
            ROOT / "native (dev)" / "icon.png",
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


APP_USER_MODEL_ID = "Merserk.VisualEnhancer.v10"
APP_DISPLAY_NAME = "Visual Enhancer"


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
            # 16 bytes: vt + reserved, then the value union. Only VT_LPWSTR
            # is used here; SetValue copies the string synchronously.
            _fields_ = [
                ("vt", ctypes.c_ushort),
                ("wReserved1", ctypes.c_ushort),
                ("wReserved2", ctypes.c_ushort),
                ("wReserved3", ctypes.c_ushort),
                ("value", ctypes.c_void_p),
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
                for key, text in (
                    (PKEY_RelaunchCommand, launcher_str),
                    (PKEY_RelaunchDisplayName, APP_DISPLAY_NAME),
                    (PKEY_RelaunchIconResource, launcher_str + ",0"),
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
    # Startup sweep of stale temp caches (24h+ old): crash orphans, old
    # staged pastes and previews. Queues are in-memory per session, so no
    # live references can exist yet. Finish the sweep before showing the UI.
    try:
        cleanup_old_caches()
    except Exception:
        pass
    log_native_chrome(f"dpi-awareness={enable_dpi_awareness()}")
    # Keep decode/stage/encode threads at full speed on laptops even when the
    # window is in the background (Windows 11 EcoQoS would otherwise throttle
    # them and starve the GPU).
    app_log.info("startup", f"power_throttling_opt_out={disable_power_throttling()}")
    # Declare our taskbar identity before any UI exists; otherwise Windows
    # groups the window under the host executable ("Python").
    set_process_appusermodel_id()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QGuiApplication(sys.argv)
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
    app.aboutToQuit.connect(bridge.shutdown)
    bridge.initializeRuntime()
    mark("runtime_ready" if bridge.runtimeState == "Ready" else "runtime_failed")

    engine = QQmlApplicationEngine()
    engine.addImageProvider("preview", image_provider)
    engine.addImageProvider("icons", IconImageProvider())
    engine.rootContext().setContextProperty("backend", bridge)

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
    root_window.create()
    # Brand the live taskbar button (name + VE icon + launcher relaunch) and
    # pin the window icon explicitly so pythonw.exe art never leaks through.
    try:
        if icon_path is not None:
            root_window.setIcon(QIcon(str(icon_path)))
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

    if bridge.windowMaximized:
        root_window.showMaximized()
    else:
        root_window.showNormal()
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

    try:
        return app.exec()
    finally:
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
