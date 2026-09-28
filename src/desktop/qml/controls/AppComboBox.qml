import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Controls.Basic as Basic
import ".."

Item {
    id: control
    property string label: ""
    property var model: []
    property var currentValue: undefined
    property int currentIndex: -1
    property bool enabled: true
    property int comboHeight: Theme.controlHeight
    // Drop-up mode for combos docked at the window bottom (e.g. the FI
    // action-bar length picker): the list opens above the box instead of
    // falling off-window. Defaults off; every existing combo is unaffected.
    property bool dropUp: false
    signal activated(var value, int index)

    implicitWidth: 200
    implicitHeight: label !== "" ? (comboHeight + 20) : comboHeight
    opacity: enabled ? 1.0 : 0.45
    activeFocusOnTab: enabled

    function getLabel(item) {
        if (item === undefined || item === null) return ""
        if (typeof item === "object" && item.label !== undefined) return qsTranslate("App", item.label)
        return qsTranslate("App", item.toString())
    }
    function getValue(item) {
        if (item === undefined || item === null) return undefined
        if (typeof item === "object" && item.value !== undefined) return item.value
        return item
    }
    function syncIndex() {
        currentIndex = -1
        if (!model) return
        for (var i = 0; i < model.length; i++) {
            if (getValue(model[i]) === currentValue) { currentIndex = i; return }
        }
    }
    function displayText() {
        if (currentIndex >= 0 && model && currentIndex < model.length) return getLabel(model[currentIndex])
        return currentValue === undefined || currentValue === null || currentValue === "" ? qsTranslate("App", "Select...") : qsTranslate("App", "Unavailable: %1").arg(currentValue)
    }
    onCurrentValueChanged: syncIndex()
    onModelChanged: syncIndex()
    Component.onCompleted: syncIndex()
    Keys.onPressed: (event) => {
        if (!enabled || !model || model.length === 0) return
        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            popup.open(); event.accepted = true
        } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Right) {
            var i = Math.min(model.length - 1, Math.max(-1, currentIndex) + 1)
            activated(getValue(model[i]), i); event.accepted = true
        } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Left) {
            var j = Math.max(0, currentIndex <= 0 ? 0 : currentIndex - 1)
            activated(getValue(model[j]), j); event.accepted = true
        }
    }

    Text {
        visible: control.label !== ""; anchors.top: parent.top; anchors.left: parent.left
        text: control.label; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; color: Theme.textSecondary
    }
    Rectangle {
        id: box; anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
        height: control.comboHeight; radius: Theme.radiusMedium
        color: mouseArea.containsMouse || popup.visible ? Theme.bgInputHover : Theme.bgInput
        border.color: control.activeFocus || popup.visible ? Theme.accent : (mouseArea.containsMouse ? Theme.borderActive : Theme.borderDefault)
        border.width: 1
        Text {
            anchors.left: parent.left; anchors.leftMargin: 10; anchors.right: arrow.left; anchors.rightMargin: 6
            anchors.verticalCenter: parent.verticalCenter; elide: Text.ElideRight
            text: control.displayText(); font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel
            color: control.currentIndex >= 0 ? Theme.textPrimary : Theme.warning
        }
        AppIcon { id: arrow; anchors.right: parent.right; anchors.rightMargin: 10; anchors.verticalCenter: parent.verticalCenter; iconName: popup.visible ? "chevron_up" : "chevron_down"; iconSize: 12; color: Theme.textMuted }
        MouseArea {
            id: mouseArea; anchors.fill: parent; hoverEnabled: control.enabled
            cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
            onClicked: { if (control.enabled) { control.forceActiveFocus(); popup.visible ? popup.close() : popup.open() } }
        }
    }

    QQC2.Popup {
        id: popup
        x: 0; y: control.dropUp ? (box.y - height - 4) : (box.y + box.height + 4)
        width: control.width; height: Math.min(260, Math.max(36, (control.model ? control.model.length : 0) * 32 + 8))
        padding: 4; modal: false
        closePolicy: QQC2.Popup.CloseOnEscape | QQC2.Popup.CloseOnPressOutside
        background: Rectangle { radius: Theme.radiusMedium; color: Theme.bgSurface; border.color: Theme.borderActive; border.width: 1 }
        contentItem: ListView {
            id: listView
            property bool hasOverflow: contentHeight > height + 0.5
            clip: true
            model: control.model
            boundsBehavior: Flickable.StopAtBounds
            delegate: Rectangle {
                required property var modelData
                required property int index
                width: listView.width; height: 32; radius: Theme.radiusSmall
                color: itemMouse.containsMouse ? Theme.bgHover : (control.currentIndex === index ? Theme.bgSelected : "transparent")
                Text {
                    anchors.left: parent.left; anchors.leftMargin: 8; anchors.right: check.left; anchors.rightMargin: 6; anchors.verticalCenter: parent.verticalCenter
                    text: control.getLabel(modelData); elide: Text.ElideRight
                    font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel
                    color: control.currentIndex === index ? Theme.accent : Theme.textSecondary
                }
                AppIcon {
                    id: check
                    anchors.right: parent.right
                    anchors.rightMargin: listView.hasOverflow ? verticalScrollBar.width + 8 : 8
                    anchors.verticalCenter: parent.verticalCenter
                    visible: control.currentIndex === index
                    iconName: "check"
                    iconSize: 14
                    color: Theme.accent
                }
                MouseArea {
                    id: itemMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: { control.activated(control.getValue(modelData), index); popup.close(); control.forceActiveFocus() }
                }
            }

            QQC2.ScrollBar.vertical: Basic.ScrollBar {
                id: verticalScrollBar
                width: 10
                padding: 2
                policy: QQC2.ScrollBar.AlwaysOn
                visible: listView.hasOverflow
                active: visible
                interactive: true

                background: Rectangle {
                    implicitWidth: 8
                    radius: width / 2
                    color: Theme.bgInput
                    opacity: 0.8
                }

                contentItem: Rectangle {
                    implicitWidth: 6
                    radius: width / 2
                    color: verticalScrollBar.pressed ? Theme.accent
                                                     : (verticalScrollBar.hovered ? Theme.textSecondary
                                                                                  : Theme.textMuted)
                    opacity: verticalScrollBar.pressed || verticalScrollBar.hovered ? 1.0 : 0.85
                }
            }
        }
    }
}
