import QtQuick
import "../controls"

Column {
    id: root
    objectName: "rtx-video-hdr-controls"
    property var appBridge: null
    width: parent ? parent.width : 320
    spacing: 12

    AppSlider {
        width: parent.width
        label: qsTranslate("App", "HDR Contrast")
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
        label: qsTranslate("App", "HDR Saturation")
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
        label: qsTranslate("App", "Middle Gray")
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
        label: qsTranslate("App", "Peak Luminance")
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
        label: qsTranslate("App", "HDR Precision")
        model: appBridge ? appBridge.hdrPrecisionChoices : []
        currentValue: appBridge ? appBridge.upscaleHdrPrecision : "Packed 10-bit"
        onActivated: v => {
            if (appBridge)
                appBridge.upscaleHdrPrecision = v;
        }
    }
}
