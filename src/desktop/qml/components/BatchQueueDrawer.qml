pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls.Basic as QQC2
import QtQuick.Dialogs
import ".."
import "../controls"

Rectangle {
    id: drawer
    objectName: "batchQueueDrawer"
    property var appBridge: null
    property var queueModel: null
    property bool imageQueue: false
    property bool collapsed: true
    property int previousFileCount: 0
    property bool hidden: false
    property int expandedHeight: appBridge ? appBridge.queueHeight : 330

    readonly property bool canModify: appBridge ? appBridge.canModifyQueue : true
    readonly property int fileCount: queueModel ? queueModel.count : 0
    readonly property int maximumHeight: Math.max(180, Math.min(720, parent ? parent.height - 180 : 720))
    readonly property bool showFps: !imageQueue && width >= 1040
    readonly property bool showRemaining: width >= 790
    readonly property bool showCompleted: width >= 900
    readonly property int inset: 12
    readonly property int columnGap: 12
    readonly property int numberWidth: 28
    readonly property int fpsWidth: 52
    readonly property int remainingWidth: 80
    readonly property int completedWidth: 84
    readonly property int statusWidth: 96
    readonly property int actionsWidth: 28
    readonly property real tableWidth: Math.max(0, width - inset * 2 - 10)
    readonly property int columnCount: 5 + (showFps ? 1 : 0) + (showRemaining ? 1 : 0) + (showCompleted ? 1 : 0)
    readonly property real flexibleWidth: Math.max(0, tableWidth - numberWidth - statusWidth - actionsWidth
                                                 - (showFps ? fpsWidth : 0) - (showRemaining ? remainingWidth : 0)
                                                 - (showCompleted ? completedWidth : 0) - columnGap * (columnCount - 1))
    readonly property real fileWidth: Math.floor(flexibleWidth * 0.57)
    readonly property real progressWidth: flexibleWidth - fileWidth
    readonly property color processingColor: Qt.lighter(Theme.accent, 1.5)

    // Show newly added files, then leave manual collapse intact until another
    // addition. Empty queues reclaim the viewport space automatically.
    onFileCountChanged: {
        if (fileCount === 0 || fileCount > previousFileCount)
            collapsed = fileCount === 0
        previousFileCount = fileCount
    }
    onQueueModelChanged: {
        collapsed = !queueModel || queueModel.count === 0
        previousFileCount = queueModel ? queueModel.count : 0
    }
    Component.onCompleted: {
        collapsed = fileCount === 0
        previousFileCount = fileCount
    }

    height: hidden ? 0 : (collapsed ? header.height : Math.min(maximumHeight, Math.max(180, expandedHeight)))
    visible: !hidden
    color: Theme.bgInput
    border.color: Theme.borderSubtle
    border.width: 1
    clip: true
    Behavior on height {
        enabled: !resizeHandle.pressed
        NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutQuad }
    }

    function clockTime(seconds, full) {
        if (!isFinite(seconds) || seconds < 0) return "—"
        var value = Math.max(0, Math.ceil(seconds))
        var hours = Math.floor(value / 3600)
        var minutes = Math.floor(value / 60) % 60
        var tail = String(value % 60).padStart(2, "0")
        if (full || hours > 0) return String(hours).padStart(2, "0") + ":" + String(minutes).padStart(2, "0") + ":" + tail
        return String(minutes).padStart(2, "0") + ":" + tail
    }
    function durationText(seconds) {
        var value = Math.max(0, Math.ceil(seconds))
        if (value < 60) return qsTranslate("App", "%1 s").arg(value)
        var minutes = Math.floor(value / 60) % 60
        var hours = Math.floor(value / 3600)
        if (hours > 0) return qsTranslate("App", "%1 h %2 min").arg(hours).arg(minutes)
        return qsTranslate("App", "%1 min %2 s").arg(minutes).arg(value % 60)
    }
    function fileSize(bytes) {
        if (bytes <= 0) return ""
        var units = ["B", "KB", "MB", "GB", "TB"]
        var unit = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)))
        return (bytes / Math.pow(1024, unit)).toFixed(unit > 1 ? 1 : 0) + " " + units[unit]
    }

    component ColumnHeading: Text {
        height: columnHeader.height
        verticalAlignment: Text.AlignVCenter
        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; font.weight: Font.Medium
        color: Theme.textSecondary
        elide: Text.ElideRight
    }
    component MetricText: Text {
        anchors.horizontalCenter: parent.horizontalCenter
        font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeBody
        color: Theme.textPrimary
    }
    component MetricCaption: Text {
        anchors.horizontalCenter: parent.horizontalCenter
        font.family: Theme.fontFamily; font.pixelSize: 10
        color: Theme.textMuted
    }
    component QueueToolTip: QQC2.ToolTip {
        id: tip
        width: Math.min(480, implicitWidth)
        delay: 650
        padding: 8
        contentItem: Text {
            text: tip.text
            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
            color: Theme.textPrimary; wrapMode: Text.Wrap
        }
        background: Rectangle { color: Theme.bgBase; radius: Theme.radiusSmall; border.color: Theme.borderActive }
    }
    component RowAction: QQC2.MenuItem {
        id: action
        property string menuIcon: ""
        implicitHeight: 36
        leftPadding: 12; rightPadding: 12
        contentItem: Row {
            spacing: 10
            opacity: action.enabled ? 1 : 0.4
            AppIcon { iconName: action.menuIcon; iconSize: 16; anchors.verticalCenter: parent.verticalCenter }
            Text {
                text: action.text; color: Theme.textPrimary
                font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeBody
                anchors.verticalCenter: parent.verticalCenter
            }
        }
        background: Rectangle { radius: Theme.radiusSmall; color: action.highlighted ? Theme.bgHover : "transparent" }
    }

    Rectangle {
        id: header
        anchors.top: parent.top; anchors.left: parent.left; anchors.right: parent.right
        height: 44; color: Theme.bgSurface
        Row {
            anchors.left: parent.left; anchors.leftMargin: 16; anchors.verticalCenter: parent.verticalCenter
            spacing: 10
            AppIcon { iconName: "queue"; iconSize: 18; color: Theme.textPrimary; anchors.verticalCenter: parent.verticalCenter }
            Text {
                text: qsTranslate("App", "Batch Queue"); font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSubtitle
                font.weight: Font.DemiBold; color: Theme.textPrimary; anchors.verticalCenter: parent.verticalCenter
            }
            AppBadge {
                text: drawer.fileCount + (drawer.fileCount === 1 ? qsTranslate("App", " file") : qsTranslate("App", " files"))
                variant: drawer.fileCount > 0 ? "accent" : "neutral"
                anchors.verticalCenter: parent.verticalCenter
            }
        }
        Row {
            anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
            spacing: 4
            AppIconButton { objectName: "queueAddFiles"; iconName: "add_file"; buttonSize: 28; tooltipText: qsTranslate("App", "Add files"); enabled: drawer.canModify; onClicked: fileDialog.open() }
            AppIconButton { iconName: "add_folder"; buttonSize: 28; tooltipText: qsTranslate("App", "Add folder"); enabled: drawer.canModify; onClicked: folderDialog.open() }
            Rectangle { width: 1; height: 16; color: Theme.borderDefault; anchors.verticalCenter: parent.verticalCenter }
            AppIconButton {
                iconName: "retry"; buttonSize: 28; tooltipText: qsTranslate("App", "Reset failed and cancelled items for retry")
                enabled: drawer.fileCount > 0 && drawer.canModify
                onClicked: { if (drawer.appBridge) drawer.appBridge.retryFailedQueueItems() }
            }
            AppIconButton {
                iconName: "clear_done"; buttonSize: 28; tooltipText: qsTranslate("App", "Clear completed items")
                enabled: drawer.fileCount > 0 && drawer.canModify
                onClicked: { if (drawer.appBridge) drawer.appBridge.clearCompletedQueueItems() }
            }
            AppIconButton {
                iconName: "clear_all"; buttonSize: 28; tooltipText: qsTranslate("App", "Clear all items")
                enabled: drawer.fileCount > 0 && drawer.canModify
                onClicked: { if (drawer.appBridge) drawer.appBridge.clearActiveQueue() }
            }
            Rectangle { width: 1; height: 16; color: Theme.borderDefault; anchors.verticalCenter: parent.verticalCenter }
            AppIconButton {
                objectName: "queueCollapse"
                iconName: drawer.collapsed ? "chevron_up" : "chevron_down"; buttonSize: 28
                tooltipText: drawer.collapsed ? qsTranslate("App", "Expand queue") : qsTranslate("App", "Collapse queue")
                onClicked: drawer.collapsed = !drawer.collapsed
            }
        }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.borderSubtle }
    }

    Rectangle {
        id: columnHeader
        objectName: "queueColumnHeader"
        anchors.top: header.bottom; anchors.left: parent.left; anchors.right: parent.right
        height: drawer.collapsed ? 0 : 30; visible: !drawer.collapsed; color: Theme.bgInputHover
        Row {
            x: drawer.inset; width: drawer.tableWidth; spacing: drawer.columnGap
            ColumnHeading { width: drawer.numberWidth; text: "#"; horizontalAlignment: Text.AlignHCenter }
            ColumnHeading { width: drawer.fileWidth; text: qsTranslate("App", "Thumbnail / File") }
            ColumnHeading { width: drawer.progressWidth; text: qsTranslate("App", "Job progress") }
            ColumnHeading { width: drawer.fpsWidth; text: "FPS"; visible: drawer.showFps; horizontalAlignment: Text.AlignHCenter }
            ColumnHeading { width: drawer.remainingWidth; text: qsTranslate("App", "Remaining"); visible: drawer.showRemaining; horizontalAlignment: Text.AlignHCenter }
            ColumnHeading { width: drawer.completedWidth; text: qsTranslate("App", "Completed in"); visible: drawer.showCompleted; horizontalAlignment: Text.AlignHCenter }
            ColumnHeading { width: drawer.statusWidth; text: qsTranslate("App", "Status"); horizontalAlignment: Text.AlignHCenter }
            Item { width: drawer.actionsWidth; height: columnHeader.height }
        }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.borderSubtle }
    }

    ListView {
        id: queueList
        objectName: "batchQueueList"
        anchors.top: columnHeader.bottom; anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom
        anchors.bottomMargin: 1
        clip: true; spacing: 0
        orientation: ListView.Vertical
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds
        visible: !drawer.collapsed
        model: drawer.queueModel
        reuseItems: true; cacheBuffer: 168
        currentIndex: drawer.queueModel ? drawer.queueModel.selectedIndex : -1
        WheelHandler {
            target: null
            enabled: queueList.contentHeight > queueList.height
            onWheel: (event) => {
                var delta = event.pixelDelta.y || event.angleDelta.y / 120 * 48
                if (!delta) return
                queueList.cancelFlick()
                var top = queueList.originY
                var bottom = top + Math.max(0, queueList.contentHeight - queueList.height)
                queueList.contentY = Math.max(top, Math.min(bottom, queueList.contentY - delta))
                event.accepted = true
            }
        }
        QQC2.ScrollBar.vertical: QQC2.ScrollBar {
            id: queueScrollBar
            width: 8; policy: QQC2.ScrollBar.AsNeeded; minimumSize: 0.08
            visible: queueList.contentHeight > queueList.height
            contentItem: Rectangle {
                implicitWidth: 4; radius: 2
                color: queueScrollBar.pressed ? Theme.textSecondary : Theme.borderActive
                opacity: queueScrollBar.active || queueScrollBar.hovered ? 1 : 0.65
            }
            background: Rectangle { color: "transparent" }
        }
        onCurrentIndexChanged: { if (currentIndex >= 0) positionViewAtIndex(currentIndex, ListView.Contain) }
        onModelChanged: contentY = originY

        delegate: Rectangle {
            id: queueRow
            objectName: "batchQueueRow"
            required property int index
            required property string fileName
            required property string inputPath
            required property string outputPath
            required property string itemState
            required property real progress
            required property string detail
            required property real elapsedSeconds
            required property string inputDimensions
            required property string outputDimensions
            required property string thumbnailUrl
            required property bool selected
            required property real fileSizeBytes
            required property real durationSeconds
            required property int frameCount
            required property int processedFrames
            required property int processingTotalFrames
            required property real processingFps
            required property real remainingSeconds
            readonly property bool running: itemState === "Running"
            readonly property bool complete: itemState === "Completed" || itemState === "CompletedWithWarnings"
            readonly property bool failed: itemState === "Failed"
            readonly property bool stopped: itemState === "Cancelled" || itemState === "Skipped"
            readonly property real fraction: complete ? 1 : Math.max(0, Math.min(1, progress))
            readonly property color stateColor: failed ? Theme.danger : (complete ? Theme.success : (running ? drawer.processingColor : (stopped ? Theme.warning : Theme.textSecondary)))
            readonly property string statusText: qsTranslate("App", running ? "Processing" : (complete ? "Completed" : (failed ? "Error" : (stopped ? itemState : "Waiting"))))
            readonly property string progressTitle: {
                if (failed) return qsTranslate("App", "Failed")
                if (complete) return qsTranslate("App", "Saved and verified")
                if (stopped) return itemState === "Skipped" ? qsTranslate("App", "Skipped before processing") : qsTranslate("App", "Stopped by user")
                if (!running) return qsTranslate("App", "Waiting to start")
                var stage = detail.match(/\bStage\s+(\d+)\s+of\s+(\d+)\b/i)
                return stage ? qsTranslate("App", "Processing Stage %1 of %2").arg(stage[1]).arg(stage[2]) : qsTranslate("App", "Processing")
            }
            readonly property string metadataText: {
                var parts = []
                if (inputDimensions) parts.push(inputDimensions.replace("×", " × "))
                if (!drawer.imageQueue && frameCount > 0) parts.push(frameCount.toLocaleString(Qt.locale("en_US"), "f", 0) + " frames")
                var size = drawer.fileSize(fileSizeBytes)
                if (size) parts.push(size)
                return parts.join("  ·  ") || (drawer.imageQueue ? "Image" : "Video")
            }
            width: queueList.width; height: 84
            color: selected ? Theme.bgSelected : (rowMouse.containsMouse ? Theme.bgInputHover : (index % 2 ? Qt.darker(Theme.bgInput, 1.08) : Theme.bgInput))
            activeFocusOnTab: drawer.canModify
            Accessible.role: Accessible.ListItem
            Accessible.name: fileName + ", " + statusText + ", " + Math.round(fraction * 100) + " percent"
            Keys.onReturnPressed: { if (drawer.canModify && drawer.appBridge) drawer.appBridge.selectQueueItem(index) }
            Keys.onSpacePressed: { if (drawer.canModify && drawer.appBridge) drawer.appBridge.selectQueueItem(index) }
            Keys.onMenuPressed: rowMenu.openForButton()
            ListView.onPooled: rowMenu.close()
            ListView.onReused: rowMenu.close()
            Connections {
                target: queueList
                function onContentYChanged() { rowMenu.close() }
            }
            Rectangle { width: 3; height: parent.height - 1; color: Theme.accent; visible: queueRow.selected }
            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: Theme.borderSubtle }
            Rectangle { anchors.fill: parent; color: "transparent"; border.color: Theme.accent; visible: queueRow.activeFocus }
            MouseArea {
                id: rowMouse
                anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.LeftButton | Qt.RightButton
                cursorShape: drawer.canModify ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: (mouse) => {
                    if (mouse.button === Qt.RightButton) rowMenu.popup(rowMouse, mouse.x, mouse.y)
                    else if (drawer.canModify && drawer.appBridge) drawer.appBridge.selectQueueItem(queueRow.index)
                }
            }
            Row {
                x: drawer.inset; width: drawer.tableWidth; height: parent.height - 1; spacing: drawer.columnGap
                Text {
                    width: drawer.numberWidth; height: parent.height; text: queueRow.index + 1
                    horizontalAlignment: Text.AlignHCenter; verticalAlignment: Text.AlignVCenter
                    font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeBody
                    color: queueRow.selected ? Theme.textPrimary : Theme.textMuted
                }
                Item {
                    width: drawer.fileWidth; height: parent.height
                    Rectangle {
                        id: thumbnail
                        width: drawer.width >= 1000 ? 90 : 76; height: 54
                        anchors.verticalCenter: parent.verticalCenter; radius: Theme.radiusSmall; clip: true
                        color: queueRow.failed ? Theme.dangerBg : Theme.bgBase
                        border.width: 1
                        border.color: queueRow.failed ? Qt.rgba(Theme.danger.r, Theme.danger.g, Theme.danger.b, 0.5) : Theme.borderSubtle
                        Image {
                            id: thumbnailImage
                            anchors.fill: parent; anchors.margins: 1
                            source: queueRow.thumbnailUrl; sourceSize: Qt.size(180, 108)
                            // Posters are decoded in a worker; the memory provider is cheap to read.
                            fillMode: Image.PreserveAspectCrop; asynchronous: false
                            opacity: queueRow.failed ? 0.35 : 1
                        }
                        AppIcon {
                            anchors.centerIn: parent
                            iconName: queueRow.failed ? "error" : (drawer.imageQueue ? "image_file" : "video_file")
                            iconSize: queueRow.failed ? 26 : 22; color: queueRow.failed ? Theme.danger : Theme.textMuted
                            visible: queueRow.failed || thumbnailImage.status !== Image.Ready
                        }
                        Rectangle {
                            anchors.right: parent.right; anchors.bottom: parent.bottom; anchors.margins: 3
                            width: durationLabel.implicitWidth + 8; height: 17; radius: 3; color: "#D90F0F11"
                            visible: !drawer.imageQueue && queueRow.durationSeconds > 0
                            Text {
                                id: durationLabel
                                anchors.centerIn: parent; text: drawer.clockTime(queueRow.durationSeconds, false)
                                font.family: Theme.monoFontFamily; font.pixelSize: 10; color: Theme.textPrimary
                            }
                        }
                    }
                    Column {
                        anchors.left: thumbnail.right; anchors.leftMargin: 12; anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter; spacing: 6
                        Text {
                            width: parent.width; text: queueRow.fileName; elide: Text.ElideMiddle
                            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; font.weight: Font.Medium
                            color: queueRow.failed ? Theme.dangerHover : Theme.textPrimary
                        }
                        Text {
                            width: parent.width; text: queueRow.metadataText; elide: Text.ElideRight
                            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textSecondary
                        }
                    }
                    HoverHandler { id: fileHover }
                    QueueToolTip { visible: fileHover.hovered; text: queueRow.inputPath + "\n" + queueRow.metadataText }
                }
                Item {
                    width: drawer.progressWidth; height: parent.height
                    Column {
                        anchors.verticalCenter: parent.verticalCenter; width: parent.width; spacing: 6
                        Text {
                            width: parent.width; text: queueRow.progressTitle; elide: Text.ElideRight
                            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
                            color: queueRow.failed ? Theme.dangerHover : (queueRow.running ? Theme.textPrimary : Theme.textSecondary)
                        }
                        Rectangle {
                            width: parent.width; height: 6; radius: 3; color: Theme.bgBase; border.color: Theme.borderSubtle
                            Rectangle {
                                width: Math.max(height, parent.width * queueRow.fraction); height: parent.height; radius: 3
                                visible: queueRow.fraction > 0
                                color: queueRow.failed ? Theme.danger : (queueRow.complete ? Theme.success : (queueRow.stopped ? Theme.warning : Theme.accent))
                                Behavior on width { NumberAnimation { duration: Theme.animFast } }
                            }
                        }
                        Row {
                            width: parent.width; spacing: 8
                            Text {
                                id: percentLabel
                                text: Math.floor(queueRow.fraction * 100) + "%"
                                font.family: Theme.monoFontFamily; font.pixelSize: Theme.fontSizeSmall
                                color: queueRow.complete ? Theme.success : Theme.textPrimary
                            }
                            Text {
                                width: Math.max(0, parent.width - percentLabel.width - parent.spacing)
                                text: {
                                    if (!drawer.showCompleted && queueRow.complete && queueRow.elapsedSeconds > 0)
                                        return qsTranslate("App", "Completed in %1").arg(drawer.clockTime(queueRow.elapsedSeconds, false))
                                    if (!drawer.showRemaining && queueRow.running && queueRow.remainingSeconds >= 0)
                                        return qsTranslate("App", "~%1 remaining").arg(drawer.clockTime(queueRow.remainingSeconds, false))
                                    if (queueRow.complete && !drawer.imageQueue && queueRow.frameCount > 0)
                                        return qsTranslate("App", "%1 source frames").arg(queueRow.frameCount.toLocaleString(Qt.locale(), "f", 0))
                                    if (queueRow.outputDimensions) return qsTranslate("App", "Output %1").arg(queueRow.outputDimensions)
                                    return queueRow.running ? qsTranslate("App", "Overall job progress") : ""
                                }
                                elide: Text.ElideRight; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textSecondary
                            }
                        }
                    }
                    HoverHandler { id: progressHover }
                    QueueToolTip {
                        visible: progressHover.hovered && queueRow.detail !== ""
                        text: {
                            var lines = [queueRow.detail]
                            if (queueRow.running && queueRow.processingFps > 0)
                                lines.push(queueRow.processingFps.toFixed(1) + qsTranslate("App", " input FPS"))
                            if (queueRow.running && queueRow.remainingSeconds >= 0)
                                lines.push(qsTranslate("App", "Estimated time to finish this job, including export: %1").arg(drawer.clockTime(queueRow.remainingSeconds, true)))
                            if (queueRow.complete && queueRow.elapsedSeconds > 0)
                                lines.push(qsTranslate("App", "Completed in %1").arg(drawer.clockTime(queueRow.elapsedSeconds, true)))
                            return lines.join("\n")
                        }
                    }
                }
                Item {
                    width: drawer.fpsWidth; height: parent.height; visible: drawer.showFps
                    Column {
                        anchors.centerIn: parent; spacing: 4
                        MetricText { text: queueRow.running && queueRow.processingFps > 0 ? queueRow.processingFps.toFixed(1) : "—" }
                        MetricCaption { text: "FPS" }
                    }
                    HoverHandler { id: fpsHover }
                    QueueToolTip { visible: fpsHover.hovered; text: qsTranslate("App", "Measured input frames per second in the current stage") }
                }
                Item {
                    width: drawer.remainingWidth; height: parent.height; visible: drawer.showRemaining
                    Column {
                        anchors.centerIn: parent; spacing: 4
                        MetricText { text: queueRow.running && queueRow.remainingSeconds >= 0 ? "~" + drawer.clockTime(queueRow.remainingSeconds, false) : "—" }
                        MetricCaption { text: queueRow.running && queueRow.remainingSeconds >= 0 ? qsTranslate("App", "estimated") : "" }
                    }
                    HoverHandler { id: remainingHover }
                    QueueToolTip { visible: remainingHover.hovered; text: qsTranslate("App", "Estimated time to finish all processing stages and export for this file") }
                }
                Item {
                    width: drawer.completedWidth; height: parent.height; visible: drawer.showCompleted
                    Column {
                        anchors.centerIn: parent; spacing: 4
                        MetricText { text: queueRow.complete && queueRow.elapsedSeconds > 0 ? drawer.clockTime(queueRow.elapsedSeconds, false) : "—" }
                        MetricCaption { text: queueRow.complete && queueRow.elapsedSeconds > 0 ? drawer.durationText(queueRow.elapsedSeconds) : "" }
                    }
                }
                Item {
                    width: drawer.statusWidth; height: parent.height
                    Text {
                        objectName: "queueStatusText"
                        anchors.fill: parent
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        text: queueRow.statusText
                        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeBody
                        font.weight: Font.Medium; color: queueRow.stateColor
                    }
                    HoverHandler { id: statusHover }
                    QueueToolTip { visible: statusHover.hovered && queueRow.detail !== ""; text: queueRow.detail }
                }
                Item {
                    width: drawer.actionsWidth; height: parent.height
                    AppIconButton {
                        id: actionButton
                        objectName: "queueRowActions"
                        anchors.centerIn: parent; buttonSize: 28; iconName: ""
                        tooltipText: qsTranslate("App", "Actions for %1").arg(queueRow.fileName)
                        showTooltip: !rowMenu.opened
                        onClicked: rowMenu.openForButton()
                        Column {
                            anchors.centerIn: parent; spacing: 3
                            Repeater { model: 3; Rectangle { width: 3; height: 3; radius: 1.5; color: Theme.textSecondary } }
                        }
                    }
                    QQC2.Menu {
                        id: rowMenu
                        objectName: "queueRowMenu"
                        parent: actionButton
                        popupType: QQC2.Popup.Item
                        margins: 8
                        width: 210; padding: 4
                        function openForButton() {
                            // popup() changes the parent and coordinates. Set
                            // them on every open so a previous right-click (or
                            // recycled row) cannot displace the button menu.
                            var overlay = QQC2.Overlay.overlay
                            var top = actionButton.mapToItem(overlay, 0, 0).y
                            var below = actionButton.height + 4
                            var y = overlay && top + below + height > overlay.height - margins
                                  ? -height - 4 : below
                            popup(actionButton, actionButton.width - width, y)
                        }
                        closePolicy: QQC2.Popup.CloseOnEscape | QQC2.Popup.CloseOnPressOutside
                        background: Rectangle { color: Theme.bgSurface; radius: Theme.radiusMedium; border.color: Theme.borderActive }
                        RowAction { text: qsTranslate("App", "Preview file"); menuIcon: "preview"; enabled: drawer.canModify; onTriggered: { if (drawer.appBridge) drawer.appBridge.selectQueueItem(queueRow.index) } }
                        RowAction { text: qsTranslate("App", "Open source folder"); menuIcon: "folder"; onTriggered: { if (drawer.appBridge) drawer.appBridge.openFolder(queueRow.inputPath) } }
                        RowAction { objectName: "queueRevealOutput"; text: qsTranslate("App", "Show output in Explorer"); menuIcon: "reveal_in_explorer"; enabled: queueRow.outputPath !== ""; onTriggered: { if (drawer.appBridge) drawer.appBridge.openFolder(queueRow.outputPath) } }
                        QQC2.MenuSeparator { contentItem: Rectangle { implicitHeight: 1; color: Theme.borderSubtle } }
                        RowAction { objectName: "queueRemoveItem"; text: qsTranslate("App", "Remove from queue"); menuIcon: "trash"; enabled: drawer.canModify; onTriggered: { if (drawer.appBridge) drawer.appBridge.removeQueueItem(queueRow.index) } }
                    }
                }
            }
        }
    }

    Column {
        anchors.centerIn: queueList; width: Math.min(420, drawer.width - 40); spacing: 8
        visible: !drawer.collapsed && drawer.fileCount === 0
        AppIcon { iconName: "inbox_import"; iconSize: 26; color: Theme.textMuted; anchors.horizontalCenter: parent.horizontalCenter }
        Text {
            width: parent.width; text: drawer.imageQueue ? qsTranslate("App", "Your image queue is empty") : qsTranslate("App", "Your video queue is empty")
            horizontalAlignment: Text.AlignHCenter; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; color: Theme.textPrimary
        }
        Text {
            width: parent.width; text: qsTranslate("App", "Add files, choose a folder, or drop media into the preview.")
            horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap
            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textSecondary
        }
    }

    MouseArea {
        id: resizeHandle
        objectName: "queueResizeHandle"
        anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top
        height: 5; z: 100; visible: !drawer.collapsed && !drawer.hidden
        cursorShape: Qt.SizeVerCursor; hoverEnabled: true
        property real pressY: 0
        property real startHeight: 0
        onPressed: (mouse) => { pressY = mapToItem(drawer.parent, mouse.x, mouse.y).y; startHeight = drawer.height }
        onPositionChanged: (mouse) => {
            if (!pressed) return
            var currentY = mapToItem(drawer.parent, mouse.x, mouse.y).y
            var value = Math.round(Math.max(180, Math.min(drawer.maximumHeight, startHeight + pressY - currentY)))
            if (drawer.appBridge) drawer.appBridge.queueHeight = value
            else drawer.expandedHeight = value
        }
        Rectangle { anchors.top: parent.top; width: parent.width; height: 2; color: Theme.accent; visible: resizeHandle.containsMouse || resizeHandle.pressed }
    }

    // File drops append to the workflow's Image and Video queues even when
    // this drawer already contains items. This target does not handle clicks.
    DropArea {
        id: queueDropArea
        objectName: "queueDropArea"
        anchors.fill: parent
        z: 200
        enabled: drawer.canModify
        onDropped: (drop) => {
            if (!drop.hasUrls || !drawer.appBridge) return
            var urls = []
            for (var i = 0; i < drop.urls.length; i++) urls.push(drop.urls[i].toString())
            drawer.appBridge.addFiles(urls)
        }
    }
    Rectangle {
        anchors.fill: parent
        z: 199
        color: "#220387C2"
        border.color: Theme.accent
        border.width: 2
        visible: queueDropArea.containsDrag
    }

    FolderDialog { id: folderDialog; title: qsTranslate("App", "Add Folder to Batch"); onAccepted: { if (drawer.appBridge) drawer.appBridge.addFiles([selectedFolder.toString()]) } }
    FileDialog {
        id: fileDialog
        title: qsTranslate("App", "Add Files to Batch"); fileMode: FileDialog.OpenFiles
        nameFilters: [qsTranslate("App", "All Media Files (*.*)")]
        onAccepted: {
            if (!drawer.appBridge || selectedFiles.length === 0) return
            var urls = []
            for (var i = 0; i < selectedFiles.length; i++) urls.push(selectedFiles[i].toString())
            drawer.appBridge.addFiles(urls)
        }
    }
}
