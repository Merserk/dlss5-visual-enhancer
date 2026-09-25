import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    width: parent ? parent.width : 320
    spacing: 12
    AppSegmentedControl {
        width: parent.width
        label: "NR Style"
        model: appBridge ? appBridge.nrStyleChoices : []
        currentValue: appBridge ? appBridge.nrStyle : "Default"
        onActivated: v => {
            if (appBridge)
                appBridge.nrStyle = v;
        }
    }

    AppSlider {
        width: parent.width
        label: "NR Intensity"
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
        label: "NR Passes"
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
        label: "Local Tone Strength"
        from: 0.0
        to: 2.0
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
        label: "Local Structure Strength"
        from: 0.0
        to: 2.0
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
        label: "Skin Structure Strength"
        from: -1.0
        to: 2.0
        stepSize: 0.05
        defaultValue: -1.0
        value: appBridge ? appBridge.skinStructureStrength : -1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.skinStructureStrength = v;
        }
    }

    AppSwitch {
        label: "Automatic Mask"
        checked: appBridge ? appBridge.automaticMask : false
        onToggled: c => {
            if (appBridge)
                appBridge.automaticMask = c;
        }
    }

    AppSlider {
        visible: !root.isImage
        width: parent.width
        label: "Shimmer Suppression"
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 0.70
        value: appBridge ? appBridge.shimmerSuppression : 0.70
        onValueModified: v => {
            if (appBridge)
                appBridge.shimmerSuppression = v;
        }
    }

    Text {
        width: parent.width
        text: "Composition & Masking"
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeSubtitle
        color: Theme.textSecondary
    }
    // Detail-Only Preset Button
    Row {
        width: parent.width
        spacing: 8

        AppButton {
            text: "Apply Detail-Only Preset"
            iconName: "quality_enhance"
            width: parent.width
            buttonHeight: 28
            onClicked: {
                if (appBridge)
                    appBridge.applyDetailOnly();
            }
        }
    }

    // Sliders
    AppSlider {
        width: parent.width
        label: "NR Color Strength"
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 1.0
        value: appBridge ? appBridge.nrColorStrength : 1.0
        onValueModified: v => {
            if (appBridge)
                appBridge.nrColorStrength = v;
        }
    }

    AppSlider {
        width: parent.width
        label: "Tone Preservation"
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 0.0
        value: appBridge ? appBridge.tonePreservation : 0.0
        onValueModified: v => {
            if (appBridge)
                appBridge.tonePreservation = v;
        }
    }

    AppSlider {
        width: parent.width
        label: "Face / Skin Protection"
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 0.0
        value: appBridge ? appBridge.faceSkinProtection : 0.0
        onValueModified: v => {
            if (appBridge)
                appBridge.faceSkinProtection = v;
        }
    }

    AppSlider {
        width: parent.width
        label: "Grain Preservation"
        from: 0.0
        to: 1.0
        stepSize: 0.05
        defaultValue: 0.0
        value: appBridge ? appBridge.grainPreservation : 0.0
        onValueModified: v => {
            if (appBridge)
                appBridge.grainPreservation = v;
        }
    }

    AppSlider {
        width: parent.width
        label: "Mask Feather"
        unit: "px"
        from: 0
        to: 128
        stepSize: 1
        precision: 0
        defaultValue: 0
        value: appBridge ? appBridge.maskFeather : 0
        onValueModified: v => {
            if (appBridge)
                appBridge.maskFeather = Math.round(v);
        }
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
                    text: "Load Custom Mask..."
                    iconName: "load_mask"
                    buttonHeight: 24
                    onClicked: maskDialog.open()
                }

                AppButton {
                    text: "Clear Mask"
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
                text: appBridge ? appBridge.customMaskStatus : "No mask loaded"
                font.family: Theme.monoFontFamily
                font.pixelSize: Theme.fontSizeSmall
                color: Theme.textMuted
            }
        }
    }

    FileDialog {
        id: maskDialog
        title: "Select Custom NR Mask"
        nameFilters: ["Image Files (*.png *.jpg *.jpeg *.webp *.tiff *.bmp)", "All Files (*.*)"]
        onAccepted: {
            if (appBridge && selectedFile) {
                appBridge.selectCustomMask(selectedFile.toString());
            }
        }
    }
}
