"""Restore the initial window rectangle once, then leave geometry to Qt/Windows."""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QGuiApplication, QScreen, QWindow


def fit_restore_geometry(saved: QRect, minimum: QSize, screens: list[QScreen],
                         preferred: QScreen, *, has_position: bool) -> tuple[QScreen, QRect]:
    """Use the saved monitor when present; recover safely after display changes.

    All rectangles are Qt device-independent coordinates. Screen origins may
    be negative, and mixed-DPI Windows screens need not touch in this space.
    """
    screen = preferred
    overlap = 0
    if has_position:
        for candidate in screens:
            intersection = saved.intersected(candidate.geometry())
            area = intersection.width() * intersection.height() if not intersection.isEmpty() else 0
            if area > overlap:
                screen, overlap = candidate, area
    work = screen.availableGeometry()
    width = max(minimum.width(), min(saved.width(), work.width()))
    height = max(minimum.height(), min(saved.height(), work.height()))
    # When a monitor has disappeared, center on a connected monitor instead
    # of leaving the window in an empty part of the virtual desktop.
    if has_position and overlap:
        x = saved.x()
        y = saved.y()
    else:
        x = work.x() + (work.width() - width) // 2
        y = work.y() + (work.height() - height) // 2
    x = max(work.x(), min(x, work.x() + max(0, work.width() - width)))
    y = max(work.y(), min(y, work.y() + max(0, work.height() - height)))
    return screen, QRect(x, y, width, height)


def restore_window_geometry(window: QWindow, saved: QRect, *, has_position: bool) -> None:
    screens = QGuiApplication.screens()
    preferred = window.screen() or QGuiApplication.primaryScreen()
    if not screens or preferred is None:
        return
    screen, geometry = fit_restore_geometry(saved, window.minimumSize(), screens,
                                             preferred, has_position=has_position)
    # Select the monitor before setting its logical coordinates, so Qt uses
    # that monitor's scale factor when creating the native window.
    window.setScreen(screen)
    window.setGeometry(geometry)
