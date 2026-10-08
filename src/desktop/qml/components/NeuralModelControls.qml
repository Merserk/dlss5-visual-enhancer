import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    width: parent ? parent.width : 320
    spacing: 12
    AppSegmentedControl {
        width: parent.width
        label: qsTranslate("App", "NR Style")
        model: appBridge ? appBridge.nrStyleChoices : []
        currentValue: appBridge ? appBridge.nrStyle : "Style 0"
        onActivated: v => {
            if (appBridge)
                appBridge.nrStyle = v;
        }
    }

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "NR Intensity")
        from: 0.0
        to: 2.0
        stepSize: 0.05
        defaultValue: 1.0
        value: appBridge ? appBridge.nrIntensity : 1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.nrIntensity = v;
        }
    }

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "NR Passes")
        from: 1
        to: 4
        stepSize: 1
        precision: 0
        defaultValue: 1
        value: appBridge ? appBridge.nrPasses : 1
        onValueModified: v => {
            if (appBridge)
                appBridge.nrPasses = Math.round(v);
        }
    }

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "Local Tone Strength")
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 1.0
        value: appBridge ? appBridge.localToneStrength : 1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.localToneStrength = v;
        }
    }

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "Local Structure Strength")
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 1.0
        value: appBridge ? appBridge.localStructureStrength : 1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.localStructureStrength = v;
        }
    }

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "Skin Structure Strength")
        enabled: appBridge ? appBridge.automaticMask && !appBridge.customMaskStatus : false
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 1.0
        value: appBridge ? appBridge.skinStructureStrength : 1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.skinStructureStrength = v;
        }
    }

    AppSwitch {
        label: qsTranslate("App", "Automatic Mask")
        enabled: appBridge ? !appBridge.customMaskStatus : true
        checked: appBridge ? appBridge.automaticMask && !appBridge.customMaskStatus : false
        onToggled: c => {
            if (appBridge)
                appBridge.automaticMask = c;
        }
    }

    Text {
        width: parent.width
        visible: appBridge ? !!appBridge.customMaskStatus : false
        text: qsTranslate("App", "Control Mask replaces Automatic Mask. RGB channels control intensity, tone, and structure.")
        wrapMode: Text.WordWrap
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.textMuted
    }

    // NR Control Mask Box
    Rectangle {
        objectName: "nrControlMaskBox"
        width: parent.width
        height: 64
        radius: Theme.radiusMedium
        color: Theme.bgInput
        border.color: Theme.borderSubtle
        border.width: 1

        Column {
            anchors.centerIn: parent
            spacing: 4

            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 8

                AppButton {
                    text: qsTranslate("App", "Load Control Mask...")
                    iconName: "load_mask"
                    buttonHeight: 24
                    onClicked: maskDialog.open()
                }

                AppButton {
                    text: qsTranslate("App", "Clear Mask")
                    enabled: appBridge ? !!appBridge.customMaskStatus : false
                    iconName: "clear_mask"
                    buttonHeight: 24
                    onClicked: {
                        if (appBridge)
                            appBridge.clearCustomMask();
                    }
                }
            }

            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                text: appBridge && appBridge.customMaskStatus ? appBridge.customMaskStatus : qsTranslate("App", "No mask loaded")
                font.family: Theme.monoFontFamily
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.textMuted
            }
        }
    }

    AppComboBox {
        objectName: "nrOpticalFlowQuality"
        width: parent.width
        visible: appBridge ? appBridge.nrMode === "Video" : false
        label: qsTranslate("App", "Optical Flow Quality")
        model: appBridge ? appBridge.opticalFlowQualityChoices : ["High", "Medium", "Low"]
        currentValue: appBridge ? appBridge.opticalFlowQuality : "High"
        onActivated: v => {
            if (appBridge)
                appBridge.opticalFlowQuality = v;
        }
    }

    FileDialog {
        id: maskDialog
        title: qsTranslate("App", "Select NR Control Mask")
        nameFilters: [qsTranslate("App", "Image Files (*.png *.jpg *.jpeg *.webp *.tiff *.bmp)"), qsTranslate("App", "All Files (*.*)")]
        onAccepted: {
            if (appBridge && selectedFile) {
                appBridge.selectCustomMask(selectedFile.toString());
            }
        }
    }
}
