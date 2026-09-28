import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

AppCard {
    id: root

    property var appBridge: null
    title: qsTranslate("App", "Composition & Masking")

    Column {
        width: parent.width
        spacing: 12

        // Detail-Only Preset Button
        Row {
            width: parent.width
            spacing: 8

            AppButton {
                text: qsTranslate("App", "Apply Detail-Only Preset")
                iconName: "quality_enhance"
                width: parent.width
                buttonHeight: 28
                onClicked: {
                    if (appBridge) appBridge.applyDetailOnly()
                }
            }
        }

        // Sliders
        AppSlider {
            width: parent.width
            label: qsTranslate("App", "NR Color Strength")
            from: 0.0
            to: 1.0
            stepSize: 0.05
            defaultValue: 1.0
            value: appBridge ? appBridge.nrColorStrength : 1.0
            onValueModified: (v) => { if (appBridge) appBridge.nrColorStrength = v }
        }

        AppSlider {
            width: parent.width
            label: qsTranslate("App", "Tone Preservation")
            from: 0.0
            to: 1.0
            stepSize: 0.05
            defaultValue: 0.0
            value: appBridge ? appBridge.tonePreservation : 0.0
            onValueModified: (v) => { if (appBridge) appBridge.tonePreservation = v }
        }

        AppSlider {
            width: parent.width
            label: qsTranslate("App", "Face / Skin Protection")
            from: 0.0
            to: 1.0
            stepSize: 0.05
            defaultValue: 0.0
            value: appBridge ? appBridge.faceSkinProtection : 0.0
            onValueModified: (v) => { if (appBridge) appBridge.faceSkinProtection = v }
        }

        AppSlider {
            width: parent.width
            label: qsTranslate("App", "Grain Preservation")
            from: 0.0
            to: 1.0
            stepSize: 0.05
            defaultValue: 0.0
            value: appBridge ? appBridge.grainPreservation : 0.0
            onValueModified: (v) => { if (appBridge) appBridge.grainPreservation = v }
        }

        AppSlider {
            width: parent.width
            label: qsTranslate("App", "Mask Feather")
            unit: "px"
            from: 0
            to: 128
            stepSize: 1
            precision: 0
            defaultValue: 0
            value: appBridge ? appBridge.maskFeather : 0
            onValueModified: (v) => { if (appBridge) appBridge.maskFeather = Math.round(v) }
        }

        // Custom NR Mask Box
        Rectangle {
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
                        text: qsTranslate("App", "Load Custom Mask...")
                        iconName: "load_mask"
                        buttonHeight: 24
                        onClicked: maskDialog.open()
                    }

                    AppButton {
                        text: qsTranslate("App", "Clear Mask")
                        iconName: "clear_mask"
                        buttonHeight: 24
                        onClicked: {
                            if (appBridge) appBridge.clearCustomMask()
                        }
                    }
                }

                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: appBridge ? appBridge.customMaskStatus : qsTranslate("App", "No mask loaded")
                    font.family: Theme.monoFontFamily
                    font.pixelSize: Theme.fontSizeSmall
                    color: Theme.textMuted
                }
            }
        }
    }

    FileDialog {
        id: maskDialog
        title: qsTranslate("App", "Select Custom NR Mask")
        nameFilters: [qsTranslate("App", "Image Files (*.png *.jpg *.jpeg *.webp *.tiff *.bmp)"), qsTranslate("App", "All Files (*.*)")]
        onAccepted: {
            if (appBridge && selectedFile) {
                appBridge.selectCustomMask(selectedFile.toString())
            }
        }
    }
}
