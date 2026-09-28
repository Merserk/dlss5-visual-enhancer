import QtQuick
import QtQuick.Dialogs
import ".."

Item {
    id: control
    property string label: ""
    property string placeholderText: ""
    property string selectedPath: ""
    property bool selectFolder: false
    property bool enabled: true
    property var nameFilters: [qsTranslate("App", "All Files (*.*)")]
    signal pathChanged(string path)

    implicitWidth: 260
    implicitHeight: label !== "" ? (Theme.controlHeight + 20) : Theme.controlHeight
    opacity: enabled ? 1.0 : 0.45

    function syncPath() {
        if (!pathInput.activeFocus || pathInput.text !== control.selectedPath)
            pathInput.text = control.selectedPath
    }
    onSelectedPathChanged: syncPath()
    Component.onCompleted: syncPath()

    Text {
        visible: control.label !== ""
        anchors.top: parent.top; anchors.left: parent.left
        text: control.label
        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel
        color: Theme.textSecondary
    }

    Row {
        anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right
        height: Theme.controlHeight; spacing: 6
        Rectangle {
            width: parent.width - browseBtn.width - 6; height: parent.height
            radius: Theme.radiusMedium; color: Theme.bgInput
            border.color: pathInput.activeFocus ? Theme.accent : Theme.borderDefault; border.width: 1
            clip: true
            TextInput {
                id: pathInput
                anchors.left: parent.left; anchors.leftMargin: 8
                anchors.right: clearBtn.visible ? clearBtn.left : parent.right; anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                enabled: control.enabled
                font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; color: Theme.textPrimary
                selectByMouse: true; selectionColor: Theme.accent; clip: true
                onEditingFinished: { control.pathChanged(text); control.syncPath() }
                onAccepted: { control.pathChanged(text); control.syncPath() }
                Text {
                    anchors.fill: parent; visible: pathInput.text === "" && !pathInput.activeFocus
                    text: control.placeholderText; font.family: Theme.fontFamily
                    font.pixelSize: Theme.fontSizeLabel; color: Theme.textMuted
                }
            }
            AppIcon {
                id: clearBtn; visible: pathInput.text !== "" && control.enabled
                anchors.right: parent.right; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter
                iconName: "close"; iconSize: 14; color: clearMouse.containsMouse ? Theme.textPrimary : Theme.textMuted
                MouseArea {
                    id: clearMouse; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: { pathInput.text = ""; control.pathChanged("") }
                }
            }
        }
        AppButton {
            id: browseBtn; text: qsTranslate("App", "Browse"); iconName: "browse_folder"; width: 88; buttonHeight: Theme.controlHeight; enabled: control.enabled
            onClicked: control.selectFolder ? folderDialog.open() : fileDialog.open()
        }
    }

    FolderDialog {
        id: folderDialog
        title: qsTranslate("App", "Select Folder")
        onAccepted: {
            // NOTE: QML url has no toLocalFile(); pass the URL string and
            // let Python _clean_path() convert via QUrl.toLocalFile().
            var s = selectedFolder.toString()
            pathInput.text = s
            control.pathChanged(s)
        }
    }
    FileDialog {
        id: fileDialog
        title: qsTranslate("App", "Select File")
        fileMode: FileDialog.OpenFile
        nameFilters: control.nameFilters
        onAccepted: {
            var u = selectedFile
            if ((u === undefined || u === null) && selectedFiles !== undefined && selectedFiles.length > 0)
                u = selectedFiles[0]
            if ((u === undefined || u === null) && currentFile !== undefined)
                u = currentFile
            if (u === undefined || u === null)
                return
            var s = u.toString()
            if (!s)
                return
            pathInput.text = s
            control.pathChanged(s)
        }
    }
}
