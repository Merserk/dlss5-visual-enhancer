import QtQuick
import ".."
import "../controls"
import "../components"

Item {
    id: root
    objectName: "neural-rendering-view"

    property var appBridge: null
    property alias workflowRepeater: stageRepeater
    readonly property int adaptiveInspectorWidth: width >= 2560 ? 520 : (width >= 1920 ? 460 : (width >= 1600 ? 420 : (width >= 1280 ? 380 : 340)))

    readonly property bool isImage: appBridge ? (appBridge.nrMode === "Image") : true
    readonly property var activeQueue: appBridge ? (isImage ? appBridge.nrImageQueue : appBridge.nrVideoQueue) : null
    property var collapsedCards: ({})
    property string draggedStageId: ""
    property int dragOriginIndex: -1
    property int dragTargetIndex: -1
    property real draggedCardHeight: 0

    function collapseKey(stageId, mode) { return mode + ":" + stageId }
    function storedCollapsed(stageId, mode) {
        var key = collapseKey(stageId, mode)
        return collapsedCards[key] === undefined ? true : collapsedCards[key]
    }
    function rememberCollapsed(stageId, mode, value) {
        collapsedCards[collapseKey(stageId, mode)] = value
    }
    function stageSlotOffset(stageId, index) {
        if (!draggedStageId || stageId === draggedStageId) return 0
        var distance = draggedCardHeight + contentCol.spacing
        if (dragTargetIndex > dragOriginIndex
                && index > dragOriginIndex && index <= dragTargetIndex) return -distance
        if (dragTargetIndex < dragOriginIndex
                && index >= dragTargetIndex && index < dragOriginIndex) return distance
        return 0
    }

    function stageTitle(id) {
        if (id === "denoising") return "Denoising"
        if (id === "neural_model") return "DLSS Neural Rendering"
        if (id === "scale_method") return "Scaling"
        if (id === "dlss_super_resolution") return "DLSS Super Resolution"
        if (id === "super_resolution") return "RTX Super Resolution"
        if (id === "coloring") return "Coloring"
        if (id === "cas_sharpening") return "Sharpening"
        return "DLSS Frame Generation"
    }

    Component { id: neuralControls; NeuralModelControls { appBridge: root.appBridge } }
    Component { id: scaleControls; ScaleMethodControls { appBridge: root.appBridge } }
    Component { id: dlssControls; DlssSuperResolutionControls { appBridge: root.appBridge } }
    Component { id: superControls; SuperResolutionControls { appBridge: root.appBridge } }
    Component { id: coloringControls; ColoringControls { appBridge: root.appBridge } }
    Component { id: denoisingControls; DenoisingControls { appBridge: root.appBridge } }
    Component { id: sharpeningControls; SharpeningControls { appBridge: root.appBridge } }
    Component { id: frameControls; FrameGenerationControls { appBridge: root.appBridge } }

    Row {
        anchors.fill: parent

        // Main Center Area: Media Viewport + Batch Queue Drawer
        Item {
            width: parent.width - sidebar.width
            height: parent.height

            MediaViewport {
                id: viewport
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: queueDrawer.top
                appBridge: root.appBridge
            }

            BatchQueueDrawer {
                id: queueDrawer
                anchors.bottom: parent.bottom
                anchors.left: parent.left
                anchors.right: parent.right
                appBridge: root.appBridge
                queueModel: root.activeQueue
                hidden: appBridge ? appBridge.focusPreview : false
                expandedHeight: appBridge ? appBridge.queueHeight : 190
            }
        }

        // Right Sidebar: Parameters & Inspector (Topaz-style)
        Rectangle {
            id: sidebar
            width: appBridge && appBridge.focusPreview ? 0 : Math.max(root.adaptiveInspectorWidth, Math.min(560, appBridge ? appBridge.inspectorWidth : root.adaptiveInspectorWidth))
            height: parent.height
            visible: width > 0
            color: Theme.bgSurface
            border.color: Theme.borderSubtle
            border.width: 1

            MouseArea {
                id: inspectorResizeHandle
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 6
                z: 100
                cursorShape: Qt.SizeHorCursor
                onPositionChanged: (mouse) => {
                    if (!pressed || !appBridge) return
                    var pt = inspectorResizeHandle.mapToItem(root, mouse.x, mouse.y)
                    appBridge.inspectorWidth = Math.max(300, Math.min(560, root.width - pt.x))
                }
            }

            Column {
                anchors.fill: parent

                // Workflow Submode Switcher: Image vs Video
                Rectangle {
                    width: parent.width
                    height: 48
                    color: Theme.bgSurface
                    border.color: Theme.borderSubtle
                    border.width: 1

                    Row {
                        anchors.centerIn: parent
                        spacing: 8

                        AppSegmentedControl {
                            model: ["Image", "Video"]
                            currentValue: root.appBridge ? root.appBridge.nrMode : "Image"
                            onActivated: (val) => {
                                if (root.appBridge) root.appBridge.nrMode = val
                            }
                        }
                    }
                }

                // Scrollable Cards Area
                Flickable {
                    id: flick
                    width: parent.width
                    height: parent.height - 48 - actionBar.height
                    contentWidth: width
                    contentHeight: contentCol.implicitHeight + 24
                    clip: true
                    boundsBehavior: Flickable.StopAtBounds
                    interactive: root.draggedStageId === ""

                    Column {
                        id: contentCol
                        width: parent.width - 24
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.top: parent.top
                        anchors.topMargin: 12
                        spacing: 12

                        Repeater {
                            id: stageRepeater
                            model: appBridge ? appBridge.workflowStages : []
                            delegate: WorkflowStageCard {
                                objectName: "workflow-stage-" + stageId
                                width: contentCol.width
                                appBridge: root.appBridge
                                stageId: modelData.id
                                stageEnabled: modelData.enabled
                                stageIndex: index
                                stageCount: root.workflowRepeater ? root.workflowRepeater.count : 0
                                stageRepeater: root.workflowRepeater
                                title: root.stageTitle(stageId)
                                slotOffset: root.stageSlotOffset(stageId, stageIndex)
                                property string layoutMode: root.isImage ? "Image" : "Video"
                                property bool restoringCollapse: false
                                function restoreCollapse() {
                                    if (!stageId) return
                                    restoringCollapse = true
                                    collapsed = root.storedCollapsed(stageId, layoutMode)
                                    restoringCollapse = false
                                }
                                onStageIdChanged: restoreCollapse()
                                onLayoutModeChanged: restoreCollapse()
                                Component.onCompleted: restoreCollapse()
                                onCollapsedChanged: {
                                    if (!restoringCollapse && stageId)
                                        root.rememberCollapsed(stageId, layoutMode, collapsed)
                                }
                                onDragBegan: (id, origin, cardHeight) => {
                                    root.draggedStageId = id
                                    root.dragOriginIndex = origin
                                    root.dragTargetIndex = origin
                                    root.draggedCardHeight = cardHeight
                                }
                                onDragMoved: target => { root.dragTargetIndex = target }
                                onDragFinished: (id, target) => {
                                    root.draggedStageId = ""
                                    root.dragOriginIndex = -1
                                    root.dragTargetIndex = -1
                                    if (target !== stageIndex && root.appBridge)
                                        root.appBridge.moveWorkflowStage(id, target)
                                }
                                Loader {
                                    width: parent.width
                                    sourceComponent: stageId === "neural_model" ? neuralControls
                                                   : stageId === "denoising" ? denoisingControls
                                                   : stageId === "scale_method" ? scaleControls
                                                   : stageId === "dlss_super_resolution" ? dlssControls
                                                   : stageId === "super_resolution" ? superControls
                                                   : stageId === "coloring" ? coloringControls
                                                   : stageId === "cas_sharpening" ? sharpeningControls
                                                   : frameControls
                                }
                            }
                        }

                        AppCard {
                            width: parent.width
                            title: "Export Settings"
                            ExportSettingsControls {
                                width: parent.width
                                appBridge: root.appBridge
                            }
                        }
                    }
                }

                // Sticky Bottom Action Bar
                Rectangle {
                    id: actionBar
                    width: parent.width
                    height: 88
                    color: Theme.bgSurface
                    border.color: Theme.borderSubtle
                    border.width: 1

                    Column {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        Row {
                            width: parent.width
                            spacing: 8

                            AppButton {
                                text: "Preview"
                                iconName: "preview"
                                width: Math.max(100, parent.width - (root.isImage ? 84 : 156))
                                buttonHeight: 30
                                enabled: appBridge ? appBridge.canPreview : false
                                onClicked: {
                                    if (appBridge) appBridge.renderPreviewAt(viewport.playheadMs)
                                }
                            }

                            AppComboBox {
                                width: 72
                                comboHeight: 30
                                dropUp: true
                                visible: !root.isImage
                                model: appBridge ? appBridge.nrPreviewLengthChoices : []
                                currentValue: appBridge ? appBridge.nrPreviewLength : "3"
                                onActivated: (v) => { if (appBridge) appBridge.nrPreviewLength = v }
                            }

                            AppIconButton {
                                iconName: "reset"
                                buttonSize: 30
                                tooltipText: "Reset neural rendering settings"
                                onClicked: { if (appBridge) appBridge.resetTabSettings("neural-rendering") }
                            }

                            AppIconButton {
                                iconName: "outputs_folder"
                                buttonSize: 30
                                tooltipText: "Open outputs folder"
                                onClicked: { if (appBridge) appBridge.openFolder("") }
                            }
                        }

                        AppButton {
                            text: appBridge && appBridge.canStop ? "Stop" : (root.isImage ? "Render Image(s)" : "Render Video(s)")
                            iconName: appBridge && appBridge.canStop ? "stop" : "start_render"
                            variant: appBridge && appBridge.canStop ? "danger" : "primary"
                            width: parent.width
                            buttonHeight: 34
                            enabled: appBridge ? (appBridge.canStop || (appBridge.operationState === "Idle" && appBridge.runtimeState === "Ready" && !appBridge.isLiveRunning)) : false
                            onClicked: {
                                if (appBridge) {
                                    if (appBridge.canStop) {
                                        appBridge.stopActiveBatch()
                                    } else {
                                        appBridge.requestActiveBatchExport()
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
