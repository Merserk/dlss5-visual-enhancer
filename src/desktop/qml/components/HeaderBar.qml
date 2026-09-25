import QtQuick
import QtQuick.Window
import ".."
import "../controls"

Rectangle {
    id: root
    property var appBridge: null
    property var windowRef: null
    readonly property bool compact: width < 1320
    readonly property bool narrow: width < 1140

    height: 50
    color: Theme.bgSurface
    border.color: Theme.borderSubtle
    border.width: 1

    // QML drag/double-click fallbacks. On Windows the native filter reports
    // HTCAPTION for empty header space, so the OS owns drag, double-click
    // maximize and edge snap there (double-handling would fight it). The
    // fallbacks stay live until hit-test rects are published, so the header
    // is never dead while the filter is still blind.
    readonly property bool nativeChrome: windowRef && windowRef.nativeChromeActive === true && windowRef.chromeRectsReady === true
    MouseArea {
        anchors.fill: parent; z: 0
        onPressed: { if (!root.nativeChrome && windowRef && windowRef.visibility !== Window.Maximized && windowRef.visibility !== Window.FullScreen) windowRef.startSystemMove() }
        onDoubleClicked: {
            if (!windowRef || root.nativeChrome) return
            root.toggleMaximize()
        }
    }

    // Native maximize/restore (the OS filter supplies a real frame behind the
    // frameless visuals, so geometry, taskbar and animations are correct).
    function toggleMaximize() {
        if (!windowRef) return
        if (windowRef.visibility === Window.Maximized || windowRef.visibility === Window.FullScreen) windowRef.showNormal()
        else windowRef.showMaximized()
    }

    // Window-relative chrome rects (DIPs) for the native hit-test filter, so
    // snap/min/max/close land exactly on the drawn buttons and tab clicks
    // still reach QML. Refreshed whenever the header layout changes.
    function updateChromeRects() {
        if (!windowRef || !windowRef.contentItem) return
        var p
        p = minBtn.mapToItem(windowRef.contentItem, 0, 0)
        windowRef.minBtnRect = Qt.rect(p.x, p.y, Math.max(minBtn.width, minBtn.implicitWidth), Math.max(minBtn.height, minBtn.implicitHeight))
        p = maxBtn.mapToItem(windowRef.contentItem, 0, 0)
        windowRef.maxBtnRect = Qt.rect(p.x, p.y, Math.max(maxBtn.width, maxBtn.implicitWidth), Math.max(maxBtn.height, maxBtn.implicitHeight))
        p = closeBtn.mapToItem(windowRef.contentItem, 0, 0)
        windowRef.closeBtnRect = Qt.rect(p.x, p.y, Math.max(closeBtn.width, closeBtn.implicitWidth), Math.max(closeBtn.height, closeBtn.implicitHeight))
        p = navTabs.mapToItem(windowRef.contentItem, 0, 0)
        windowRef.navTabsRect = Qt.rect(p.x, p.y, navTabs.width, navTabs.height)
        windowRef.chromeRectsReady = windowRef.minBtnRect.width > 0 && windowRef.maxBtnRect.width > 0
            && windowRef.closeBtnRect.width > 0 && windowRef.navTabsRect.width > 0
            && windowRef.minBtnRect.height > 0 && windowRef.maxBtnRect.height > 0
            && windowRef.closeBtnRect.height > 0 && windowRef.navTabsRect.height > 0
    }
    onWidthChanged: updateChromeRects()
    Component.onCompleted: Qt.callLater(updateChromeRects)
    Connections {
        target: rightArea
        function onWidthChanged() { root.updateChromeRects() }
    }
    Connections {
        target: navTabs
        function onWidthChanged() { root.updateChromeRects() }
    }
    Connections {
        target: windowRef
        function onHeightChanged() { root.updateChromeRects() }
        function onWidthChanged() { root.updateChromeRects() }
        // Maximize/restore settles geometry asynchronously; refresh once the
        // state lands so the native filter never aims at pre-toggle rects.
        function onVisibilityChanged() { Qt.callLater(root.updateChromeRects) }
    }

    Row {
        id: brand; z: 2
        anchors.left: parent.left; anchors.leftMargin: 12; anchors.verticalCenter: parent.verticalCenter; spacing: 8
        Image {
            source: Qt.resolvedUrl("../../../../native (dev)/icon.png")
            sourceSize: Qt.size(112, 112)
            width: 28; height: 28
            fillMode: Image.PreserveAspectFit
            smooth: true
            anchors.verticalCenter: parent.verticalCenter
        }
        Text { text: "Visual Enhancer"; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeTitle; font.weight: Font.Normal; color: Theme.textPrimary; anchors.verticalCenter: parent.verticalCenter }
    }

    Row {
        id: rightArea; z: 2
        anchors.right: parent.right; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter; spacing: root.compact ? 6 : 10
        Row {
            spacing: 2
            // Window-chrome buttons: vector icons, no hover tooltips (they sit
            // at y=0 where an above-anchored popup would clip outside the window).
            // On Windows the native filter hit-tests these rects (snap flyout,
            // system press animations); the QML handlers are the fallback path
            // for the pre-rects blind window only. The nativeChrome guard keeps
            // single ownership: without it a transition-frame double delivery
            // would toggle maximize twice (apparent no-op) or fight the OS.
            // Minimize intentionally has no nativeChrome guard: minimize is
            // idempotent, and if the OS hit-test claimed the click QML never
            // receives it — so a click arriving here always means the OS did
            // NOT claim it (mapping miss) and minimizing is the right action.
            // maxBtn/closeBtn keep the guard (toggle/close double-delivery).
            AppIconButton { id: minBtn; iconName: "minimize"; showTooltip: false; onClicked: { if (windowRef) windowRef.showMinimized() } }
            AppIconButton {
                id: maxBtn
                iconName: windowRef && (windowRef.visibility === Window.Maximized || windowRef.visibility === Window.FullScreen) ? "restore_window" : "maximize"
                showTooltip: false
                onClicked: { if (root.nativeChrome) return; root.toggleMaximize() }
            }
            AppIconButton { id: closeBtn; iconName: "close"; showTooltip: false; hoverColor: Theme.danger; onClicked: { if (root.nativeChrome) return; if (windowRef) windowRef.close() } }
        }
    }

    // Menu-style navigation: plain compact text tabs grouped on the left,
    // active tab shown by accent text color only (no button chrome).
    Row {
        id: navTabs; z: 3
        anchors.left: brand.right; anchors.leftMargin: 16
        anchors.verticalCenter: parent.verticalCenter
        spacing: 4
        readonly property var tabs: [
            { id:"neural-rendering", full:"Neural Rendering", short:"Neural", icon:"neural_rendering" },
            { id:"live", full:"Live", short:"Live", icon:"live_video" },
            { id:"settings", full:"Settings", short:"Settings", icon:"settings" },
            { id:"help", full:"Help", short:"Help", icon:"help" }
        ]
        Repeater {
            model: navTabs.tabs
            Item {
                id: tabBtn
                height: 28
                width: tabLabel.implicitWidth + 42
                readonly property bool isActive: appBridge ? appBridge.activeTab === modelData.id : false
                Row {
                    anchors.centerIn: parent
                    spacing: 6
                    AppIcon {
                        iconName: modelData.icon
                        iconSize: 20
                        color: tabBtn.isActive ? Theme.accent : (tabMouse.containsMouse ? Theme.textPrimary : Theme.textSecondary)
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        id: tabLabel
                        text: root.compact ? modelData.short : modelData.full
                        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel
                        font.weight: Font.Normal
                        color: tabBtn.isActive ? Theme.accent : (tabMouse.containsMouse ? Theme.textPrimary : Theme.textSecondary)
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
                Accessible.role: Accessible.PageTab
                Accessible.name: modelData.full
                MouseArea {
                    id: tabMouse; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: { if (appBridge) appBridge.activeTab = modelData.id }
                }
            }
        }
    }
}
