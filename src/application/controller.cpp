#include "controller.hpp"
#include "choices.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/vulkan_device.hpp"
#include "processing/reshade_native.hpp"
#include "processing/reshade_dependencies.hpp"
#include "processing/reshade_worker.hpp"
#include "processing/vfx_native.hpp"
#include "processing/vfx_worker.hpp"
#include "processing/fsr_worker.hpp"
#include <QClipboard>
#include <QCoreApplication>
#include <QDateTime>
#include <QElapsedTimer>
#include <QDesktopServices>
#include <QDir>
#include <QDirIterator>
#include <QFileInfo>
#include <QFont>
#include <QGuiApplication>
#include <QFutureWatcher>
#include <QLibraryInfo>
#include <QLocale>
#include <QtConcurrent/QtConcurrentRun>
#include <algorithm>
#include <mutex>
#include <ranges>

namespace ve {
namespace {
const char* binding(Property property) {
    for (const auto& entry : settingBindings)
        if (entry.property == property)
            return entry.key;
    return nullptr;
}
QVariantList labeled(const QStringList& values, const QString& suffix = {}) {
    QVariantList result;
    for (const auto& value : values)
        result.append(QVariantMap{{"label", value + suffix}, {"value", value}});
    return result;
}
struct CachedReShadePreset {
    std::mutex mutex;
    QString key;
    QVariantMap raw;
};
CachedReShadePreset& reshadePresetCache() {
    static CachedReShadePreset cache;
    return cache;
}
QVariantMap inspectCachedReShadePreset(const Settings& raw, bool video) {
    const auto rs = reshadeSettingsForMode(raw, video);
    if (rs.reshade_preset_path.trimmed().isEmpty()) {
        return ReShadeEngine::inspectPreset(raw, video);
    }
    const QString cacheKey = QString::fromLatin1(reshadeDependencyIdentity(raw, video, Paths::instance().root).toHex()) + '|' +
                             rs.reshade_shader_search_paths + '|' + rs.reshade_texture_search_paths + '|' +
                             rs.reshade_parameter_overrides + '|' + rs.ai_gpu_uuid;
    auto& cache = reshadePresetCache();
    {
        std::lock_guard lock(cache.mutex);
        if (cache.key == cacheKey)
            return cache.raw;
    }
    auto inspected = ReShadeEngine::inspectPreset(raw, video);
    {
        std::lock_guard lock(cache.mutex);
        cache.key = cacheKey;
        cache.raw = inspected;
    }
    return inspected;
}
} // namespace
void invalidateReShadePresetCache() {
    auto& cache = reshadePresetCache();
    std::lock_guard lock(cache.mutex);
    cache.key.clear();
}
void AppController::requestReShadeInspection() {
    if (shuttingDown_) return;
    auto snapshot = reshadeSettingsForMode(settings_, nrMode_ == "Video");
    snapshot.reshade_parameter_overrides = reshadeGraphOverrides(snapshot.reshade_parameter_overrides);
    const QFileInfo preset(snapshot.reshade_preset_path);
    const QString key = nrMode_ + '|' + preset.absoluteFilePath() + '|' +
        QString::number(preset.lastModified().toMSecsSinceEpoch()) + '|' +
        snapshot.reshade_shader_search_paths + '|' + snapshot.reshade_texture_search_paths + '|' +
        snapshot.reshade_parameter_overrides + '|' + snapshot.ai_gpu_uuid + '|' + QString::number(reshadeReloadGeneration_);
    reshadeRequestedKey_ = key;
    if (reshadeLoadedKey_ == key || reshadeInspectionBusy_) return;
    reshadeInspectionBusy_ = true;
    auto* watcher = new QFutureWatcher<QVariantMap>(this);
    connect(watcher, &QFutureWatcher<QVariantMap>::finished, this, [this, watcher, key] {
        const auto result = watcher->result();
        watcher->deleteLater();
        reshadeInspectionBusy_ = false;
        if (shuttingDown_) return;
        if (key == reshadeRequestedKey_) {
            reshadeInspection_ = result;
            reshadeLoadedKey_ = key;
        }
        requestReShadeInspection(); // Coalesce changes made while the worker was compiling.
        emit reshadePresetInfoChanged();
        emit reshadeStatusChanged();
    });
    watcher->setFuture(QtConcurrent::run(&reshadeInspectionPool_, [snapshot] {
        try {
            // Uniform overrides are overlaid by the editors. Only graph changes
            // require recompilation, preserving the imported preset baseline.
            return inspectCachedReShadePreset(snapshot, true);
        } catch (const std::exception& e) {
            return QVariantMap{{"valid", false}, {"error", QString::fromUtf8(e.what())}};
        }
    }));
    // Getters can start inspection too. Notify on the next event turn to avoid
    // changing a QML binding while it is being evaluated.
    QMetaObject::invokeMethod(this, [this] {
        if (!shuttingDown_) {
            emit reshadePresetInfoChanged();
            emit reshadeStatusChanged();
        }
    }, Qt::QueuedConnection);
}
AppController::AppController(PreviewProvider* previews, QObject* parent)
    : QmlFacade(parent), previews_(previews),
      settings_(Settings::load(Paths::instance().configurationFile())),
      desktop_() {
    if (!transientFiles_.isValid())
        throw std::runtime_error("Cannot create the desktop temporary directory.");
    pool_.setMaxThreadCount(3);
    previewPool_.setMaxThreadCount(1);
    cachePool_.setMaxThreadCount(1);
    reshadeInspectionPool_.setMaxThreadCount(1);
    cacheTrimTimer_.setSingleShot(true);
    cacheTrimTimer_.setInterval(100);
    connect(&cacheTrimTimer_, &QTimer::timeout, this, &AppController::cleanupCaches);
    connect(this, &AppController::languageChanged, this, [this] { cacheStatus_.clear(); emit processingCacheChanged(); });
    cacheRefreshTimer_.setSingleShot(true);
    cacheRefreshTimer_.setInterval(300);
    connect(&cacheRefreshTimer_, &QTimer::timeout, this, [this] { updateProcessingCache(); });
    connect(&ProcessingCache::instance(), &ProcessingCache::usageChanged, this, [this] {
        if (tab_ == "settings" && !cacheRefreshTimer_.isActive()) cacheRefreshTimer_.start();
    }, Qt::QueuedConnection);
    connect(this, &AppController::operationStateChanged, this, &AppController::processingCacheChanged);
    connect(this, &AppController::runtimeStateChanged, this, &AppController::processingCacheChanged);
    for (auto& context : contexts_)
        context.queue = new BatchModel(this);
    upscaleMode_ = settings_.upscale_mode;
    inspectorWidth_ = desktop_.value("layout/inspectorWidth", 400).toInt();
    queueHeight_ = std::clamp(desktop_.value("layout/queueHeight", 330).toInt(), 180, 720);
    logHeight_ = desktop_.value("layout/logHeight", 220).toInt();
    autoPreview_ = desktop_.value("preview/realtimeEnabled", true).toBool();
    destinationMode_ = desktop_.value("export/destinationMode", "output").toString();
    exportFolder_ = desktop_.value("export/folder").toString();
    saveTimer_.setSingleShot(true);
    saveTimer_.setInterval(450);
    connect(&saveTimer_, &QTimer::timeout, this, &AppController::saveSettings);
    autoTimer_.setSingleShot(true);
    autoTimer_.setInterval(35);
    connect(&autoTimer_, &QTimer::timeout, this, [this] {
        if (autoPreview_ && idle() && context().source.width > 0 &&
            !workflowSettings(settings_, workflow(), context().source.video)
                 .stages(context().source.video)
                 .isEmpty())
            preview(context().playheadSeconds * 1000, true);
    });
    scrubTimer_.setSingleShot(true);
    scrubTimer_.setInterval(35);
    connect(&scrubTimer_, &QTimer::timeout, this, [this] {
        if (autoPreview_ && idle())
            preview(context().playheadSeconds * 1000, true);
        else if (autoPreview_ && operation_.startsWith("Preview"))
            previewDirty_ = true;
    });
    cleanupTimer_.setInterval(3600000);
    connect(&cleanupTimer_, &QTimer::timeout, this, &AppController::cleanupCaches);
    cleanupTimer_.start();
    connect(
        &Log::instance(), &Log::message, this,
        [this](const QString& level, const QString& tag, const QString& message) {
            _append_backend_log(level, tag, message);
        },
        Qt::QueuedConnection);
    connect(this, &AppController::operationStateChanged, this, &AppController::previewBusyChanged);
    setLanguage(desktop_.value("interface/language", "en_US").toString());
    // A cold cache directory scan must not delay the first visible window.
    QTimer::singleShot(1500, this, &AppController::cleanupCaches);
}
AppController::~AppController() {
    QElapsedTimer elapsed;
    elapsed.start();
    shutdownAll();
    cachePool_.waitForDone();
    auto release = QtConcurrent::run(&previewPool_, [this] {
        videoFrames_.clear();
        videoStill_.reset();
    });
    release.waitForFinished();
    previewPool_.waitForDone();
    pool_.waitForDone();
    reshadeInspectionPool_.waitForDone();
    info("shutdown", QString("Processing workers stopped in %1 ms.").arg(elapsed.elapsed()));
}
void AppController::attachWindow(QQuickWindow* window) {
    window_ = window;
}
int AppController::contextIndex() const {
    if (tab_ == "frame-interpolation")
        return 4;
    if (tab_ == "upscale")
        return upscaleMode_ == "Video" ? 3 : 2;
    return nrMode_ == "Video" ? 1 : 0;
}
QString AppController::contextKey() const {
    return QStringList{"nr-image", "nr-video", "upscale-image", "upscale-video", "fi-video"}[contextIndex()];
}
Workflow AppController::workflow() const {
    return tab_ == "upscale"               ? Workflow::Upscale
           : tab_ == "frame-interpolation" ? Workflow::FrameInterpolation
                                           : Workflow::NeuralRendering;
}
PreviewContext& AppController::context() {
    return contexts_[size_t(contextIndex())];
}
const PreviewContext& AppController::context() const {
    return contexts_[size_t(contextIndex())];
}
QString AppController::translate(const QString& text) const {
    return QCoreApplication::translate("App", text.toUtf8().constData());
}
QVariant AppController::read(Property property) const {
    const bool fsrVideo=nrMode_=="Video"&&settings_.stages(true).contains("fsr_frame_generation");
    if(fsrVideo&&property==Property::codecChoices){
        return labeled(QStringList{"H.264","H.265","AV1"});
    }
    if(fsrVideo&&property==Property::videoCodec){auto codec=settings_.codec;codec.remove(" (NVIDIA NVENC)");return codec;}
    if (const auto key = binding(property))
        return settings_.value(QStringView(QString::fromLatin1(key)));
    if (auto choices = staticChoices(property))
        return *choices;
    const auto& c = context();
    switch (property) {
    case Property::activeTab:
        return tab_;
    case Property::nrMode:
        return nrMode_;
    case Property::upscaleMode:
        return upscaleMode_;
    case Property::dlssModeChoices:
        return labeled({"DLAA", "Quality", "Balanced", "Performance", "Ultra Performance"});
    case Property::workflowStages: {
        QVariantList cards;
        const auto& order = nrMode_ == "Image" ? settings_.image_stage_order : settings_.video_stage_order;
        const auto& active =
            nrMode_ == "Image" ? settings_.image_enabled_stages : settings_.video_enabled_stages;
        for (const auto& stage : order)
            cards.append(QVariantMap{{"id", stage}, {"enabled", active.contains(stage)}});
        return cards;
    }
    case Property::rrSettings: {
        QVariantMap result;const auto values=settings_.values();
        for(auto it=values.begin();it!=values.end();++it)if(it.key().startsWith("rr_"))result.insert(it.key(),it.value());
        return result;
    }
    case Property::reshadeSettings: {
        QVariantMap result;
        const auto values = settings_.values();
        for (auto it = values.begin(); it != values.end(); ++it)
            if (it.key().startsWith("reshade_"))
                result.insert(it.key(), it.value());
        const auto rs = reshadeSettingsForMode(settings_, nrMode_ == "Video");
        result.insert("active_preset_path", rs.reshade_preset_path);
        result.insert("active_shader_search_paths", rs.reshade_shader_search_paths);
        result.insert("active_texture_search_paths", rs.reshade_texture_search_paths);
        result.insert("active_mix", rs.reshade_mix);
        result.insert("active_depth_mode", rs.reshade_depth_mode);
        result.insert("active_depth_resolution", rs.reshade_depth_resolution);
        result.insert("active_temporal_stability", rs.reshade_temporal_stability);
        result.insert("active_temporal_strength", rs.reshade_temporal_strength);
        result.insert("active_precision_policy", rs.reshade_precision_policy);
        result.insert("active_hdr_policy", rs.reshade_hdr_policy);
        result.insert("active_preserve_alpha", rs.reshade_preserve_alpha);
        result.insert("active_deterministic_seed", rs.reshade_deterministic_seed);
        result.insert("active_parameter_overrides", rs.reshade_parameter_overrides);
        return result;
    }
    case Property::reshadePresetInfo: {
        const_cast<AppController*>(this)->requestReShadeInspection();
        auto map = reshadeInspection_;
        map.insert("loading", reshadeInspectionBusy_ || reshadeLoadedKey_ != reshadeRequestedKey_);
        QVariantList parameters;
        for (const auto& item : map.value("parameters").toList()) {
            auto p = item.toMap();
            const auto vals = p.value("values").toList();
            QVariant currentVal;
            if (vals.size() == 1)
                currentVal = vals.front();
            else if (!vals.isEmpty()) {
                QStringList parts;
                for (const auto& v : vals)
                    parts.append(v.toString());
                currentVal = parts.join(',');
            }
            p.insert("effect_file", p.value("effect").toString());
            p.insert("min", p.value("ui_min").toDouble());
            p.insert("max", p.value("ui_max").toDouble());
            p.insert("step", p.value("ui_step").toDouble());
            p.insert("default_value", currentVal);
            p.insert("current_value", currentVal);
            parameters.append(p);
        }
        map.insert("parameters", parameters);
        QVariantList techniques;
        for (const auto& item : map.value("techniques").toList()) {
            auto t = item.toMap();
            t.insert("effect_file", t.value("effect").toString());
            techniques.append(t);
        }
        map.insert("techniques", techniques);
        return map;
    }
    case Property::reshadeStatus: {
        const_cast<AppController*>(this)->requestReShadeInspection();
        if (reshadeInspectionBusy_) return QString{};
        const auto rs = reshadeSettingsForMode(settings_, nrMode_ == "Video");
        if (rs.reshade_preset_path.isEmpty()) return QCoreApplication::translate("App", "Choose a preset to begin.");
        if (!reshadeInspection_.value("valid").toBool()) return reshadeInspection_.value("error").toString();
        int enabled = 0;
        for (const auto& t : reshadeInspection_.value("techniques").toList()) if (t.toMap().value("enabled").toBool()) ++enabled;
        const bool depth = rs.reshade_depth_mode != "Off" &&
            (rs.reshade_depth_mode == "AI Depth" || reshadeInspection_.value("requires_depth").toBool());
        return QCoreApplication::translate("App", "%1 technique(s) enabled · %2").arg(enabled).arg(depth
            ? QCoreApplication::translate("App", "AI depth enabled")
            : QCoreApplication::translate("App", "Depth not required or disabled"));
    }
    case Property::vfxSettings: {
        QVariantMap result;const auto values=settings_.values();
        for(auto it=values.begin();it!=values.end();++it)if(it.key().startsWith("vfx_"))result.insert(it.key(),it.value());
        return result;
    }
    case Property::vfxStatus: {
        auto text=vfxWorkerDiagnostics();auto info=QJsonDocument::fromJson(text.toUtf8()).object();
        if(info.isEmpty()||info.value("gpu_ordinal").toInt()!=gpuOrdinal(settings_.ai_gpu_uuid)){text=VideoEffects::diagnostics();info=QJsonDocument::fromJson(text.toUtf8()).object();}
        if(info.isEmpty())return text;
        if(!info.value("initialized").toBool())return info.value("last_error").toString().isEmpty()?QCoreApplication::translate("App", "GPU support and installed models are checked before processing."):info.value("last_error").toString();
        QStringList unavailable;
        for(const auto& feature:info.value("features").toArray()){
            const auto f=feature.toObject();
            if(!f.value("installed").toBool()||!f.value("hardware_supported").toBool()||f.value("initialization_error").toInt()!=0)unavailable.append(f.value("effect").toString());
        }
        auto summary=info.value("gpu").toString()+" | NVIDIA VFX 1.3";
        if(!unavailable.isEmpty())summary+=" | "+QCoreApplication::translate("App", "Unavailable")+": "+unavailable.join(", ");return summary;
    }
    case Property::ffmpegDeviceChoices: {
        QVariantList choices{
            QVariantMap{{"label", translate("Automatic (Best Available)")}, {"value", "auto"}},
            QVariantMap{{"label", translate("CPU (Software decoding / encoding)")}, {"value", "cpu"}}};
        for (const auto& device : vulkanDevices())
            choices.append(QVariantMap{{"label", device.label()}, {"value", device.selection()}});
        return choices;
    }
    case Property::ffmpegDeviceStatus: {
        if (settings_.ffmpeg_device == "cpu")
            return QCoreApplication::translate("App", "CPU codecs; GPU pixel filters");
        for (const auto& device : vulkanDevices())
            if (settings_.ffmpeg_device == "auto" || settings_.ffmpeg_device == device.selection())
                return device.label();
        return QCoreApplication::translate("App", "The selected GPU is unavailable.");
    }
    case Property::lutAdjustmentValues: {
        QVariantMap values;
        const auto map = settings_.values();
        for (auto it = map.begin(); it != map.end(); ++it)
            if (it.key().startsWith("lut_") &&
                !QStringList{"lut_path", "lut_reference_image", "lut_resolution"}.contains(it.key()))
                values.insert(it.key(), it.value());
        return values;
    }
    case Property::lutAutoBusy:
        return analysisJob_ && !lutSaving_ && lutAutoMode_ != "reference";
    case Property::lutReferenceBusy:
        return analysisJob_ && lutAutoMode_ == "reference";
    case Property::lutAutoMode:
        return lutAutoMode_;
    case Property::lutReferenceStatus:
        return translate(lutReferenceStatus_);
    case Property::lutSaveBusy:
        return lutSaving_;
    case Property::lutSaveStatus:
        return lutSavedPath_.isEmpty() ? translate(lutSaveStatus_)
                                      : QCoreApplication::translate("App", "Saved LUT to %1").arg(lutSavedPath_);
    case Property::isProcessing:
        return !idle();
    case Property::overallProgress:
        return progress_;
    case Property::canPauseExport:
        return operation_ == "BatchRunning" && c.source.video;
    case Property::exportPaused:
    case Property::exportPauseRequested:
        return job_ && job_->paused();
    case Property::exportPhaseText:
        return translate(exportPhase_);
    case Property::statusMessage:
        return translate(status_);
    case Property::previewError:
        return translate(c.error);
    case Property::splitPosition:
        return split_;
    case Property::hasOutputPreview:
        return !c.outputUrl.isEmpty();
    case Property::previewInputUrl:
        return c.inputUrl;
    case Property::previewOutputUrl:
        return c.outputUrl;
    case Property::previewPosterUrl:
        return c.posterUrl;
    case Property::previewSourceSeconds:
        return c.previewSeconds;
    case Property::previewSourceFrame:
        return c.previewFrame;
    case Property::previewRenderedRanges:
        return c.ranges;
    case Property::inputInfoText:
        return c.inputInfo;
    case Property::outputInfoText:
        return c.outputInfo;
    case Property::presetStatus:
        return translate(presetStatus_);
    case Property::processingCacheBytes: return cacheSnapshot_.bytes;
    case Property::processingCacheReclaimableBytes: return cacheSnapshot_.reclaimableBytes;
    case Property::processingCacheBusy: return cacheBusy_;
    case Property::processingCacheCleaning: return cacheCleaning_ || cacheCleanPending_;
    case Property::processingCacheReady: return cacheReady_;
    case Property::canCleanProcessingCache:
        // Read-only size refreshes keep the last snapshot usable. Only cleanup
        // or active processing should disable the action.
        return cacheReady_ && !cacheCleaning_ && cacheSnapshot_.reclaimableBytes > 0 && cacheMaintenanceSafe();
    case Property::processingCacheSizeText:
        return cacheSnapshot_.bytes > 0 && cacheSnapshot_.bytes < 10000000
            ? QString("<%1 GB").arg(QLocale().toString(.01, 'f', 2))
            : QString("%1 GB").arg(QLocale().toString(double(cacheSnapshot_.bytes) / 1e9, 'f', 2));
    case Property::processingCacheStatus: return cacheStatus_;
    case Property::processingCacheDetails:
        return QCoreApplication::translate("App", "Total: %1 bytes; available to clean: %2 bytes.")
            .arg(QLocale().toString(cacheSnapshot_.bytes), QLocale().toString(cacheSnapshot_.reclaimableBytes))
            + '\n' + QCoreApplication::translate("App", "Current previews and active processing files are kept.");
    case Property::previewBusy:
        return operation_.startsWith("Preview") || autoTimer_.isActive() || scrubTimer_.isActive() || analysisJob_ != nullptr;
    case Property::operationState:
        return operation_;
    case Property::fastColorPreviewActive:
        return fastPreview_;
    case Property::canModifyQueue:
        return idle();
    case Property::canPreview:
        if (!idle() || runtime_ != "Ready" || analysisJob_ != nullptr || c.queue->count() == 0 || c.source.width <= 0)
            return false;
        try {
            const auto previewSettings = c.source.video ? videoPreviewSettings(settings_) : settings_;
            return !estimatePipeline(c.source, previewSettings, workflow()).stages.isEmpty();
        } catch (const std::exception&) { return false; }
    case Property::canRender:
        return idle() && runtime_ == "Ready" && analysisJob_ == nullptr && c.queue->count() > 0 &&
               c.source.width > 0 && read(Property::renderValidationMessage).toString().isEmpty() &&
               (workflow() != Workflow::NeuralRendering || !settings_.stages(nrMode_ == "Video").isEmpty());
    case Property::canStop:
        return operation_ != "Idle" && !operation_.endsWith("Cancelling");
    case Property::runtimeState:
        return runtime_;
    case Property::runtimeError:
        return translate(runtimeError_);
    case Property::startupWarnings:
        return warnings_.join('\n');
    case Property::sourceWidth:
        return c.source.width;
    case Property::sourceHeight:
        return c.source.height;
    case Property::sourceFps:
        return c.source.fps;
    case Property::sourceDuration:
        return c.source.duration;
    case Property::outputWidth:
        return c.outputSize.width() > 0 ? c.outputSize.width() : 0;
    case Property::outputHeight:
        return c.outputSize.height() > 0 ? c.outputSize.height() : 0;
    case Property::inspectorWidth:
        return inspectorWidth_;
    case Property::queueHeight:
        return queueHeight_;
    case Property::logHeight:
        return logHeight_;
    case Property::focusPreview:
        return focus_;
    case Property::exportDestinationMode:
        return destinationMode_;
    case Property::exportFolder:
        return exportFolder_;
    case Property::autoPreviewEnabled:
        return autoPreview_;
    case Property::language:
        return language_;
    case Property::gpuName:
    case Property::gpuVram:{
        if(gpus_.empty()||(nrMode_=="Video"&&settings_.stages(true).contains("fsr_frame_generation"))){
            const FsrAdapter* selected=nullptr;
            for(const auto& gpu:fsrAdapters_){
                if(settings_.fsr_adapter_id==gpu.id||settings_.fsr_adapter_id==QString::number(gpu.index)){selected=&gpu;break;}
                if(settings_.fsr_adapter_id=="auto"&&gpu.available&&(!selected||gpu.memoryBytes>selected->memoryBytes))selected=&gpu;
            }
            if(selected)return property==Property::gpuName?selected->name:selected->memoryBytes?QString::number(selected->memoryBytes/(1024.*1024*1024),'f',1)+" GB":translate("Shared GPU memory");
        }
        return property==Property::gpuName?(gpus_.empty()?QString("GPU"):gpus_.front().name):(gpus_.empty()?QString{}:QString::number(gpus_.front().memoryMiB/1024)+" GB");
    }
    case Property::gpuCount:
        return int(gpus_.size());
    case Property::nrImageQueue:
        return QVariant::fromValue<QObject*>(contexts_[0].queue);
    case Property::nrVideoQueue:
        return QVariant::fromValue<QObject*>(contexts_[1].queue);
    case Property::upscaleImageQueue:
        return QVariant::fromValue<QObject*>(contexts_[2].queue);
    case Property::upscaleVideoQueue:
        return QVariant::fromValue<QObject*>(contexts_[3].queue);
    case Property::fiQueue:
        return QVariant::fromValue<QObject*>(contexts_[4].queue);
    case Property::previewInputIsVideo:
        return c.source.video;
    case Property::previewOutputIsVideo:
        return c.outputIsVideo;
    case Property::windowWidth:
        return std::max(1080, desktop_.value("window/width", 1440).toInt());
    case Property::windowHeight:
        return std::max(700, desktop_.value("window/height", 920).toInt());
    case Property::windowX:
        return desktop_.value("window/x", -1);
    case Property::windowY:
        return desktop_.value("window/y", -1);
    case Property::windowMaximized:
        return desktop_.value("window/maximized", false);
    case Property::appVersion:
        return "v" VE_APP_VERSION;
    case Property::diagnosticsText:
        return NativeEngines::instance().diagnostics() + "\nFSR: "+fsrWorkerDiagnostics()+"\nQt " + qVersion() + " | C++23\n" +
               Paths::instance().root;
    case Property::fsrAdapterChoices:{
        QVariantList values{QVariantMap{{"label",QCoreApplication::translate("App","Auto")},{"value","auto"}}};
        for(const auto& adapter:fsrAdapters_)values.append(QVariantMap{{"label",adapter.name+(adapter.available?QString{}:QString(" — ")+adapter.unavailableReason)},{"value",adapter.id},{"available",adapter.available}});
        return values;
    }
    case Property::fsrStatus:{
        for(const auto& adapter:fsrAdapters_)if(settings_.fsr_adapter_id=="auto"?adapter.available:settings_.fsr_adapter_id==adapter.id||settings_.fsr_adapter_id==QString::number(adapter.index))
            return adapter.available?translate("Frame generation available"):adapter.unavailableReason;
        return settings_.fsr_adapter_id=="auto"?translate("No compatible FSR adapter detected"):translate("Selected FSR adapter is unavailable");
    }
    case Property::containerChoices:
        return settings_.codec.isEmpty() ? QVariantList{} : labeled(containersForCodec(settings_.codec));
    case Property::upscaleContainerChoices:
        return labeled(containersForCodec(settings_.upscale_codec));
    case Property::fiContainerChoices:
        return labeled(containersForCodec(settings_.frame_interpolation_codec));
    case Property::aiGpuChoices:
    case Property::videoGpuChoices: {
        QVariantList values{QVariantMap{{"label", QCoreApplication::translate("App", "Auto")}, {"value", "auto"}}};
        for (const auto& gpu : gpus_)
            values.append(
                QVariantMap{{"label", gpu.name + " (" + QString::number(gpu.memoryMiB / 1024) + " GB)"},
                            {"value", gpu.uuid}});
        return values;
    }
    case Property::customMaskStatus:
        return settings_.nr_mask.isEmpty() ? QString{}
                                           : translate("Active: %1 — %2×%3.")
                                                 .arg(settings_.nr_mask.value("source_name").toString())
                                                 .arg(settings_.nr_mask.value("width").toInt())
                                                 .arg(settings_.nr_mask.value("height").toInt());
    case Property::videoContainer:
        return settings_.container;
    case Property::upscaleContainer:
        return settings_.upscale_container;
    case Property::fiContainer:
        return settings_.frame_interpolation_container;
    case Property::videoHdrSupported:
        return hdrSupported(settings_.codec);
    case Property::fiHdrSupported:
        return hdrSupported(settings_.frame_interpolation_codec);
    case Property::upscaleHdrSupported:
        return hdrSupported(settings_.upscale_codec);
    case Property::renderValidationMessage: {
        try {
            if (auto valid = settings_.validate(); !valid)
                return translate(valid.error());
            if (c.source.width > 0) {
                const auto estimate = estimatePipeline(c.source, settings_, workflow());
                (void)estimate;
            }
            return QString{};
        } catch (const std::exception& e) {
            return translate(QString::fromUtf8(e.what()));
        }
    }
    case Property::frameGenerationEstimate:
        return c.source.fps > 0 ? translate("%1 FPS").arg(QString::number(c.source.fps, 'g', 5)) + " → " +
                                      translate("%1 FPS").arg(QString::number(frameGenerationFrameRate(c.source.fps, settings_,
                                          tab_ == "frame-interpolation" && settings_.frame_interpolation_provider == "FSR 3"), 'g', 7))
                                : QString{};
    case Property::rtxSuperResolutionEstimate:
    case Property::outputEstimate: {
        if (c.source.width <= 0)
            return QString{};
        try {
            const auto estimate = estimatePipeline(c.source, settings_, workflow());
            return QString::number(c.source.width) + QChar(0xD7) + QString::number(c.source.height) + " → " +
                   QString::number(estimate.width) + QChar(0xD7) + QString::number(estimate.height) +
                   (c.source.video ? " | " + translate("%1 FPS").arg(QString::number(estimate.fps, 'g', 5)) : QString{});
        } catch (const std::exception& e) {
            return translate(QString::fromUtf8(e.what()));
        }
    }
    case Property::currentLogPath:
        return Log::instance().sessionPath();
    case Property::fullLog:
        return logs_.join('\n');
    default:
        return {};
    }
}
void AppController::write(Property property, const QVariant& value) {
    if (const auto* key = binding(property)) {
        if (auto result = settings_.set(QString::fromLatin1(key), value); !result) {
            status_ = result.error();
            emit statusMessageChanged();
            return;
        }
        settingsChanged();
        return;
    }
    switch (property) {
    case Property::activeTab: {
        auto tab = value.toString();
        if (tab == "upscale" || tab == "frame-interpolation") tab = "neural-rendering";
        if (!QStringList{"neural-rendering", "settings", "help"}.contains(tab) || tab == tab_)
            return;
        tab_ = tab;
        emit activeTabChanged();
        if (tab_ == "settings") updateProcessingCache();
        notifyContext();
        if (autoPreview_ && idle())
            autoTimer_.start(previewDelay());
        break;
    }
    case Property::videoContainer:
    case Property::upscaleContainer:
    case Property::fiContainer: {
        const auto key = property == Property::videoContainer     ? "container"
                         : property == Property::upscaleContainer ? "upscale_container"
                                                                  : "frame_interpolation_container";
        if (auto result = settings_.set(QString::fromLatin1(key), value); !result) {
            status_ = result.error();
            emit statusMessageChanged();
            return;
        }
        settingsChanged();
        break;
    }
    case Property::nrMode:
        if (value.toString() == "Image" || value.toString() == "Video") {
            nrMode_ = value.toString();
            emit nrModeChanged();
            if (!reshadeRequestedKey_.isEmpty()) requestReShadeInspection();
            emit reshadePresetInfoChanged();
            emit reshadeStatusChanged();
            emit workflowStagesChanged();
            emit settingsUpdated();
            notifyContext();
        }
        break;
    case Property::upscaleMode:
        if (value.toString() == "Image" || value.toString() == "Video") {
            upscaleMode_ = value.toString();
            settings_.upscale_mode = upscaleMode_;
            settingsChanged();
            emit upscaleModeChanged();
            notifyContext();
        }
        break;
    case Property::splitPosition:
        split_ = std::clamp(value.toDouble(), 0., 1.);
        emit splitPositionChanged();
        break;
    case Property::inspectorWidth:
        inspectorWidth_ = std::clamp(value.toInt(), 280, 600);
        persistLayout();
        break;
    case Property::queueHeight:
        queueHeight_ = std::clamp(value.toInt(), 180, 720);
        persistLayout();
        break;
    case Property::logHeight:
        logHeight_ = std::clamp(value.toInt(), 80, 600);
        persistLayout();
        break;
    case Property::focusPreview:
        focus_ = value.toBool();
        emit layoutChanged();
        break;
    case Property::autoPreviewEnabled:
        if (autoPreview_ == value.toBool()) break;
        autoPreview_ = value.toBool();
        desktop_.setValue("preview/realtimeEnabled", autoPreview_);
        desktop_.sync();
        emit autoPreviewChanged();
        if (autoPreview_)
            autoTimer_.start(previewDelay());
        else {
            previewDirty_ = false;
            autoTimer_.stop();
            scrubTimer_.stop();
            if (prefetchJob_) prefetchJob_->cancel();
        }
        break;
    case Property::language:
        setLanguage(value.toString());
        break;
    case Property::exportDestinationMode:
        destinationMode_ = value.toString();
        desktop_.setValue("export/destinationMode", destinationMode_);
        emit exportPrefsChanged();
        break;
    case Property::exportFolder:
        exportFolder_ = localPath(value.toString());
        desktop_.setValue("export/folder", exportFolder_);
        emit exportPrefsChanged();
        break;
    default:
        break;
    }
}
QVariant AppController::execute(Action action, const QVariantList& args) {
    try {
        switch (action) {
        case Action::refreshProcessingCache: updateProcessingCache(); return {};
        case Action::cleanProcessingCache: updateProcessingCache(true); return {};
        case Action::setVfxSetting:
        case Action::setRrSetting:
        case Action::setReShadeSetting:
        case Action::reloadReShadePreset:
        case Action::saveReShadePresetCopy:
        case Action::setReShadeParameter:
        case Action::resetReShadeParameters:
        case Action::setReShadeTechniqueEnabled:
        case Action::moveReShadeTechnique:
        case Action::setLutFile:
        case Action::setLutAdjustment:
        case Action::resetLutAdjustments:
        case Action::autoAdjustLut:
        case Action::autoAdjustLutFixedFrames:
        case Action::setLutReferenceImage:
        case Action::saveLut:
        case Action::setWorkflowStageEnabled:
        case Action::moveWorkflowStage:
        case Action::selectCustomMask:
        case Action::clearCustomMask:
        case Action::resetToDefaults:
        case Action::resetTabSettings:
        case Action::exportPreset:
        case Action::exportPresetTo:
        case Action::importPreset:
            return handleSettings(action, args);
        default:
            return handleWorkflow(action, args);
        }
    } catch (const std::exception& e) {
        status_ = QString::fromUtf8(e.what());
        log(status_, "error");
        emit statusMessageChanged();
        if (action == Action::exportPreset || action == Action::exportPresetTo || action == Action::importPreset) {
            presetStatus_ = translate(status_);
            emit presetStatusChanged();
        }
        if (action == Action::startActiveBatchWithExport)
            return status_;
        return {};
    }
}
void AppController::notifyContext() {
    QStringList retained;
    for (const auto& current : contexts_) {
        retained << current.inputUrl << current.outputUrl << current.posterUrl;
        for (int row = 0; row < current.queue->count(); ++row)
            retained.append(current.queue->entry(row).thumbnail);
    }
    previews_->retain(retained);
    emit mediaPreviewChanged();
    emit previewInputUrlChanged();
    emit previewOutputUrlChanged();
    emit previewPosterUrlChanged();
    emit previewSourceChanged();
    emit previewRangesChanged();
    emit hasOutputPreviewChanged();
    emit inputInfoTextChanged();
    emit outputInfoTextChanged();
    emit sourceSizeChanged();
    emit outputSizeChanged();
    emit estimateChanged();
    emit operationStateChanged();
}
void AppController::setOperation(const QString& operation, const QString& status) {
    operation_ = operation;
    if (!status.isEmpty()) {
        status_ = status;
        emit statusMessageChanged();
    }
    emit operationStateChanged();
    emit isProcessingChanged();
}
void AppController::log(const QString& message, const QString& tag) {
    Log::instance().append("INFO", tag, message);
}
void AppController::saveSettings() {
    if (auto result = settings_.save(Paths::instance().configurationFile()); !result)
        log(result.error(), "settings");
}
void AppController::settingsChanged() {
    if (!reshadeRequestedKey_.isEmpty()) requestReShadeInspection();
    emit reshadeStatusChanged();
    emit vfxStatusChanged();
    if (prefetchJob_) prefetchJob_->cancel();
    saveTimer_.start();
    emit settingsUpdated();
    emit workflowStagesChanged();
    emit estimateChanged();
    emit runtimeStateChanged();
    ++settingsRevision_;
    for (auto& context : contexts_) {
        context.ranges.clear();
    }
    emit previewRangesChanged();
    if (operation_ == "PreviewRunning") {
        previewDirty_ = true;
        if (job_) job_->cancel();
    }
    if (autoPreview_ && (idle() || operation_ == "PreviewRunning"))
        autoTimer_.start(previewDelay());
}
void AppController::suspendProcessing() {
    if (operation_ == "BatchRunning" && job_) {
        job_->pause(true);
        for (auto& current : contexts_)
            current.queue->setPaused(true);
        exportPhase_ = QT_TRANSLATE_NOOP("App", "Paused");
        emit exportPhaseChanged();
    } else
        stopOperation();
}
void AppController::persistLayout() {
    desktop_.setValue("layout/inspectorWidth", inspectorWidth_);
    desktop_.setValue("layout/queueHeight", queueHeight_);
    desktop_.setValue("layout/logHeight", logHeight_);
    desktop_.sync();
    emit layoutChanged();
}
void AppController::setLanguage(const QString& code) {
    const QStringList codes{"en_US", "ar", "de", "es", "fr", "hi", "ja", "ko", "pl", "uk", "zh_CN", "zh_TW"};
    if (!codes.contains(code))
        return;
    for (const auto& translator : translators_)
        QCoreApplication::removeTranslator(translator.get());
    translators_.clear();
    const QLocale locale(code);
    if (code != "en_US") {
        for (const auto& name : {"qtbase", "qtdeclarative"}) {
            auto translator = std::make_unique<QTranslator>();
            if (translator->load(locale, name, "_", QLibraryInfo::path(QLibraryInfo::TranslationsPath))) {
                QCoreApplication::installTranslator(translator.get());
                translators_.push_back(std::move(translator));
            }
        }
        auto translator = std::make_unique<QTranslator>();
        if (translator->load(Paths::instance().at("resources/desktop/translations/app_" + code + ".qm"))) {
            QCoreApplication::installTranslator(translator.get());
            translators_.push_back(std::move(translator));
        }
    }
    language_ = code;
    QLocale::setDefault(locale);
    QGuiApplication::setLayoutDirection(locale.textDirection());
    desktop_.setValue("interface/language", code);
    desktop_.sync();
    emit languageChanged();
    emit settingsUpdated();
    emit statusMessageChanged();
    emit gpuChoicesChanged();
    emit exportPhaseChanged();
    emit lutSaveChanged();
    emit lutReferenceChanged();
}
void AppController::cleanupCaches() {
    if (cacheBusy_) return;
    if (!cacheMaintenanceSafe()) {
        // A preview completion callback can arrive just before its worker has
        // unwound. Retry after the producer releases its job-local resources.
        if (idle() && !job_ && !analysisJob_ && !metadataJob_ && metadataScans_.empty() &&
            !runtimeInitializationStarted_ && previewPool_.activeThreadCount() > 0)
            cacheTrimTimer_.start();
        return;
    }
    if (lastCacheCleanup_.isValid() && lastCacheCleanup_.elapsed() < 5000) {
        cacheTrimTimer_.start(int(5000 - lastCacheCleanup_.elapsed()));
        return;
    }
    lastCacheCleanup_.restart();
    updateProcessingCache(true, true);
}
} // namespace ve
