pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Dialogs
import QtQuick.Controls as QQC2
import "../controls"

Column {
    id: root
    objectName: "reshade-parameter-editor"
    required property var parameter
    property var currentValue
    readonly property var shownValue: currentValue
    readonly property string title: parameter.ui_label || parameter.name
    readonly property bool scalar: (parameter.components || 1) === 1
    readonly property bool integer: parameter.type === "int" || parameter.type === "uint"
    readonly property bool booleanValue: parameter.type === "bool" && scalar
    readonly property bool choiceValue: scalar && (parameter.choices || []).length > 0
    readonly property bool sliderValue: scalar && (parameter.ui_type === "slider" || parameter.ui_type === "drag") && parameter.max > parameter.min
    signal valueEdited(var value)
    spacing: 4

    function valueText(value) {
        if (Array.isArray(value)) return value.join(",")
        if (typeof value === "boolean") return value ? "1" : "0"
        return value === undefined ? "" : String(value)
    }
    // The controller already debounces preview rendering and saving. Commit
    // edits immediately so collapsing an editor cannot discard its last value.
    function commit(value) { valueEdited(value) }
    HoverHandler { id: hover }
    QQC2.ToolTip.visible: hover.hovered && !!parameter.ui_tooltip
    QQC2.ToolTip.text: parameter.ui_tooltip || ""
    QQC2.ToolTip.delay: 650
    Loader {
        width: parent.width
        sourceComponent: root.booleanValue ? booleanEditor : root.choiceValue ? choiceEditor
                         : root.sliderValue ? sliderEditor : textEditor
    }
    Component {
        id: booleanEditor
        AppSwitch {
            width: root.width
            label: root.title
            checked: root.shownValue === true || root.shownValue === "true" || Number(root.shownValue) !== 0 && isFinite(Number(root.shownValue))
            onToggled: value => root.commit(value ? 1 : 0)
        }
    }
    Component {
        id: choiceEditor
        AppComboBox {
            width: root.width
            label: root.title
            model: (root.parameter.choices || []).map(function(label, index) { return {label: label, value: index} })
            currentValue: Number(root.shownValue)
            onActivated: value => root.commit(value)
        }
    }
    Component {
        id: sliderEditor
        AppSlider {
            width: root.width
            label: root.title
            from: root.parameter.min; to: root.parameter.max
            precision: root.integer ? 0 : 4
            stepSize: root.integer ? Math.max(1, root.parameter.step || 1) : root.parameter.step || 0.001
            defaultValue: Number(root.parameter.default_value) || 0
            value: Number(root.shownValue) || 0
            onValueModified: value => root.commit(root.integer ? Math.round(value) : value)
        }
    }
    Component {
        id: textEditor
        Row {
            width: root.width
            spacing: 6
            AppTextField {
                width: parent.width - (colorButton.visible ? colorButton.width + 6 : 0)
                label: root.title
                commitOnEveryEdit: false
                text: root.valueText(root.shownValue)
                onTextEdited: value => root.commit(value)
            }
            AppButton {
                id: colorButton
                visible: root.parameter.ui_type === "color" && (root.parameter.components || 1) >= 3
                width: 48
                anchors.bottom: parent.bottom
                text: qsTranslate("App", "Color")
                onClicked: {
                    var v = root.valueText(root.shownValue).split(",").map(Number)
                    colorDialog.selectedColor = Qt.rgba(v[0] || 0, v[1] || 0, v[2] || 0, v.length > 3 ? v[3] : 1)
                    colorDialog.open()
                }
            }
        }
    }
    ColorDialog {
        id: colorDialog
        title: root.title
        options: (root.parameter.components || 1) === 4 ? ColorDialog.ShowAlphaChannel : 0
        onAccepted: {
            var values = [selectedColor.r, selectedColor.g, selectedColor.b]
            if ((root.parameter.components || 1) === 4) values.push(selectedColor.a)
            root.commit(values.join(","))
        }
    }
}
