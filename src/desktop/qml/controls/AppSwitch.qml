import QtQuick
import ".."

Item {
    id: control

    property string label: ""
    property bool checked: false
    property bool enabled: true

    signal toggled(bool isChecked)

    implicitWidth: Math.max(120, labelText.implicitWidth + 48)
    implicitHeight: 26
    opacity: enabled ? 1.0 : 0.45
    activeFocusOnTab: enabled
    Keys.onPressed: (event) => { if (enabled && (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) { toggled(!checked); event.accepted = true } }

    Row {
        anchors.fill: parent
        spacing: 10

        Rectangle {
            id: track
            width: 38
            height: 20
            radius: 10
            anchors.verticalCenter: parent.verticalCenter
            color: control.checked ? Theme.accent : Theme.bgInput
            border.color: control.activeFocus ? Theme.accent : (control.checked ? Theme.accent : (mouseArea.containsMouse ? Theme.borderActive : Theme.borderDefault))
            border.width: 1

            Behavior on color { ColorAnimation { duration: Theme.animFast } }

            // Switch thumb
            Rectangle {
                id: thumb
                y: 2
                x: control.checked !== control.LayoutMirroring.enabled ? (track.width - width - 2) : 2
                width: 16
                height: 16
                radius: 8
                color: "#FFFFFF"

                Behavior on x {
                    NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutQuad }
                }
            }
        }

        Text {
            id: labelText
            anchors.verticalCenter: parent.verticalCenter
            text: control.label
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeLabel
            color: mouseArea.containsMouse ? Theme.textPrimary : Theme.textSecondary
        }
    }

    MouseArea {
        id: mouseArea
        anchors.fill: parent
        hoverEnabled: control.enabled
        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
        onClicked: {
            if (control.enabled) {
                control.toggled(!control.checked)
            }
        }
    }
}
