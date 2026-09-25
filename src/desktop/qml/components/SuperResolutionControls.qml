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
    AppSwitch {
        visible: !root.isImage
        label: "Enable RTX VSR"
        checked: appBridge ? appBridge.upscaleVsrEnabled : true
        enabled: appBridge ? (!checked || appBridge.upscaleHdrEnabled) : true
        onToggled: c => {
            if (appBridge)
                appBridge.upscaleVsrEnabled = c;
        }
    }

    AppComboBox {
        width: parent.width
        label: "VSR Quality"
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
        label: "Sizing Mode"
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
        label: "Scale Factor"
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
            label: "Width (px)"
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
            label: "Height (px)"
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
        label: "Lock Aspect Ratio"
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
        visible: appBridge && appBridge.outputEstimate !== ""
        text: appBridge ? appBridge.outputEstimate : ""
        wrapMode: Text.Wrap
        font.family: Theme.monoFontFamily
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.textMuted
    }

    Column {
        visible: !root.isImage
        width: parent.width
        spacing: 12
        Text {
            text: "RTX Video HDR"
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSubtitle
            color: Theme.textSecondary
        }
        AppSwitch {
            label: "Convert SDR to HDR"
            checked: appBridge ? appBridge.upscaleHdrEnabled : false
            enabled: appBridge ? (appBridge.upscaleHdrSupported && (!checked || appBridge.upscaleVsrEnabled)) : false
            onToggled: c => {
                if (appBridge)
                    appBridge.upscaleHdrEnabled = c;
            }
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
            onValueModified: v => {
                if (appBridge)
                    appBridge.upscaleHdrContrast = Math.round(v);
            }
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
            onValueModified: v => {
                if (appBridge)
                    appBridge.upscaleHdrSaturation = Math.round(v);
            }
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
            onValueModified: v => {
                if (appBridge)
                    appBridge.upscaleHdrMiddleGray = Math.round(v);
            }
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
            onValueModified: v => {
                if (appBridge)
                    appBridge.upscaleHdrPeakLuminance = Math.round(v);
            }
        }

        AppSegmentedControl {
            width: parent.width
            label: "HDR Precision"
            model: appBridge ? appBridge.hdrPrecisionChoices : []
            currentValue: appBridge ? appBridge.upscaleHdrPrecision : "Packed 10-bit"
            onActivated: v => {
                if (appBridge)
                    appBridge.upscaleHdrPrecision = v;
            }
        }
    }
}
