import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Dialogs
import ".."
import "../controls"

QQC2.Dialog {
    id: exportDialog
    property var appBridge: null
    property string contextKey: ""
    property string destinationMode: "output"
    property string selectedFolder: ""
    property string renameMode: "Auto"
    property string customSuffix: ""
    property string errorMessage: ""

    function openForContext(key) {
        if (!appBridge) return
        contextKey = key
        destinationMode = appBridge.exportDestinationMode ? appBridge.exportDestinationMode : "output"
        selectedFolder = appBridge.exportFolder || ""
        errorMessage = ""
        if (key === "nr-image") {
            renameMode = appBridge.imageRenameMode
            customSuffix = appBridge.imageCustomSuffix
        } else if (key === "nr-video") {
            renameMode = appBridge.videoRenameMode
            customSuffix = appBridge.videoCustomSuffix
        } else if (key === "upscale-image") {
            renameMode = appBridge.upscaleImageRenameMode
            customSuffix = appBridge.upscaleImageCustomSuffix
        } else if (key === "upscale-video") {
            renameMode = appBridge.upscaleRenameMode
            customSuffix = appBridge.upscaleCustomSuffix
        } else if (key === "fi-video") {
            renameMode = appBridge.fiRenameMode
            customSuffix = appBridge.fiCustomSuffix
        } else {
            return
        }
        open()
    }

    parent: QQC2.Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(560, parent ? parent.width - 40 : 560)
    z: 20000
    focus: true
    modal: true
    closePolicy: QQC2.Popup.CloseOnEscape
    leftPadding: 20
    rightPadding: 20
    topPadding: 12
    bottomPadding: 20

    header: QQC2.Label {
        text: "Export"
        color: Theme.textPrimary
        font.family: Theme.fontFamily
        font.pixelSize: Theme.fontSizeTitle
        font.weight: Font.Bold
        leftPadding: 20
        rightPadding: 20
        topPadding: 16
        bottomPadding: 4
    }
    background: Rectangle { color: Theme.bgSurface; border.width: 0; radius: Theme.radiusLarge }

    contentItem: Column {
        width: exportDialog.width - exportDialog.leftPadding - exportDialog.rightPadding
        spacing: 12

        AppSegmentedControl {
            width: parent.width
            label: "Export Path"
            model: [
                { label: "Same as Input", value: "input" },
                { label: "Output", value: "output" },
                { label: "Select Folder", value: "folder" }
            ]
            currentValue: exportDialog.destinationMode
            onActivated: (value) => { exportDialog.destinationMode = value; exportDialog.errorMessage = "" }
        }

        Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: exportDialog.destinationMode === "input"
                  ? "Save each result beside its source file."
                  : (exportDialog.destinationMode === "output"
                     ? "Save results in the program's outputs folder."
                     : "Choose the folder where results will be saved.")
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeLabel
            color: Theme.textSecondary
        }

        Column {
            width: parent.width
            spacing: 6
            visible: exportDialog.destinationMode === "folder"
            AppButton {
                text: "Choose Folder..."
                iconName: "browse_folder"
                onClicked: folderDialog.open()
            }
            Text {
                width: parent.width
                text: exportDialog.selectedFolder
                      ? decodeURIComponent(exportDialog.selectedFolder.replace(/^file:\/\/\//, ""))
                      : "No folder selected"
                elide: Text.ElideMiddle
                font.family: Theme.fontFamily
                font.pixelSize: Theme.fontSizeLabel
                color: exportDialog.selectedFolder ? Theme.textPrimary : Theme.textMuted
            }
        }

        AppSegmentedControl {
            width: parent.width
            label: "Rename Mode"
            model: exportDialog.appBridge ? exportDialog.appBridge.renameModeChoices : []
            currentValue: exportDialog.renameMode
            onActivated: (value) => { exportDialog.renameMode = value; exportDialog.errorMessage = "" }
        }

        AppTextField {
            visible: exportDialog.renameMode === "Custom"
            width: parent.width
            label: "Custom Suffix"
            text: exportDialog.customSuffix
            onTextEdited: (value) => { exportDialog.customSuffix = value; exportDialog.errorMessage = "" }
        }

        Text {
            visible: exportDialog.errorMessage !== ""
            width: parent.width
            text: exportDialog.errorMessage
            wrapMode: Text.WordWrap
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeLabel
            color: Theme.danger
        }

        Row {
            width: parent.width
            spacing: 8
            AppButton {
                text: "Cancel"
                width: (parent.width - 8) / 2
                onClicked: exportDialog.close()
            }
            AppButton {
                text: "Export / Start"
                iconName: "start_render"
                variant: "primary"
                width: (parent.width - 8) / 2
                onClicked: {
                    if (!exportDialog.appBridge) return
                    var error = exportDialog.appBridge.startActiveBatchWithExport(
                        exportDialog.contextKey,
                        exportDialog.destinationMode,
                        exportDialog.selectedFolder,
                        exportDialog.renameMode,
                        exportDialog.customSuffix)
                    if (error) exportDialog.errorMessage = error
                    else exportDialog.close()
                }
            }
        }
    }

    FolderDialog {
        id: folderDialog
        title: "Select Export Folder"
        onAccepted: {
            exportDialog.selectedFolder = selectedFolder.toString()
            exportDialog.errorMessage = ""
        }
    }
}
