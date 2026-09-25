import QtQuick
import ".."
import "../controls"
import "../components"

Item {
    id: root

    property var appBridge: null
    readonly property int adaptiveInspectorWidth: width >= 2560 ? 520 : (width >= 1920 ? 460 : (width >= 1600 ? 420 : (width >= 1280 ? 380 : 340)))

    readonly property bool isImage: appBridge ? (appBridge.upscaleMode === "Image") : true
    readonly property var activeQueue: appBridge ? (isImage ? appBridge.upscaleImageQueue : appBridge.upscaleVideoQueue) : null

    Row {
        anchors.fill: parent

        // Main Center Area: Media Viewport + Batch Queue Drawer
        Item {
            width: parent.width - sidebar.width
            height: parent.height

            MediaViewport {
                id: viewport
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: queueDrawer.top
                appBridge: root.appBridge
            }

            BatchQueueDrawer {
                id: queueDrawer
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                appBridge: root.appBridge
                queueModel: root.activeQueue
                hidden: appBridge ? appBridge.focusPreview : false
                expandedHeight: appBridge ? appBridge.queueHeight : 190
            }
        }

        // Right Sidebar
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
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 6
                z: 100
                cursorShape: Qt.SizeHorCursor
                onPositionChanged: (mouse) => {
                    if (!pressed || !appBridge) return
                    var pt = inspectorResizeHandle.mapToItem(root, mouse.x, mouse.y)
                    appBridge.inspectorWidth = Math.max(300, Math.min(560, root.width - pt.x))
                }
            }

            Column {
                anchors.fill: parent

                // Workflow Submode Switcher: Image vs Video
                Rectangle {
                    width: parent.width
                    height: 48
                    color: Theme.bgSurface
                    border.color: Theme.borderSubtle
                    border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: 8

                        AppSegmentedControl {
                            model: ["Image", "Video"]
                            currentValue: root.appBridge ? root.appBridge.upscaleMode : "Image"
                            onActivated: (val) => {
                                if (root.appBridge) root.appBridge.upscaleMode = val
                            }
                        }
                    }
                }

                // Scrollable Cards Area
                Flickable {
                    width: parent.width
                    height: parent.height - 48 - actionBar.height
                    contentWidth: width
                    contentHeight: contentCol.implicitHeight + 24
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds

                    Column {
                        id: contentCol
                        width: parent.width - 24
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 12
                        spacing: 12

                        // Card 1: Super Resolution
                        AppCard {
                            width: parent.width
                            title: "DLSS Super Resolution"

                            Column {
                                width: parent.width
                                spacing: 12

                                AppComboBox {
                                    width: parent.width
                                    label: "Engine"
                                    model: appBridge ? appBridge.upscaleEngineChoices : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageEngine : "RTX Video Super Resolution") : (appBridge ? appBridge.upscaleEngine : "RTX Video Super Resolution")
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageEngine = v
                                            else appBridge.upscaleEngine = v
                                        }
                                    }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: "DLSS Mode"
                                    visible: appBridge && (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) === "DLSS"
                                    model: appBridge ? appBridge.dlssModeChoices : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageDlssMode : "Quality") : (appBridge ? appBridge.upscaleDlssMode : "Quality")
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageDlssMode = v
                                            else appBridge.upscaleDlssMode = v
                                        }
                                    }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: "DLSS Preset"
                                    visible: appBridge && (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) === "DLSS"
                                    model: appBridge ? appBridge.dlssPresetChoices : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageDlssPreset : "Default") : (appBridge ? appBridge.upscaleDlssPreset : "Default")
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageDlssPreset = v
                                            else appBridge.upscaleDlssPreset = v
                                        }
                                    }
                                }

                                AppSwitch {
                                    visible: !root.isImage
                                             && appBridge && appBridge.upscaleEngine !== "DLSS"
                                    label: "Enable RTX VSR"
                                    checked: appBridge ? appBridge.upscaleVsrEnabled : true
                                    enabled: appBridge ? (!checked || appBridge.upscaleHdrEnabled) : true
                                    onToggled: (c) => { if (appBridge) appBridge.upscaleVsrEnabled = c }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: "VSR Quality"
                                    visible: !appBridge || (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) !== "DLSS"
model: appBridge ? appBridge.vsrQualityChoices : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageVsrQuality : 4) : (appBridge ? appBridge.upscaleVsrQuality : 4)
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageVsrQuality = v
                                            else appBridge.upscaleVsrQuality = v
                                        }
                                    }
                                }

                                AppSegmentedControl {
                                    width: parent.width
                                    label: "Sizing Mode"
                                    visible: !appBridge || (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) !== "DLSS"
                                    model: appBridge ? (root.isImage ? appBridge.imageSizeModeChoices : appBridge.videoSizeModeChoices) : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageSizeMode : "Scale factor") : (appBridge ? appBridge.upscaleSizeMode : "Scale factor")
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageSizeMode = v
                                            else appBridge.upscaleSizeMode = v
                                        }
                                    }
                                }

                                AppComboBox {
                                    width: parent.width
                                    label: "Scale Factor"
                                    visible: appBridge && (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) !== "DLSS" && (root.isImage ? appBridge.upscaleImageSizeMode : appBridge.upscaleSizeMode) === "Scale factor"
                                    model: appBridge ? (root.isImage ? appBridge.imageScaleFactorChoices : appBridge.videoScaleFactorChoices) : []
                                    currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageScaleFactor : 2.0) : (appBridge ? appBridge.upscaleScaleFactor : 2.0)
                                    onActivated: (v) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageScaleFactor = v
                                            else appBridge.upscaleScaleFactor = v
                                        }
                                    }
                                }

                                Row {
                                    width: parent.width
                                    spacing: 8
                                    visible: appBridge && (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) !== "DLSS" && (root.isImage ? appBridge.upscaleImageSizeMode : appBridge.upscaleSizeMode) === "Custom dimensions"

                                    AppTextField {
                                        width: (parent.width - 8) / 2
                                        label: "Width (px)"
                                        text: (root.isImage ? appBridge.upscaleImageWidth : appBridge.upscaleWidth).toString()
                                        commitOnEveryEdit: false
                                        onTextEdited: (t) => {
                                            var num = parseInt(t)
                                            if (!isNaN(num) && num > 0) {
                                                if (root.isImage) appBridge.upscaleImageWidth = num
                                                else appBridge.upscaleWidth = num
                                            }
                                        }
                                    }

                                    AppTextField {
                                        width: (parent.width - 8) / 2
                                        label: "Height (px)"
                                        text: (root.isImage ? appBridge.upscaleImageHeight : appBridge.upscaleHeight).toString()
                                        commitOnEveryEdit: false
                                        onTextEdited: (t) => {
                                            var num = parseInt(t)
                                            if (!isNaN(num) && num > 0) {
                                                if (root.isImage) appBridge.upscaleImageHeight = num
                                                else appBridge.upscaleHeight = num
                                            }
                                        }
                                    }
                                }

                                AppCheckBox {
                                    label: "Lock Aspect Ratio"
                                    visible: !appBridge || (root.isImage ? appBridge.upscaleImageEngine : appBridge.upscaleEngine) !== "DLSS"
                                    checked: root.isImage ? (appBridge ? appBridge.upscaleImageAspectLock : true) : (appBridge ? appBridge.upscaleAspectLock : true)
                                    onToggled: (c) => {
                                        if (appBridge) {
                                            if (root.isImage) appBridge.upscaleImageAspectLock = c
                                            else appBridge.upscaleAspectLock = c
                                        }
                                    }
                                }

                                Text {
                                    width: parent.width
                                    visible: appBridge && appBridge.outputEstimate !== ""
                                    text: appBridge ? appBridge.outputEstimate : ""
                                    wrapMode: Text.Wrap
                                    font.family: Theme.monoFontFamily
                                    font.pixelSize: Theme.fontSizeSmall
                                    color: Theme.textMuted
                                }
                            }
                        }

                        // Card 2: RTX Video HDR (Video Only)
                        AppCard {
                            visible: !root.isImage
                            width: parent.width
                            title: "RTX Video HDR"

                            Column {
                                width: parent.width
                                spacing: 12

                                AppSwitch {
                                    label: "Convert SDR to HDR"
                                    checked: appBridge ? appBridge.upscaleHdrEnabled : false
                                    enabled: appBridge ? (appBridge.upscaleHdrSupported && (!checked || appBridge.upscaleVsrEnabled || appBridge.upscaleEngine === "DLSS")) : false
                                    onToggled: (c) => { if (appBridge) appBridge.upscaleHdrEnabled = c }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: "HDR Contrast"
                                    from: 0
                                    to: 200
                                    stepSize: 1
                                    precision: 0
                                    defaultValue: 100
                                    value: appBridge ? appBridge.upscaleHdrContrast : 100
                                    onValueModified: (v) => { if (appBridge) appBridge.upscaleHdrContrast = Math.round(v) }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: "HDR Saturation"
                                    from: 0
                                    to: 200
                                    stepSize: 1
                                    precision: 0
                                    defaultValue: 100
                                    value: appBridge ? appBridge.upscaleHdrSaturation : 100
                                    onValueModified: (v) => { if (appBridge) appBridge.upscaleHdrSaturation = Math.round(v) }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: "Middle Gray"
                                    from: 10
                                    to: 100
                                    stepSize: 1
                                    precision: 0
                                    defaultValue: 50
                                    value: appBridge ? appBridge.upscaleHdrMiddleGray : 50
                                    onValueModified: (v) => { if (appBridge) appBridge.upscaleHdrMiddleGray = Math.round(v) }
                                }

                                AppSlider {
                                    width: parent.width
                                    label: "Peak Luminance"
                                    unit: "nits"
                                    from: 400
                                    to: 2000
                                    stepSize: 50
                                    precision: 0
                                    defaultValue: 1000
                                    value: appBridge ? appBridge.upscaleHdrPeakLuminance : 1000
                                    onValueModified: (v) => { if (appBridge) appBridge.upscaleHdrPeakLuminance = Math.round(v) }
                                }

                                AppSegmentedControl {
                                    width: parent.width
                                    label: "HDR Precision"
                                    model: appBridge ? appBridge.hdrPrecisionChoices : []
                                    currentValue: appBridge ? appBridge.upscaleHdrPrecision : "Packed 10-bit"
                                    onActivated: (v) => { if (appBridge) appBridge.upscaleHdrPrecision = v }
                                }
                            }
                        }

                        // Card 3: Export Settings
                        AppCard {
                            width: parent.width
                            title: "Export & Formats"

                            Column {
                                width: parent.width
                                spacing: 12

                                Column {
                                    visible: root.isImage
                                    width: parent.width
                                    spacing: 12

                                    AppComboBox {
                                        width: parent.width
                                        label: "Format"
                                        model: appBridge ? appBridge.imageFormatChoices : []
                                        currentValue: appBridge ? appBridge.upscaleImageOutputFormat : "PNG"
                                        onActivated: (v) => { if (appBridge) appBridge.upscaleImageOutputFormat = v }
                                    }

                                    AppSlider {
                                        width: parent.width
                                        label: "Quality"
                                        from: 1
                                        to: 100
                                        stepSize: 1
                                        precision: 0
                                        defaultValue: 95
                                        value: appBridge ? appBridge.upscaleImageQuality : 95
                                        onValueModified: (v) => { if (appBridge) appBridge.upscaleImageQuality = Math.round(v) }
                                    }

                                }

                                Column {
                                    visible: !root.isImage
                                    width: parent.width
                                    spacing: 12

                                    AppComboBox {
                                        width: parent.width
                                        label: "Codec"
                                        model: appBridge ? appBridge.codecChoices : []
                                        currentValue: appBridge ? appBridge.upscaleCodec : "H.265 (NVIDIA NVENC)"
                                        onActivated: (v) => { if (appBridge) appBridge.upscaleCodec = v }
                                    }

                                    AppComboBox {
                                        width: parent.width
                                        label: "Quality"
                                        visible: !appBridge || appBridge.fixedQualityCodecs.indexOf(appBridge.upscaleCodec) < 0
                                        model: appBridge ? appBridge.encodingQualityChoices : []
                                        currentValue: appBridge ? appBridge.upscaleQuality : "Auto (Default)"
                                        onActivated: (v) => { if (appBridge) appBridge.upscaleQuality = v }
                                    }

                                    Text {
                                        visible: appBridge && appBridge.fixedQualityCodecs.indexOf(appBridge.upscaleCodec) >= 0
                                        text: "Quality: Fixed by codec"
                                        color: Theme.textSecondary
                                        font.pixelSize: Theme.fontSizeSmall
                                    }

                                }
                            }
                        }
                    }
                }

                // Bottom Action Bar
                Rectangle {
                    id: actionBar
                    width: parent.width
                    height: 88
                    color: Theme.bgSurface
                    border.color: Theme.borderSubtle
                    border.width: 1

                    Column {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        Row {
                            width: parent.width
                            spacing: 8
                            visible: root.isImage

                            AppButton {
                                text: "Preview"
                                iconName: "preview"
                                width: (parent.width - 16) / 3
                                buttonHeight: 30
                                enabled: appBridge ? appBridge.canPreview : false
                                onClicked: {
                                    if (appBridge) appBridge.renderPreviewAt(viewport.playheadMs)
                                }
                            }

                            AppButton {
                                text: "Reset"
                                iconName: "reset"
                                width: (parent.width - 16) / 3
                                buttonHeight: 30
                                onClicked: { if (appBridge) appBridge.resetTabSettings("upscale") }
                            }

                            AppButton {
                                text: "Outputs"
                                iconName: "outputs_folder"
                                width: (parent.width - 16) / 3
                                buttonHeight: 30
                                onClicked: { if (appBridge) appBridge.openFolder("") }
                            }
                        }

                        Row {
                            width: parent.width
                            spacing: 8
                            visible: !root.isImage

                            AppButton {
                                text: "Preview"
                                iconName: "preview"
                                width: Math.max(100, parent.width - 156)
                                buttonHeight: 30
                                enabled: appBridge ? appBridge.canPreview : false
                                onClicked: { if (appBridge) appBridge.renderPreviewAt(viewport.playheadMs) }
                            }

                            AppComboBox {
                                width: 72
                                comboHeight: 30
                                dropUp: true
                                model: appBridge ? appBridge.upscalePreviewLengthChoices : []
                                currentValue: appBridge ? appBridge.upscalePreviewLength : "3"
                                onActivated: (value) => { if (appBridge) appBridge.upscalePreviewLength = value }
                            }

                            AppIconButton {
                                iconName: "reset"
                                buttonSize: 30
                                tooltipText: "Reset upscale settings"
                                onClicked: { if (appBridge) appBridge.resetTabSettings("upscale") }
                            }

                            AppIconButton {
                                iconName: "outputs_folder"
                                buttonSize: 30
                                tooltipText: "Open outputs folder"
                                onClicked: { if (appBridge) appBridge.openFolder("") }
                            }
                        }

                        AppButton {
                            text: appBridge && appBridge.canStop ? "Stop" : (root.isImage ? "Upscale Image(s)" : "Upscale Video(s)")
                            iconName: appBridge && appBridge.canStop ? "stop" : "start_render"
                            variant: appBridge && appBridge.canStop ? "danger" : "primary"
                            width: parent.width
                            buttonHeight: 34
                            enabled: appBridge ? (appBridge.canStop || (appBridge.operationState === "Idle" && appBridge.runtimeState === "Ready" && !appBridge.isLiveRunning)) : false
                            onClicked: {
                                if (appBridge) {
                                    if (appBridge.canStop) appBridge.stopActiveBatch()
                                    else appBridge.requestActiveBatchExport()
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
