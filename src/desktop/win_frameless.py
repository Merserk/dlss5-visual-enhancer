from __future__ import annotations

"""Native frameless-window behaviors on Windows.

A Qt frameless window loses OS chrome behaviors: no Snap Layout flyout, no
Win+arrow snapping, no drag-to-snap/restore. This module restores them while
keeping the custom QML title-bar visuals:

- Adds WS_THICKFRAME / MAXIMIZEBOX / MINIMIZEBOX / SYSMENU to the style.
- ``WM_NCCALCSIZE`` returns 0 so the client fills the whole window (no visible
  frame; maximized fills exactly the work area, no transparent DWM insets).
- ``WM_NCHITTEST`` returns HTMINBUTTON/HTMAXBUTTON/HTCLOSE over the QML chrome
  buttons (the Snap Layout flyout appears over maximize), HTCAPTION over empty
  header background (native drag, double-click maximize, edge snap with
  proportional drag-restore), resize codes on the frame edges, and HTCLIENT
  over interactive QML (nav tabs) so clicks still reach Qt.
- ``WM_GETMINMAXINFO`` clamps maximized size/position to the monitor work area
  (taskbar stays visible) and enforces the 1080x700 minimum.

No-op on other platforms. On success the installer sets the dynamic property
``nativeChromeActive`` on the window so QML stands down its own drag/resize
handling where the OS takes over.
"""

import ctypes
import math
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, Qt

from ..core.paths import LOGS

WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_GETMINMAXINFO = 0x0024
WM_SYSCOMMAND = 0x0112
WM_NCLBUTTONDOWN = 0x00A1

# Caption-action syscommands (wParam masked with 0xFFF0 per MS docs; the
# low bits are internally used). SC_CLOSE is deliberately never intercepted
# so the QML unsaved-operation confirm dialog keeps working.
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_RESTORE = 0xF060

SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE = 9

HTCLIENT = 1
HTCAPTION = 2
HTMINBUTTON = 8
HTMAXBUTTON = 9
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
HTCLOSE = 20

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_EX_APPWINDOW = 0x00040000
WS_THICKFRAME = 0x00040000
WS_MAXIMIZEBOX = 0x00010000
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000
# Frame bits the OS requires for caption-button actions. DefWindowProc
# consumes an HTMINBUTTON click yet silently drops the minimize when
# WS_MINIMIZEBOX is absent (same gating for maximize/resize) — a fully
# dead button no QML fallback can rescue, since the click never reaches Qt.
STYLE_CHROME_BITS = WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_SYSMENU

SM_CXSIZEFRAME = 32
SM_CXPADDEDBORDER = 92
MONITOR_DEFAULTTONEAREST = 2

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020

HEADER_HEIGHT_DIP = 50.0
MIN_WIDTH_DIP = 1080
MIN_HEIGHT_DIP = 700
# Hit-test tolerance (DIPs) applied around each chrome button rect. The QML
# buttons are 28px with 2px gaps; without tolerance, clicks landing exactly
# in a gap (or on a 1px fractional-DPR rounding seam) fall through to
# HTCAPTION resize/drag handling and look like dead clicks that need several
# retries. 3 DIPs fully covers the gaps; overlapping claims resolve
# deterministically in check order (close > max > min), same as before.
CHROME_BTN_TOL_DIP = 3.0


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", _POINT),
        ("ptMaxSize", _POINT),
        ("ptMaxPosition", _POINT),
        ("ptMinTrackSize", _POINT),
        ("ptMaxTrackSize", _POINT),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", _POINT),
    ]


_USER32 = None
_LOG_CAP_BYTES = 64 * 1024


def log_native_chrome(text: str) -> None:
    """Append one diagnostics line; never raises (used for snap/maximize triage)."""
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        path = LOGS / "native-chrome.log"
        try:
            if path.is_file() and path.stat().st_size > _LOG_CAP_BYTES:
                path.unlink(missing_ok=True)
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def _user32():
    """Cached user32 handle with prototypes (Windows only; never call elsewhere)."""
    global _USER32
    if _USER32 is None:
        u = ctypes.windll.user32
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = wintypes.LPARAM
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LPARAM]
        u.SetWindowLongPtrW.restype = wintypes.LPARAM
        u.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        u.SetWindowPos.restype = wintypes.BOOL
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(_POINT)]
        u.ClientToScreen.restype = wintypes.BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        _USER32 = u
    return _USER32


def _msg_address(message) -> int | None:
    try:
        return int(message.__int__())
    except Exception:
        pass
    try:
        return int(message)
    except Exception:
        return None


class WinFramelessFilter(QAbstractNativeEventFilter):
    """Hit-test + frame handling for the single main window (Windows only)."""

    def __init__(self, window) -> None:
        super().__init__()
        self._window = window
        try:
            self._hwnd = int(window.winId())
        except Exception:
            self._hwnd = 0
        self._logged_calcsize = False
        self._logged_minmax = False

    # -- helpers ---------------------------------------------------------
    def _dip_rect(self, name: str) -> tuple[float, float, float, float] | None:
        try:
            rect = self._window.property(name)
        except Exception:
            return None
        if rect is None:
            return None
        try:
            x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        except Exception:
            return None
        if w <= 0 or h <= 0:
            return None
        return (float(x), float(y), float(w), float(h))

    def _rects_ready(self) -> bool:
        """True once QML has published all hit-test rects with real sizes."""
        for name in ("closeBtnRect", "maxBtnRect", "minBtnRect", "navTabsRect"):
            if self._dip_rect(name) is None:
                return False
        return True

    def _device_ratio(self) -> float:
        try:
            dpr = float(self._window.devicePixelRatio())
        except Exception:
            return 1.0
        return dpr if dpr > 0 else 1.0

    def _window_rect(self) -> tuple[int, int, int, int] | None:
        rect = _RECT()
        try:
            if not _user32().GetWindowRect(self._hwnd, ctypes.byref(rect)):
                return None
        except Exception:
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def _client_origin(self) -> tuple[int, int] | None:
        """Screen position (physical px) of the Qt client/content origin.

        For a WS_THICKFRAME frameless window GetWindowRect includes the
        invisible resize borders, while QML DIP rects are relative to the
        client origin. Mapping buttons from the outer origin shifts every
        hit region ~7px (more at >100% DPI) off the drawn button, making
        edge clicks fall through to resize/caption handling.
        """
        try:
            pt = _POINT(0, 0)
            if not _user32().ClientToScreen(self._hwnd, ctypes.byref(pt)):
                return None
        except Exception:
            return None
        return (pt.x, pt.y)

    def _in_dip_rect(
        self, name: str, px: int, py: int,
        origin_x: int, origin_y: int, dpr: float,
        tol_dip: float = 0.0,
    ) -> bool:
        rect = self._dip_rect(name)
        if rect is None:
            return False
        # Inflate in DIP space, then snap outward to whole physical pixels so
        # a fractional-DPR rounding edge can never carve a dead seam into a
        # 28px target or its 2px inter-button gaps.
        x0 = math.floor(origin_x + (rect[0] - tol_dip) * dpr)
        y0 = math.floor(origin_y + (rect[1] - tol_dip) * dpr)
        x1 = math.ceil(origin_x + (rect[0] + rect[2] + tol_dip) * dpr)
        y1 = math.ceil(origin_y + (rect[1] + rect[3] + tol_dip) * dpr)
        return (x0 <= px < x1) and (y0 <= py < y1)

    # -- message handlers -------------------------------------------------
    def _hit_test(self, px: int, py: int) -> int:
        bounds = self._window_rect()
        if bounds is None:
            return HTCLIENT
        left, top, right, bottom = bounds
        dpr = self._device_ratio()
        try:
            frame = _user32().GetSystemMetrics(SM_CXSIZEFRAME)
            frame += _user32().GetSystemMetrics(SM_CXPADDEDBORDER)
        except Exception:
            frame = 8
        if frame < 1:
            frame = 8

        # Custom chrome buttons first (above everything else up there).
        # DIP rects are client-relative, so they must be mapped from the
        # client origin (ClientToScreen), NOT the outer GetWindowRect origin
        # which includes the invisible thick-frame borders.
        origin = self._client_origin()
        if origin is None:
            origin = (left, top)
        cx, cy = origin
        # Keep the required style bits present (Qt re-applies its own flags
        # at show() and on some state changes). Log-on-repair only.
        try:
            _style = int(_user32().GetWindowLongPtrW(self._hwnd, GWL_STYLE))
            if _style and (_style & STYLE_CHROME_BITS) != STYLE_CHROME_BITS:
                ensure_win_style(self._hwnd)
        except Exception:
            pass
        if self._in_dip_rect("closeBtnRect", px, py, cx, cy, dpr, CHROME_BTN_TOL_DIP):
            return HTCLOSE
        if self._in_dip_rect("maxBtnRect", px, py, cx, cy, dpr, CHROME_BTN_TOL_DIP):
            return HTMAXBUTTON
        if self._in_dip_rect("minBtnRect", px, py, cx, cy, dpr, CHROME_BTN_TOL_DIP):
            return HTMINBUTTON

        try:
            state = self._window.windowState()
        except Exception:
            state = Qt.WindowNoState
        fullscreen = bool(state & Qt.WindowFullScreen)
        maximized = bool(state & Qt.WindowMaximized)
        try:
            maximized = maximized or bool(_user32().IsZoomed(self._hwnd))
        except Exception:
            pass

        # Resize only restored windows. The top of a maximized header must
        # remain a caption so dragging it can restore and move the window.
        if not maximized and not fullscreen:
            in_left = px < left + frame
            in_right = px >= right - frame
            in_top = py < top + frame
            in_bottom = py >= bottom - frame
            if in_top and in_left:
                return HTTOPLEFT
            if in_top and in_right:
                return HTTOPRIGHT
            if in_bottom and in_left:
                return HTBOTTOMLEFT
            if in_bottom and in_right:
                return HTBOTTOMRIGHT
            if in_left:
                return HTLEFT
            if in_right:
                return HTRIGHT
            if in_top:
                return HTTOP
            if in_bottom:
                return HTBOTTOM

        # Until QML publishes hit-test rects, keep the header client-side so
        # every button stays clickable via the QML fallbacks.
        if not self._rects_ready():
            return HTCLIENT

        # Interactive nav tabs stay client-side so clicks reach QML.
        if self._in_dip_rect("navTabsRect", px, py, cx, cy, dpr):
            return HTCLIENT

        # Empty header background behaves like a title bar (native drag,
        # double-click maximize, drag-to-snap with proportional restore).
        if not fullscreen and (py - cy) < HEADER_HEIGHT_DIP * dpr:
            return HTCAPTION
        return HTCLIENT

    def _clamp_max_info(self, lparam: int = 0) -> None:
        try:
            info = ctypes.cast(lparam, ctypes.POINTER(_MINMAXINFO)).contents
        except Exception:
            return
        try:
            u = _user32()
            monitor = u.MonitorFromWindow(self._hwnd, MONITOR_DEFAULTTONEAREST)
            if not monitor:
                return
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not u.GetMonitorInfoW(monitor, ctypes.byref(mi)):
                return
            info.ptMaxPosition.x = abs(mi.rcWork.left - mi.rcMonitor.left)
            info.ptMaxPosition.y = abs(mi.rcWork.top - mi.rcMonitor.top)
            info.ptMaxSize.x = abs(mi.rcWork.right - mi.rcWork.left)
            info.ptMaxSize.y = abs(mi.rcWork.bottom - mi.rcWork.top)
            if not self._logged_minmax:
                self._logged_minmax = True
                log_native_chrome(
                    f"minmax pos=({info.ptMaxPosition.x},{info.ptMaxPosition.y}) "
                    f"size=({info.ptMaxSize.x},{info.ptMaxSize.y}) "
                    f"work=({mi.rcWork.left},{mi.rcWork.top},{mi.rcWork.right},{mi.rcWork.bottom})"
                )
            dpr = self._device_ratio()
            info.ptMinTrackSize.x = int(MIN_WIDTH_DIP * dpr)
            info.ptMinTrackSize.y = int(MIN_HEIGHT_DIP * dpr)
        except Exception:
            pass

    # -- filter entry ------------------------------------------------------
    def nativeEventFilter(self, eventType, message):
        if isinstance(eventType, bytes):
            event_name = eventType
        elif isinstance(eventType, bytearray):
            event_name = bytes(eventType)
        else:
            try:
                event_name = bytes(eventType)
            except Exception:
                try:
                    event_name = str(eventType).encode("utf-8", "ignore")
                except Exception:
                    return False, 0
        if event_name != b"windows_generic_MSG":
            return False, 0
        addr = _msg_address(message)
        if addr is None:
            return False, 0
        try:
            msg = ctypes.cast(addr, ctypes.POINTER(_MSG)).contents
        except Exception:
            return False, 0
        try:
            hwnd = int(msg.hWnd or 0)
        except Exception:
            return False, 0
        if hwnd != self._hwnd:
            return False, 0
        try:
            code = int(msg.message)
        except Exception:
            return False, 0
        if code == WM_NCHITTEST:
            try:
                lparam = int(msg.lParam)
            except Exception:
                return False, 0
            px = ctypes.c_short(lparam & 0xFFFF).value
            py = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            try:
                return True, int(self._hit_test(px, py))
            except Exception:
                return False, 0
        if code == WM_NCCALCSIZE:
            if not self._logged_calcsize:
                self._logged_calcsize = True
                log_native_chrome("nccalcsize handled")
            return True, 0
        if code == WM_GETMINMAXINFO:
            try:
                self._clamp_max_info(lparam=int(msg.lParam))
            except Exception:
                pass
            return True, 0
        if code == WM_SYSCOMMAND:
            # Caption actions also arrive without any button press (Alt+Space
            # menu, taskbar thumbnail buttons). Execute them directly: same
            # outcome as a healthy OS path, completion when it drops them.
            # SC_CLOSE is never touched so the QML unsaved-operation confirm
            # dialog keeps working. Failures only are logged.
            try:
                cmd = int(msg.wParam) & 0xFFF0
            except Exception:
                return False, 0
            if cmd == SC_MINIMIZE:
                try:
                    _user32().ShowWindow(self._hwnd, SW_MINIMIZE)
                except Exception as exc:
                    log_native_chrome(f"syscommand SC_MINIMIZE failed: {exc!r}")
                return True, 0
            if cmd == SC_MAXIMIZE:
                try:
                    _user32().ShowWindow(self._hwnd, SW_MAXIMIZE)
                except Exception as exc:
                    log_native_chrome(f"syscommand SC_MAXIMIZE failed: {exc!r}")
                return True, 0
            if cmd == SC_RESTORE:
                try:
                    _user32().ShowWindow(self._hwnd, SW_RESTORE)
                except Exception as exc:
                    log_native_chrome(f"syscommand SC_RESTORE failed: {exc!r}")
                return True, 0
            return False, 0
        if code == WM_NCLBUTTONDOWN:
            # Press-side caption actions for min/max: the release half of
            # caption gestures never reaches DefWindowProc on this setup (no
            # UP, no syscommand), so release-based handling cannot work.
            # Acting on the press with the identical absolute action instead.
            # Absolute actions, so anything completing later anyway is a
            # harmless no-op double. Close is excluded: destroying on press
            # would bypass the unsaved-work guard. Failures only are logged.
            try:
                ht = int(msg.wParam) & 0xFFFF
            except Exception:
                return False, 0
            if ht == HTMINBUTTON:
                try:
                    _user32().ShowWindow(self._hwnd, SW_MINIMIZE)
                except Exception as exc:
                    log_native_chrome(f"press-act SC_MINIMIZE failed: {exc!r}")
            elif ht == HTMAXBUTTON:
                try:
                    try:
                        _press_zoomed = bool(_user32().IsZoomed(self._hwnd))
                    except Exception:
                        _press_zoomed = False
                    if _press_zoomed:
                        _user32().ShowWindow(self._hwnd, SW_RESTORE)
                    else:
                        _user32().ShowWindow(self._hwnd, SW_MAXIMIZE)
                except Exception as exc:
                    log_native_chrome(f"press-act SC_MAXIMIZE failed: {exc!r}")
            return False, 0
        return False, 0


def ensure_win_style(hwnd: int) -> str:
    """Re-assert native frame style bits Qt may have overwritten.

    Qt (6.8+) re-applies its own window flags at show() and on some state
    changes, silently stripping the THICKFRAME/MAXIMIZEBOX/MINIMIZEBOX/
    SYSMENU bits install_win_chrome added pre-show. Returns a short status
    for logging; never raises.
    """
    if sys.platform != "win32" or not hwnd:
        return "skipped"
    try:
        u = _user32()
        before = int(u.GetWindowLongPtrW(hwnd, GWL_STYLE))
        status = f"{before:#x} (intact)"
        if (before & STYLE_CHROME_BITS) != STYLE_CHROME_BITS:
            wanted = before | STYLE_CHROME_BITS
            u.SetWindowLongPtrW(hwnd, GWL_STYLE, wanted)
            u.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
            )
            after = int(u.GetWindowLongPtrW(hwnd, GWL_STYLE))
            log_native_chrome(f"style-healed {before:#x}->{after:#x}")
            status = f"{before:#x}->{after:#x}"
        # A minimized popup without a taskbar button strands the window
        # (nothing to click to restore it). Guarantee the affordance.
        try:
            ex_before = int(u.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
            if not (ex_before & WS_EX_APPWINDOW):
                u.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_before | WS_EX_APPWINDOW)
                log_native_chrome(f"exstyle-appwindow {ex_before:#x}->{ex_before | WS_EX_APPWINDOW:#x}")
                status += " +appwindow"
        except Exception:
            pass
        return status
    except Exception as exc:
        log_native_chrome(f"style-ensure failed: {exc!r}")
        return "failed"


def install_win_chrome(window) -> bool:
    """Enable native frame behaviors for a frameless QML window.

    Returns True when the OS path is active (QML reads the dynamic
    ``nativeChromeActive`` property to stand down its fallbacks).
    """
    if sys.platform != "win32":
        return False
    try:
        hwnd = int(window.winId())
    except Exception:
        return False
    if not hwnd:
        return False
    try:
        u = _user32()
        style_before = int(u.GetWindowLongPtrW(hwnd, GWL_STYLE))
        style = style_before | WS_THICKFRAME | WS_MAXIMIZEBOX | WS_MINIMIZEBOX | WS_SYSMENU
        u.SetWindowLongPtrW(hwnd, GWL_STYLE, style)
        style_after = int(u.GetWindowLongPtrW(hwnd, GWL_STYLE))
        u.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
        app = QCoreApplication.instance()
        filt = WinFramelessFilter(window)
        if app is not None:
            app.installNativeEventFilter(filt)
        # Kept alive on the window; QML reads nativeChromeActive as fallback guard.
        window._win_frameless_filter = filt  # type: ignore[attr-defined]
        window.setProperty("nativeChromeActive", True)
        try:
            dpr = float(window.devicePixelRatio())
        except Exception:
            dpr = 0.0
        log_native_chrome(
            f"install hwnd={hwnd:#x} style {style_before:#x}->{style_after:#x} dpr={dpr}"
        )
        return True
    except Exception as exc:
        log_native_chrome(f"install failed: {exc!r}")
        return False


def remove_win_chrome(window) -> None:
    """Release the Python native filter while Qt and its window still exist."""
    filt = getattr(window, "_win_frameless_filter", None)
    if filt is None:
        return
    try:
        app = QCoreApplication.instance()
        if app is not None:
            app.removeNativeEventFilter(filt)
    finally:
        # The filter references the window, and the window keeps the filter
        # alive. Break that cycle even if unregistering raises.
        window._win_frameless_filter = None  # type: ignore[attr-defined]
        filt._window = None
