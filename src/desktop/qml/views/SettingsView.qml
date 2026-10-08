import QtQuick
import QtQuick.Dialogs
import QtQuick.Controls as QQC2
import ".."
import "../controls"

Rectangle {
    id: root
    property var appBridge: null
    property string presetName: ""
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
                            model: appBridge ? appBridge.aiGpuChoices : []
                            currentValue: appBridge ? appBridge.aiGpuUuid : "auto"
                            onActivated: (v) => { if (appBridge) appBridge.aiGpuUuid = v }
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
                    width: cardColumn.width; title: qsTranslate("App", "Cache Memory (Rolling Cache)"); collapsible: false
                    Column {
                        width: parent.width; spacing: 10
                        AppComboBox {
                            objectName: "cache-mode"
                            width: parent.width; label: qsTranslate("App", "Mode")
                            model: [
                                {label: qsTranslate("App", "Native precision"), value: "Fast lossless"},
                                {label: qsTranslate("App", "Compressed RGB (8-bit)"), value: "Fast compressed"}
                            ]
                            currentValue: appBridge ? appBridge.cacheMode : "Fast lossless"
                            onActivated: (v) => { if (appBridge) appBridge.cacheMode = v }
                        }
                        Column {
                            width: parent.width; spacing: 2
                            AppSlider {
                                id: cacheSizeSlider
                                objectName: "cache-size"
                                width: parent.width; label: qsTranslate("App", "Cache size")
                                from: 5; to: 30; stepSize: 5; precision: 0; defaultValue: 10; unit: "GB"
                                showValueControls: false
                                value: appBridge ? appBridge.cacheSizeGB : 10
                                onValueModified: (v) => {
                                    if (appBridge) appBridge.cacheSizeGB = Math.round(v / 5) * 5
                                }
                            }
                            Item {
                                width: parent.width; height: 18
                                Repeater {
                                    model: [5, 10, 15, 20, 25, 30]
                                    Text {
                                        required property int index
                                        required property int modelData
                                        objectName: "cache-size-stop-" + modelData
                                        x: Math.max(0, Math.min(parent.width - width,
                                            7 + index * (parent.width - 14) / 5 - width / 2))
                                        text: modelData === 10 ? qsTranslate("App", "%1 GB (Default)").arg(modelData) : modelData + " GB"
                                        color: cacheSizeSlider.value === modelData ? Theme.accent : Theme.textMuted
                                        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
                                    }
                                }
                            }
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
                    width: cardColumn.width; title: qsTranslate("App", "Settings Presets"); collapsible: false
                    Column {
                        width: parent.width; spacing: 12
                        AppTextField { width: parent.width; placeholderText: qsTranslate("App", "Preset name (e.g. 4K Master)..."); text: root.presetName; onTextEdited: (t) => root.presetName = t }
                        Flow {
                            width: parent.width
                            spacing: 8
                            AppButton {
                                text: qsTranslate("App", "Export Preset...")
                                iconName: "export"
                                onClicked: {
                                    if (root.presetName.trim().length === 0) { if (appBridge) appBridge.exportPreset(""); return }
                                    presetSaveDialog.open()
                                }
                            }
                            AppButton { text: qsTranslate("App", "Import..."); iconName: "import"; onClicked: presetImportDialog.open() }
                        }
                        Text { visible: appBridge && appBridge.presetStatus !== ""; width: parent.width; wrapMode: Text.Wrap; text: appBridge ? appBridge.presetStatus : ""; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.accent }
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

    FileDialog {
        id: presetImportDialog; title: qsTranslate("App", "Import Preset JSON"); nameFilters: [qsTranslate("App", "JSON Presets (*.json)"), qsTranslate("App", "All Files (*.*)")]
        onAccepted: { if (appBridge && selectedFile) appBridge.importPreset(selectedFile.toString()) }
    }
    FileDialog {
        id: presetSaveDialog; title: qsTranslate("App", "Export Preset JSON"); fileMode: FileDialog.SaveFile; nameFilters: [qsTranslate("App", "JSON Presets (*.json)")]
        onAccepted: { if (appBridge && selectedFile) appBridge.exportPresetTo(root.presetName, selectedFile.toString()) }
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
