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
    AppComboBox {
        width: parent.width
        label: "Scaling Method"
        model: root.isImage ? (appBridge ? appBridge.imageScalingFilterChoices : ["Lanczos4", "Area", "Bicubic", "Bilinear", "Nearest"])
                            : (appBridge ? appBridge.videoScalingFilterChoices : ["Spline36", "Lanczos", "Bicubic", "Area", "Bilinear", "EWA Lanczos"])
        currentValue: root.isImage ? (appBridge ? appBridge.imageScalingFilter : "Lanczos4")
                                   : (appBridge ? appBridge.videoScalingFilter : "Spline36")
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.imageScalingFilter = v;
                else
                    appBridge.videoScalingFilter = v;
            }
        }
    }

    AppComboBox {
        width: parent.width
        label: "Scale"
        model: appBridge ? appBridge.nrScaleChoices : []
        currentValue: appBridge ? appBridge.upscalingFactor : 1.0
        onActivated: v => {
            if (appBridge)
                appBridge.upscalingFactor = v;
        }
    }

}
