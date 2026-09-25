import QtQuick
import ".."
import "../controls"

Item {
    id: card
    property var appBridge: null
    property string stageId: ""
    property string title: ""
    property bool stageEnabled: false
    property int stageIndex: 0
    property int stageCount: 0
    property var stageRepeater: null
    property bool collapsed: true
    property real dragOffset: 0
    property real slotOffset: 0
    property int dragTarget: stageIndex
    readonly property bool dragging: dragger.active
    default property alias content: contentContainer.data

    signal dragBegan(string stageId, int originIndex, real cardHeight)
    signal dragMoved(int targetIndex)
    signal dragFinished(string stageId, int targetIndex)

    implicitWidth: 320
    implicitHeight: header.height + (collapsed ? 0 : contentContainer.childrenRect.height + 24)
    z: dragging ? 100 : 0
    scale: dragging ? 1.018 : 1.0
    transform: [
        Translate { y: card.dragOffset },
        Translate {
            y: card.slotOffset
            Behavior on y { NumberAnimation { duration: 150; easing.type: Easing.OutCubic } }
        }
    ]
    Behavior on scale { NumberAnimation { duration: 120; easing.type: Easing.OutCubic } }

    Rectangle {
        anchors.fill: parent
        radius: Theme.radiusLarge
        color: card.dragging ? Theme.bgPressed : Theme.bgCard
        border.color: card.dragging ? Theme.accent : Theme.borderSubtle
        border.width: 1
        Behavior on color { ColorAnimation { duration: 120 } }
    }

    Row {
        id: header
        x: 10
        y: 0
        width: parent.width - 20
        height: 42
        spacing: 3

        AppSwitch {
            objectName: "workflow-stage-switch-" + card.stageId
            width: 40
            height: 28
            anchors.verticalCenter: parent.verticalCenter
            checked: card.stageEnabled
            onToggled: value => {
                if (card.appBridge)
                    card.appBridge.setWorkflowStageEnabled(card.stageId, value);
            }
            Accessible.name: card.title + (card.stageEnabled ? " enabled" : " disabled")
        }

        Text {
            id: titleText
            width: Math.max(70, parent.width - 40 - 22 * 3 - 3 * 4)
            height: parent.height
            verticalAlignment: Text.AlignVCenter
            text: card.title
            elide: Text.ElideRight
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeSubtitle
            font.weight: Font.DemiBold
            color: card.stageEnabled ? Theme.textPrimary : Theme.textSecondary
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.OpenHandCursor
                onClicked: card.collapsed = !card.collapsed
            }
        }

        AppIconButton {
            iconName: "chevron_up"
            buttonSize: 22
            anchors.verticalCenter: parent.verticalCenter
            tooltipText: "Move " + card.title + " up"
            enabled: card.stageIndex > 0
            onClicked: {
                if (card.appBridge)
                    card.appBridge.moveWorkflowStage(card.stageId, card.stageIndex - 1);
            }
        }
        AppIconButton {
            iconName: "chevron_down"
            buttonSize: 22
            anchors.verticalCenter: parent.verticalCenter
            tooltipText: "Move " + card.title + " down"
            enabled: card.stageIndex < card.stageCount - 1
            onClicked: {
                if (card.appBridge)
                    card.appBridge.moveWorkflowStage(card.stageId, card.stageIndex + 1);
            }
        }
        AppIconButton {
            iconName: card.collapsed ? "chevron_right" : "chevron_left"
            buttonSize: 22
            anchors.verticalCenter: parent.verticalCenter
            tooltipText: card.collapsed ? "Expand " + card.title : "Collapse " + card.title
            onClicked: card.collapsed = !card.collapsed
        }

        DragHandler {
            id: dragger
            target: null
            acceptedButtons: Qt.LeftButton
            yAxis.enabled: true
            xAxis.enabled: false
            onTranslationChanged: {
                if (!active) return;
                card.dragOffset = translation.y;
                var target = card.stageIndex;
                var other;
                if (card.dragOffset < 0) {
                    var top = card.y + card.dragOffset;
                    for (var i = card.stageIndex - 1; i >= 0; --i) {
                        other = card.stageRepeater ? card.stageRepeater.itemAt(i) : null;
                        if (other && top < other.y + other.height / 2)
                            target = i;
                    }
                } else if (card.dragOffset > 0) {
                    var bottom = card.y + card.height + card.dragOffset;
                    for (var j = card.stageIndex + 1; j < card.stageCount; ++j) {
                        other = card.stageRepeater ? card.stageRepeater.itemAt(j) : null;
                        if (other && bottom > other.y + other.height / 2)
                            target = j;
                    }
                }
                if (target !== card.dragTarget) {
                    card.dragTarget = target;
                    card.dragMoved(target);
                }
            }
            onActiveChanged: {
                if (active) {
                    card.dragTarget = card.stageIndex;
                    card.dragBegan(card.stageId, card.stageIndex, card.height);
                } else {
                    card.dragFinished(card.stageId, card.dragTarget);
                    card.dragOffset = 0;
                    card.dragTarget = card.stageIndex;
                }
            }
        }
    }

    Rectangle {
        visible: !card.collapsed
        x: 12
        y: header.height
        width: parent.width - 24
        height: 1
        color: Theme.borderSubtle
    }
    Item {
        id: contentContainer
        visible: !card.collapsed
        x: 12
        y: header.height + 12
        width: parent.width - 24
        height: childrenRect.height
        opacity: card.stageEnabled ? 1.0 : 0.6
    }
}
