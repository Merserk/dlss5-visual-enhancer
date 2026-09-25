import QtQuick
import "../controls"

Column {
    id: root
    property var appBridge: null
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    width: parent ? parent.width : 320
    spacing: 12

    AppSlider {
        objectName: "denoising-strength"
        width: parent.width
        label: "Noise Reduction"
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 50; unit: "%"
        value: root.appBridge ? root.appBridge.denoiseStrength : 50
        onValueModified: v => { if (root.appBridge) root.appBridge.denoiseStrength = Math.round(v) }
    }

    AppCheckBox {
        objectName: "denoising-deblock"
        label: "Compression Artifact Cleanup"
        checked: root.appBridge ? root.appBridge.denoiseDeblock : true
        onToggled: v => { if (root.appBridge) root.appBridge.denoiseDeblock = v }
    }

    AppSlider {
        objectName: "denoising-temporal"
        visible: !root.isImage
        width: parent.width
        label: "Temporal Noise Reduction"
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 30; unit: "%"
        value: root.appBridge ? root.appBridge.denoiseTemporal : 30
        onValueModified: v => { if (root.appBridge) root.appBridge.denoiseTemporal = Math.round(v) }
    }

}
