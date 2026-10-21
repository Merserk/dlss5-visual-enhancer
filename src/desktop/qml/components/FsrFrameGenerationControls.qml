import QtQuick
import ".."
import "../controls"

Column {
    id: root
    property var appBridge: null
    width: parent ? parent.width : 320
    spacing: 12

    FrameRateControls {
        width: parent.width
        appBridge: root.appBridge
        fsr: true
    }
}
