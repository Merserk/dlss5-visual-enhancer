import QtQuick
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    width: parent ? parent.width : 320
    spacing: 12

    Column {
        visible: root.isImage
        width: parent.width
        spacing: 12
        AppComboBox {
            width: parent.width
            label: qsTranslate("App", "Output Format")
            model: appBridge ? appBridge.imageFormatChoices : []
            currentValue: appBridge ? appBridge.imageFormat : "PNG"
            onActivated: v => {
                if (appBridge)
                    appBridge.imageFormat = v;
            }
        }
        AppComboBox {
            objectName: "imageBitDepthSelector"
            width: parent.width
            label: qsTranslate("App", "Bit Depth")
            model: appBridge && ["PNG", "TIFF"].indexOf(appBridge.imageFormat) >= 0
                   ? [{label: "8 Bit", value: 8}, {label: "16 Bit", value: 16}]
                   : [{label: "8 Bit", value: 8}]
            currentValue: appBridge ? appBridge.imageBitDepth : 8
            onActivated: v => {
                if (appBridge)
                    appBridge.imageBitDepth = v;
            }
        }
        AppSlider {
            visible: appBridge && ["JPEG", "WebP", "AVIF"].indexOf(appBridge.imageFormat) >= 0
            width: parent.width
            label: qsTranslate("App", "Image Quality")
            from: 1
            to: 100
            stepSize: 1
            precision: 0
            defaultValue: 95
            value: appBridge ? appBridge.imageQuality : 95
            onValueModified: v => {
                if (appBridge)
                    appBridge.imageQuality = Math.round(v);
            }
        }
    }

    Column {
        visible: !root.isImage
        width: parent.width
        spacing: 12
        AppComboBox {
            width: parent.width
            label: qsTranslate("App", "Video Codec")
            model: appBridge ? appBridge.codecChoices : []
            currentValue: appBridge ? appBridge.videoCodec : "H.264 (NVIDIA NVENC)"
            onActivated: v => {
                if (appBridge)
                    appBridge.videoCodec = v;
            }
        }
        AppComboBox {
            objectName: "videoContainerSelector"
            width: parent.width
            label: qsTranslate("App", "Container")
            model: appBridge ? appBridge.containerChoices : []
            currentValue: appBridge ? appBridge.videoContainer : "MP4"
            enabled: !!appBridge
            onActivated: v => {
                if (appBridge)
                    appBridge.videoContainer = v;
            }
        }
        AppComboBox {
            width: parent.width
            label: qsTranslate("App", "Encoding Quality")
            visible: !appBridge || appBridge.fixedQualityCodecs.indexOf(appBridge.videoCodec) < 0
            model: appBridge ? appBridge.encodingQualityChoices : []
            currentValue: appBridge ? appBridge.videoQuality : "Auto (Default)"
            onActivated: v => {
                if (appBridge)
                    appBridge.videoQuality = v;
            }
        }
        Text {
            visible: appBridge && appBridge.fixedQualityCodecs.indexOf(appBridge.videoCodec) >= 0
            text: qsTranslate("App", "Encoding Quality: Fixed by codec")
            color: Theme.textSecondary
            font.pixelSize: Theme.fontSizeSmall
        }
        AppCheckBox {
            label: qsTranslate("App", "Preserve / Export 10-bit HDR")
            checked: appBridge ? appBridge.videoHdrMode : false
            enabled: appBridge ? appBridge.videoHdrSupported : false
            onToggled: v => {
                if (appBridge)
                    appBridge.videoHdrMode = v;
            }
        }
        Text {
            visible: appBridge && appBridge.renderValidationMessage !== ""
            width: parent.width
            wrapMode: Text.Wrap
            text: appBridge ? appBridge.renderValidationMessage : ""
            color: Theme.danger
            font.pixelSize: Theme.fontSizeSmall
        }
    }
}
