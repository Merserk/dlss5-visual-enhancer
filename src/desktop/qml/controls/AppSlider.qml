import QtQuick
import ".."

Item {
    id: control

    property string label: ""
    property real from: 0.0
    property real to: 1.0
    property real stepSize: 0.05
    property real value: 0.0
    property real defaultValue: 0.0
    property int precision: 2
    property string unit: ""
    property bool enabled: true
    property bool showValueControls: true

    signal valueModified(real newValue)

    implicitWidth: 260
    implicitHeight: 48
    opacity: enabled ? 1.0 : 0.45
    activeFocusOnTab: enabled && !showValueControls

    function formatNumber(number) {
        // Normalize floating-point residue and negative zero in the readout.
        var rounded = Number(number.toFixed(control.precision))
        return rounded.toFixed(control.precision)
    }

    function syncNumber() {
        if (!numberInput) return
        numberInput.dirty = false
        numberInput.text = formatNumber(control.value)
    }

    function parseNumber(text) {
        var normalized = text.trim().replace(/\u2212/g, "-")
        normalized = normalized.replace(/[\u0660-\u0669\u06f0-\u06f9\u0966-\u096f]/g, function(digit) {
            var code = digit.charCodeAt(0)
            var zero = code >= 0x0966 ? 0x0966 : (code >= 0x06f0 ? 0x06f0 : 0x0660)
            return String(code - zero)
        }).replace(/[,\u066b]/g, ".")
        if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(normalized)) return NaN
        return Number(normalized)
    }

    function commitNumber() {
        if (!numberInput.dirty) return
        var next = parseNumber(numberInput.text)
        numberInput.dirty = false
        if (control.enabled && control.showValueControls && isFinite(next)) {
            next = Math.max(control.from, Math.min(control.to, next))
            // Manual values use the control's precision, without snapping to
            // the coarser step used for dragging or keyboard increments.
            next = Number(next.toFixed(control.precision))
            if (next !== control.value) control.valueModified(next)
        }
        syncNumber()
    }

    onValueChanged: syncNumber()
    onPrecisionChanged: syncNumber()
    Component.onCompleted: syncNumber()

    function keyboardStep(direction) {
        var step = stepSize > 0 ? stepSize : (to - from) / 100
        var next = Math.max(from, Math.min(to, value + direction * step))
        valueModified(next)
    }
    Keys.onPressed: (event) => {
        if (!enabled || numberInput.activeFocus) return
        if (event.key === Qt.Key_Left || event.key === Qt.Key_Down) { keyboardStep(-1); event.accepted = true }
        else if (event.key === Qt.Key_Right || event.key === Qt.Key_Up) { keyboardStep(1); event.accepted = true }
        else if (event.key === Qt.Key_Home) { valueModified(from); event.accepted = true }
        else if (event.key === Qt.Key_End) { valueModified(to); event.accepted = true }
    }

    // Top row: label and optional reset/numeric controls.
    Row {
        id: headerRow
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: 18

        Text {
            id: labelText
            width: Math.min(implicitWidth, Math.max(0, headerRow.width - (control.showValueControls ? valueDisplayRow.implicitWidth + 8 : 0)))
            text: control.label
            elide: Text.ElideRight
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeLabel
            color: Theme.textSecondary
            anchors.verticalCenter: parent.verticalCenter
        }

        Item {
            // Spacer
            width: Math.max(8, headerRow.width - labelText.width - (control.showValueControls ? valueDisplayRow.implicitWidth : 0))
            height: 1
        }

        Row {
            id: valueDisplayRow
            visible: control.showValueControls
            spacing: 6
            LayoutMirroring.enabled: false
            LayoutMirroring.childrenInherit: true
            anchors.verticalCenter: parent.verticalCenter

            // Reserve the reset slot so changing away from the default
            // doesn't move the field or its label, including in RTL layouts.
            Rectangle {
                id: resetButton
                readonly property bool available: Math.abs(control.value - control.defaultValue) > 0.001
                width: 14
                height: 14
                radius: 7
                color: resetArea.containsMouse ? Theme.bgHover : "transparent"
                opacity: available ? 1.0 : 0.0
                anchors.verticalCenter: parent.verticalCenter

                AppIcon {
                    anchors.centerIn: parent
                    iconName: "reset"
                    iconSize: 12
                    color: resetArea.containsMouse ? Theme.accent : Theme.textMuted
                }

                MouseArea {
                    id: resetArea
                    anchors.fill: parent
                    enabled: control.enabled && control.showValueControls && resetButton.available
                    hoverEnabled: enabled
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        control.valueModified(control.defaultValue)
                    }
                }
            }

            Item {
                width: numberBox.width
                height: headerRow.height
                anchors.verticalCenter: parent.verticalCenter

                Rectangle {
                    id: numberBox
                    // Keep the same size for every value and editing state.
                    width: 44
                    height: parent.height
                    radius: 3
                    color: numberInput.activeFocus || numberHover.hovered ? Theme.bgInput : "transparent"
                    border.width: 1
                    border.color: numberInput.activeFocus ? Theme.accent : (numberHover.hovered ? Theme.borderDefault : "transparent")

                    HoverHandler {
                        id: numberHover
                        enabled: control.enabled && control.showValueControls
                        cursorShape: Qt.IBeamCursor
                    }

                    TextInput {
                        id: numberInput
                        objectName: "sliderNumberInput"
                        property bool dirty: false
                        anchors.fill: parent
                        anchors.leftMargin: 4
                        anchors.rightMargin: 4
                        enabled: control.enabled && control.showValueControls
                        activeFocusOnTab: enabled
                        font.family: Theme.monoFontFamily
                        font.pixelSize: Theme.fontSizeSmall
                        font.weight: Font.DemiBold
                        color: Math.abs(control.value - control.defaultValue) > 0.001 ? Theme.accent : Theme.textPrimary
                        horizontalAlignment: TextInput.AlignLeft
                        verticalAlignment: TextInput.AlignVCenter
                        selectByMouse: true
                        selectionColor: Theme.accent
                        selectedTextColor: "#FFFFFF"
                        clip: true
                        maximumLength: 24
                        inputMethodHints: Qt.ImhFormattedNumbersOnly
                        Accessible.name: control.label
                        Accessible.description: control.unit
                        // Allow an unfinished sign/decimal while typing. Validate
                        // the complete number when committing, then clamp it.
                        validator: RegularExpressionValidator {
                            regularExpression: /\s*[+\-−]?[0-9٠-٩۰-۹०-९]*([.,٫][0-9٠-٩۰-۹०-९]*)?\s*/
                        }
                        onTextEdited: dirty = true
                        onActiveFocusChanged: {
                            if (activeFocus) {
                                Qt.callLater(function() { if (numberInput.activeFocus) numberInput.selectAll() })
                            }
                        }
                        onEditingFinished: control.commitNumber()
                        Keys.onPressed: (event) => {
                            if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                                control.commitNumber()
                                control.forceActiveFocus()
                                event.accepted = true
                            } else if (event.key === Qt.Key_Escape) {
                                control.syncNumber()
                                control.forceActiveFocus()
                                event.accepted = true
                            } else if (event.key === Qt.Key_Up || event.key === Qt.Key_Down) {
                                control.commitNumber()
                                control.keyboardStep(event.key === Qt.Key_Up ? 1 : -1)
                                control.syncNumber()
                                selectAll()
                                event.accepted = true
                            }
                        }
                    }
                }
            }
        }
    }

    // Slider track & thumb
    Item {
        id: trackArea
        // Keep the numeric axis and pointer math consistent in RTL layouts.
        LayoutMirroring.enabled: false
        LayoutMirroring.childrenInherit: true
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 22

        // Background groove
        Rectangle {
            id: groove
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: parent.left
            anchors.right: parent.right
            height: 4
            radius: 2
            color: Theme.bgInput
            border.color: Theme.borderSubtle
            border.width: 1
        }

        // Active highlighted track fill
        Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            anchors.left: groove.left
            width: Math.max(0, Math.min(groove.width, (thumb.x + thumb.width / 2)))
            height: 4
            radius: 2
            color: Theme.accent
        }

        // Thumb handle
        Rectangle {
            id: thumb
            y: (trackArea.height - height) / 2
            x: {
                var range = control.to - control.from
                if (range <= 0) return 0
                var pct = Math.max(0.0, Math.min(1.0, (control.value - control.from) / range))
                return pct * (trackArea.width - width)
            }
            width: 14
            height: 14
            radius: 7
            color: sliderMouse.containsMouse || sliderMouse.drag.active ? "#FFFFFF" : Theme.textPrimary
            border.color: control.activeFocus || numberInput.activeFocus || sliderMouse.drag.active ? Theme.accent : Theme.borderActive
            border.width: 2

            Rectangle {
                anchors.centerIn: parent
                width: 4
                height: 4
                radius: 2
                color: Theme.accent
            }

            Behavior on scale {
                NumberAnimation { duration: Theme.animFast }
            }
        }

        MouseArea {
            id: sliderMouse
            anchors.fill: parent
            enabled: control.enabled
            hoverEnabled: control.enabled
            cursorShape: control.enabled ? Qt.PointingHandCursor : Qt.ArrowCursor

            function updateValue(mouseX) {
                var clampedX = Math.max(0, Math.min(trackArea.width - thumb.width, mouseX - thumb.width / 2))
                var pct = clampedX / (trackArea.width - thumb.width)
                var rawVal = control.from + pct * (control.to - control.from)
                if (control.stepSize > 0) {
                    var steps = Math.round((rawVal - control.from) / control.stepSize)
                    rawVal = control.from + steps * control.stepSize
                }
                rawVal = Math.max(control.from, Math.min(control.to, rawVal))
                control.valueModified(rawVal)
            }

            onPressed: (mouse) => {
                if (control.enabled) {
                    control.forceActiveFocus(Qt.MouseFocusReason)
                    updateValue(mouse.x)
                }
            }

            onPositionChanged: (mouse) => {
                if (control.enabled && pressed) updateValue(mouse.x)
            }

            onDoubleClicked: {
                if (control.enabled && control.showValueControls) {
                    control.valueModified(control.defaultValue)
                }
            }
        }
    }
}
