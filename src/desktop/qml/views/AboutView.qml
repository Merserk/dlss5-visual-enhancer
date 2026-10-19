import QtQuick
import ".."
import "../controls"

Rectangle {
    id: root
    property var appBridge: null
    color: Theme.bgBase

    Item {
        anchors.fill: parent; anchors.margins: 28
        Column {
            id: aboutCol
            width: Math.min(parent.width, 1120); anchors.centerIn: parent; spacing: 20
            Column {
                width: parent.width
                anchors.horizontalCenter: parent.horizontalCenter; spacing: 4
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: "Visual Enhancer"; font.family: Theme.fontFamily; font.pixelSize: 22; font.weight: Font.Bold; color: Theme.textPrimary
                }
                Text {
                    width: parent.width
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: qsTranslate("App", "Advanced neural image & video processing"); font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; color: Theme.textSecondary
                    horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
                }
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter; spacing: 12
                AppButton { text: qsTranslate("App", "Visit Website"); iconName: "external_link"; onClicked: Qt.openUrlExternally("https://visual-enhancer.merserk.com/") }
                AppButton { text: "GitHub"; iconName: "github"; onClicked: Qt.openUrlExternally("https://github.com/Merserk/dlss5-visual-enhancer") }
                AppButton { text: qsTranslate("App", "Support Project"); iconName: "heart"; onClicked: Qt.openUrlExternally("https://www.patreon.com/Merserk") }
            }
            Row {
                anchors.horizontalCenter: parent.horizontalCenter; spacing: 16
                Text {
                    text: qsTranslate("App", "Uses Qt under LGPLv3")
                    font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textSecondary
                }
                Text {
                    text: "<a href='licenses'>" + qsTranslate("App", "Third-Party Licenses") + "</a>"
                    textFormat: Text.RichText; linkColor: Theme.accent
                    font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
                    activeFocusOnTab: true
                    Accessible.role: Accessible.Link
                    Accessible.name: qsTranslate("App", "Third-Party Licenses")
                    function openLicenses() { if (root.appBridge) root.appBridge.openThirdPartyNotices() }
                    onLinkActivated: openLicenses()
                    Keys.onReturnPressed: openLicenses()
                    Keys.onEnterPressed: openLicenses()
                    HoverHandler { cursorShape: Qt.PointingHandCursor }
                }
            }
            Text { width: parent.width; anchors.horizontalCenter: parent.horizontalCenter; text: qsTranslate("App", "© 2026 Merserk. NVIDIA, DLSS, and RTX are trademarks of NVIDIA Corporation."); font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textMuted; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap }
        }
    }
}
