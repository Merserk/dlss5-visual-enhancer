import QtQuick
import QtQuick.Window
import ".."
import "../controls"
import "../components"

Item {
    id: root

    property var appBridge: null
    readonly property int adaptiveInspectorWidth: width >= 2560 ? 520 : (width >= 1920 ? 470 : (width >= 1600 ? 430 : (width >= 1280 ? 390 : 350)))
    readonly property bool showLiveVideo: appBridge && (appBridge.isLiveRunning || appBridge.operationState === "LiveStarting")

    property string sourceMode: "Online"
    property string onlineUrl: ""
    property string localVideoPath: ""
    property string sourceQuality: "Auto"
    property string maxHeight: "720"
    property string segmentSeconds: "2"
    property string targetFps: "Auto"
    property real bufferSeconds: 6.0
    property bool videoExpanded: false

    function syncLiveVideo() {
        if (!root.showLiveVideo || !appBridge || !appBridge.mpvEmbed) return
        // Native overlay guard: the embed container is a top-level OS window
        // that StackLayout/clip cannot hide. Never (re)position or re-show
        // it unless the Live tab is the visible one and this view is shown.
        if (!root.visible) return
        try {
            if (appBridge.activeTab !== "live") return
        } catch (e) {}
        var host = Window.window
        if (!host || !host.contentItem) return
        // Client-relative: the embed container is a child window, so its
        // origin is the client area (below the native caption), not the
        // outer frame. This holds with any window frame style.
        var pt = videoSurface.mapToGlobal(0, 0)
        var origin = host.contentItem.mapToGlobal(0, 0)
        appBridge.mpvEmbed.setGeometry(pt.x - origin.x, pt.y - origin.y, videoSurface.width, videoSurface.height)
    }

    onVisibleChanged: {
        // StackLayout hides non-current tabs: re-sync on return so the
        // native window snaps back to the slot without waiting for the
        // 250ms keep-alive timer.
        if (visible && showLiveVideo) Qt.callLater(syncLiveVideo)
    }

    onShowLiveVideoChanged: {
        if (showLiveVideo) Qt.callLater(syncLiveVideo)
        else videoExpanded = false
    }

    Row {
        anchors.fill: parent

        // Left Area: Live Telemetry & Monitoring Canvas
        Rectangle {
            width: parent.width - sidebar.width
            height: parent.height
            color: Theme.bgBase

            Column {
                id: leftCol
                anchors.fill: parent
                anchors.margins: 24
                spacing: 16

                // Live Monitor Header
                Row {
                    id: liveHeader
                    width: parent.width
                    spacing: 12

                    Rectangle {
                        width: 12
                        height: 12
                        radius: 6
                        color: appBridge && appBridge.isLiveRunning ? Theme.danger : Theme.textMuted
                        anchors.verticalCenter: parent.verticalCenter

                        SequentialAnimation on opacity {
                            running: appBridge && appBridge.isLiveRunning
                            loops: Animation.Infinite
                            NumberAnimation { from: 1.0; to: 0.3; duration: 800 }
                            NumberAnimation { from: 0.3; to: 1.0; duration: 800 }
                        }
                    }

                    Text {
                        text: appBridge && appBridge.isLiveRunning ? qsTranslate("App", "LIVE STREAM ACTIVE") : qsTranslate("App", "LIVE ENGINE STANDBY")
                        font.family: Theme.fontFamily
                        font.pixelSize: Theme.fontSizeTitle
                        font.weight: Font.Bold
                        color: appBridge && appBridge.isLiveRunning ? Theme.danger : Theme.textSecondary
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }

                // In-tab player (embedded MPV). Always visible so standby
                // mirrors the active layout; idle shows a black blank slot.
                Column {
                    id: livePlayer
                    width: parent.width
                    visible: true
                    spacing: 8

                    Item {
                        id: videoSurface
                        width: parent.width
                        // Flexible: take all vertical room left by header /
                        // transport / KPI / log strip (theater mode takes all).
                        // Idle shows a 16:9 black blank of the same shape.
                        height: {
                            if (!root.showLiveVideo) return Math.max(200, Math.min(width * 9 / 16, 720))
                            if (root.videoExpanded)
                                return Math.max(200, leftCol.height - liveHeader.height - 34 - 8 - 16)
                            var room = leftCol.height - liveHeader.height - kpiFlow.height - 120 - 34 - 8 - 48
                            return Math.max(200, Math.min(Math.min(width * 9 / 16, 720), room))
                        }
                        onWidthChanged: root.syncLiveVideo()
                        onHeightChanged: root.syncLiveVideo()

                        Rectangle {
                            anchors.fill: parent
                            radius: Theme.radiusLarge
                            color: "#000000"
                            border.color: Theme.borderDefault
                            border.width: 1
                        }

                        // Shown only while a session is starting/running and the
                        // first frame hasn't rendered yet: idle is a pure
                        // black blank, never a "Buffering..." message.
                        Column {
                            anchors.centerIn: parent
                            spacing: 10
                            visible: root.showLiveVideo && (appBridge ? !appBridge.livePlayerStarted : true)

                            Rectangle {
                                width: 12
                                height: 12
                                radius: 6
                                color: Theme.accent
                                anchors.horizontalCenter: parent.horizontalCenter

                                SequentialAnimation on opacity {
                                    running: true
                                    loops: Animation.Infinite
                                    NumberAnimation { from: 1.0; to: 0.3; duration: 600 }
                                    NumberAnimation { from: 0.3; to: 1.0; duration: 600 }
                                }
                            }

                            Text {
                                text: appBridge && appBridge.operationState === "LiveStarting"
                                      ? qsTranslate("App", "Loading... Please wait.")
                                      : qsTranslate("App", "Buffering... Please wait.")
                                font.family: Theme.fontFamily
                                font.pixelSize: Theme.fontSizeBody
                                color: Theme.textSecondary
                                anchors.horizontalCenter: parent.horizontalCenter
                            }
                        }
                    }

                    Row {
                        id: liveTransport
                        width: parent.width
                        height: 34
                        spacing: 8

                        AppButton {
                            width: 96
                            buttonHeight: 32
                            text: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.paused ? qsTranslate("App", "Resume") : qsTranslate("App", "Pause")
                            iconName: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.paused ? "play" : "pause"
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { if (appBridge && appBridge.mpvEmbed) appBridge.mpvEmbed.togglePause() }
                        }
                        AppButton {
                            width: 96
                            buttonHeight: 32
                            text: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.muted ? qsTranslate("App", "Unmute") : qsTranslate("App", "Mute")
                            iconName: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.muted ? "volume_up" : "mute"
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { if (appBridge && appBridge.mpvEmbed) appBridge.mpvEmbed.toggleMute() }
                        }
                        AppIconButton {
                            buttonSize: 32
                            iconName: "volume_down"
                            tooltipText: qsTranslate("App", "Volume down")
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { if (appBridge && appBridge.mpvEmbed) appBridge.mpvEmbed.volumeDown() }
                        }
                        Text {
                            width: 56
                            text: qsTranslate("App", "Vol %1").arg(appBridge && appBridge.mpvEmbed ? appBridge.mpvEmbed.volume : "-")
                            font.family: Theme.monoFontFamily
                            font.pixelSize: Theme.fontSizeSmall
                            color: Theme.textSecondary
                            elide: Text.ElideRight
                            anchors.verticalCenter: parent.verticalCenter
                            horizontalAlignment: Text.AlignHCenter
                        }
                        AppIconButton {
                            buttonSize: 32
                            iconName: "volume_up"
                            tooltipText: qsTranslate("App", "Volume up")
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { if (appBridge && appBridge.mpvEmbed) appBridge.mpvEmbed.volumeUp() }
                        }
                        // Spacer: pushes Expand + Full Screen to the right end
                        // (plain Row cannot right-anchor children). Clamped so
                        // narrow windows degrade to left-packed, never overlap.
                        Item {
                            width: Math.max(0, parent.width - 596)
                            height: 1
                        }
                        AppButton {
                            width: 104
                            buttonHeight: 32
                            text: root.videoExpanded ? qsTranslate("App", "Restore") : qsTranslate("App", "Expand")
                            iconName: root.videoExpanded ? "collapse" : "expand"
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { root.videoExpanded = !root.videoExpanded; Qt.callLater(root.syncLiveVideo) }
                        }
                        AppButton {
                            width: 124
                            buttonHeight: 32
                            text: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.fullscreen ? qsTranslate("App", "Exit Full") : qsTranslate("App", "Full Screen")
                            iconName: appBridge && appBridge.mpvEmbed && appBridge.mpvEmbed.fullscreen ? "exit_fullscreen" : "fullscreen"
                            enabled: appBridge && appBridge.isLiveRunning
                            onClicked: { if (appBridge && appBridge.mpvEmbed) appBridge.mpvEmbed.toggleFullscreen() }
                        }
                    }
                }

                Connections {
                    target: root.Window.window
                    function onXChanged() { root.syncLiveVideo() }
                    function onYChanged() { root.syncLiveVideo() }
                    function onWidthChanged() { root.syncLiveVideo() }
                    function onHeightChanged() { root.syncLiveVideo() }
                    function onScreenChanged() { Qt.callLater(root.syncLiveVideo) }
                }

                Connections {
                    target: appBridge
                    function onActiveTabChanged() {
                        if (appBridge && appBridge.activeTab === "live" && root.showLiveVideo)
                            Qt.callLater(root.syncLiveVideo)
                    }
                }

                Timer {
                    interval: 250
                    running: root.showLiveVideo && root.visible && appBridge && appBridge.activeTab === "live"
                    repeat: true
                    onTriggered: root.syncLiveVideo()
                }

                // High-signal telemetry remains visible at a glance on large displays.
                // Hidden in theater mode so the video takes the whole canvas.
                Flow {
                    id: kpiFlow
                    width: parent.width
                    visible: !root.videoExpanded
                    height: root.videoExpanded ? 0 : (width >= 900 ? 72 : 144)
                    spacing: 8

                    Repeater {
                        model: [
                            { label: qsTranslate("App", "SOURCE"), value: appBridge && appBridge.liveSourceFps > 0 ? appBridge.liveSourceFps.toFixed(2) + " fps" : "-" },
                            { label: qsTranslate("App", "OUTPUT"), value: appBridge && appBridge.liveEffectiveFps > 0 ? appBridge.liveEffectiveFps.toFixed(2) + " fps" : (appBridge && appBridge.liveTargetFps > 0 ? appBridge.liveTargetFps.toFixed(2) + " fps" : "-") },
                            { label: "DLSS", value: appBridge && appBridge.liveDlssMs > 0 ? appBridge.liveDlssMs.toFixed(1) + " ms" : "-" },
                            { label: qsTranslate("App", "ENCODE"), value: appBridge && appBridge.liveEncodeMs > 0 ? appBridge.liveEncodeMs.toFixed(1) + " ms" : "-" },
                            { label: qsTranslate("App", "DROPPED"), value: appBridge ? String(appBridge.liveDroppedFrames) : "0" },
                            { label: qsTranslate("App", "REBUFFER"), value: appBridge ? String(appBridge.liveRebufferEvents) : "0" }
                        ]
                        delegate: Rectangle {
                            required property var modelData
                            width: kpiFlow.width >= 900 ? (kpiFlow.width - 40) / 6 : (kpiFlow.width - 16) / 3
                            height: 64
                            radius: Theme.radiusMedium
                            color: Theme.bgSurface
                            border.color: Theme.borderSubtle
                            Column {
                                anchors.fill: parent; anchors.margins: 10; spacing: 4
                                Text { text: modelData.label; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; font.weight: Font.DemiBold }
                                Text { text: modelData.value; color: Theme.textPrimary; font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeBody; font.weight: Font.DemiBold; elide: Text.ElideRight; width: parent.width }
                            }
                        }
                    }
                }

                // Telemetry Terminal Box: fills the remaining canvas in both
                // idle and running states (scrollable), hidden in theater mode.
                Rectangle {
                    id: telemetryBox
                    width: parent.width
                    visible: !root.videoExpanded
                    height: {
                        if (root.videoExpanded) return 0
                        return Math.max(140, parent.height - liveHeader.height - livePlayer.height - kpiFlow.height - 48)
                    }
                    radius: Theme.radiusLarge
                    color: Theme.bgSurface
                    border.color: Theme.borderDefault
                    border.width: 1
                    clip: true

                    Flickable {
                        anchors.fill: parent
                        anchors.margins: 16
                        contentWidth: width
                        contentHeight: telemetryText.implicitHeight
                        clip: true

                        Text {
                            id: telemetryText
                            width: parent.width
                            text: appBridge ? appBridge.liveStatusText : qsTranslate("App", "Idle.")
                            font.family: Theme.monoFontFamily
                            font.pixelSize: 13
                            lineHeight: 1.4
                            color: appBridge && appBridge.isLiveRunning ? Theme.textPrimary : Theme.textMuted
                        }
                    }
                }
            }
        }

        // Right Sidebar: Live Configuration
        Rectangle {
            id: sidebar
            width: appBridge && appBridge.focusPreview ? 0 : Math.max(root.adaptiveInspectorWidth, Math.min(560, appBridge ? appBridge.inspectorWidth : root.adaptiveInspectorWidth))
            height: parent.height
            visible: width > 0
            color: Theme.bgSurface
            border.color: Theme.borderSubtle
            border.width: 1

            MouseArea {
                id: inspectorResizeHandle
                anchors.left: parent.left; anchors.top: parent.top; anchors.bottom: parent.bottom
                width: 6; z: 100; cursorShape: Qt.SizeHorCursor
                onPositionChanged: (mouse) => {
                    if (!pressed || !appBridge) return
                    var pt = inspectorResizeHandle.mapToItem(root, mouse.x, mouse.y)
                    appBridge.inspectorWidth = Math.max(300, Math.min(560, root.LayoutMirroring.enabled ? pt.x : root.width - pt.x))
                }
            }

            Column {
                anchors.fill: parent

                Flickable {
                    width: parent.width
                    height: parent.height - actionBar.height
                    contentWidth: width
                    contentHeight: liveCol.implicitHeight + 24
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds

                    Column {
                        id: liveCol
                        width: parent.width - 24
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 12
                        spacing: 12

                        // Card 1: Source Selection
                        AppCard {
                            width: parent.width
                            title: qsTranslate("App", "Stream Source")

                            Column {
                                width: parent.width
                                spacing: 12

                                AppSegmentedControl {
                                    width: parent.width
                                    model: ["Online", "Local"]
                                    currentValue: root.sourceMode
                                    onActivated: (v) => { root.sourceMode = v }
                                }

                                AppTextField {
                                    visible: root.sourceMode === "Online"
                                    width: parent.width
                                    label: qsTranslate("App", "Online Stream URL")
                                    placeholderText: qsTranslate("App", "YouTube, Twitch, or HLS/RTMP URL...")
                                    text: root.onlineUrl
                                    onTextEdited: (t) => { root.onlineUrl = t }
                                }

                                AppFilePicker {
                                    visible: root.sourceMode === "Local"
                                    width: parent.width
                                    label: qsTranslate("App", "Local Video File")
                                    placeholderText: qsTranslate("App", "Select MP4/MKV video...")
                                    selectedPath: root.localVideoPath
                                    nameFilters: [
                                        qsTranslate("App", "Video Files (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.ts *.mts *.m2ts *.mxf *.vob *.wmv *.flv *.mpg *.mpeg *.mpe *.ogv *.3gp *.3g2 *.asf *.divx *.f4v *.m2v *.m1v *.m2t)"),
                                        qsTranslate("App", "All Files (*.*)")
                                    ]
                                    onPathChanged: (p) => { root.localVideoPath = p }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: qsTranslate("App", "Source Quality")
                                    model: appBridge ? appBridge.liveSourceQualityChoices : []
                                    currentValue: root.sourceQuality
                                    onActivated: (v) => { root.sourceQuality = v }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: qsTranslate("App", "Max Input Resolution")
                                    model: appBridge ? appBridge.liveMaxHeightChoices : []
                                    currentValue: root.maxHeight
                                    onActivated: (v) => { root.maxHeight = v }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: qsTranslate("App", "HLS Segment Duration")
                                    model: appBridge ? appBridge.liveSegmentChoices : []
                                    currentValue: root.segmentSeconds
                                    onActivated: (v) => { root.segmentSeconds = v }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: qsTranslate("App", "Target FPS")
                                    model: appBridge ? appBridge.liveFpsChoices : []
                                    currentValue: root.targetFps
                                    onActivated: (v) => { root.targetFps = v }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: qsTranslate("App", "Playback Buffer")
                                    unit: qsTranslate("App", "sec")
                                    from: 2
                                    to: 30
                                    stepSize: 1
                                    precision: 0
                                    defaultValue: 6
                                    value: root.bufferSeconds
                                    onValueModified: (v) => { root.bufferSeconds = Math.round(v) }
                                }
                            }
                        }

                        // Card 2: Live AI Neural Controls (Dynamic Live Sync!)
                        AppCard {
                            width: parent.width
                            title: qsTranslate("App", "Live DLSS Effects")

                            Column {
                                width: parent.width
                                spacing: 12

                                AppSegmentedControl {
                                    width: parent.width
                                    label: qsTranslate("App", "NR Style")
                                    model: appBridge ? appBridge.nrStyleChoices : []
                                    currentValue: appBridge ? appBridge.liveNrStyle : "Default"
                                    onActivated: (v) => { if (appBridge) appBridge.liveNrStyle = v }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: qsTranslate("App", "NR Intensity")
                                    from: 0.0
                                    to: 2.0
                                    stepSize: 0.05
                                    defaultValue: 1.0
                                    value: appBridge ? appBridge.liveNrIntensity : 1.0
                                    onValueModified: (v) => { if (appBridge) appBridge.liveNrIntensity = v }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: qsTranslate("App", "Shimmer Suppression")
                                    from: 0.0
                                    to: 1.0
                                    stepSize: 0.05
                                    defaultValue: 0.70
                                    value: appBridge ? appBridge.liveShimmerSuppression : 0.70
                                    onValueModified: (v) => { if (appBridge) appBridge.liveShimmerSuppression = v }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: qsTranslate("App", "Local Tone Strength")
                                    from: 0.0
                                    to: 2.0
                                    stepSize: 0.05
                                    defaultValue: 1.0
                                    value: appBridge ? appBridge.liveLocalToneStrength : 1.0
                                    onValueModified: (v) => { if (appBridge) appBridge.liveLocalToneStrength = v }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: qsTranslate("App", "Local Structure Strength")
                                    from: 0.0
                                    to: 2.0
                                    stepSize: 0.05
                                    defaultValue: 1.0
                                    value: appBridge ? appBridge.liveLocalStructureStrength : 1.0
                                    onValueModified: (v) => { if (appBridge) appBridge.liveLocalStructureStrength = v }
                                }

                                AppSlider { width: parent.width; label: qsTranslate("App", "NR Passes"); from: 1; to: 4; stepSize: 1; precision: 0; defaultValue: 1; value: appBridge ? appBridge.liveNrPasses : 1; onValueModified: (v) => { if (appBridge) appBridge.liveNrPasses = Math.round(v) } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Skin Structure"); from: -1; to: 2; stepSize: 0.05; defaultValue: -1; value: appBridge ? appBridge.liveSkinStructureStrength : -1; onValueModified: (v) => { if (appBridge) appBridge.liveSkinStructureStrength = v } }
                                AppSwitch { label: qsTranslate("App", "Automatic Mask"); checked: appBridge ? appBridge.liveAutomaticMask : false; onToggled: (v) => { if (appBridge) appBridge.liveAutomaticMask = v } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Color Strength"); from: 0; to: 1; stepSize: 0.05; defaultValue: 1; value: appBridge ? appBridge.liveNrColorStrength : 1; onValueModified: (v) => { if (appBridge) appBridge.liveNrColorStrength = v } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Tone Preservation"); from: 0; to: 1; stepSize: 0.05; defaultValue: 0; value: appBridge ? appBridge.liveTonePreservation : 0; onValueModified: (v) => { if (appBridge) appBridge.liveTonePreservation = v } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Face / Skin Protection"); from: 0; to: 1; stepSize: 0.05; defaultValue: 0; value: appBridge ? appBridge.liveFaceSkinProtection : 0; onValueModified: (v) => { if (appBridge) appBridge.liveFaceSkinProtection = v } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Grain Preservation"); from: 0; to: 1; stepSize: 0.05; defaultValue: 0; value: appBridge ? appBridge.liveGrainPreservation : 0; onValueModified: (v) => { if (appBridge) appBridge.liveGrainPreservation = v } }
                                AppSlider { width: parent.width; label: qsTranslate("App", "Mask Feather"); from: 0; to: 128; stepSize: 1; precision: 0; defaultValue: 0; value: appBridge ? appBridge.liveMaskFeather : 0; onValueModified: (v) => { if (appBridge) appBridge.liveMaskFeather = Math.round(v) } }
                                AppComboBox { width: parent.width; dropUp: true; label: qsTranslate("App", "Live Scale"); model: appBridge ? appBridge.nrScaleChoices : []; currentValue: appBridge ? appBridge.liveUpscalingFactor : 1.0; onActivated: (v) => { if (appBridge) appBridge.liveUpscalingFactor = v } }
                            }
                        }
                    }
                }

                // Action Bar
                Rectangle {
                    id: actionBar
                    width: parent.width
                    height: 72
                    color: Theme.bgSurface
                    border.color: Theme.borderSubtle
                    border.width: 1

                    Row {
                        anchors.fill: parent
                        anchors.margins: 10
                        spacing: 8

                        AppButton {
                            width: parent.width - 98
                            buttonHeight: 36
                            text: appBridge && appBridge.operationState === "LiveStarting" ? qsTranslate("App", "Cancel Live Startup") : (appBridge && appBridge.isLiveRunning ? qsTranslate("App", "Stop Live Session") : (appBridge && appBridge.operationState === "LiveStopping" ? qsTranslate("App", "Stopping Live...") : qsTranslate("App", "Start Live Session")))
                            iconName: appBridge && appBridge.canStop ? "stop" : "start_render"
                            variant: appBridge && appBridge.canStop ? "danger" : "primary"
                            enabled: appBridge ? (appBridge.canStartLive || appBridge.canStop) : false
                            onClicked: {
                                if (!appBridge) return
                                if (appBridge.canStop) {
                                    appBridge.stopActiveBatch()
                                } else if (appBridge.canStartLive) {
                                    appBridge.startLive(
                                        root.sourceMode,
                                        root.onlineUrl,
                                        root.localVideoPath,
                                        root.sourceQuality,
                                        root.maxHeight,
                                        root.segmentSeconds,
                                        root.targetFps,
                                        root.bufferSeconds
                                    )
                                }
                            }
                        }

                        AppButton {
                            text: qsTranslate("App", "Reset")
                            iconName: "reset"
                            width: 90
                            buttonHeight: 36
                            onClicked: {
                                root.sourceMode = "Online"
                                root.onlineUrl = ""
                                root.localVideoPath = ""
                                root.sourceQuality = "Auto"
                                root.maxHeight = "720"
                                root.segmentSeconds = "2"
                                root.targetFps = "Auto"
                                root.bufferSeconds = 6
                                if (appBridge) appBridge.resetTabSettings("live")
                            }
                        }
                    }
                }
            }
        }
    }
}
