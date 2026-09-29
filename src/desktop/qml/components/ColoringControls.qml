import QtQuick
import QtQuick.Dialogs
import QtQuick.Layouts
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    property real playheadMs: 0
    property string lutPanel: "Color Adjustment"
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    readonly property string autoFrameText: root.isImage ? qsTranslate("App", "Auto") : qsTranslate("App", "Auto 1 Frame")
    readonly property string autoFixedText: qsTranslate("App", "Auto Fixed Frames")
    readonly property real autoFrameWidth: Math.max(80, Math.ceil(frameAutoMetrics.advanceWidth) + 48)
    readonly property real autoFixedWidth: Math.max(80, Math.ceil(fixedAutoMetrics.advanceWidth) + 48)
    readonly property real autoButtonWidth: Math.max(autoFrameWidth, autoFixedWidth)
    width: parent ? parent.width : 320
    spacing: 12

    TextMetrics {
        id: frameAutoMetrics
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLabel
        text: root.autoFrameText
    }
    TextMetrics {
        id: fixedAutoMetrics
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLabel
        text: root.autoFixedText
    }

    AppComboBox {
        width: parent.width
        label: qsTranslate("App", "Method")
        model: root.isImage ? (appBridge ? appBridge.coloringModeChoices : [])
                            : [{label: qsTranslate("App", "Apply LUT"), value: "LUT"}]
        currentValue: root.isImage ? (appBridge ? appBridge.coloringMode : "Color Match") : "LUT"
        onActivated: value => {
            if (value === "LUT") root.lutPanel = "Color Adjustment"
            if (appBridge && root.isImage) appBridge.coloringMode = value
        }
    }

    Column {
        width: parent.width
        spacing: 12
        visible: root.isImage && appBridge && appBridge.coloringMode === "Color Match"

        AppComboBox {
            width: parent.width
            label: qsTranslate("App", "Match Colors From")
            model: appBridge ? appBridge.colorMatchSourceChoices : []
            currentValue: appBridge ? appBridge.colorMatchSource : "Input Image"
            onActivated: value => { if (appBridge) appBridge.colorMatchSource = value }
        }

        AppFilePicker {
            width: parent.width
            visible: appBridge && appBridge.colorMatchSource === "Selected Image"
            label: qsTranslate("App", "Selected Reference Image")
            placeholderText: qsTranslate("App", "Choose a reference image")
            selectedPath: appBridge ? appBridge.colorMatchReference : ""
            nameFilters: [qsTranslate("App", "Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp *.avif *.heic *.heif *.svg *.dng *.cr2 *.nef *.arw)"),
                          qsTranslate("App", "All Files (*.*)")]
            onPathChanged: path => {
                if (appBridge) appBridge.setColorMatchReference(path)
                Qt.callLater(syncPath)
            }
        }
    }

    Column {
        width: parent.width
        visible: !root.isImage || (appBridge && appBridge.coloringMode === "LUT")
        spacing: 12

        AppSegmentedControl {
            width: parent.width
            model: ["Color Adjustment", "LUT File"]
            currentValue: root.lutPanel
            onActivated: value => { root.lutPanel = value }
        }

        AppFilePicker {
            width: parent.width
            visible: root.lutPanel === "LUT File"
            label: qsTranslate("App", "LUT File (.cube)")
            placeholderText: qsTranslate("App", "Choose a 3D .cube LUT")
            selectedPath: appBridge ? appBridge.lutFile : ""
            nameFilters: [qsTranslate("App", "3D LUT Files (*.cube)"), qsTranslate("App", "All Files (*.*)")]
            onPathChanged: path => {
                if (appBridge) appBridge.setLutFile(path)
                Qt.callLater(syncPath)
            }
        }

        Column {
            width: parent.width
            spacing: 12
            visible: root.lutPanel === "Color Adjustment"

            AppComboBox {
                id: resolutionControl
                objectName: "coloring-lut-resolution"
                width: parent.width
                label: qsTranslate("App", "LUT Resolution")
                model: appBridge ? appBridge.lutResolutionChoices : []
                currentValue: appBridge ? appBridge.lutResolution : 33
                onActivated: value => { if (appBridge) appBridge.lutResolution = value }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "LUT Strength")
                from: 0; to: 200; stepSize: 1; precision: 0; defaultValue: 100; unit: "%"
                value: appBridge ? appBridge.lutAdjustmentValues.lut_mix : 100
                onValueModified: v => { if (appBridge) appBridge.setLutAdjustment("lut_mix", v) }
            }

            GridLayout {
                id: autoButtons
                width: parent.width
                columnSpacing: 8
                rowSpacing: 8
                columns: !root.isImage && 2 * root.autoButtonWidth + columnSpacing > width ? 1 : 2

                AppButton {
                    id: frameAutoButton
                    objectName: "coloring-auto-adjustments"
                    // Let the layout own width; AppButton's default binding
                    // would reset it when the busy label changes.
                    width: 0
                    Layout.row: 0
                    Layout.column: 0
                    Layout.fillWidth: true
                    Layout.minimumWidth: root.isImage ? root.autoFrameWidth : root.autoButtonWidth
                    Layout.preferredWidth: root.isImage ? autoButtons.width - 100 : root.autoButtonWidth
                    text: appBridge && appBridge.lutAutoBusy && appBridge.lutAutoMode === "frame"
                          ? qsTranslate("App", "Analyzing…")
                          : root.autoFrameText
                    iconName: "quality_enhance"
                    enabled: appBridge && !!appBridge.previewInputUrl
                             && appBridge.canModifyQueue && !appBridge.lutAutoBusy && !appBridge.lutReferenceBusy
                    onClicked: { if (appBridge) appBridge.autoAdjustLut(root.playheadMs) }
                }

                AppButton {
                    id: fixedAutoButton
                    objectName: "coloring-auto-fixed-frames"
                    width: 0
                    visible: !root.isImage
                    Layout.row: autoButtons.columns === 1 ? 1 : 0
                    Layout.column: autoButtons.columns === 1 ? 0 : 1
                    Layout.fillWidth: true
                    Layout.minimumWidth: root.autoButtonWidth
                    Layout.preferredWidth: root.autoButtonWidth
                    text: appBridge && appBridge.lutAutoBusy && appBridge.lutAutoMode === "fixed"
                          ? qsTranslate("App", "Analyzing…") : root.autoFixedText
                    iconName: "quality_enhance"
                    enabled: frameAutoButton.enabled
                    onClicked: { if (appBridge) appBridge.autoAdjustLutFixedFrames() }
                }

                AppButton {
                    id: resetButton
                    objectName: "coloring-reset-adjustments"
                    width: 0
                    Layout.row: root.isImage ? 0 : (autoButtons.columns === 1 ? 2 : 1)
                    Layout.column: root.isImage ? 1 : 0
                    Layout.columnSpan: root.isImage ? 1 : autoButtons.columns
                    Layout.fillWidth: !root.isImage
                    Layout.minimumWidth: implicitWidth
                    Layout.preferredWidth: root.isImage ? 92 : autoButtons.width
                    text: qsTranslate("App", "Reset")
                    iconName: "reset"
                    enabled: appBridge && appBridge.canModifyQueue && !appBridge.lutAutoBusy && !appBridge.lutReferenceBusy
                    onClicked: { if (appBridge) appBridge.resetLutAdjustments() }
                }
            }

            Text {
                text: qsTranslate("App", "Light")
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                font.weight: Font.DemiBold
                color: Theme.textPrimary
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Exposure")
                from: -3; to: 3; stepSize: 0.05; precision: 2; unit: "EV"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_exposure : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_exposure", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Contrast")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_contrast : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_contrast", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Highlights")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_highlights : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_highlights", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Shadows")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_shadows : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_shadows", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Whites")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_whites : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_whites", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Blacks")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_blacks : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_blacks", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Midtones")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_midtones : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_midtones", v) }
            }

            Text {
                text: qsTranslate("App", "Color")
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                font.weight: Font.DemiBold
                color: Theme.textPrimary
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Temperature")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: ""; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_temperature : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_temperature", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Tint")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: ""; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_tint : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_tint", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Hue Shift")
                from: -180; to: 180; stepSize: 1; precision: 0; unit: "°"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_hue : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_hue", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Vibrance")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_vibrance : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_vibrance", v) }
            }

            AppSlider {
                width: parent.width
                label: qsTranslate("App", "Saturation")
                from: -100; to: 100; stepSize: 1; precision: 0; unit: "%"; defaultValue: 0
                value: root.appBridge ? root.appBridge.lutAdjustmentValues.lut_saturation : 0
                onValueModified: v => { if (root.appBridge) root.appBridge.setLutAdjustment("lut_saturation", v) }
            }
        }

        AppButton {
            objectName: "coloring-save-lut"
            width: parent.width
            text: appBridge && appBridge.lutSaveBusy ? qsTranslate("App", "Saving LUT…") : qsTranslate("App", "Save LUT")
            iconName: "export"
            variant: "primary"
            enabled: appBridge && !appBridge.lutSaveBusy && !appBridge.lutReferenceBusy
                     && (root.lutPanel === "Color Adjustment" || !!appBridge.lutFile)
            onClicked: lutSaveDialog.open()
        }

        Text {
            width: parent.width
            visible: appBridge && appBridge.lutSaveStatus !== ""
            text: appBridge ? appBridge.lutSaveStatus : ""
            wrapMode: Text.WordWrap
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSmall
            color: Theme.textSecondary
        }

        AppFilePicker {
            objectName: "coloring-lut-reference-image"
            width: parent.width
            label: qsTranslate("App", "Reference Image")
            placeholderText: qsTranslate("App", "Choose an image to match its colors")
            selectedPath: appBridge ? appBridge.lutReferenceImage : ""
            enabled: appBridge && appBridge.canModifyQueue && !appBridge.lutAutoBusy
            nameFilters: [qsTranslate("App", "Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp *.avif *.heic *.heif *.svg *.dng *.cr2 *.nef *.arw)"),
                          qsTranslate("App", "All Files (*.*)")]
            onPathChanged: path => {
                if (appBridge) appBridge.setLutReferenceImage(path)
                Qt.callLater(syncPath)
            }
        }

        Text {
            objectName: "coloring-lut-reference-status"
            width: parent.width
            visible: appBridge && appBridge.lutReferenceStatus !== ""
            text: appBridge ? appBridge.lutReferenceStatus : ""
            wrapMode: Text.WordWrap
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSmall
            color: appBridge && appBridge.lutReferenceBusy ? Theme.accent : Theme.textSecondary
        }
    }

    FileDialog {
        id: lutSaveDialog
        title: qsTranslate("App", "Save Graded 3D LUT")
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cube"
        nameFilters: [qsTranslate("App", "3D LUT Files (*.cube)")]
        onAccepted: { if (appBridge && selectedFile) appBridge.saveLut(selectedFile.toString()) }
    }
}
