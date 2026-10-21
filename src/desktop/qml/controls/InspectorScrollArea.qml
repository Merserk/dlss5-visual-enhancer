import QtQuick
import QtQuick.Window

Flickable {
    id: root

    readonly property bool autoScrolling: scrollTimer.running
    property real anchorY: 0
    property real pointerY: 0
    property bool movedWhileHeld: false
    property double previousTick: 0

    function clearControlFocus() {
        var window = root.Window.window
        var item = window ? window.activeFocusItem : null
        var input = item as TextInput
        var edit = item as TextEdit
        if (input) input.deselect()
        else if (edit) edit.deselect()
        // Losing focus commits editable values through their editingFinished handler.
        root.forceActiveFocus(Qt.MouseFocusReason)
    }

    function stopScrolling() { scrollTimer.stop() }

    function startScrolling(y) {
        clearControlFocus()
        if (!interactive || contentHeight <= height) return
        cancelFlick()
        anchorY = y
        pointerY = y
        movedWhileHeld = false
        previousTick = Date.now()
        scrollTimer.start()
    }

    function movePointer(x, y, held) {
        if (x < 0 || x >= width || y < 0 || y >= height) {
            stopScrolling()
            return
        }
        pointerY = y
        if (held && Math.abs(y - anchorY) > 10) movedWhileHeld = true
    }

    onActiveFocusChanged: { if (!activeFocus) stopScrolling() }
    onVisibleChanged: { if (!visible) stopScrolling() }
    onEnabledChanged: { if (!enabled) stopScrolling() }
    onInteractiveChanged: { if (!interactive) stopScrolling() }
    Keys.onEscapePressed: event => {
        event.accepted = root.autoScrolling
        root.stopScrolling()
    }

    Connections {
        target: root.Window.window
        function onActiveChanged() {
            if (!root.Window.window.active) root.stopScrolling()
        }
    }

    Timer {
        id: scrollTimer
        interval: 16
        repeat: true
        onTriggered: {
            var now = Date.now()
            var elapsed = Math.min(0.05, (now - root.previousTick) / 1000)
            root.previousTick = now
            var distance = root.pointerY - root.anchorY
            var offset = Math.max(0, Math.abs(distance) - 10)
            var speed = Math.min(1800, offset * 4 + offset * offset * 0.025)
            var next = root.contentY + (distance < 0 ? -speed : speed) * elapsed
            root.contentY = Math.max(root.originY,
                Math.min(root.originY + Math.max(0, root.contentHeight - root.height), next))
        }
    }

    // Stay behind the controls inside contentItem, whose blank space does not
    // consume presses. Compensate for scrolling to cover the whole viewport.
    MouseArea {
        objectName: "inspectorBlankSpace"
        parent: root.contentItem
        x: 0
        y: root.contentY
        width: root.width
        height: root.height
        z: -1
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        preventStealing: (pressedButtons & Qt.MiddleButton) !== 0
        onPressed: mouse => {
            if (mouse.button === Qt.MiddleButton) root.startScrolling(mouse.y)
            else root.clearControlFocus()
        }
        onPositionChanged: mouse => {
            if (root.autoScrolling) root.movePointer(mouse.x, mouse.y, true)
        }
        onReleased: mouse => {
            if (mouse.button === Qt.MiddleButton && root.movedWhileHeld) root.stopScrolling()
        }
        onCanceled: root.stopScrolling()
    }

    MouseArea {
        objectName: "inspectorAutoScrollOverlay"
        parent: root
        anchors.fill: parent
        z: 1000
        visible: root.autoScrolling
        enabled: visible
        hoverEnabled: true
        acceptedButtons: Qt.AllButtons
        preventStealing: true
        cursorShape: Qt.SizeVerCursor
        onPositionChanged: mouse => root.movePointer(mouse.x, mouse.y, false)
        onPressed: root.stopScrolling()
        onExited: root.stopScrolling()
        onWheel: wheel => {
            root.stopScrolling()
            wheel.accepted = false
        }

    }
}
