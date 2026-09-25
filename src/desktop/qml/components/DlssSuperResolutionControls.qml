import QtQuick
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
        label: "DLSS Mode"
        model: appBridge ? appBridge.dlssModeChoices : []
        currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageDlssMode : "Quality")
                                   : (appBridge ? appBridge.upscaleDlssMode : "Quality")
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageDlssMode = v;
                else
                    appBridge.upscaleDlssMode = v;
            }
        }
    }

    AppComboBox {
        width: parent.width
        label: "DLSS Preset"
        model: appBridge ? appBridge.dlssPresetChoices : []
        currentValue: root.isImage ? (appBridge ? appBridge.upscaleImageDlssPreset : "Default")
                                   : (appBridge ? appBridge.upscaleDlssPreset : "Default")
        onActivated: v => {
            if (appBridge) {
                if (root.isImage)
                    appBridge.upscaleImageDlssPreset = v;
                else
                    appBridge.upscaleDlssPreset = v;
            }
        }
    }
}
