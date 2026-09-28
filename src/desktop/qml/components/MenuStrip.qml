import QtQuick
import QtQuick.Controls as QQC2
import ".."

// Topaz-style menu strip: File / View / Process / Help. Every item maps to
// an existing bridge slot/property; window-level actions (exit, fullscreen,
// console) are forwarded as signals owned by Main.qml.
Row {
    id: root
    property var appBridge: null
    property bool consoleOpen: false
    signal exitRequested()
    signal toggleFullscreenRequested()
    signal toggleConsoleRequested()

    height: 44
    spacing: 2

    // Only one menu open at a time; hovering another title switches to it.
    property var openMenu: null
    function toggleMenu(menu, button) {
        if (root.openMenu === menu) {
            menu.dismiss()
            root.openMenu = null
        } else {
            if (root.openMenu) root.openMenu.dismiss()
            menu.popup(button, 0, button.height)
            root.openMenu = menu
        }
    }
    function onMenuClosed(menu) {
        if (root.openMenu === menu) root.openMenu = null
    }

    // Shared dark chrome for all menus.
    Component {
        id: menuBackground
        Rectangle {
            color: Theme.bgSurface
            border.color: Theme.borderDefault
            border.width: 1
            radius: 8
            implicitWidth: 250
        }
    }
    Component {
        id: menuDelegate
        QQC2.MenuItem {
            id: mi
            contentItem: Text {
                text: mi.text
                color: !mi.enabled ? Theme.textMuted : (mi.highlighted ? Theme.textPrimary : Theme.textSecondary)
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeBody
                elide: Text.ElideRight
                verticalAlignment: Text.AlignVCenter
            }
            background: Rectangle {
                color: mi.highlighted && mi.enabled ? Theme.bgHover : "transparent"
                radius: 5
            }
            indicator: Item {
                implicitWidth: 26
                implicitHeight: 20
                Rectangle {
                    anchors.centerIn: parent
                    width: 6
                    height: 6
                    radius: 3
                    color: mi.checked ? Theme.accent : "transparent"
                    border.color: Theme.borderDefault
                    border.width: mi.checked ? 0 : 1
                    visible: mi.checkable
                }
            }
        }
    }

    // -- menu title buttons -------------------------------------------
    Repeater {
        model: [
            { label: qsTranslate("App", "File"), menu: fileMenu },
            { label: qsTranslate("App", "View"), menu: viewMenu },
            { label: qsTranslate("App", "Process"), menu: processMenu },
            { label: qsTranslate("App", "Help"), menu: helpMenu }
        ]
        Item {
            required property var modelData
            height: 44
            width: btnLabel.implicitWidth + 24
            Text {
                id: btnLabel
                anchors.centerIn: parent
                text: parent.modelData.label
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                color: root.openMenu === parent.modelData.menu ? Theme.accent
                    : (btnMouse.containsMouse ? Theme.textPrimary : Theme.textSecondary)
            }
            MouseArea {
                id: btnMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.toggleMenu(parent.modelData.menu, parent)
                onContainsMouseChanged: {
                    if (containsMouse && root.openMenu && root.openMenu !== parent.modelData.menu)
                        root.toggleMenu(parent.modelData.menu, parent)
                }
            }
        }
    }

    // -- File ----------------------------------------------------------
    QQC2.Menu {
        id: fileMenu
        background: menuBackground
        delegate: menuDelegate
        onClosed: root.onMenuClosed(fileMenu)
        QQC2.MenuItem {
            text: qsTranslate("App", "Open Outputs Folder")
            enabled: root.appBridge !== null
            onTriggered: { if (root.appBridge) root.appBridge.openFolder("") }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Open Logs Folder")
            enabled: root.appBridge && root.appBridge.currentLogPath !== ""
            onTriggered: { if (root.appBridge) root.appBridge.openFolder(root.appBridge.currentLogPath) }
        }
        Loader { sourceComponent: menuSeparator }
        QQC2.MenuItem {
            text: qsTranslate("App", "Exit")
            onTriggered: root.exitRequested()
        }
    }

    // -- View ----------------------------------------------------------
    QQC2.Menu {
        id: viewMenu
        background: menuBackground
        delegate: menuDelegate
        onClosed: root.onMenuClosed(viewMenu)
        QQC2.MenuItem {
            text: qsTranslate("App", "Neural Rendering")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "neural-rendering"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "neural-rendering" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Upscale")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "upscale"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "upscale" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Frame Interpolation")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "frame-interpolation"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "frame-interpolation" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Live")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "live"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "live" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Settings")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "settings"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "settings" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Help")
            checkable: true
            checked: root.appBridge && root.appBridge.activeTab === "help"
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "help" }
        }
        Loader { sourceComponent: menuSeparator }
        QQC2.MenuItem {
            text: qsTranslate("App", "Focus Preview")
            checkable: true
            checked: root.appBridge ? root.appBridge.focusPreview : false
            onTriggered: { if (root.appBridge) root.appBridge.focusPreview = !root.appBridge.focusPreview }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Fullscreen")
            onTriggered: root.toggleFullscreenRequested()
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Console")
            checkable: true
            checked: root.consoleOpen
            onTriggered: root.toggleConsoleRequested()
        }
    }

    // -- Process -------------------------------------------------------
    QQC2.Menu {
        id: processMenu
        background: menuBackground
        delegate: menuDelegate
        onClosed: root.onMenuClosed(processMenu)
        QQC2.MenuItem {
            text: qsTranslate("App", "Start / Render")
            enabled: root.appBridge ? ((root.appBridge.activeTab === "neural-rendering" || root.appBridge.activeTab === "upscale" || root.appBridge.activeTab === "frame-interpolation") && root.appBridge.operationState === "Idle" && root.appBridge.runtimeState === "Ready" && !root.appBridge.isLiveRunning) : false
            onTriggered: { if (root.appBridge) root.appBridge.requestActiveBatchExport() }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Preview")
            enabled: root.appBridge ? root.appBridge.canPreview : false
            onTriggered: { if (root.appBridge) root.appBridge.renderPreview() }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Stop")
            enabled: root.appBridge ? root.appBridge.canStop : false
            onTriggered: { if (root.appBridge) root.appBridge.stopActiveBatch() }
        }
        Loader { sourceComponent: menuSeparator }
        QQC2.MenuItem {
            text: qsTranslate("App", "Stop Live Session")
            enabled: root.appBridge ? root.appBridge.isLiveRunning : false
            onTriggered: { if (root.appBridge) root.appBridge.stopLive() }
        }
    }

    // -- Help ----------------------------------------------------------
    QQC2.Menu {
        id: helpMenu
        background: menuBackground
        delegate: menuDelegate
        onClosed: root.onMenuClosed(helpMenu)
        QQC2.MenuItem {
            text: qsTranslate("App", "Help")
            onTriggered: { if (root.appBridge) root.appBridge.activeTab = "help" }
        }
        QQC2.MenuItem {
            text: qsTranslate("App", "Open Logs Folder")
            enabled: root.appBridge && root.appBridge.currentLogPath !== ""
            onTriggered: { if (root.appBridge) root.appBridge.openFolder(root.appBridge.currentLogPath) }
        }
    }
}
