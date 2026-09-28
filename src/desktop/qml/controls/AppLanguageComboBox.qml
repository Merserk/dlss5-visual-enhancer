import QtQuick
import QtQuick.Window
import QtQuick.Controls as QQC2
import QtQuick.Controls.Basic as Basic
import ".."

// Qt's ComboBox provides keyboard navigation, type-ahead and accessibility.
// Autonyms retain their own font and text direction, all aligned to the left.
Item {
    id: control
    property string label: ""
    property var model: []
    property string currentValue: "en_US"
    signal activated(string value)
    implicitWidth: 360
    implicitHeight: languageLabel.implicitHeight + 6 + combo.implicitHeight

    Text {
        id: languageLabel
        width: parent.width
        text: control.label
        color: Theme.textSecondary
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeLabel
        wrapMode: Text.Wrap
    }
    Basic.ComboBox {
        id: combo
        objectName: "languageComboBox"
        width: parent.width
        anchors.top: languageLabel.bottom
        anchors.topMargin: 6
        implicitHeight: Theme.controlHeight
        model: control.model
        textRole: "label"
        valueRole: "value"
        currentIndex: {
            for (var i = 0; i < control.model.length; ++i)
                if (control.model[i].value === control.currentValue) return i
            return -1
        }
        onActivated: control.activated(currentValue)
        Accessible.name: control.label
        leftPadding: mirrored ? 28 : 10
        rightPadding: mirrored ? 10 : 28
        contentItem: Text {
            objectName: "languageDisplayText"
            text: combo.displayText
            font.family: combo.currentIndex >= 0 ? control.model[combo.currentIndex].fontFamily : Theme.fontFamily
            font.pixelSize: Theme.fontSizeLabel
            color: Theme.textPrimary
            verticalAlignment: Text.AlignVCenter
            horizontalAlignment: Text.AlignLeft
            LayoutMirroring.enabled: false
            elide: Text.ElideRight
        }
        indicator: AppIcon {
            x: combo.mirrored ? 10 : combo.width - width - 10
            y: (combo.height - height) / 2
            iconName: combo.popup.visible ? "chevron_up" : "chevron_down"
            iconSize: 12
            color: Theme.textMuted
        }
        background: Rectangle {
            color: combo.hovered || combo.popup.visible ? Theme.bgInputHover : Theme.bgInput
            radius: Theme.radiusMedium
            border.width: 1
            border.color: combo.activeFocus || combo.popup.visible ? Theme.accent : Theme.borderDefault
        }
        delegate: Basic.ItemDelegate {
            objectName: "languageOption"
            required property var modelData
            required property int index
            width: combo.width - 8
            implicitHeight: Theme.controlHeight
            leftPadding: combo.mirrored ? 22 : 12
            rightPadding: combo.mirrored ? 12 : 22
            highlighted: combo.highlightedIndex === index
            contentItem: Text {
                objectName: "languageOptionText"
                text: modelData.label
                font.family: modelData.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                color: combo.currentIndex === index ? Theme.accent : Theme.textPrimary
                verticalAlignment: Text.AlignVCenter
                horizontalAlignment: Text.AlignLeft
                LayoutMirroring.enabled: false
                elide: Text.ElideRight
            }
            background: Rectangle {
                radius: Theme.radiusSmall
                color: parent.highlighted ? Theme.bgHover : (combo.currentIndex === parent.index ? Theme.bgSelected : "transparent")
            }
        }
        popup: QQC2.Popup {
            objectName: "languagePopup"
            y: combo.height + 4
            width: combo.width
            implicitHeight: Math.min(contentItem.implicitHeight + 8,
                                     combo.Window.window ? Math.max(120, combo.Window.window.height - combo.mapToItem(null, 0, combo.height).y - 12) : contentItem.implicitHeight + 8)
            margins: 8
            padding: 4
            contentItem: ListView {
                id: languageList
                clip: true
                implicitHeight: contentHeight
                model: combo.popup.visible ? combo.delegateModel : null
                currentIndex: combo.highlightedIndex
                boundsBehavior: Flickable.StopAtBounds
                QQC2.ScrollBar.vertical: Basic.ScrollBar {
                    width: 10
                    policy: QQC2.ScrollBar.AsNeeded
                    visible: languageList.contentHeight > languageList.height + 0.5
                    background: Rectangle { color: Theme.bgInput; radius: 4 }
                    contentItem: Rectangle {
                        implicitWidth: 6; radius: 3
                        color: parent.pressed ? Theme.accent : Theme.textMuted
                    }
                }
            }
            background: Rectangle {
                color: Theme.bgSurface
                radius: Theme.radiusMedium
                border.color: Theme.borderActive
                border.width: 1
            }
            onOpened: contentItem.positionViewAtIndex(combo.currentIndex, ListView.Contain)
        }
    }
}
