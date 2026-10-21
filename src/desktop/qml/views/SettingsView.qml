import QtQuick
import QtQuick.Controls as QQC2
import ".."
import "../controls"

Rectangle {
    id: root
    property var appBridge: null
    color: Theme.bgBase

    Flickable {
        id: settingsViewport
        anchors.fill: parent; anchors.margins: 28
        contentWidth: width
        contentHeight: Math.max(height, settingsCol.implicitHeight)
        clip: true; boundsBehavior: Flickable.StopAtBounds

        Column {
            id: settingsCol
            width: Math.min(parent.width, 1120)
            anchors.horizontalCenter: parent.horizontalCenter
            y: Math.max(0, (settingsViewport.height - implicitHeight) / 2)
            spacing: 20

            Column {
                width: parent.width; spacing: 4
                Text { width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; text: qsTranslate("App", "Preferences & System Configuration"); font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeHeader; font.weight: Font.Bold; color: Theme.textPrimary }
            }

            Column {
                id: cardColumn
                width: parent.width; spacing: 14

                AppCard {
                    width: cardColumn.width; title: qsTranslate("App", "Language"); collapsible: false
                    Column {
                        width: parent.width; spacing: 10
                        AppLanguageComboBox {
                            width: parent.width
                            label: qsTranslate("App", "Interface Language")
                            model: appBridge ? appBridge.languageChoices : [{label: "English (US)", value: "en_US", fontFamily: Theme.fontFamily}]
                            currentValue: appBridge ? appBridge.language : "en_US"
                            onActivated: (value) => { if (appBridge) appBridge.language = value }
                        }
                    }
                }

                AppCard {
                    width: cardColumn.width; title: qsTranslate("App", "Hardware & GPU Acceleration"); collapsible: false
                    Column {
                        width: parent.width; spacing: 16
                        AppComboBox {
                            width: parent.width; label: qsTranslate("App", "AI Processing GPU (Tensor Cores / NGX)")
                            objectName: "ai-processing-device"
                            model: appBridge ? appBridge.aiGpuChoices : []
                            currentValue: appBridge ? appBridge.aiGpuUuid : "auto"
                            onActivated: (v) => { if (appBridge) appBridge.aiGpuUuid = v }
                        }
                        AppComboBox {
                            objectName: "fsr-processing-device"
                            width: parent.width; label: qsTranslate("App", "AI Processing GPU (DirectX 12 / FidelityFX)")
                            model: appBridge ? appBridge.fsrAdapterChoices : []
                            currentValue: appBridge ? appBridge.fsrAdapterId : "auto"
                            onActivated: (value) => { if (appBridge) appBridge.fsrAdapterId = value }
                        }
                        AppComboBox {
                            objectName: "ffmpeg-vulkan-device"
                            width: parent.width; label: qsTranslate("App", "Decoding / Encoding Device")
                            model: appBridge ? appBridge.ffmpegDeviceChoices : [{label: qsTranslate("App", "Automatic (Best Available)"), value: "auto"}, {label: "CPU", value: "cpu"}]
                            currentValue: appBridge ? appBridge.ffmpegDevice : "auto"
                            onActivated: (v) => { if (appBridge) appBridge.ffmpegDevice = v }
                        }
                    }
                }

                AppCard {
                    width: cardColumn.width; title: qsTranslate("App", "Preview"); collapsible: false
                    Column {
                        width: parent.width; spacing: 16
                        AppSwitch { width: parent.width; label: qsTranslate("App", "Realtime Preview"); checked: appBridge ? appBridge.autoPreviewEnabled : true; enabled: appBridge ? appBridge.runtimeState === "Ready" : false; onToggled: (v) => { if (appBridge) appBridge.autoPreviewEnabled = v } }
                    }
                }

                AppCard {
                    objectName: "processingCacheCard"
                    width: cardColumn.width; title: qsTranslate("App", "Processing Cache"); collapsible: false
                    Flow {
                        width: parent.width; spacing: 16
                        AppButton {
                            objectName: "cleanProcessingCacheButton"
                            text: appBridge && appBridge.processingCacheCleaning ? qsTranslate("App", "Cleaning…") : qsTranslate("App", "Clean Cache")
                            enabled: appBridge ? appBridge.canCleanProcessingCache : false
                            onClicked: { if (appBridge) appBridge.cleanProcessingCache() }
                        }
                        Text {
                            objectName: "processingCacheSize"
                            height: 36; verticalAlignment: Text.AlignVCenter
                            text: appBridge && appBridge.processingCacheReady ? appBridge.processingCacheSizeText : qsTranslate("App", "Calculating…")
                            color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeBody
                        }
                    }
                }

                AppCard {
                    width: cardColumn.width; title: qsTranslate("App", "Maintenance & Defaults"); collapsible: false
                    Column {
                        width: parent.width; spacing: 12
                        Flow {
                            width: parent.width; spacing: 8
                            AppButton { text: qsTranslate("App", "Open Logs"); iconName: "logs_folder"; onClicked: { if (appBridge) appBridge.openFolder(appBridge.currentLogPath) } }
                            AppButton { text: qsTranslate("App", "Reset All Settings to Defaults"); iconName: "reset"; variant: "danger"; onClicked: resetDialog.open() }
                        }
                    }
                }
            }
        }
    }

    onVisibleChanged: { if (visible && appBridge) appBridge.refreshProcessingCache() }
    Timer {
        interval: 2000; repeat: true; running: root.visible && root.appBridge !== null
        onTriggered: { if (root.appBridge) root.appBridge.refreshProcessingCache() }
    }

    QQC2.Dialog {
        id: resetDialog; width: 420; x: Math.max(20, (root.width - width) / 2); y: Math.max(20, (root.height - height) / 2); modal: true
        footer: Item {
            implicitHeight: 48
            Row {
                anchors.right: parent.right; anchors.rightMargin: 20
                anchors.verticalCenter: parent.verticalCenter; spacing: 8
                AppButton { text: qsTranslate("App", "No"); onClicked: resetDialog.reject() }
                AppButton { text: qsTranslate("App", "Yes"); variant: "danger"; onClicked: resetDialog.accept() }
            }
        }
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 12
        spacing: 8
        // Borderless custom header: the default Dialog title chrome draws
        // its own light separator line under the title.
        header: QQC2.Label {
            text: qsTranslate("App", "Reset all settings?")
            color: Theme.textPrimary; font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeTitle; font.weight: Font.Bold
            leftPadding: 20; rightPadding: 20; topPadding: 16; bottomPadding: 4
        }
        contentItem: Text { width: 360; wrapMode: Text.Wrap; text: qsTranslate("App", "This restores every processing setting and clears the custom mask. This cannot be undone unless you exported a preset."); color: Theme.textPrimary; font.family: Theme.fontFamily }
        // Borderless: the previous danger-colored border read as a light
        // outline around the whole box against the dark surface.
        background: Rectangle { color: Theme.bgSurface; border.width: 0; radius: Theme.radiusLarge }
        onAccepted: { if (appBridge) appBridge.resetToDefaults() }
    }
}
