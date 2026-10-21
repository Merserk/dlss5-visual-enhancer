pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Dialogs
import ".."
import "../controls"

Column {
    id: root
    objectName: "reshade-controls"
    property var appBridge: null
    readonly property bool isImage: appBridge ? appBridge.nrMode === "Image" : true
    readonly property var values: appBridge ? appBridge.reshadeSettings : ({})
    readonly property var presetInfo: appBridge ? appBridge.reshadePresetInfo : ({})
    readonly property var techniques: presetInfo.techniques || []
    readonly property var parameters: presetInfo.parameters || []
    readonly property bool checking: !!presetInfo.loading
    readonly property bool hasDepthActive: (values.active_depth_mode || "Auto") !== "Off"
        && ((values.active_depth_mode || "Auto") === "AI Depth" || !!presetInfo.requires_depth)
    readonly property var overrides: parseOverrides(values.active_parameter_overrides || "")
    readonly property var groups: effectGroups()
    property var expandedEffects: ({})
    property var savedEffectTechniques: ({})
    readonly property string effectSelectionContext: (isImage ? "image:" : "video:") + (values.active_preset_path || "")
    onEffectSelectionContextChanged: savedEffectTechniques = ({})
    property bool advanced: false
    property bool showInternal: false
    property string search: ""
    width: parent ? parent.width : 320
    spacing: 12

    Connections {
        target: root.appBridge
        function onReshadeParametersReset() { root.savedEffectTechniques = ({}) }
    }

    function resetToPreset() {
        if (!appBridge || checking) return
        // Finish any pending field edit before the controller discards overrides.
        root.forceActiveFocus(Qt.MouseFocusReason)
        appBridge.resetReShadeParameters()
    }

    function setSetting(key, value) { if (appBridge) appBridge.setReShadeSetting(key, value) }
    function parseOverrides(raw) {
        try { if (raw.trim().charAt(0) === "{") return JSON.parse(raw) } catch (e) {}
        var values = {}
        raw.split("\n").forEach(function(line) {
            var eq = line.indexOf("=")
            if (eq > 0) values[line.slice(0, eq).trim()] = line.slice(eq + 1).trim()
        })
        return values
    }
    function currentValue(parameter) { return overrides[parameter.key] === undefined ? parameter.current_value : overrides[parameter.key] }
    function effectGroups() {
        var result = [], indices = {}
        techniques.forEach(function(technique, index) {
            var effect = technique.effect_file || technique.effect || technique.name
            if (indices[effect] === undefined) {
                indices[effect] = result.length
                result.push({effect: effect, techniques: [], parameters: [], enabled: 0})
            }
            var group = result[indices[effect]]
            group.techniques.push({technique: technique, index: index})
            if (technique.enabled) group.enabled++
        })
        parameters.forEach(function(parameter) {
            var index = indices[parameter.effect_file || parameter.effect]
            if (index !== undefined) result[index].parameters.push(parameter)
        })
        return result
    }
    function matches(parameter) {
        if (parameter.hidden && !showInternal) return false
        var query = search.toLowerCase().trim()
        return !query || (parameter.ui_label + " " + parameter.name + " " + parameter.effect_file).toLowerCase().indexOf(query) >= 0
    }
    function groupMatches(group) {
        var query = search.toLowerCase().trim()
        return !query || group.effect.toLowerCase().indexOf(query) >= 0 || group.parameters.some(matches)
            || group.techniques.some(function(t) { return t.technique.name.toLowerCase().indexOf(query) >= 0 })
    }
    function toggleEffect(effect) {
        var next = Object.assign({}, expandedEffects)
        next[effect] = !next[effect]
        expandedEffects = next
    }
    function techniqueName(technique) { return technique.qualified || technique.name }
    function setEffectEnabled(group, enabled) {
        if (!appBridge || checking) return
        var names = group.techniques.map(function(t) { return techniqueName(t.technique) })
        var selected = (savedEffectTechniques[group.effect] || []).filter(function(name) { return names.indexOf(name) >= 0 })
        if (!enabled) {
            let saved = Object.assign({}, savedEffectTechniques)
            saved[group.effect] = group.techniques.filter(function(t) { return t.technique.enabled })
                .map(function(t) { return techniqueName(t.technique) })
            savedEffectTechniques = saved
        } else if (!selected.length && names.length) {
            // Start one technique for an unused effect; variants such as DOF
            // should not all be stacked by a single master switch.
            selected = [names[0]]
        }
        var next = Object.assign({}, overrides)
        next.Techniques = techniques.filter(function(t) {
            return (t.effect_file || t.effect || t.name) === group.effect
                ? enabled && selected.indexOf(techniqueName(t)) >= 0 : !!t.enabled
        }).map(techniqueName).join(",")
        setSetting("parameter_overrides", JSON.stringify(next))
    }
    AppFilePicker {
        objectName: "reshadePresetPath"
        width: parent.width
        label: qsTranslate("App", "ReShade Preset (.ini)")
        placeholderText: qsTranslate("App", "Choose a ReShade .ini preset")
        selectedPath: root.values.active_preset_path || ""
        nameFilters: [qsTranslate("App", "ReShade Presets (*.ini)"), qsTranslate("App", "All Files (*.*)")]
        onPathChanged: path => root.setSetting("preset_path", path)
    }
    Row {
        width: parent.width
        spacing: 8
        AppButton {
            objectName: "reshadeReloadPreset"
            width: (parent.width - 8) / 2
            buttonHeight: 28
            text: qsTranslate("App", "Reload Preset")
            iconName: "reset"
            enabled: !!root.values.active_preset_path && !root.checking
            onClicked: if (root.appBridge) root.appBridge.reloadReShadePreset()
        }
        AppButton {
            objectName: "reshadeSavePresetCopy"
            width: (parent.width - 8) / 2
            buttonHeight: 28
            text: qsTranslate("App", "Save Preset Copy")
            iconName: "export"
            enabled: !!root.values.active_preset_path && !root.checking && root.presetInfo.valid !== false
            onClicked: savePresetDialog.open()
        }
    }
    FileDialog {
        id: savePresetDialog
        title: qsTranslate("App", "Save ReShade Preset Copy")
        fileMode: FileDialog.SaveFile
        nameFilters: [qsTranslate("App", "ReShade Presets (*.ini)")]
        onAccepted: if (root.appBridge) root.appBridge.saveReShadePresetCopy(selectedFile.toString())
    }
    Rectangle {
        width: parent.width
        visible: !root.checking && root.presetInfo.valid === false && !!root.presetInfo.error
        height: statusText.implicitHeight + 18
        radius: Theme.radiusMedium
        color: Theme.dangerBg
        Text {
            id: statusText
            objectName: "reshadePresetErrorText"
            x: 9; y: 9; width: parent.width - 18
            wrapMode: Text.WordWrap
            text: root.checking ? "" : root.presetInfo.error || ""
            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall
            color: Theme.danger
        }
    }
    AppSlider {
        objectName: "reshadeMixSlider"
        width: parent.width
        label: qsTranslate("App", "Effect Mix")
        from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 100
        value: root.values.active_mix === undefined ? 100 : root.values.active_mix
        onValueModified: value => root.setSetting("mix", Math.round(value))
    }
    AppComboBox {
        objectName: "reshadeDepthMode"
        width: parent.width
        label: qsTranslate("App", "Depth Input") + (root.checking ? "" : " ("
               + (root.hasDepthActive ? qsTranslate("App", "Enabled") : qsTranslate("App", "Disabled")) + ")")
        model: [{label: qsTranslate("App", "Auto"), value: "Auto"},
                {label: qsTranslate("App", "Always Generate AI Depth"), value: "AI Depth"},
                {label: qsTranslate("App", "Disabled"), value: "Off"}]
        currentValue: root.values.active_depth_mode || "Auto"
        onActivated: value => root.setSetting("depth_mode", value)
    }
    AppComboBox {
        objectName: "reshadeMotionMode"
        width: parent.width
        visible: !root.isImage
        label: qsTranslate("App", "Video Motion & Depth Stabilization")
        model: [{label: qsTranslate("App", "Auto"), value: "Auto"},
                {label: qsTranslate("App", "Optical Flow"), value: "Quality"},
                {label: qsTranslate("App", "Off (Per-Frame Depth)"), value: "Spatial Only (Off)"}]
        currentValue: root.values.active_temporal_stability || "Auto"
        onActivated: value => root.setSetting("temporal_stability", value)
    }
    Text {
        width: parent.width; wrapMode: Text.WordWrap
        visible: !root.checking && !!root.presetInfo.requires_depth && root.values.active_depth_mode === "Off"
        text: qsTranslate("App", "This preset requires depth. Select Auto or Always to process it.")
        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textMuted
    }
    Row {
        width: parent.width; visible: !!root.values.active_preset_path
        Text {
            width: parent.width - resetButton.width
            anchors.verticalCenter: parent.verticalCenter
            text: qsTranslate("App", "Techniques & Settings")
            font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel; font.weight: Font.DemiBold; color: Theme.textPrimary
        }
        AppButton {
            id: resetButton
            objectName: "reshadeResetParameters"
            width: Math.min(implicitWidth, parent.width * 0.45); buttonHeight: 25
            text: qsTranslate("App", "Reset Values")
            enabled: !root.checking
            onClicked: root.resetToPreset()
        }
    }
    AppTextField {
        objectName: "reshadeParameterSearch"
        width: parent.width; visible: root.techniques.length > 0
        placeholderText: qsTranslate("App", "Search effects and parameters…")
        text: root.search
        onTextEdited: value => root.search = value
    }
    Repeater {
        model: root.groups
        delegate: Rectangle {
            id: effectCard
            required property var modelData
            readonly property var group: modelData
            objectName: "reshade-effect-" + group.effect
            readonly property bool expanded: !!root.expandedEffects[group.effect] || root.search.trim().length > 0
            width: root.width
            visible: root.groupMatches(group)
            height: visible ? groupContents.height + (expanded ? 12 : 0) : 0
            radius: Theme.radiusMedium; color: Theme.bgInput; border.width: 1
            border.color: expanded || headerMouse.containsMouse || effectHeader.activeFocus ? Theme.borderActive : Theme.borderSubtle
            Column {
                id: groupContents
                width: parent.width; spacing: 12
                Item {
                    id: effectHeader
                    objectName: "reshade-effect-header-" + effectCard.group.effect
                    width: parent.width; height: 44
                    activeFocusOnTab: true
                    Accessible.role: Accessible.Button
                    Accessible.name: effectCard.group.effect.replace(/\.fx$/i, "")
                    Keys.onPressed: event => {
                        if (event.key === Qt.Key_Space || event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
                            root.toggleEffect(effectCard.group.effect)
                            event.accepted = true
                        }
                    }
                    Rectangle {
                        x: 1; y: 1; width: parent.width - 2; height: parent.height - 2
                        radius: Theme.radiusMedium - 1
                        color: headerMouse.pressed ? Theme.bgPressed : headerMouse.containsMouse ? Theme.bgInputHover : "transparent"
                    }
                    MouseArea {
                        id: headerMouse
                        anchors.fill: parent; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                            effectHeader.forceActiveFocus()
                            root.toggleEffect(effectCard.group.effect)
                        }
                    }
                    AppIcon {
                        x: 12; anchors.verticalCenter: parent.verticalCenter
                        iconName: effectCard.expanded ? "chevron_down" : "chevron_right"
                        iconSize: 14; color: Theme.textSecondary
                    }
                    Text {
                        x: 36; width: effectSwitch.x - x - 12
                        anchors.verticalCenter: parent.verticalCenter
                        text: effectCard.group.effect.replace(/\.fx$/i, "")
                        elide: Text.ElideRight
                        font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeLabel
                        color: Theme.textPrimary
                    }
                    AppSwitch {
                        id: effectSwitch
                        objectName: "reshade-effect-switch-" + effectCard.group.effect
                        x: parent.width - width - 12; width: 38; height: parent.height
                        checked: effectCard.group.enabled > 0
                        enabled: !root.checking
                        Accessible.role: Accessible.CheckBox
                        Accessible.name: effectCard.group.effect.replace(/\.fx$/i, "")
                        Accessible.checked: checked
                        onToggled: value => root.setEffectEnabled(effectCard.group, value)
                    }
                }
                Loader {
                    x: 12; width: parent.width - 24; active: effectCard.expanded
                    height: active && item ? (item as Item).implicitHeight : 0
                    sourceComponent: Column {
                        width: groupContents.width - 24; spacing: 12
                        Repeater {
                            model: effectCard.group.techniques
                            delegate: Row {
                                id: techniqueRow
                                required property var modelData
                                width: parent.width; spacing: 4
                                AppSwitch {
                                    width: parent.width - 56
                                    label: techniqueRow.modelData.technique.name
                                    checked: !!techniqueRow.modelData.technique.enabled
                                    enabled: !root.checking
                                    onToggled: value => { if (root.appBridge) root.appBridge.setReShadeTechniqueEnabled(techniqueRow.modelData.technique.qualified || techniqueRow.modelData.technique.name, value) }
                                }
                                AppIconButton {
                                    iconName: "chevron_up"; buttonSize: 24
                                    enabled: !root.checking && techniqueRow.modelData.index > 0
                                    onClicked: if (root.appBridge) root.appBridge.moveReShadeTechnique(techniqueRow.modelData.index, techniqueRow.modelData.index - 1)
                                }
                                AppIconButton {
                                    iconName: "chevron_down"; buttonSize: 24
                                    enabled: !root.checking && techniqueRow.modelData.index + 1 < root.techniques.length
                                    onClicked: if (root.appBridge) root.appBridge.moveReShadeTechnique(techniqueRow.modelData.index, techniqueRow.modelData.index + 1)
                                }
                            }
                        }
                        Text {
                            width: parent.width; visible: effectCard.group.enabled === 0
                            text: /fish.?eye/i.test(effectCard.group.effect)
                                  ? qsTranslate("App", "Fisheye distortion is off by default. Enable the technique to apply it.")
                                  : qsTranslate("App", "Enable a technique above to apply this effect.")
                            wrapMode: Text.WordWrap; font.family: Theme.fontFamily; font.pixelSize: Theme.fontSizeSmall; color: Theme.textMuted
                        }
                        Repeater {
                            model: effectCard.group.parameters.filter(root.matches)
                            delegate: ReShadeParameter {
                                required property var modelData
                                width: parent.width; parameter: modelData
                                currentValue: root.currentValue(modelData)
                                enabled: !root.checking
                                onValueEdited: value => { if (root.appBridge) root.appBridge.setReShadeParameter(modelData.key, value) }
                            }
                        }
                    }
                }
            }
        }
    }
    AppButton {
        objectName: "reshadeAdvancedToggle"
        width: parent.width; buttonHeight: 28; variant: "ghost"
        text: qsTranslate("App", "Advanced")
        iconName: root.advanced ? "chevron_down" : "chevron_right"
        onClicked: root.advanced = !root.advanced
    }
    Loader {
        width: parent.width; active: root.advanced
        height: active && item ? (item as Item).implicitHeight : 0
        sourceComponent: Column {
            width: root.width; spacing: 12
            AppFilePicker {
                objectName: "reshadeShadersPicker"
                width: parent.width; selectFolder: true
                label: qsTranslate("App", "Shader Search Folder (.fx / .fxh)")
                placeholderText: qsTranslate("App", "Automatic (Preset & Runtime Shaders)")
                selectedPath: root.values.active_shader_search_paths || ""
                onPathChanged: path => root.setSetting("shader_search_paths", path)
            }
            AppFilePicker {
                objectName: "reshadeTexturesPicker"
                width: parent.width; selectFolder: true
                label: qsTranslate("App", "Texture & LUT Search Folder")
                placeholderText: qsTranslate("App", "Automatic (Preset & Runtime Textures)")
                selectedPath: root.values.active_texture_search_paths || ""
                onPathChanged: path => root.setSetting("texture_search_paths", path)
            }
            AppSlider {
                objectName: "reshadeTemporalBlend"
                width: parent.width
                visible: !root.isImage && root.hasDepthActive && root.values.active_temporal_stability !== "Spatial Only (Off)"
                label: qsTranslate("App", "Temporal Depth Blend")
                from: 0; to: 100; stepSize: 1; precision: 0; defaultValue: 75
                value: root.values.active_temporal_strength === undefined ? 75 : root.values.active_temporal_strength
                onValueModified: value => root.setSetting("temporal_strength", Math.round(value))
            }
            AppComboBox {
                objectName: "reshadeDepthDetail"
                width: parent.width; label: qsTranslate("App", "Depth Detail")
                model: [{label: qsTranslate("App", "Fast (280)"), value: 280}, {label: qsTranslate("App", "Standard (504)"), value: 504}, {label: qsTranslate("App", "High (756)"), value: 756}, {label: qsTranslate("App", "Maximum (1008)"), value: 1008}]
                currentValue: root.values.active_depth_resolution || 1008
                onActivated: value => root.setSetting("depth_resolution", value)
            }
            AppComboBox {
                objectName: "reshadeEffectPrecision"
                width: parent.width; label: qsTranslate("App", "Effect Precision")
                model: [{label: qsTranslate("App", "Auto"), value: "Auto"}, {label: "FP16", value: "FP16"},
                        {label: qsTranslate("App", "8-bit sRGB Compatibility"), value: "8-bit sRGB Compatibility"}]
                currentValue: root.values.active_precision_policy || "Auto"
                onActivated: value => root.setSetting("precision_policy", value)
            }
            AppComboBox {
                objectName: "reshadeHdrHandling"
                width: parent.width; label: qsTranslate("App", "HDR Handling")
                model: [{label: qsTranslate("App", "Auto (SDR Preset Adapter)"), value: "Auto"}, {label: qsTranslate("App", "HDR-Aware Preset"), value: "HDR Aware"}, {label: qsTranslate("App", "SDR Adapter"), value: "SDR Adapter"}, {label: qsTranslate("App", "Reject Unsupported"), value: "Reject Unsupported"}]
                currentValue: root.values.active_hdr_policy || "Auto"
                onActivated: value => root.setSetting("hdr_policy", value)
            }
            AppSwitch {
                width: parent.width; label: qsTranslate("App", "Preserve Source Transparency")
                checked: root.values.active_preserve_alpha !== false
                onToggled: value => root.setSetting("preserve_alpha", value)
            }
            AppSwitch {
                width: parent.width; label: qsTranslate("App", "Show Internal Shader Parameters")
                checked: root.showInternal
                onToggled: value => root.showInternal = value
            }
        }
    }
}
