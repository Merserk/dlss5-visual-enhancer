import QtQuick
import "../controls"

Column {
    id: root
    property var appBridge: null
    width: parent ? parent.width : 320
    spacing: 12

    AppComboBox {
        objectName: "sharpening-method"
        width: parent.width
        label: "Sharpening Method"
        model: root.appBridge ? root.appBridge.sharpeningMethodChoices : ["NVIDIA NIS", "AMD CAS"]
        currentValue: root.appBridge ? root.appBridge.sharpeningMethod : "AMD CAS"
        onActivated: value => { if (root.appBridge) root.appBridge.sharpeningMethod = value }
    }

    AppSlider {
        objectName: "sharpening-strength"
        width: parent.width
        label: "Sharpness"
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 50; unit: "%"
        value: root.appBridge ? root.appBridge.casSharpness : 50
        onValueModified: v => { if (root.appBridge) root.appBridge.casSharpness = Math.round(v) }
    }
}
