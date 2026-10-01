import QtQuick
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    objectName: "rtx-super-resolution-controls"
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    width: parent ? parent.width : 320
    spacing: 12

    AppComboBox {
        width: parent.width
        label: qsTranslate("App", "VSR Quality")
        model: appBridge ? appBridge.vsrQualityChoices : []
        currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageVsrQuality : 4) : (appBridge ? appBridge.upscaleVsrQuality : 4)
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageVsrQuality = v;
                else
                    appBridge.upscaleVsrQuality = v;
            }
        }
    }

    AppSegmentedControl {
        width: parent.width
        label: qsTranslate("App", "Sizing Mode")
        model: appBridge ? (root.isImage ? appBridge.imageSizeModeChoices : appBridge.videoSizeModeChoices) : []
        currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageSizeMode : "Scale factor") : (appBridge ? appBridge.upscaleSizeMode : "Scale factor")
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageSizeMode = v;
                else
                    appBridge.upscaleSizeMode = v;
            }
        }
    }

    AppComboBox {
        width: parent.width
        label: qsTranslate("App", "Scale Factor")
        visible: appBridge && (root.isImage ? appBridge.upscaleImageSizeMode : appBridge.upscaleSizeMode) === "Scale factor"
        model: appBridge ? (root.isImage ? appBridge.imageScaleFactorChoices : appBridge.videoScaleFactorChoices) : []
        currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageScaleFactor : 2.0) : (appBridge ? appBridge.upscaleScaleFactor : 2.0)
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageScaleFactor = v;
                else
                    appBridge.upscaleScaleFactor = v;
            }
        }
    }

    Row {
        width: parent.width
        spacing: 8
        visible: appBridge && (root.isImage ? appBridge.upscaleImageSizeMode : appBridge.upscaleSizeMode) === "Custom dimensions"

        AppTextField {
            width: (parent.width - 8) / 2
            label: qsTranslate("App", "Width (px)")
            text: (root.isImage ? (appBridge ? appBridge.upscaleImageWidth : 3840)
                                : (appBridge ? appBridge.upscaleWidth : 3840)).toString()
            commitOnEveryEdit: false
            onTextEdited: t => {
                var num = parseInt(t);
                if (!isNaN(num) && num > 0) {
                    if (root.isImage)
                        appBridge.upscaleImageWidth = num;
                    else
                        appBridge.upscaleWidth = num;
                }
            }
        }

        AppTextField {
            width: (parent.width - 8) / 2
            label: qsTranslate("App", "Height (px)")
            text: (root.isImage ? (appBridge ? appBridge.upscaleImageHeight : 2160)
                                : (appBridge ? appBridge.upscaleHeight : 2160)).toString()
            commitOnEveryEdit: false
            onTextEdited: t => {
                var num = parseInt(t);
                if (!isNaN(num) && num > 0) {
                    if (root.isImage)
                        appBridge.upscaleImageHeight = num;
                    else
                        appBridge.upscaleHeight = num;
                }
            }
        }
    }

    AppCheckBox {
        label: qsTranslate("App", "Lock Aspect Ratio")
        checked: root.isImage ? (appBridge ? appBridge.upscaleImageAspectLock : true) : (appBridge ? appBridge.upscaleAspectLock : true)
        onToggled: c => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageAspectLock = c;
                else
                    appBridge.upscaleAspectLock = c;
            }
        }
    }

    Text {
        width: parent.width
        visible: appBridge && appBridge.rtxSuperResolutionEstimate !== ""
        text: appBridge ? appBridge.rtxSuperResolutionEstimate : ""
        wrapMode: Text.Wrap
        font.family: Theme.monoFontFamily
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.textMuted
    }

}
