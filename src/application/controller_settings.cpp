#include "controller.hpp"
#include "infrastructure/paths.hpp"
#include "media/image_codecs.hpp"
#include "processing/color_analysis.hpp"
#include "processing/effects.hpp"
#include "processing/reshade_native.hpp"
#include "processing/reshade_dependencies.hpp"
#include <QDateTime>
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QMetaObject>
#include <QRegularExpression>
#include <QUuid>
#include <QtConcurrent/QtConcurrentRun>
#include <cmath>

namespace ve {
void invalidateReShadePresetCache();
namespace {
QString resolveReShadeSettingKey(const QString& rawKey, bool imageMode) {
    QString base = rawKey;
    if (base.startsWith("reshade_image_"))
        return base;
    if (base.startsWith("reshade_"))
        base = base.mid(8);
    if (base == "effect_search_paths")
        base = "shader_search_paths";
    if (base == "temporal_stability" || base == "temporal_strength")
        return "reshade_" + base;
    return (imageMode ? QStringLiteral("reshade_image_") : QStringLiteral("reshade_")) + base;
}
QString normalizePathString(const QVariant& value) {
    QStringList rawList;
    if (value.typeId() == QMetaType::QStringList || value.typeId() == QMetaType::QVariantList)
        rawList = value.toStringList();
    else
        rawList = value.toString().split(';', Qt::SkipEmptyParts);
    QStringList cleaned;
    for (const auto& entry : rawList) {
        const auto p = localPath(entry.trimmed());
        if (!p.isEmpty() && !cleaned.contains(p, Qt::CaseInsensitive))
            cleaned.append(p);
    }
    return cleaned.join(';');
}
void upsertReShadeOverrideLine(QString& overridesText, const QString& key, const QString& value) {
    QJsonObject obj;
    const auto trimmed = overridesText.trimmed();
    if (trimmed.startsWith('{')) {
        QJsonParseError err{};
        const auto doc = QJsonDocument::fromJson(trimmed.toUtf8(), &err);
        if (err.error == QJsonParseError::NoError && doc.isObject())
            obj = doc.object();
    } else if (!trimmed.isEmpty()) {
        for (const auto& rawLine : trimmed.split('\n', Qt::SkipEmptyParts)) {
            const auto line = rawLine.trimmed();
            const int eq = line.indexOf('=');
            if (eq > 0)
                obj.insert(line.left(eq).trimmed(), line.mid(eq + 1).trimmed());
        }
    }
    obj.insert(key, value);
    overridesText = QString::fromUtf8(QJsonDocument(obj).toJson(QJsonDocument::Compact));
}
} // namespace
void AppController::initializeReShadeImageSettings() {
    if (nrMode_ == "Image" && !settings_.reshade_image_configured) {
        const auto active = reshadeSettingsForMode(settings_, false);
        settings_.reshade_image_preset_path = active.reshade_preset_path;
        settings_.reshade_image_shader_search_paths = active.reshade_shader_search_paths;
        settings_.reshade_image_texture_search_paths = active.reshade_texture_search_paths;
        settings_.reshade_image_parameter_overrides = active.reshade_parameter_overrides;
        settings_.reshade_image_mix = active.reshade_mix;
        settings_.reshade_image_depth_mode = active.reshade_depth_mode;
        settings_.reshade_image_depth_resolution = active.reshade_depth_resolution;
        settings_.reshade_image_precision_policy = active.reshade_precision_policy;
        settings_.reshade_image_hdr_policy = active.reshade_hdr_policy;
        settings_.reshade_image_preserve_alpha = active.reshade_preserve_alpha;
        settings_.reshade_image_deterministic_seed = active.reshade_deterministic_seed;
        settings_.reshade_image_configured = true;
    }
}
QVariant AppController::handleSettings(Action action, const QVariantList& args) {
    auto setPath = [&](const QString& key, const QString& path) {
        auto result = settings_.set(key, localPath(path));
        if (!result)
            throw std::runtime_error(result.error().toStdString());
        settingsChanged();
    };
    switch (action) {
    case Action::setRrSetting: {
        const auto key=args.value(0).toString();
        if(!key.startsWith("rr_")||!settings_.values().contains(key))throw std::runtime_error("Unknown Ray Reconstruction setting.");
        auto result=settings_.set(key,args.value(1));if(!result)throw std::runtime_error(result.error().toStdString());
        settingsChanged();return {};
    }
    case Action::setReShadeSetting: {
        const bool imageMode = nrMode_ == "Image";
        initializeReShadeImageSettings();
        const auto key = resolveReShadeSettingKey(args.value(0).toString(), imageMode);
        if (!key.startsWith("reshade_") || !settings_.values().contains(key))
            throw std::runtime_error("Unknown ReShade setting.");
        auto value = args.value(1);
        if (key.endsWith("_preset_path")) {
            auto cleaned = localPath(value.toString());
            const QFileInfo fi(cleaned);
            if (fi.isFile() && fi.fileName().compare("ReShade.ini", Qt::CaseInsensitive) == 0) {
                QSettings ini(cleaned, QSettings::IniFormat);
                const auto target = ini.value("GENERAL/PresetFiles").toString();
                if (!target.isEmpty()) {
                    const auto candidate = QDir(fi.absolutePath()).filePath(target.split(',').front().trimmed());
                    if (QFileInfo::exists(candidate)) {
                        cleaned = QDir::cleanPath(candidate);
                    }
                }
            }
            const auto previous = settings_.value(key).toString();
            value = cleaned;
            if (cleaned.compare(previous, Qt::CaseInsensitive) != 0) {
                if (key.startsWith("reshade_image_")) {
                    settings_.reshade_image_parameter_overrides.clear();
                } else {
                    settings_.reshade_parameter_overrides.clear();
                }
            }
        } else if (key.endsWith("_search_paths")) {
            value = normalizePathString(value);
        }
        auto result = settings_.set(key, value);
        if (!result)
            throw std::runtime_error(result.error().toStdString());
        settingsChanged();
        return {};
    }
    case Action::reloadReShadePreset: {
        invalidateReShadePresetCache();
        ++reshadeReloadGeneration_;
        reshadeLoadedKey_.clear();
        previewStages_.clear();
        settingsChanged();
        status_ = QT_TRANSLATE_NOOP("App", "Reloaded ReShade preset.");
        emit statusMessageChanged();
        return {};
    }
    case Action::saveReShadePresetCopy: {
        auto path = localPath(args.value(0).toString());
        if (path.isEmpty())
            throw std::runtime_error("Choose a destination path for the ReShade preset.");
        if (QFileInfo(path).suffix().compare("ini", Qt::CaseInsensitive) != 0)
            path += ".ini";
        ReShadeEngine::exportPresetCopy(settings_, nrMode_ == "Video", path);
        status_ = QCoreApplication::translate("App", "Saved ReShade preset to %1").arg(QFileInfo(path).fileName());
        emit statusMessageChanged();
        return path;
    }
    case Action::setReShadeParameter: {
        initializeReShadeImageSettings();
        const auto paramKey = args.value(0).toString().trimmed();
        if (paramKey.isEmpty())
            return {};
        const auto rawVal = args.value(1);
        QString formatted;
        if (rawVal.typeId() == QMetaType::Bool)
            formatted = rawVal.toBool() ? QStringLiteral("1") : QStringLiteral("0");
        else if (rawVal.typeId() == QMetaType::QVariantList || rawVal.typeId() == QMetaType::QStringList)
            formatted = rawVal.toStringList().join(',');
        else
            formatted = rawVal.toString().trimmed();
        auto& overrides =
            nrMode_ == "Image" ? settings_.reshade_image_parameter_overrides : settings_.reshade_parameter_overrides;
        upsertReShadeOverrideLine(overrides, paramKey, formatted);
        settingsChanged();
        return {};
    }
    case Action::resetReShadeParameters: {
        const auto active = reshadeSettingsForMode(settings_, nrMode_ == "Video");
        const auto defaults = ReShadeEngine::presetResetOverrides(active.reshade_preset_path);
        initializeReShadeImageSettings();
        auto& text = nrMode_ == "Image" ? settings_.reshade_image_parameter_overrides : settings_.reshade_parameter_overrides;
        text = defaults;
        invalidateReShadePresetCache();
        ++reshadeReloadGeneration_;
        reshadeLoadedKey_.clear();
        previewStages_.clear();
        settingsChanged();
        emit reshadeParametersReset();
        return {};
    }
    case Action::setReShadeTechniqueEnabled: {
        initializeReShadeImageSettings();
        const auto name = args.value(0).toString().trimmed();
        const bool enabled = args.value(1).toBool();
        if (name.isEmpty())
            return {};
        const auto techniques = read(Property::reshadePresetInfo).toMap().value("techniques").toList();
        QStringList enabledQualified;
        for (const auto& item : techniques) {
            const auto t = item.toMap();
            const auto tName = t.value("name").toString();
            const auto qualified = t.value("qualified").toString().isEmpty()
                                       ? tName
                                       : t.value("qualified").toString();
            const bool isTarget = name.contains('@') ? qualified.compare(name, Qt::CaseInsensitive) == 0
                                                    : tName.compare(name, Qt::CaseInsensitive) == 0;
            const bool techEnabled = isTarget ? enabled : t.value("enabled").toBool();
            if (techEnabled)
                enabledQualified.append(qualified);
        }
        auto& overrides =
            nrMode_ == "Image" ? settings_.reshade_image_parameter_overrides : settings_.reshade_parameter_overrides;
        upsertReShadeOverrideLine(overrides, QStringLiteral("Techniques"), enabledQualified.join(','));
        settingsChanged();
        return {};
    }
    case Action::moveReShadeTechnique: {
        initializeReShadeImageSettings();
        const int from = args.value(0).toInt();
        const int to = args.value(1).toInt();
        auto techniques = read(Property::reshadePresetInfo).toMap().value("techniques").toList();
        if (from >= 0 && from < techniques.size()) {
            const int clampedTo = std::clamp(to, 0, int(techniques.size()) - 1);
            if (from != clampedTo) {
                techniques.move(from, clampedTo);
                QStringList sortingQualified;
                QStringList enabledQualified;
                for (const auto& item : techniques) {
                    const auto t = item.toMap();
                    const auto qualified = t.value("qualified").toString().isEmpty()
                                               ? t.value("name").toString()
                                               : t.value("qualified").toString();
                    sortingQualified.append(qualified);
                    if (t.value("enabled").toBool())
                        enabledQualified.append(qualified);
                }
                auto& overrides = nrMode_ == "Image" ? settings_.reshade_image_parameter_overrides
                                                     : settings_.reshade_parameter_overrides;
                upsertReShadeOverrideLine(overrides, QStringLiteral("TechniqueSorting"), sortingQualified.join(','));
                upsertReShadeOverrideLine(overrides, QStringLiteral("Techniques"), enabledQualified.join(','));
                settingsChanged();
            }
        }
        return {};
    }
    case Action::setVfxSetting: {
        const auto key=args.value(0).toString();
        if(!key.startsWith("vfx_")||!settings_.values().contains(key))throw std::runtime_error("Unknown NVIDIA VFX setting.");
        auto value=args.value(1);if(key.endsWith("_path"))value=localPath(value.toString());
        auto result=settings_.set(key,value);if(!result)throw std::runtime_error(result.error().toStdString());
        settingsChanged();return {};
    }
    case Action::setLutFile: {
        const auto path = localPath(args[0].toString());
        if (!path.isEmpty()) {
            const auto lut = CubeLut::load(path);
            (void)lut;
        }
        setPath("lut_path", path);
        return {};
    }
    case Action::setLutReferenceImage:
        setPath("lut_reference_image", args[0].toString());
        if (!settings_.lut_reference_image.isEmpty())
            analyzeLut(false, -1, true);
        return {};
    case Action::setLutAdjustment: {
        const auto key = args[0].toString();
        if (!read(Property::lutAdjustmentValues).toMap().contains(key))
            throw std::runtime_error("Unknown LUT adjustment.");
        const auto value = args[1].toDouble();
        auto result = settings_.set(key, key == "lut_exposure" ? QVariant(std::nearbyint(value * 100) / 100)
                                                               : QVariant(int(std::nearbyint(value))));
        if (!result)
            throw std::runtime_error(result.error().toStdString());
        settingsChanged();
        return {};
    }
    case Action::resetLutAdjustments: {
        const Settings defaults;
        const auto map = defaults.values();
        for (auto it = map.begin(); it != map.end(); ++it)
            if (it.key().startsWith("lut_") &&
                !QStringList{"lut_path", "lut_reference_image", "lut_resolution", "lut_mix"}.contains(
                    it.key())) {
                auto result = settings_.set(it.key(), it.value());
                (void)result;
            }
        settingsChanged();
        return {};
    }
    case Action::autoAdjustLut:
        analyzeLut(false, args.isEmpty() ? -1 : args[0].toDouble());
        return {};
    case Action::autoAdjustLutFixedFrames:
        analyzeLut(true);
        return {};
    case Action::saveLut: {
        if (lutSaving_)
            return {};
        auto path = localPath(args[0].toString());
        if (QFileInfo(path).suffix().compare("cube", Qt::CaseInsensitive) != 0)
            path += ".cube";
        lutSaving_ = true;
        lutSavedPath_.clear();
        lutSaveStatus_ = "Saving LUT…";
        emit lutSaveChanged();
        const auto settings = settings_;
        const auto control = std::make_shared<JobControl>();
        analysisJob_ = control;
        auto future = QtConcurrent::run(&pool_, [this, path, settings, control] {
            QString error;
            try {
                saveGradedLut(path, settings, *control);
            } catch (const std::exception& e) {
                error = QString::fromUtf8(e.what());
            }
            QMetaObject::invokeMethod(
                this,
                [this, path, error] {
                    lutSaving_ = false;
                    analysisJob_.reset();
                    lutSavedPath_ = error.isEmpty() ? path : QString{};
                    lutSaveStatus_ = error;
                    emit lutSaveChanged();
                    emit previewBusyChanged();
                },
                Qt::QueuedConnection);
        });
        (void)future;
        return {};
    }
    case Action::setWorkflowStageEnabled: {
        const auto stage = args[0].toString();
        auto& active = nrMode_ == "Image" ? settings_.image_enabled_stages : settings_.video_enabled_stages;
        const auto& order = nrMode_ == "Image" ? settings_.image_stage_order : settings_.video_stage_order;
        if (!order.contains(stage))
            throw std::runtime_error("Unknown processing card.");
        if (args[1].toBool()) {
            if(QStringList{"frame_generation","fsr_frame_generation","vfx_frame_generation"}.contains(stage))
                for(const auto& engine:QStringList{"frame_generation","fsr_frame_generation","vfx_frame_generation"})if(engine!=stage)active.removeAll(engine);
            if(stage=="dlss_ray_reconstruction")active.removeAll("dlss_super_resolution");
            else if(stage=="dlss_super_resolution")active.removeAll("dlss_ray_reconstruction");
            if (!active.contains(stage))
                active.append(stage);
        } else
            active.removeAll(stage);
        const auto enabled=args[1].toBool();
        if(stage=="vfx_vsr")settings_.vfx_vsr_enabled=enabled;
        else if(stage=="vfx_frame_generation")settings_.vfx_frame_generation_enabled=enabled;
        else if(stage=="vfx_upscale")settings_.vfx_upscale_enabled=enabled;
        if(QStringList{"frame_generation","fsr_frame_generation","vfx_frame_generation"}.contains(stage))settings_.vfx_frame_generation_enabled=active.contains("vfx_frame_generation");
        settingsChanged();
        return {};
    }
    case Action::moveWorkflowStage: {
        auto& order = nrMode_ == "Image" ? settings_.image_stage_order : settings_.video_stage_order;
        const int from = int(order.indexOf(args[0].toString())),
                  to = std::clamp(args[1].toInt(), 0, int(order.size()) - 1);
        if (from >= 0 && from != to) {
            order.move(from, to);
            settingsChanged();
        }
        return {};
    }
    case Action::selectCustomMask: {
        JobControl control;
        const auto path = localPath(args[0].toString());
        const auto image = decodeControlMask(path, control);
        settings_.nr_mask = {{"path", path},
                             {"pixels", image},
                             {"width", image.width()},
                             {"height", image.height()},
                             {"source_name", QFileInfo(path).fileName()},
                             {"revision", QDateTime::currentMSecsSinceEpoch()}};
        settingsChanged();
        return {};
    }
    case Action::clearCustomMask:
        settings_.nr_mask.clear();
        settingsChanged();
        return {};
    case Action::resetToDefaults:
        settings_ = Settings{};
        upscaleMode_ = settings_.upscale_mode;
        settingsChanged();
        saveSettings();
        emit upscaleModeChanged();
        presetStatus_ = QT_TRANSLATE_NOOP("App", "All settings were reset to factory defaults.");
        emit presetStatusChanged();
        return {};
    case Action::resetTabSettings: {
        const QString tab = args[0].toString();
        if (!idle())
            throw std::runtime_error("Stop the current operation before resetting settings.");
        const Settings defaults;
        const auto values = defaults.values();
        auto candidate = settings_;
        QString prefix;
        if (tab == "upscale")
            prefix = "upscale_";
        else if (tab == "frame-interpolation")
            prefix = "frame_interpolation_";
        if (prefix.isEmpty() && tab != "neural-rendering")
            return {};
        const auto applies = [&](const QString& key) {
            return !prefix.isEmpty()
                       ? key.startsWith(prefix) || (tab == "frame-interpolation" && key.startsWith("fsr_frame_generation_"))
                       : !QStringList{"ai_gpu_uuid",
                                                                  "fsr_adapter_id",
                                                                  "video_gpu_uuid",
                                                                  "ffmpeg_device",
                                                                  "container",
                                                                  "upscale_mode",
                                                                  "upscale_codec",
                                                                  "upscale_container",
                                                                  "upscale_quality",
                                                                  "upscale_preview_length",
                                                                  "upscale_image_output_format",
                                                                  "upscale_image_quality",
                                                                  "upscale_image_rename_mode",
                                                                  "upscale_image_custom_suffix",
                                                                  "frame_interpolation_codec",
                                                                  "frame_interpolation_container",
                                                                  "frame_interpolation_quality",
                                                                  "frame_interpolation_hdr_mode",
                                                                  "frame_interpolation_rename_mode",
                                                                  "frame_interpolation_custom_suffix",
                                                                  "frame_interpolation_preview_length"}
                                                          .contains(key);
        };
#define VE_SETTING(type, name, initial)                                                                      \
    if (applies(QStringLiteral(#name)))                                                                      \
        candidate.name = defaults.name;
#include "configuration/settings_fields.inc"
#undef VE_SETTING
        if (auto valid = candidate.validate(); !valid)
            throw std::runtime_error(valid.error().toStdString());
        settings_ = std::move(candidate);
        upscaleMode_ = settings_.upscale_mode;
        settingsChanged();
        saveSettings();
        emit upscaleModeChanged();
        const auto tabName = tab == "upscale" ? translate("Upscale")
                           : tab == "frame-interpolation" ? translate("Frame Interpolation")
                                                          : translate("Neural Rendering");
        presetStatus_ = QCoreApplication::translate("App", "%1 settings were reset to defaults.").arg(tabName);
        emit presetStatusChanged();
        return {};
    }
    case Action::exportPreset:
    case Action::exportPresetTo: {
        auto name = args[0].toString().trimmed();
        QString path;
        if (action == Action::exportPresetTo) {
            path = localPath(args[1].toString());
            if (name.isEmpty()) {
                name = QFileInfo(path).completeBaseName().trimmed();
                name.remove(QRegularExpression("[\\x00-\\x1f]"));
                if (name.isEmpty())
                    name = "preset_" + QDateTime::currentDateTime().toString("yyyy-MM-dd_hh-mm-ss");
                name = name.left(120);
                if (!name.isEmpty() && name.back().isHighSurrogate()) name.chop(1);
            }
        } else {
            QString stem = name;
            stem.replace(QRegularExpression("[^\\p{L}\\p{N}_-]+"), "_");
            stem = stem.left(80);
            if (stem.isEmpty())
                throw std::runtime_error("Please enter a preset name.");
            path = transientFiles_.filePath("preset-" + QUuid::createUuid().toString(QUuid::WithoutBraces) + "-" + stem + ".json");
        }
        if (auto result = settings_.exportPreset(path, name); !result)
            throw std::runtime_error(result.error().toStdString());
        presetStatus_ = QCoreApplication::translate("App", "Exported preset '%1' to %2")
                            .arg(name, QFileInfo(path).fileName());
        emit presetStatusChanged();
        return path;
    }
    case Action::importPreset: {
        if (!idle())
            throw std::runtime_error("Stop the current operation before importing a preset.");
        auto result = settings_.importPreset(localPath(args[0].toString()));
        if (!result) {
            presetStatus_ = QCoreApplication::translate("App", "Import failed: %1").arg(translate(result.error()));
            emit presetStatusChanged();
            return false;
        }
        presetStatus_ = QCoreApplication::translate("App", "Imported preset '%1' successfully.").arg(*result);
        upscaleMode_ = settings_.upscale_mode;
        settingsChanged();
        saveSettings();
        emit presetStatusChanged();
        emit upscaleModeChanged();
        return true;
    }
    default:
        throw std::logic_error("Action does not belong to the settings service.");
    }
}
void AppController::analyzeLut(bool fixed, double positionMs, bool matching) {
    if (!idle() || analysisJob_ || context().queue->selectedPath().isEmpty())
        return;
    autoTimer_.stop();
    scrubTimer_.stop();
    const auto input = context().queue->selectedPath();
    const auto media = context().source;
    const auto settings = settings_;
    const double seconds = positionMs >= 0 ? positionMs / 1000 : context().playheadSeconds;
    const auto index = contextIndex();
    const auto generation = context().generation;
    analysisJob_ = std::make_shared<JobControl>();
    const auto control = analysisJob_;
    lutAutoMode_ = matching ? "reference" : fixed ? "fixed" : "frame";
    lutReferenceStatus_ = matching ? "Analyzing input and reference colors…" : QString{};
    emit lutAutoChanged();
    emit lutReferenceChanged();
    emit previewBusyChanged();
    auto future = QtConcurrent::run(&pool_, [this, input, media, settings, seconds, fixed, matching, control,
                                             index, generation] {
        QVariantMap changes;
        QString error;
        try {
            ColorSamples samples;
            size_t frameCount = 1;
            if (media.video && (fixed || matching)) {
                const auto times = fixed ? fixedColorFrameTimes(media.duration, media.fps)
                                         : std::vector<double>{media.duration * .05, media.duration * .2,
                                                               media.duration * .35, media.duration * .5,
                                                               media.duration * .65, media.duration * .8,
                                                               media.duration * .95};
                std::vector<ColorSamples> frames;
                for (const auto time : times) {
                    control->checkpoint();
                    auto sample = imageColorSamples(decodeVideoFrame(input, time, *control), 32000, fixed);
                    if (sample.size() >= 32)
                        frames.push_back(std::move(sample));
                }
                samples = combinedVideoSamples(std::move(frames), &frameCount);
            } else
                samples = imageColorSamples(media.video ? decodeVideoFrame(input, seconds, *control)
                                                        : decodeImage(input, *control));
            if (matching)
                changes = fitReferenceColor(
                    samples, imageColorSamples(decodeImage(settings.lut_reference_image, *control)), settings,
                    *control);
            else
                changes = recommendColor(samples, settings, *control, media.hdr, frameCount);
        } catch (const std::exception& e) {
            error = QString::fromUtf8(e.what());
        }
        QMetaObject::invokeMethod(
            this,
            [this, changes, error, index, generation, settings, matching] {
                if (shuttingDown_)
                    return;
                analysisJob_.reset();
                lutAutoMode_.clear();
                bool unchanged = contextIndex() == index && context().generation == generation &&
                                 settings_.lut_path == settings.lut_path &&
                                 settings_.lut_reference_image == settings.lut_reference_image;
                for (const auto& key : read(Property::lutAdjustmentValues).toMap().keys())
                    unchanged = unchanged && settings_.value(key) == settings.value(key);
                if (error.isEmpty() && unchanged) {
                    for (auto it = changes.begin(); it != changes.end(); ++it) {
                        if (matching && it.key() == "lut_mix")
                            continue;
                        auto result = settings_.set(it.key(), it.value());
                        (void)result;
                    }
                    settingsChanged();
                    status_ = matching ? QT_TRANSLATE_NOOP("App", "Reference color adjustments ready")
                                       : QT_TRANSLATE_NOOP("App", "Automatic color adjustments applied.");
                    if (!autoPreview_)
                        preview();
                } else
                    status_ = error.isEmpty()
                                  ? QT_TRANSLATE_NOOP("App", "Color adjustment skipped because the input or color controls changed.")
                                  : error;
                if (matching)
                    lutReferenceStatus_ = status_;
                emit lutAutoChanged();
                emit lutReferenceChanged();
                emit previewBusyChanged();
                emit statusMessageChanged();
            },
            Qt::QueuedConnection);
    });
    (void)future;
}
} // namespace ve
