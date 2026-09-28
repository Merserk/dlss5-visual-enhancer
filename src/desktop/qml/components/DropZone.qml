import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

Rectangle {
    id: root

    property var appBridge: null
    property string mediaKind: "image/video" // "image", "video", "image/video"
    property bool emptyState: true

    // The drop target stays over a loaded preview; only the empty-state
    // picker and prompt disappear after an input has been selected.
    color: dropArea.containsDrag ? (emptyState ? Theme.bgSelected : "#220387C2") : "transparent"
    border.color: !emptyState && dropArea.containsDrag ? Theme.accent : "transparent"
    border.width: 1

    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    DropArea {
        id: dropArea
        objectName: "mediaDropArea"
        anchors.fill: parent
        onDropped: (drop) => {
            if (drop.hasUrls && appBridge) {
                var urls = []
                for (var i = 0; i < drop.urls.length; i++) {
                    urls.push(drop.urls[i].toString())
                }
                appBridge.addFiles(urls)
            }
        }
    }

    MouseArea {
        id: clickArea
        anchors.fill: parent
        visible: root.emptyState
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: fileDialog.open()
    }

    Column {
        anchors.centerIn: parent
        visible: root.emptyState
        spacing: 16
        width: Math.min(parent.width - 40, 480)

        AppIcon {
            anchors.horizontalCenter: parent.horizontalCenter
            iconName: "inbox_import"
            iconSize: 48
            color: dropArea.containsDrag ? Theme.accent : Theme.textSecondary
        }

        // Main prompt
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            text: qsTranslate("App", "Drag and drop media files here")
            font.family: Theme.fontFamily
            font.pixelSize: Theme.fontSizeTitle
            font.weight: Font.DemiBold
            color: Theme.textPrimary
        }

        // Action Buttons
        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 12

            AppButton {
                text: qsTranslate("App", "Choose Files...")
                iconName: "add_file"
                variant: "secondary"
                onClicked: fileDialog.open()
            }

            AppButton {
                text: qsTranslate("App", "Choose Folder...")
                iconName: "add_folder"
                variant: "secondary"
                onClicked: folderDialog.open()
            }
        }
    }

    FileDialog {
        id: fileDialog
        title: qsTranslate("App", "Select Input Media")
        fileMode: FileDialog.OpenFiles
        nameFilters: [
            qsTranslate("App", "All Media Files (*.mp4 *.mkv *.mov *.avi *.webm *.png *.jpg *.jpeg *.webp *.avif *.tiff *.bmp *.cr2 *.nef *.arw *.dng *.heic)"),
            qsTranslate("App", "Video Files (*.mp4 *.mkv *.mov *.avi *.webm)"),
            qsTranslate("App", "Image Files (*.png *.jpg *.jpeg *.webp *.avif *.tiff *.bmp *.cr2 *.nef *.arw *.dng *.heic)"),
            qsTranslate("App", "All Files (*.*)")
        ]
        onAccepted: {
            if (appBridge && selectedFiles.length > 0) {
                var urls = []
                for (var i = 0; i < selectedFiles.length; i++) {
                    urls.push(selectedFiles[i].toString())
                }
                appBridge.addFiles(urls)
            }
        }
    }

    FolderDialog {
        id: folderDialog
        title: qsTranslate("App", "Select Folder Containing Media")
        onAccepted: {
            if (appBridge) {
                var folderPath = selectedFolder.toString()
                appBridge.addFiles([folderPath])
            }
        }
    }
}
