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
        label: "Target Output Frame Rate"
        model: appBridge ? appBridge.fiFpsChoices : []
        currentValue: appBridge ? appBridge.fiTargetFps : "60"
        onActivated: v => {
            if (appBridge)
                appBridge.fiTargetFps = v;
        }
    }

    AppSegmentedControl {
        width: parent.width
        label: "DLSS-G Engine"
        model: appBridge ? appBridge.fiEngineChoices : []
        currentValue: appBridge ? appBridge.fiEngine : "Auto"
        onActivated: v => {
            if (appBridge)
                appBridge.fiEngine = v;
        }
    }

    Text {
        width: parent.width
        text: appBridge ? appBridge.outputEstimate : ""
        wrapMode: Text.Wrap
        font.family: Theme.monoFontFamily
        font.pixelSize: Theme.fontSizeSmall
        color: Theme.textMuted
    }
}
