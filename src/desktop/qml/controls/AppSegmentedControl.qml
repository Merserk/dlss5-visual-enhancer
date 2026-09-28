import QtQuick
import ".."

Item {
    id: control

    property string label: ""
    property var model: [] // e.g. ["Image", "Video"] or [{label: "...", value: "..."}]
    property var currentValue: undefined
    property int currentIndex: 0
    property int controlHeight: Theme.controlHeightSmall
    property bool enabled: true

    signal activated(var value, int index)

    implicitWidth: 220
    implicitHeight: label !== "" ? (controlHeight + 20) : controlHeight
    opacity: enabled ? 1.0 : 0.45
    activeFocusOnTab: enabled
    Keys.onPressed: (event) => {
        if (!enabled || !model || model.length === 0) return
        var next = currentIndex
        if (event.key === Qt.Key_Right || event.key === Qt.Key_Down) next = Math.min(model.length - 1, Math.max(-1, currentIndex) + 1)
        else if (event.key === Qt.Key_Left || event.key === Qt.Key_Up) next = Math.max(0, currentIndex <= 0 ? 0 : currentIndex - 1)
        else return
        activated(getValue(model[next]), next); event.accepted = true
    }

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
        var nextIndex = -1
        if (model) {
            for (var i = 0; i < model.length; i++) {
                if (getValue(model[i]) === currentValue) {
                    nextIndex = i
                    break
                }
            }
        }
        if (currentIndex !== nextIndex) currentIndex = nextIndex
    }

    onCurrentValueChanged: syncIndex()
    onModelChanged: syncIndex()

    Text {
        id: labelText
        visible: control.label !== ""
        anchors.top: parent.top
        anchors.left: parent.left
        text: control.label
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLabel
        color: Theme.textSecondary
    }

    Rectangle {
        id: container
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: control.controlHeight
        radius: Theme.radiusMedium
        color: Theme.bgInput
        border.color: control.activeFocus ? Theme.accent : Theme.borderSubtle
        border.width: 1

        // Animated Active Pill
        Rectangle {
            id: indicator
            y: 2
            height: parent.height - 4
            width: (parent.width - 4) / Math.max(1, (control.model ? control.model.length : 1))
            x: 2 + (control.LayoutMirroring.enabled ? Math.max(0, control.model.length - 1 - control.currentIndex) : Math.max(0, control.currentIndex)) * width
            visible: control.currentIndex >= 0
            radius: Theme.radiusSmall
            color: Theme.bgCard
            border.color: Theme.borderActive
            border.width: 1

            Behavior on x {
                NumberAnimation { duration: Theme.animNormal; easing.type: Easing.OutQuad }
            }
        }

        // Segments Row
        Row {
            anchors.fill: parent
            anchors.margins: 2

            Repeater {
                model: control.model

                Item {
                    id: segmentItem
                    width: (container.width - 4) / Math.max(1, control.model.length)
                    height: container.height - 4

                    Text {
                        anchors.centerIn: parent
                        text: getLabel(modelData)
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeLabel
                        font.weight: control.currentIndex === index ? Font.DemiBold : Font.Normal
                        color: control.currentIndex === index ? Theme.textPrimary : (segMouse.containsMouse ? Theme.textPrimary : Theme.textMuted)
                    }

                    MouseArea {
                        id: segMouse
                        anchors.fill: parent
                        hoverEnabled: control.enabled
                        cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                        onClicked: {
                            if (control.enabled) {
                                var val = getValue(modelData)
                                control.activated(val, index)
                            }
                        }
                    }
                }
            }
        }
    }
}
