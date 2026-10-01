import QtQuick
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    width: parent ? parent.width : 320
    spacing: 12

    AppSlider {
        objectName: "grain-amount"
        width: parent.width
        label: qsTranslate("App", "Grain Amount")
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 20; unit: "%"
        value: root.appBridge ? root.appBridge.grainAmount : 20
        onValueModified: v => { if (root.appBridge) root.appBridge.grainAmount = Math.round(v) }
    }

    AppSlider {
        objectName: "grain-size"
        width: parent.width
        label: qsTranslate("App", "Grain Size")
        from: 0.5; to: 4.0; stepSize: 0.05; precision: 2; defaultValue: 1.0; unit: "px"
        value: root.appBridge ? root.appBridge.grainSize : 1.0
        onValueModified: v => { if (root.appBridge) root.appBridge.grainSize = v }
    }

    AppSlider {
        objectName: "grain-color"
        width: parent.width
        label: qsTranslate("App", "Color Grain")
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 0; unit: "%"
        value: root.appBridge ? root.appBridge.grainColor : 0
        onValueModified: v => { if (root.appBridge) root.appBridge.grainColor = Math.round(v) }
    }

    AppSlider {
        objectName: "grain-response"
        width: parent.width
        label: qsTranslate("App", "Tonal Response")
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 70; unit: "%"
        value: root.appBridge ? root.appBridge.grainResponse : 70
        onValueModified: v => { if (root.appBridge) root.appBridge.grainResponse = Math.round(v) }
    }

    AppSlider {
        objectName: "grain-seed"
        width: parent.width
        label: qsTranslate("App", "Grain Seed")
        from: 0; to: 65535; stepSize: 1; precision: 0; defaultValue: 0
        value: root.appBridge ? root.appBridge.grainSeed : 0
        onValueModified: v => { if (root.appBridge) root.appBridge.grainSeed = Math.round(v) }
    }

    AppSwitch {
        objectName: "grain-animated"
        width: parent.width
        visible: root.appBridge ? root.appBridge.nrMode === "Video" : false
        label: qsTranslate("App", "Animated Grain")
        checked: root.appBridge ? root.appBridge.grainAnimated : true
        onToggled: v => { if (root.appBridge) root.appBridge.grainAnimated = v }
    }

}
