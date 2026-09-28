import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Controls.Basic as Basic
import "PlaybackVolume.js" as PlaybackVolume
import ".."
import "../controls"

Item {
    id: control
    property var audioOutput: null
    property int buttonSize: 28
    readonly property int storedLevel: audioOutput ? PlaybackVolume.levelForGain(audioOutput.volume) : 0
    readonly property bool silenced: !audioOutput || audioOutput.muted || storedLevel === 0
    readonly property int volumeLevel: silenced ? 0 : storedLevel
    property int lastNonZeroLevel: 80
    property bool keyboardAdjusting: false

    implicitWidth: buttonSize; implicitHeight: buttonSize
    width: implicitWidth; height: implicitHeight
    enabled: !!audioOutput
    LayoutMirroring.enabled: false
    LayoutMirroring.childrenInherit: true

    onStoredLevelChanged: { if (storedLevel > 0) lastNonZeroLevel = storedLevel }
    onEnabledChanged: { if (!enabled) volumePopup.close() }
    Component.onCompleted: { if (storedLevel > 0) lastNonZeroLevel = storedLevel }

    function setVolume(level) {
        if (!audioOutput || !isFinite(level)) return
        var next = Math.round(Math.max(0, Math.min(100, level)))
        if (next > 0) lastNonZeroLevel = next
        audioOutput.volume = PlaybackVolume.gainForLevel(next)
        audioOutput.muted = next === 0
    }
    function toggleMute() {
        if (!audioOutput) return
        if (silenced) {
            if (storedLevel === 0) audioOutput.volume = PlaybackVolume.gainForLevel(lastNonZeroLevel)
            audioOutput.muted = false
        } else {
            audioOutput.muted = true
        }
    }
    function openVolume(keyboard) {
        if (!enabled) return
        closeTimer.stop()
        volumePopup.open()
        if (keyboard) {
            keyboardAdjusting = true
            volumeSlider.forceActiveFocus(Qt.TabFocusReason)
        }
    }

    AppIconButton {
        id: volumeButton
        objectName: "timelineMute"
        buttonSize: control.buttonSize
        iconSize: 18
        iconName: control.silenced ? "mute" : (control.storedLevel <= 50 ? "volume_down" : "volume_up")
        tooltipText: control.silenced ? qsTranslate("App", "Unmute") : qsTranslate("App", "Mute")
        showTooltip: false
        onClicked: control.toggleMute()
        Keys.onPressed: (event) => {
            if (!control.enabled) return
            if (event.key === Qt.Key_Up || event.key === Qt.Key_Down
                    || event.key === Qt.Key_Left || event.key === Qt.Key_Right) {
                var up = event.key === Qt.Key_Up || event.key === Qt.Key_Right
                control.setVolume(control.volumeLevel + (up ? 5 : -5))
                control.openVolume(true)
                event.accepted = true
            }
        }
        MouseArea {
            anchors.fill: parent
            acceptedButtons: Qt.NoButton
            onWheel: (wheel) => {
                var delta = wheel.angleDelta.y || wheel.pixelDelta.y
                if (!delta || !control.enabled) return
                control.setVolume(control.volumeLevel + (delta > 0 ? 5 : -5))
                wheel.accepted = true
            }
        }
    }

    HoverHandler {
        id: buttonHover
        enabled: control.enabled
        onHoveredChanged: {
            if (hovered) control.openVolume(false)
            else closeTimer.restart()
        }
    }
    Timer {
        id: closeTimer
        interval: 250
        onTriggered: {
            if (!buttonHover.hovered && !popupHover.hovered && !contentHover.hovered
                    && !volumeSlider.pressed && !control.keyboardAdjusting)
                volumePopup.close()
        }
    }

    QQC2.Popup {
        id: volumePopup
        objectName: "timelineVolumePopup"
        parent: control
        x: (control.width - width) / 2; y: -height - 6
        width: 164; height: 44
        padding: 10; margins: 8
        modal: false; focus: false
        closePolicy: QQC2.Popup.CloseOnEscape | QQC2.Popup.CloseOnPressOutsideParent
        onClosed: { closeTimer.stop(); control.keyboardAdjusting = false }
        background: Rectangle {
            color: Theme.bgSurface; radius: Theme.radiusSmall
            border.color: Theme.borderDefault; border.width: 1
            HoverHandler {
                id: popupHover
                onHoveredChanged: {
                    if (hovered) closeTimer.stop()
                    else closeTimer.restart()
                }
            }
        }
        contentItem: Row {
            spacing: 8
            LayoutMirroring.enabled: false
            LayoutMirroring.childrenInherit: true
            HoverHandler {
                id: contentHover
                onHoveredChanged: {
                    if (hovered) closeTimer.stop()
                    else closeTimer.restart()
                }
            }
            Basic.Slider {
                id: volumeSlider
                objectName: "timelineVolumeSlider"
                width: 112; height: 24; padding: 0
                from: 0; to: 100; stepSize: 1
                snapMode: Basic.Slider.SnapAlways
                value: control.volumeLevel
                enabled: control.enabled
                live: true; hoverEnabled: true
                focusPolicy: Qt.StrongFocus
                Accessible.name: qsTranslate("App", "Volume")
                onMoved: control.setVolume(value)
                onPressedChanged: {
                    if (pressed) { control.keyboardAdjusting = false; closeTimer.stop() }
                    else closeTimer.restart()
                }
                onActiveFocusChanged: {
                    if (!activeFocus) { control.keyboardAdjusting = false; closeTimer.restart() }
                }
                Keys.onPressed: (event) => {
                    if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                        control.setVolume(event.key === Qt.Key_Home ? 0 : 100)
                        event.accepted = true
                    } else if (event.key === Qt.Key_Escape) {
                        volumePopup.close()
                        volumeButton.forceActiveFocus()
                        event.accepted = true
                    }
                }
                background: Rectangle {
                    x: 6; y: (volumeSlider.height - height) / 2
                    width: volumeSlider.width - 12; height: 4; radius: 2
                    color: Theme.bgSelected
                    Rectangle {
                        width: parent.width * volumeSlider.position
                        height: parent.height; radius: 2; color: Theme.accent
                    }
                }
                handle: Rectangle {
                    x: volumeSlider.visualPosition * (volumeSlider.availableWidth - width)
                    y: (volumeSlider.height - height) / 2
                    implicitWidth: 12; implicitHeight: 12
                    width: implicitWidth; height: implicitHeight; radius: 6
                    color: Theme.textPrimary
                    border.color: volumeSlider.pressed || volumeSlider.activeFocus ? Theme.accent : Theme.borderActive
                    border.width: 2
                }
            }
            Text {
                objectName: "timelineVolumeReadout"
                width: 24; height: 24
                text: control.volumeLevel
                color: Theme.textPrimary
                font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeSmall
                horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
            }
        }
    }
}
