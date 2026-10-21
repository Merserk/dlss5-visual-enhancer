#pragma once
#include "configuration/settings.hpp"
#include "presentation/batch_model.hpp"
#include "presentation/image_providers.hpp"
#include "processing/native_engines.hpp"
#include "processing/fsr_native.hpp"
#include "processing/pipeline.hpp"
#include "processing/preview_cache.hpp"
#include "media/video_preview.hpp"
#include "processing/video_still_preview.hpp"
#include "qml_facade.hpp"
#include <QFuture>
#include <QQuickWindow>
#include <QSettings>
#include <QThreadPool>
#include <QTimer>
#include <QElapsedTimer>
#include <QTranslator>
#include <QJsonObject>
#include <array>
#include <memory>

namespace ve {
struct PreviewContext {
    BatchModel* queue = nullptr;
    QString inputUrl, outputUrl, posterUrl, error, inputInfo, outputInfo;
    MediaInfo source;
    QSize outputSize;
    bool outputIsVideo = false;
    double previewSeconds = 0, playheadSeconds = 0;
    int previewFrame = 0;
    QVariantList ranges;
    quint64 generation = 0;
    quint64 posterGeneration = 0;
    std::shared_ptr<JobControl> posterJob;
};
class AppController : public QmlFacade {
    Q_OBJECT
  public:
    explicit AppController(PreviewProvider* previews, QObject* parent = nullptr);
    ~AppController() override;
    void attachWindow(QQuickWindow* window);
    void suspendProcessing();
    [[nodiscard]] QVariant read(Property property) const override;
    void write(Property property, const QVariant& value) override;
    QVariant execute(Action action, const QVariantList& arguments) override;
    [[nodiscard]] const Settings& settings() const { return settings_; }
    [[nodiscard]] QString contextKey() const;
    [[nodiscard]] QJsonObject lastPreviewProfile() const { return lastPreviewProfile_; }
  signals:
    void previewFinished(QJsonObject profile);

  private:
    QVariant handleSettings(Action action, const QVariantList& args);
    QVariant handleWorkflow(Action action, const QVariantList& args);
    [[nodiscard]] int contextIndex() const;
    [[nodiscard]] Workflow workflow() const;
    [[nodiscard]] PreviewContext& context();
    [[nodiscard]] const PreviewContext& context() const;
    [[nodiscard]] int previewDelay() const { return context().source.video ? 35 : 80; }
    [[nodiscard]] bool idle() const {
        return operation_ == "Idle" && !shuttingDown_ && !cacheCleaning_ && !cacheCleanPending_;
    }
    void notifyContext();
    void setOperation(const QString& operation, const QString& status = {});
    void log(const QString& message, const QString& tag = "desktop");
    void saveSettings();
    void settingsChanged();
    void requestReShadeInspection();
    void initializeReShadeImageSettings();
    void setLanguage(const QString& code);
    void addInputs(const QVariantList& paths, QImage clipboardImage = {}, QByteArray clipboardData = {});
    void scanMetadata(int contextIndex, const QStringList& paths);
    void loadSelected();
    [[nodiscard]] bool canFastColorPreview() const;
    void preview(double positionMs = -1, bool still = false);
    void refreshPoster(double positionMs);
    void batch(const QString& destinationMode, const QString& folder);
    void stopOperation();
    void analyzeLut(bool fixed, double positionMs = -1, bool reference = false);
    void shutdownAll();
    void persistLayout();
    void cleanupCaches();
    void updateProcessingCache(bool clean = false, bool automatic = false);
    [[nodiscard]] bool cacheMaintenanceSafe() const;
    [[nodiscard]] QSet<QString> protectedProcessingFiles() const;
    void copyClipboardText(const QString& text, quint64 request, quint32 sequence, int attempts);
    [[nodiscard]] QString translate(const QString& text) const;
    PreviewProvider* previews_;
    Settings settings_;
    QSettings desktop_;
    QString tab_ = "neural-rendering", nrMode_ = "Image", upscaleMode_ = "Image";
    QString operation_ = "Idle", status_ = "Initializing runtime…", runtime_ = "Initializing", runtimeError_,
            presetStatus_, exportPhase_;
    QString destinationMode_ = "output", exportFolder_, language_ = "en_US";
    bool shuttingDown_ = false, autoPreview_ = true, focus_ = false, fastPreview_ = false,
         previewDirty_ = false;
    double split_ = 0.5, progress_ = 0;
    int inspectorWidth_ = 400, queueHeight_ = 330, logHeight_ = 220;
    std::array<PreviewContext, 5> contexts_;
    std::vector<Gpu> gpus_;
    std::vector<FsrAdapter> fsrAdapters_;
    QStringList logs_, warnings_;
    std::vector<std::unique_ptr<QTranslator>> translators_;
    QTimer saveTimer_, autoTimer_, scrubTimer_, cleanupTimer_;
    QElapsedTimer lastCacheCleanup_;
    QTemporaryDir transientFiles_;
    ProcessingTemporaryDirectory previewClips_{ProcessingCacheKind::PreviewClips};
    QThreadPool pool_, previewPool_, cachePool_, reshadeInspectionPool_;
    QVariantMap reshadeInspection_;
    QString reshadeRequestedKey_, reshadeLoadedKey_;
    bool reshadeInspectionBusy_ = false;
    quint64 reshadeReloadGeneration_ = 0;
    ProcessingCacheSnapshot cacheSnapshot_;
    bool cacheBusy_ = false, cacheCleaning_ = false, cacheReady_ = false;
    bool cacheCleanPending_ = false;
    QString cacheStatus_;
    QTimer cacheRefreshTimer_, cacheTrimTimer_;
    VideoFrameReader videoFrames_;
    std::unique_ptr<VideoStillPreview> videoStill_;
    PreviewFrameCache previewStages_{192 * 1024};
    PreviewVideoCache previewVideos_;
    QJsonObject lastPreviewProfile_;
    std::shared_ptr<JobControl> job_, metadataJob_, analysisJob_, runtimeJob_, prefetchJob_;
    std::vector<std::shared_ptr<JobControl>> metadataScans_;
    quint64 operationId_ = 0;
    quint64 settingsRevision_ = 0;
    quint64 previewRequestRevision_ = 0;
    quint64 clipboardRequestRevision_ = 0;
    bool runtimeInitializationStarted_ = false;
    QString lutAutoMode_, lutReferenceStatus_, lutSaveStatus_, lutSavedPath_;
    bool lutSaving_ = false;
    QPointer<QQuickWindow> window_;
};
} // namespace ve
