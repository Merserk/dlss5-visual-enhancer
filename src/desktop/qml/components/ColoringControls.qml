import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    property string lutPanel: "Color Adjustment"
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    width: parent ? parent.width : 320
    spacing: 12

    AppComboBox {
        width: parent.width
        label: "Method"
        model: root.isImage ? (appBridge ? appBridge.coloringModeChoices : [])
                            : [{label: "Apply LUT", value: "LUT"}]
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
            label: "Match Colors From"
            model: appBridge ? appBridge.colorMatchSourceChoices : []
            currentValue: appBridge ? appBridge.colorMatchSource : "Input Image"
            onActivated: value => { if (appBridge) appBridge.colorMatchSource = value }
        }

        AppFilePicker {
            width: parent.width
            visible: appBridge && appBridge.colorMatchSource === "Selected Image"
            label: "Selected Reference Image"
            placeholderText: "Choose a reference image"
            selectedPath: appBridge ? appBridge.colorMatchReference : ""
            nameFilters: ["Images (*.png *.jpg *.jpeg *.webp *.tif *.tiff *.bmp *.avif *.heic *.heif *.svg *.dng *.cr2 *.nef *.arw)",
                          "All Files (*.*)"]
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
            label: "LUT File (.cube)"
            placeholderText: "Choose a 3D .cube LUT"
            selectedPath: appBridge ? appBridge.lutFile : ""
            nameFilters: ["3D LUT Files (*.cube)", "All Files (*.*)"]
            onPathChanged: path => {
                if (appBridge) appBridge.setLutFile(path)
                Qt.callLater(syncPath)
            }
        }

        Column {
            width: parent.width
            spacing: 12
            visible: root.lutPanel === "Color Adjustment"

            Item {
                width: parent.width
                height: resolutionControl.implicitHeight

                AppComboBox {
                    id: resolutionControl
                    objectName: "coloring-lut-resolution"
                    width: parent.width - resetButton.width - 8
                    height: implicitHeight
                    anchors.left: parent.left
                    label: "LUT Resolution"
                    model: appBridge ? appBridge.lutResolutionChoices : []
                    currentValue: appBridge ? appBridge.lutResolution : 33
                    onActivated: value => { if (appBridge) appBridge.lutResolution = value }
                }

                AppButton {
                    id: resetButton
                    objectName: "coloring-reset-adjustments"
                    width: 92
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    text: "Reset"
                    iconName: "reset"
                    onClicked: { if (appBridge) appBridge.resetLutAdjustments() }
                }
            }

            AppSlider {
                width: parent.width
                label: "LUT Strength"
                from: 0; to: 200; stepSize: 1; precision: 0; defaultValue: 100; unit: "%"
                value: appBridge ? appBridge.lutAdjustmentValues.lut_mix : 100
                onValueModified: v => { if (appBridge) appBridge.setLutAdjustment("lut_mix", v) }
            }

            AppButton {
                objectName: "coloring-auto-adjustments"
                width: parent.width
                text: appBridge && appBridge.lutAutoBusy ? "Analyzing…" : "Auto"
                iconName: "quality_enhance"
                enabled: appBridge && !!appBridge.previewInputUrl
                         && appBridge.canModifyQueue && !appBridge.lutAutoBusy
                onClicked: { if (appBridge) appBridge.autoAdjustLut() }
            }

            Text {
                text: "Light"
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                font.weight: Font.DemiBold
                color: Theme.textPrimary
            }

            Repeater {
                model: [
                    {key: "lut_exposure", label: "Exposure", from: -3, to: 3, step: 0.05, precision: 2, unit: "EV"},
                    {key: "lut_contrast", label: "Contrast", from: -100, to: 100, step: 1, precision: 0, unit: "%"},
                    {key: "lut_highlights", label: "Highlights", from: -100, to: 100, step: 1, precision: 0, unit: "%"},
                    {key: "lut_shadows", label: "Shadows", from: -100, to: 100, step: 1, precision: 0, unit: "%"},
                    {key: "lut_whites", label: "Whites", from: -100, to: 100, step: 1, precision: 0, unit: "%"},
                    {key: "lut_blacks", label: "Blacks", from: -100, to: 100, step: 1, precision: 0, unit: "%"},
                    {key: "lut_midtones", label: "Midtones", from: -100, to: 100, step: 1, precision: 0, unit: "%"}
                ]
                delegate: AppSlider {
                    required property var modelData
                    width: root.width
                    label: modelData.label
                    from: modelData.from; to: modelData.to; stepSize: modelData.step
                    precision: modelData.precision; unit: modelData.unit; defaultValue: 0
                    value: appBridge ? appBridge.lutAdjustmentValues[modelData.key] : 0
                    onValueModified: v => { if (appBridge) appBridge.setLutAdjustment(modelData.key, v) }
                }
            }

            Text {
                text: "Color"
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                font.weight: Font.DemiBold
                color: Theme.textPrimary
            }

            Repeater {
                model: [
                    {key: "lut_temperature", label: "Temperature", from: -100, to: 100, unit: ""},
                    {key: "lut_tint", label: "Tint", from: -100, to: 100, unit: ""},
                    {key: "lut_hue", label: "Hue Shift", from: -180, to: 180, unit: "°"},
                    {key: "lut_vibrance", label: "Vibrance", from: -100, to: 100, unit: "%"},
                    {key: "lut_saturation", label: "Saturation", from: -100, to: 100, unit: "%"}
                ]
                delegate: AppSlider {
                    required property var modelData
                    width: root.width
                    label: modelData.label
                    from: modelData.from; to: modelData.to; stepSize: 1
                    precision: 0; unit: modelData.unit; defaultValue: 0
                    value: appBridge ? appBridge.lutAdjustmentValues[modelData.key] : 0
                    onValueModified: v => { if (appBridge) appBridge.setLutAdjustment(modelData.key, v) }
                }
            }
        }

        AppButton {
            width: parent.width
            text: appBridge && appBridge.lutSaveBusy ? "Saving LUT…" : "Save LUT"
            iconName: "export"
            variant: "primary"
            enabled: appBridge && !appBridge.lutSaveBusy
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
    }

    FileDialog {
        id: lutSaveDialog
        title: "Save Graded 3D LUT"
        fileMode: FileDialog.SaveFile
        defaultSuffix: "cube"
        nameFilters: ["3D LUT Files (*.cube)"]
        onAccepted: { if (appBridge && selectedFile) appBridge.saveLut(selectedFile.toString()) }
    }
}
