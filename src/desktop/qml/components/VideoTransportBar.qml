import QtQuick
import QtMultimedia
import QtQuick.Controls as QQC2
import QtQuick.Controls.Basic as Basic
import ".."
import "../controls"

Rectangle {
    id: root
    LayoutMirroring.enabled: false
    LayoutMirroring.childrenInherit: true
    property var player: null
    property real frameRate: 30.0
    property var audioOutput: null
    // Use probe metadata until the player has loaded the source.
    property real fallbackDurationMs: 0
    property real fallbackFrameRate: 30.0
    // Only user actions refresh the viewport's preview, never playback ticks.
    signal userScrubbed(real posMs)
    property var renderedRanges: []

    readonly property bool stacked: width < 640
    readonly property bool compact: width < 480
    readonly property real effectiveFps: frameRate > 0.01 ? frameRate : (fallbackFrameRate > 0.01 ? fallbackFrameRate : 30.0)
    readonly property real effectiveDuration: Math.max(0, player && player.duration > 0 ? player.duration : fallbackDurationMs)
    readonly property real effectivePosition: player ? Math.max(0, Math.min(effectiveDuration > 0 ? effectiveDuration : player.position, player.position)) : 0
    readonly property int totalFrames: effectiveDuration > 0 ? Math.max(1, Math.round(effectiveDuration * effectiveFps / 1000)) : 0
    readonly property int currentFrame: frameAt(effectivePosition)
    readonly property bool canSeek: !!player && effectiveDuration > 0
    readonly property real lastFramePosition: framePosition(totalFrames)
    readonly property int timeCellWidth: Math.ceil(timeMetrics.advanceWidth)
    readonly property real rulerLabelGap: Math.max(72, timeCellWidth + 24)
    readonly property real rulerStep: niceInterval(effectiveDuration / 1000 / Math.max(1, Math.floor(timelineFace.width / rulerLabelGap)))

    implicitHeight: stacked ? 108 : 72
    height: implicitHeight
    color: Theme.bgInput
    border.color: Theme.borderSubtle
    border.width: 1

    function fmt(ms) {
        var total = Math.max(0, Math.floor(ms / 1000))
        var h = Math.floor(total / 3600)
        var m = Math.floor((total % 3600) / 60)
        var s = total % 60
        function pad(v) { return v < 10 ? "0" + v : "" + v }
        return h > 0 ? (h + ":" + pad(m) + ":" + pad(s)) : (pad(m) + ":" + pad(s))
    }
    function fmtPrecise(ms) {
        return fmt(ms) + "." + Math.floor(Math.max(0, ms) % 1000 / 100)
    }
    function fmtDuration(ms) {
        return ms > 0 && ms < 1000 ? fmtPrecise(ms) : fmt(ms)
    }
    function fmtExact(ms) {
        var fraction = Math.max(0, Math.floor(ms)) % 1000
        return fmt(ms) + "." + ("00" + fraction).slice(-3)
    }
    function frameAt(ms) {
        return totalFrames > 0 ? Math.min(totalFrames, Math.max(1, Math.floor(ms * effectiveFps / 1000 + 0.000001) + 1)) : 0
    }
    function fractionAt(ms) {
        return effectiveDuration > 0 && isFinite(ms) ? Math.max(0, Math.min(1, ms / effectiveDuration)) : 0
    }
    function framePosition(frame) {
        // MediaPlayer seeks in integer milliseconds. Round into the selected
        // frame, rather than letting a fractional start truncate into N-1.
        return Math.max(0, Math.min(effectiveDuration, Math.ceil((frame - 1) * 1000 / effectiveFps - 0.000001)))
    }
    function nearestFramePosition(ms) {
        var frame = Math.max(1, Math.min(totalFrames, Math.round(ms * effectiveFps / 1000) + 1))
        return framePosition(frame)
    }
    function niceInterval(seconds) {
        var intervals = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400]
        for (var i = 0; i < intervals.length; ++i) {
            if (seconds <= intervals[i]) return intervals[i]
        }
        return Math.ceil(seconds / 86400) * 86400
    }
    function seekTo(posMs) {
        if (!canSeek) return
        var target = framePosition(frameAt(Math.max(0, Math.min(effectiveDuration, posMs))))
        player.position = target
        userScrubbed(target)
    }
    function jumpTo(end) {
        if (!canSeek) return
        player.pause()
        // Show the last frame instead of seeking past it into end-of-stream.
        seekTo(end ? lastFramePosition : 0)
    }
    function stepFrames(frames) {
        if (!canSeek) return
        player.pause()
        seekTo(framePosition(Math.max(1, Math.min(totalFrames, currentFrame + frames))))
    }
    function togglePlayback() {
        if (!player) return
        if (player.playbackState === MediaPlayer.PlayingState) player.pause()
        else player.play()
    }

    TextMetrics {
        id: timeMetrics
        font.family: Theme.monoFontFamily
        font.pixelSize: Theme.fontSizeBody
        text: root.fmtDuration(root.effectiveDuration)
    }

    Row {
        id: playbackControls
        objectName: "playbackControls"
        x: 12
        y: root.stacked ? root.height - height - 8 : (root.height - height) / 2
        spacing: root.compact ? 2 : 4

        AppIconButton {
            objectName: "timelineStart"
            buttonSize: root.compact ? 20 : 30
            anchors.verticalCenter: parent.verticalCenter
            iconName: "go_to_start"; tooltipText: qsTranslate("App", "Start")
            enabled: root.canSeek
            onClicked: root.jumpTo(false)
        }
        AppIconButton {
            objectName: "timelinePreviousFrame"
            buttonSize: root.compact ? 20 : 30
            anchors.verticalCenter: parent.verticalCenter
            iconName: "previous_frame"; tooltipText: qsTranslate("App", "Previous frame")
            enabled: root.canSeek
            onClicked: root.stepFrames(-1)
        }
        AppIconButton {
            objectName: "timelinePlayPause"
            buttonSize: root.compact ? 28 : 36
            iconSize: 20
            activeColor: Theme.textPrimary
            enabled: !!root.player
            tooltipText: root.player && root.player.playbackState === MediaPlayer.PlayingState ? qsTranslate("App", "Pause") : qsTranslate("App", "Play")
            iconName: root.player && root.player.playbackState === MediaPlayer.PlayingState ? "pause" : "play"
            onClicked: root.togglePlayback()
            Rectangle {
                z: -1; anchors.fill: parent; radius: Theme.radiusSmall
                color: Theme.bgSurface; border.color: Theme.borderDefault; border.width: 1
            }
        }
        AppIconButton {
            objectName: "timelineNextFrame"
            buttonSize: root.compact ? 20 : 30
            anchors.verticalCenter: parent.verticalCenter
            iconName: "next_frame"; tooltipText: qsTranslate("App", "Next frame")
            enabled: root.canSeek
            onClicked: root.stepFrames(1)
        }
        AppIconButton {
            objectName: "timelineEnd"
            buttonSize: root.compact ? 20 : 30
            anchors.verticalCenter: parent.verticalCenter
            iconName: "go_to_end"; tooltipText: qsTranslate("App", "End")
            enabled: root.canSeek
            onClicked: root.jumpTo(true)
        }
    }

    Rectangle {
        visible: !root.stacked
        x: playbackControls.x + playbackControls.width + 12
        y: (root.height - height) / 2
        width: 1; height: 28; color: Theme.borderSubtle
    }

    Basic.Slider {
        id: seekSlider
        objectName: "timelineSeek"
        x: root.stacked ? 12 : playbackControls.x + playbackControls.width + 28
        y: 8
        width: Math.max(0, (root.stacked ? root.width - 12 : metadataControls.x - 16) - x)
        height: 52
        padding: 0
        from: 0; to: Math.max(1, root.effectiveDuration)
        value: root.effectivePosition
        stepSize: 1000 / root.effectiveFps
        snapMode: Basic.Slider.SnapAlways
        enabled: root.canSeek
        live: true
        hoverEnabled: true
        focusPolicy: Qt.StrongFocus
        Accessible.name: qsTranslate("App", "Timeline")
        onMoved: root.seekTo(value)
        Keys.onPressed: (event) => {
            if (event.key === Qt.Key_Left || event.key === Qt.Key_Down) {
                root.stepFrames(-1); event.accepted = true
            } else if (event.key === Qt.Key_Right || event.key === Qt.Key_Up) {
                root.stepFrames(1); event.accepted = true
            } else if (event.key === Qt.Key_Home || event.key === Qt.Key_End) {
                root.jumpTo(event.key === Qt.Key_End); event.accepted = true
            } else if (event.key === Qt.Key_Space) {
                root.togglePlayback(); event.accepted = true
            }
        }

        background: Item {
            id: timelineFace
            x: 6
            width: Math.max(0, seekSlider.width - 12)
            height: seekSlider.height
            opacity: root.canSeek ? 1 : 0.45

            Repeater {
                model: root.effectiveDuration > 0 ? Math.floor(root.effectiveDuration / 1000 / (root.rulerStep / 4)) + 1 : 1
                Rectangle {
                    required property int index
                    x: timelineFace.width * root.fractionAt(index * root.rulerStep / 4 * 1000)
                    y: index % 4 === 0 ? 25 : 29
                    width: 1; height: index % 4 === 0 ? 7 : 3
                    color: index % 4 === 0 ? Theme.textMuted : Theme.borderDefault
                }
            }
            Repeater {
                model: root.effectiveDuration > 0 ? Math.floor(root.effectiveDuration / 1000 / root.rulerStep) + 1 : 1
                Text {
                    required property int index
                    readonly property real tickMs: index * root.rulerStep * 1000
                    readonly property real tickX: timelineFace.width * root.fractionAt(tickMs)
                    objectName: "timelineRulerLabel"
                    // Avoid colliding with the duration at the end of the ruler.
                    visible: index === 0 || timelineFace.width - tickX >= root.rulerLabelGap
                    x: Math.max(0, Math.min(timelineFace.width - implicitWidth, tickX - implicitWidth / 2))
                    y: 3
                    text: root.rulerStep < 1 ? root.fmtPrecise(Math.round(tickMs)) : root.fmt(tickMs)
                    font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeSmall
                    color: Theme.textSecondary
                }
            }
            Text {
                objectName: "timelineDurationLabel"
                visible: root.effectiveDuration > 0
                anchors.right: parent.right; y: 3
                text: root.fmtDuration(root.effectiveDuration)
                font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeSmall
                color: Theme.textSecondary
            }
            Rectangle {
                visible: root.effectiveDuration > 0
                x: parent.width - 1; y: 25; width: 1; height: 7
                color: Theme.textMuted
            }
            Rectangle {
                id: groove
                objectName: "timelineGroove"
                y: 36; width: parent.width; height: 8; radius: 4
                color: Theme.bgSelected
                Rectangle {
                    width: parent.width * seekSlider.position
                    height: parent.height
                    topLeftRadius: 4; bottomLeftRadius: 4
                    topRightRadius: 0; bottomRightRadius: 0
                    color: Theme.accent
                }
                // Preview ranges stay green above the played portion.
                Repeater {
                    model: root.renderedRanges
                    Rectangle {
                        required property var modelData
                        readonly property real startFraction: root.fractionAt(Number(modelData.start) * 1000)
                        readonly property real endFraction: root.fractionAt(Number(modelData.end) * 1000)
                        objectName: "timelineRenderedRange"
                        x: groove.width * startFraction
                        width: Math.max(0, groove.width * (endFraction - startFraction))
                        height: groove.height
                        topLeftRadius: startFraction === 0 ? 4 : 0
                        bottomLeftRadius: topLeftRadius
                        topRightRadius: endFraction === 1 ? 4 : 0
                        bottomRightRadius: topRightRadius
                        color: Theme.success
                    }
                }
            }
        }

        handle: Item {
            objectName: "timelinePlayhead"
            x: seekSlider.visualPosition * (seekSlider.availableWidth - width)
            y: 0
            implicitWidth: 12; implicitHeight: seekSlider.height
            width: implicitWidth; height: implicitHeight
            opacity: root.canSeek ? 1 : 0.45
            Rectangle {
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.top: parent.top; anchors.bottom: parent.bottom
                width: 2
                color: Theme.accent
            }
        }

        HoverHandler {
            id: timelineHover
            enabled: seekSlider.enabled
            cursorShape: Qt.PointingHandCursor
        }
        QQC2.ToolTip {
            id: seekTip
            objectName: "timelineSeekTip"
            readonly property real targetMs: seekSlider.pressed ? root.framePosition(root.frameAt(seekSlider.value)) : root.nearestFramePosition(root.effectiveDuration * Math.max(0, Math.min(1, (timelineHover.point.position.x - timelineFace.x) / Math.max(1, timelineFace.width))))
            visible: seekSlider.enabled && (timelineHover.hovered || seekSlider.pressed)
            delay: seekSlider.pressed ? 0 : 180
            x: Math.max(0, Math.min(seekSlider.width - width, (seekSlider.pressed ? seekSlider.handle.x + 6 : timelineHover.point.position.x) - width / 2))
            y: -height - 6
            text: root.fmtExact(targetMs) + "\n" + qsTranslate("App", "Frame %1 of %2").arg(root.frameAt(targetMs)).arg(root.totalFrames)
            leftPadding: 8; rightPadding: 8; topPadding: 5; bottomPadding: 5
            contentItem: Text {
                text: seekTip.text; color: Theme.textPrimary
                font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
            }
            background: Rectangle {
                color: Theme.bgInput; radius: Theme.radiusSmall
                border.color: Theme.borderActive; border.width: 1
            }
        }
    }

    Row {
        id: metadataControls
        objectName: "timelineMetadata"
        anchors.right: parent.right; anchors.rightMargin: 12
        y: root.stacked ? root.height - height - 11 : (root.height - height) / 2
        spacing: root.compact ? 8 : 12
        Item {
            objectName: "timelineTimeReadout"
            width: root.compact ? root.timeCellWidth + 12 : root.timeCellWidth * 2 + 20
            height: root.compact ? 32 : 30
            Text {
                objectName: "timelineCurrentTime"
                x: 0; y: root.compact ? 0 : (parent.height - height) / 2
                text: root.fmt(root.effectivePosition)
                font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeBody; font.weight: Font.DemiBold
                color: Theme.textPrimary
            }
            Text {
                objectName: "timelineTotalTime"
                x: root.compact ? 0 : root.timeCellWidth + 8
                y: root.compact ? parent.height - height : (parent.height - height) / 2
                text: "/ " + root.fmtDuration(root.effectiveDuration)
                font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeBody
                color: Theme.textSecondary
            }
        }
        PlaybackVolumeControl {
            objectName: "timelineVolume"
            audioOutput: root.player ? root.audioOutput : null
            buttonSize: root.compact ? 24 : 28
            anchors.verticalCenter: parent.verticalCenter
        }
        AppComboBox {
            id: speedCombo
            objectName: "timelineSpeed"
            width: root.compact ? 60 : 72; comboHeight: 30
            anchors.verticalCenter: parent.verticalCenter
            enabled: !!root.player
            dropUp: true
            Accessible.name: qsTranslate("App", "Playback speed")
            model: [{label: "0.5x", value: 0.5}, {label: "1x", value: 1}, {label: "1.5x", value: 1.5}, {label: "2x", value: 2}]
            currentValue: root.player ? root.player.playbackRate : 1
            onActivated: (v) => { if (root.player) root.player.playbackRate = v }
        }
    }
}
