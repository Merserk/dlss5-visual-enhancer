import QtQuick
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    property bool fsr: false
    readonly property string mode: appBridge ? (fsr ? appBridge.fsrFrameRateMode : appBridge.fiFrameRateMode) : "Target FPS"
    width: parent ? parent.width : 320
    spacing: 12

    AppSegmentedControl {
        objectName: root.fsr ? "fsrFrameRateMode" : "dlssFrameRateMode"
        width: parent.width
        label: qsTranslate("App", "Frame-rate mode")
        model: [{label: qsTranslate("App", "Multiplier"), value: "Multiplier"},
                {label: qsTranslate("App", "Target FPS"), value: "Target FPS"}]
        currentValue: root.mode
        onActivated: value => {
            if (!root.appBridge) return
            if (root.fsr) root.appBridge.fsrFrameRateMode = value
            else root.appBridge.fiFrameRateMode = value
        }
    }
    AppComboBox {
        objectName: root.fsr ? "fsrMultiplier" : "dlssMultiplier"
        width: parent.width
        visible: root.mode === "Multiplier"
        label: qsTranslate("App", "Multiplier")
        model: [{label:"2x",value:2},{label:"3x",value:3},{label:"4x",value:4},
                {label:"5x",value:5},{label:"6x",value:6},{label:"7x",value:7},{label:"8x",value:8}]
        currentValue: root.appBridge ? (root.fsr ? root.appBridge.fsrMultiplier : root.appBridge.fiMultiplier) : 2
        onActivated: value => {
            if (!root.appBridge) return
            if (root.fsr) root.appBridge.fsrMultiplier = value
            else root.appBridge.fiMultiplier = value
        }
    }
    AppComboBox {
        objectName: root.fsr ? "fsrTargetFrameRate" : "dlssTargetFrameRate"
        width: parent.width
        visible: root.mode === "Target FPS"
        label: qsTranslate("App", "Target Output Frame Rate")
        model: root.appBridge ? root.appBridge.fiFpsChoices : []
        currentValue: root.appBridge ? (root.fsr ? root.appBridge.fsrTargetFps : root.appBridge.fiTargetFps) : "60"
        onActivated: value => {
            if (!root.appBridge) return
            if (root.fsr) root.appBridge.fsrTargetFps = value
            else root.appBridge.fiTargetFps = value
        }
    }
}
