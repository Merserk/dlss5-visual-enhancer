import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import "."
import "components"
import "views"

Window {
    id: appWindow
    width: typeof backend !== "undefined" && backend ? backend.windowWidth : 1440
    height: typeof backend !== "undefined" && backend ? backend.windowHeight : 920
    x: typeof backend !== "undefined" && backend && backend.windowX >= 0 ? Math.max(0, Math.min(Screen.width - width, backend.windowX)) : Math.max(0, (Screen.width - width) / 2)
    y: typeof backend !== "undefined" && backend && backend.windowY >= 0 ? Math.max(0, Math.min(Screen.height - height, backend.windowY)) : Math.max(0, (Screen.height - height) / 2)
    minimumWidth: 1080
    minimumHeight: 700
    // Python reveals the fully prepared window in its saved state.
    flags: Qt.Window | Qt.FramelessWindowHint
    title: "Visual Enhancer"
    color: Theme.bgBase

    readonly property var windowRef: appWindow
    property bool closeApproved: false

    // Chrome hit-test rects (window-relative DIPs) published by HeaderBar for
    // the native Windows filter. Default empty = filter falls back to client.
    property rect minBtnRect: Qt.rect(0, 0, 0, 0)
    property rect maxBtnRect: Qt.rect(0, 0, 0, 0)
    property rect closeBtnRect: Qt.rect(0, 0, 0, 0)
    property rect navTabsRect: Qt.rect(0, 0, 0, 0)
    property bool chromeRectsReady: false

    function saveWindowLayout() {
        if (typeof backend !== "undefined" && backend)
            backend.saveWindowLayout(appWindow.x, appWindow.y, appWindow.width, appWindow.height, appWindow.visibility === Window.Maximized)
    }

    onClosing: (close) => {
        saveWindowLayout()
        if (!closeApproved && typeof backend !== "undefined" && backend && backend.operationState !== "Idle") {
            close.accepted = false
            // The Live video is a native child window that paints above the
            // whole Qt Quick scene (including modal popups). Hide it before
            // opening the prompt so the dialog is clickable; the session
            // keeps running and re-shows from cached geometry on cancel.
            try {
                if (backend.mpvEmbed) backend.mpvEmbed.setDialogOpen(true)
            } catch (e) {}
            closeDialog.open()
        }
    }

    onVisibilityChanged: {
        // A fullscreen Live video is a separate top-level window: if the
        // app is minimized/hidden it would strand the video on screen
        // with its program gone from the taskbar. Dock it back first.
        try {
            if ((visibility === Window.Minimized || visibility === Window.Hidden)
                    && typeof backend !== "undefined" && backend && backend.mpvEmbed && backend.mpvEmbed.fullscreen)
                backend.mpvEmbed.toggleFullscreen()
        } catch (e) {}
    }

    Shortcut {
        sequence: "F11"
        onActivated: {
            // A fullscreen Live video owns the screen: F11 first hands the
            // screen back to the app instead of fighting over window state.
            try {
                if (typeof backend !== "undefined" && backend && backend.mpvEmbed && backend.mpvEmbed.fullscreen) {
                    backend.mpvEmbed.toggleFullscreen()
                    return
                }
            } catch (e) {}
            if (appWindow.visibility === Window.FullScreen) appWindow.showNormal(); else appWindow.showFullScreen()
        }
    }
    // The native Live video covers the transport bar in fullscreen, so the
    // mouse cannot reach "Exit Full" — Esc is the way out (mpv convention).
    // Guarded by `enabled` so a disabled Shortcut never steals Esc from
    // text editors, combo popups, or dialogs while not fullscreen.
    Shortcut {
        sequence: "Escape"
        enabled: typeof backend !== "undefined" && backend && backend.mpvEmbed && backend.mpvEmbed.fullscreen ? true : false
        onActivated: {
            if (typeof backend === "undefined" || !backend || !backend.mpvEmbed) return
            if (!backend.mpvEmbed.fullscreen) return
            backend.mpvEmbed.toggleFullscreen()
        }
    }
    Shortcut {
        sequence: "Ctrl+Shift+F"
        onActivated: { if (typeof backend !== "undefined" && backend) backend.focusPreview = !backend.focusPreview }
    }

    // Global editing shortcuts below must never steal keys from text
    // editors (stream URL, preset name, path fields all use TextInput).
    // NOTE: guards live in `enabled` (not only in onActivated) so a
    // disabled Shortcut never consumes the key — TextInput keeps native
    // Ctrl+V / Delete / Space behavior while editing.
    readonly property bool isTextEditing: activeFocusItem instanceof TextInput || activeFocusItem instanceof TextEdit
    readonly property bool isBatchTab: typeof backend !== "undefined" && backend ? backend.activeTab === "neural-rendering" : false
    readonly property bool isLiveTab: typeof backend !== "undefined" && backend ? backend.activeTab === "live" : false
    // True when keyboard focus sits on any tab-focusable control (custom
    // AppButton/Switch/CheckBox/ComboBox/Slider/IconButton expose
    // activeFocusOnTab:true and consume Space themselves). Mouse clicks
    // never move keyboard focus there, so mouse users keep Space-pause.
    readonly property bool isInteractiveFocused: {
        var f = activeFocusItem
        if (!f) return false
        if (f instanceof TextInput || f instanceof TextEdit) return true
        try {
            if (f.activeFocusOnTab === true) return true
        } catch (e) {}
        return false
    }

    function textEditingFocused() {
        return appWindow.isTextEditing
    }

    function batchTabActive() {
        return appWindow.isBatchTab
    }

    // Ctrl+V: paste Explorer files / screenshots into the active batch tab.
    Shortcut {
        sequence: "Ctrl+V"
        enabled: appWindow.isBatchTab && !appWindow.isTextEditing
        onActivated: {
            if (appWindow.textEditingFocused() || !appWindow.batchTabActive()) return
            if (typeof backend !== "undefined" && backend) backend.pasteFromClipboard()
        }
    }
    // Delete: same as the Clear All queue button (active tab only).
    Shortcut {
        sequence: "Delete"
        enabled: appWindow.isBatchTab && !appWindow.isTextEditing
        onActivated: {
            if (appWindow.textEditingFocused() || !appWindow.batchTabActive()) return
            if (typeof backend !== "undefined" && backend) backend.clearActiveQueue()
        }
    }
    // Space: pause/resume in the Live tab only. Suppressed while a text
    // field or any keyboard-focused control owns Space (native activation
    // wins, avoiding a double toggle on custom App* buttons); mouse users
    // are unaffected (clicking never moves keyboard focus).
    Shortcut {
        sequence: "Space"
        enabled: appWindow.isLiveTab && !appWindow.isInteractiveFocused
        onActivated: {
            if (typeof backend === "undefined" || !backend) return
            if (backend.activeTab !== "live") return
            if (appWindow.isInteractiveFocused) return
            if (backend.mpvEmbed && backend.isLiveRunning) backend.mpvEmbed.togglePause()
        }
    }

    Column {
        anchors.fill: parent
        HeaderBar { id: header; width: parent.width; appBridge: typeof backend !== "undefined" ? backend : null; windowRef: appWindow }

        Item {
            id: workspace
            width: parent.width
            height: parent.height - header.height
            clip: true
            StackLayout {
                anchors.fill: parent
                currentIndex: {
                    if (typeof backend === "undefined" || !backend) return 0
                    var tab = backend.activeTab
                    if (tab === "neural-rendering") return 0
                    if (tab === "live") return 1
                    if (tab === "settings") return 2
                    if (tab === "help") return 3
                    return 0
                }
                NeuralRenderingView { appBridge: typeof backend !== "undefined" ? backend : null }
                LiveView { appBridge: typeof backend !== "undefined" ? backend : null }
                SettingsView { appBridge: typeof backend !== "undefined" ? backend : null }
                AboutView { appBridge: typeof backend !== "undefined" ? backend : null }
            }
        }
    }

    ExportDialog {
        id: exportDialog
        appBridge: typeof backend !== "undefined" ? backend : null
    }
    Connections {
        target: typeof backend !== "undefined" ? backend : null
        function onBatchExportRequested(contextKey) {
            exportDialog.openForContext(contextKey)
        }
    }

    // QML resize hit zones (fallback where the native filter is inactive; on
    // Windows the OS owns the frame edges). Disabled while maximized/fullscreen.
    property bool qmlResizeActive: !(appWindow.nativeChromeActive === true && appWindow.chromeRectsReady === true) && appWindow.visibility !== Window.Maximized && appWindow.visibility !== Window.FullScreen
    MouseArea { z:10000; visible: qmlResizeActive; anchors.left:parent.left; anchors.top:parent.top; anchors.bottom:parent.bottom; width:6; cursorShape:Qt.SizeHorCursor; onPressed: { appWindow.startSystemResize(Qt.LeftEdge) } }
    MouseArea { z:10000; visible: qmlResizeActive; anchors.right:parent.right; anchors.top:parent.top; anchors.bottom:parent.bottom; width:6; cursorShape:Qt.SizeHorCursor; onPressed: { appWindow.startSystemResize(Qt.RightEdge) } }
    MouseArea { z:10000; visible: qmlResizeActive; anchors.top:parent.top; anchors.left:parent.left; anchors.right:parent.right; height:6; cursorShape:Qt.SizeVerCursor; onPressed: { appWindow.startSystemResize(Qt.TopEdge) } }
    MouseArea { z:10000; visible: qmlResizeActive; anchors.bottom:parent.bottom; anchors.left:parent.left; anchors.right:parent.right; height:6; cursorShape:Qt.SizeVerCursor; onPressed: { appWindow.startSystemResize(Qt.BottomEdge) } }
    MouseArea { z:10001; visible: qmlResizeActive; anchors.left:parent.left; anchors.top:parent.top; width:10; height:10; cursorShape:Qt.SizeFDiagCursor; onPressed: { appWindow.startSystemResize(Qt.LeftEdge|Qt.TopEdge) } }
    MouseArea { z:10001; visible: qmlResizeActive; anchors.right:parent.right; anchors.top:parent.top; width:10; height:10; cursorShape:Qt.SizeBDiagCursor; onPressed: { appWindow.startSystemResize(Qt.RightEdge|Qt.TopEdge) } }
    MouseArea { z:10001; visible: qmlResizeActive; anchors.left:parent.left; anchors.bottom:parent.bottom; width:10; height:10; cursorShape:Qt.SizeBDiagCursor; onPressed: { appWindow.startSystemResize(Qt.LeftEdge|Qt.BottomEdge) } }
    MouseArea { z:10001; visible: qmlResizeActive; anchors.right:parent.right; anchors.bottom:parent.bottom; width:10; height:10; cursorShape:Qt.SizeFDiagCursor; onPressed: { appWindow.startSystemResize(Qt.RightEdge|Qt.BottomEdge) } }

    QQC2.Dialog {
        id: closeDialog
        parent: QQC2.Overlay.overlay
        anchors.centerIn: parent
        width: 420
        z: 20000
        focus: true
        modal: true
        closePolicy: QQC2.Popup.CloseOnEscape
        standardButtons: QQC2.Dialog.Yes | QQC2.Dialog.No
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 12
        spacing: 8
        // Borderless custom header: the default Dialog title chrome draws
        // its own light separator line under the title.
        header: QQC2.Label {
            text: "Operation still active"
            color: Theme.textPrimary
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeTitle
            font.weight: Font.Bold
            leftPadding: 20
            rightPadding: 20
            topPadding: 16
            bottomPadding: 4
        }
        contentItem: Text {
            text: "A render, preview, scan, or Live session is still active. Cancel it and exit?"
            color: Theme.textPrimary; font.family: Theme.fontFamily; wrapMode: Text.WordWrap; width: 360
        }
        // Borderless: the previous border.color (light gray) read as a
        // white outline around the whole box against the dark surface.
        background: Rectangle { color: Theme.bgSurface; border.width: 0; radius: Theme.radiusLarge }
        onAboutToShow: {
            try {
                if (typeof backend !== "undefined" && backend && backend.mpvEmbed) backend.mpvEmbed.setDialogOpen(true)
            } catch (e) {}
        }
        onClosed: {
            // Cancelled (No / Escape / X): restore the Live video if its tab
            // is visible; the controller re-shows from cached geometry.
            try {
                if (typeof backend !== "undefined" && backend && backend.mpvEmbed) backend.mpvEmbed.setDialogOpen(false)
            } catch (e) {}
        }
        onAccepted: { appWindow.closeApproved = true; appWindow.close() }
    }
}
