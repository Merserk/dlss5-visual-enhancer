"""Dismiss a control's focus when the user clicks elsewhere in the window."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtQuick import QQuickWindow


class ClickFocusFilter(QObject):
    """Observe clicks without consuming them or covering interactive content."""

    def __init__(self, window: QQuickWindow):
        super().__init__(window)
        window.installEventFilter(self)

    def eventFilter(self, window, event):
        if event.type() != QEvent.Type.MouseButtonPress or event.button() != Qt.MouseButton.LeftButton:
            return False
        focused = window.activeFocusItem()
        if focused is None or focused == window.contentItem():
            return False

        # A native ComboBox keeps keyboard focus while its popup is open.
        # Clearing it on a row press closes the popup before Qt can select
        # the row. Let Qt handle selection and clicks outside that popup.
        ancestor = focused
        while ancestor is not None:
            if ancestor.inherits("QQuickComboBox") and ancestor.property("down"):
                return False
            ancestor = ancestor.parentItem()

        boundary = focused
        # Our text editors live inside padded input boxes. Clicking that
        # padding or the field's clear button should keep the editor focused.
        if (focused.inherits("QQuickTextInput") or focused.inherits("QQuickTextEdit")):
            parent = focused.parentItem()
            if parent is not None and not focused.inherits("QQuickTextField"):
                boundary = parent
        local = boundary.mapFromScene(event.position())
        if not boundary.contains(local):
            # Qt commits text edits on focus loss before the clicked action
            # runs. The receiving control can then take focus normally.
            focused.setFocus(False, Qt.FocusReason.MouseFocusReason)
        return False
