#include "controller.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "processing/effects.hpp"
#include "processing/vfx_native.hpp"
#include "processing/vfx_worker.hpp"
#include "processing/fsr_worker.hpp"
#include "media/timeline.hpp"
#include "media/video_preview.hpp"
#include <QClipboard>
#include <QDesktopServices>
#include <QDir>
#include <QFileInfo>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QMimeData>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QSaveFile>
#include <QScopeGuard>
#include <QTemporaryDir>
#include <QUuid>
#include <QUrl>
#include <QtConcurrent/QtConcurrentRun>
#include <algorithm>
#include <cmath>
#include <windows.h>
#include <shlobj.h>

namespace ve {
namespace {
bool selectFileInExplorer(const QString& path) {
    if (path.isEmpty()) return false;
    const auto initialized = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
    if (FAILED(initialized) && initialized != RPC_E_CHANGED_MODE) return false;
    const auto apartment = qScopeGuard([initialized] {
        if (SUCCEEDED(initialized)) CoUninitialize();
    });
    const auto native = QDir::toNativeSeparators(QFileInfo(path).absoluteFilePath()).toStdWString();
    PIDLIST_ABSOLUTE raw = nullptr;
    const auto parsed = SHParseDisplayName(native.c_str(), nullptr, &raw, 0, nullptr);
    const auto item = qScopeGuard([raw] { CoTaskMemFree(raw); });
    // A single absolute item with cidl=0 opens its parent and selects it.
    // No file association is invoked, and paths need no command-line quoting.
    return SUCCEEDED(parsed) && SUCCEEDED(SHOpenFolderAndSelectItems(raw, 0, nullptr, 0));
}
} // namespace

void AppController::copyClipboardText(const QString& text, quint64 request, quint32 sequence, int attempts) {
    if (shuttingDown_ || request != clipboardRequestRevision_ || GetClipboardSequenceNumber() != sequence) return;
    if (OpenClipboard(nullptr)) {
        CloseClipboard();
        auto* clipboard = QGuiApplication::clipboard();
        clipboard->setText(text);
        if (clipboard->ownsClipboard()) return;
    }
    if (!attempts) {
        log("The clipboard is busy. Copy again when it is available.");
        return;
    }
    QTimer::singleShot(25, this, [this, text, request, sequence, attempts] {
        copyClipboardText(text, request, sequence, attempts-1);
    });
}
QVariant AppController::handleWorkflow(Action action, const QVariantList& args) {
    switch (action) {
    case Action::initializeRuntime: {
        if (shuttingDown_ || runtime_ == "Ready" || runtimeInitializationStarted_)
            return {};
        runtimeInitializationStarted_ = true;
        runtimeJob_ = std::make_shared<JobControl>();
        const auto control = runtimeJob_;
        const auto selection = settings_.ai_gpu_uuid;
        const auto warmStages = settings_.image_enabled_stages.contains("neural_model") ? QStringList{"neural_model"} : QStringList{};
        auto future = QtConcurrent::run(&pool_, [this, selection, control, warmStages] {
            std::vector<Gpu> devices;
            std::vector<FsrAdapter> fsrDevices;
            QString error;
            try {
                devices = detectGpus(control.get());
                fsrDevices = detectFsrAdapters(control.get());
                control->checkpoint();
                const bool nvidia=std::any_of(devices.begin(),devices.end(),[](const Gpu& gpu){return gpu.compatible;});
                const bool fsr=std::any_of(fsrDevices.begin(),fsrDevices.end(),[](const FsrAdapter& gpu){return gpu.available;});
                if (!nvidia&&!fsr) throw std::runtime_error(QT_TRANSLATE_NOOP("App", "No compatible NVIDIA or FSR DirectX 12 GPU was detected."));
                // Initialize selected image NR asynchronously before the first
                // preview; dimensions and inference still belong to the job.
                if(nvidia&&!warmStages.isEmpty())NativeEngines::instance().prepare(gpuOrdinal(selection), warmStages);
                control->checkpoint();
            } catch (const std::exception& e) { error = QString::fromUtf8(e.what()); }
            QMetaObject::invokeMethod(this, [this, devices, fsrDevices, error] {
                if (shuttingDown_) return;
                runtimeInitializationStarted_ = false;
                runtimeJob_.reset();
                gpus_ = devices;
                fsrAdapters_=fsrDevices;
                runtime_ = error.isEmpty() ? "Ready" : "Failed";
                runtimeError_ = error;
                status_ = error.isEmpty() ? QT_TRANSLATE_NOOP("App", "Ready. Add files to begin.") : error;
                if (!error.isEmpty()) log(error, "runtime");
                emit runtimeStateChanged(); emit gpuChoicesChanged(); emit statusMessageChanged();
            }, Qt::QueuedConnection);
        });
        (void)future;
        return {};
    }
    case Action::saveWindowLayout:
        desktop_.setValue("window/x", args[0]);
        desktop_.setValue("window/y", args[1]);
        desktop_.setValue("window/width", args[2]);
        desktop_.setValue("window/height", args[3]);
        desktop_.setValue("window/maximized", args[4]);
        return {};
    case Action::addFiles:
        addInputs(args[0].toList());
        return {};
    case Action::_sweep_temp_caches:
        cleanupCaches();
        return {};
    case Action::selectQueueItem:
        if (idle()) {
            context().queue->select(args[0].toInt());
            loadSelected();
        }
        return {};
    case Action::removeQueueItem:
        if (idle()) {
            context().queue->remove(args[0].toInt());
            loadSelected();
        }
        return {};
    case Action::clearActiveQueue:
        if (idle()) {
            context().queue->clear();
            loadSelected();
        }
        return {};
    case Action::clearSelectedPreviewItem:
        if (idle()) {
            context().queue->select(-1);
            loadSelected();
        }
        return {};
    case Action::showSelectedInExplorer:
    case Action::showInExplorer: {
        const auto path = localPath(action == Action::showSelectedInExplorer
                                        ? context().queue->selectedPath() : args[0].toString());
        if (!path.isEmpty()) {
            // Shell parsing can wait for network locations; keep it off the UI thread.
            auto future = QtConcurrent::run(&pool_, [this, path] {
                if (!selectFileInExplorer(path))
                    QMetaObject::invokeMethod(this, [this, path] {
                        if (!shuttingDown_) log(QString("Could not show %1 in File Explorer.").arg(path), "error");
                    }, Qt::QueuedConnection);
            });
            (void)future;
        }
        return {};
    }
    case Action::clearCompletedQueueItems:
        if (idle()) {
            context().queue->clearCompleted();
            loadSelected();
        }
        return {};
    case Action::retryFailedQueueItems:
        if (idle())
            context().queue->retryFailed();
        return {};
    case Action::startActiveBatch:
    case Action::requestActiveBatchExport:
        if (!read(Property::canRender).toBool() || !context().queue->count()) {
            status_ = "No files in queue to process.";
            emit statusMessageChanged();
            return false;
        }
        emit batchExportRequested(contextKey());
        return true;
    case Action::startActiveBatchWithExport: {
        if (args[0].toString() != contextKey())
            return QCoreApplication::translate("App", "The active workflow changed. Reopen Export Path.");
        if (!idle())
            return QCoreApplication::translate("App", "Another operation is active; stop it before starting a batch.");
        if (!context().queue->count())
            return QCoreApplication::translate("App", "No files in queue to process.");
        const auto mode = args[1].toString(), folder = localPath(args[2].toString()),
                   rename = args[3].toString(), suffix = args[4].toString();
        if (!QStringList{"input", "output", "folder"}.contains(mode))
            return QCoreApplication::translate("App", "Choose an Export Path option.");
        if (mode == "folder" && !QFileInfo(folder).isDir())
            return QCoreApplication::translate("App", "Choose an existing export folder.");
        QString prefix = contextIndex() == 4   ? "frame_interpolation_"
                         : contextIndex() == 2 ? "upscale_image_"
                         : contextIndex() == 3 ? "upscale_"
                         : contextIndex() == 1 ? "video_"
                                               : "image_";
        auto candidate = settings_;
        const auto setRename = [&](QString& mode, QString& value) {
            mode = rename;
            value = suffix;
        };
        if (prefix == "frame_interpolation_")
            setRename(candidate.frame_interpolation_rename_mode, candidate.frame_interpolation_custom_suffix);
        else if (prefix == "upscale_image_")
            setRename(candidate.upscale_image_rename_mode, candidate.upscale_image_custom_suffix);
        else if (prefix == "upscale_")
            setRename(candidate.upscale_rename_mode, candidate.upscale_custom_suffix);
        else if (prefix == "video_")
            setRename(candidate.video_rename_mode, candidate.video_custom_suffix);
        else
            setRename(candidate.image_rename_mode, candidate.image_custom_suffix);
        if (auto valid = candidate.validate(); !valid)
            return valid.error();
        settings_ = std::move(candidate);
        for (const auto& path : context().queue->paths()) {
            const auto planned = renderOutputPath(path,
                                                  mode == "input"    ? QFileInfo(path).absolutePath()
                                                  : mode == "folder" ? folder
                                                                     : Paths::instance().at("outputs"),
                                                  settings_, workflow(), isVideo(path));
            (void)planned;
        }
        destinationMode_ = mode;
        if (mode == "folder")
            exportFolder_ = folder;
        desktop_.setValue("export/destinationMode", mode);
        desktop_.setValue("export/folder", exportFolder_);
        desktop_.sync();
        emit exportPrefsChanged();
        saveSettings();
        batch(mode, folder);
        return QString{};
    }
    case Action::pauseExport:
        if (job_ && operation_ == "BatchRunning") {
            job_->pause(true);
            for (auto& current : contexts_)
                current.queue->setPaused(true);
            exportPhase_ = QT_TRANSLATE_NOOP("App", "Paused");
            emit exportPhaseChanged();
        }
        return {};
    case Action::toggleExportPause:
        if (job_ && operation_ == "BatchRunning") {
            job_->pause(!job_->paused());
            for (auto& current : contexts_)
                current.queue->setPaused(job_->paused());
            exportPhase_ = job_->paused() ? QT_TRANSLATE_NOOP("App", "Paused") : "Processing";
            emit exportPhaseChanged();
        }
        return {};
    case Action::stopActiveBatch:
        stopOperation();
        return {};
    case Action::renderPreview:
        preview(-1, true);
        return {};
    case Action::notePlayheadMs:
        context().playheadSeconds = std::max(0., args[0].toDouble() / 1000);
        return {};
    case Action::renderPreviewAt:
        context().playheadSeconds = std::max(0., args[0].toDouble() / 1000);
        preview(args[0].toDouble(), false);
        return {};
    case Action::schedulePreviewAt:
        context().playheadSeconds = std::max(0., args[0].toDouble() / 1000);
        if (autoPreview_ && context().source.width > 0 && workflow() != Workflow::FrameInterpolation) {
            if (prefetchJob_) prefetchJob_->cancel();
            autoTimer_.stop();
            ++previewRequestRevision_;
            if (operation_ == "PreviewRunning" && job_) {
                previewDirty_ = true;
                job_->cancel();
            }
            scrubTimer_.start(args.size() > 1 && args[1].toBool() ? 35 : 0);
        }
        return {};
    case Action::refreshVideoPoster:
        refreshPoster(args[0].toDouble());
        return {};
    case Action::shutdown:
        shutdownAll();
        return {};
    case Action::openFolder: {
        const auto& paths = Paths::instance();
        const QMap<QString, QString> folders{{"outputs", paths.at("outputs")},
                                             {"logs", paths.at("logs")},
                                             {"config", QFileInfo(paths.configurationFile()).absolutePath()},
                                             {"temp", transientFiles_.path()}, {"root", paths.root}, {"", paths.root}};
        const auto requested = args[0].toString();
        QDesktopServices::openUrl(QUrl::fromLocalFile(folders.value(requested, localPath(requested))));
        return {};
    }
    case Action::openThirdPartyNotices:
        QDesktopServices::openUrl(QUrl::fromLocalFile(Paths::instance().at("THIRD-PARTY-NOTICES.txt")));
        return {};
    case Action::copyToClipboard:
        copyClipboardText(args[0].toString(), ++clipboardRequestRevision_, GetClipboardSequenceNumber(), 40);
        return {};
    case Action::pasteFromClipboard: {
        if (!idle())
            return {};
        const auto* mime = QGuiApplication::clipboard()->mimeData();
        if (!mime) return {};
        QVariantList local;
        if (mime->hasUrls())
            for (const auto& url : mime->urls())
                if (url.isLocalFile())
                    local.append(url.toLocalFile());
        if (!local.isEmpty()) {
            addInputs(local);
            return {};
        }
        if (mime->hasImage()) {
            const auto image = qvariant_cast<QImage>(mime->imageData());
            if (!image.isNull()) {
                addInputs({},image);
                return {};
            }
        }
        QString url;
        const auto html = mime->html();
        for (const auto& pattern :
             {"data-(?:src|original|large-image|zoom-image|full-image)\\s*=\\s*[\"'](https?://[^\"']+)",
              "<img\\b[^>]*?\\bsrc\\s*=\\s*[\"'](https?://[^\"']+)"}) {
            const auto match =
                QRegularExpression(pattern, QRegularExpression::CaseInsensitiveOption).match(html);
            if (match.hasMatch()) {
                url = match.captured(1);
                break;
            }
        }
        if (url.isEmpty()) {
            if (mime->hasUrls() && !mime->urls().isEmpty())
                url = mime->urls().front().toString();
            else
                url = QRegularExpression("https?://[^\\s\"'<>]+", QRegularExpression::CaseInsensitiveOption)
                          .match(mime->text())
                          .captured();
        }
        if (url.isEmpty())
            return {};
        auto* manager = new QNetworkAccessManager(this);
        QNetworkRequest request{QUrl(url)};
        request.setRawHeader(
            "User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36");
        request.setTransferTimeout(15000);
        auto* reply = manager->get(request);
        connect(reply, &QNetworkReply::downloadProgress, reply, [reply](qint64 received, qint64 total) {
            if (received > 30 * 1024 * 1024 || total > 30 * 1024 * 1024)
                reply->abort();
        });
        connect(reply, &QNetworkReply::finished, this, [this, reply, manager] {
            if (reply->error() != QNetworkReply::NoError) {
                status_ = QCoreApplication::translate("App", "Clipboard download failed: %1").arg(reply->errorString());
                emit statusMessageChanged();
            } else {
                const auto bytes=reply->readAll();
                if (bytes.isEmpty()) {
                    status_=QT_TRANSLATE_NOOP("App", "The clipboard URL did not contain a readable image.");
                    emit statusMessageChanged();
                } else addInputs({}, {},bytes);
            }
            reply->deleteLater();
            manager->deleteLater();
        });
        return {};
    }
    case Action::_append_backend_log: {
        const auto line = '[' + args[0].toString() + "][" + args[1].toString() + "] " + args[2].toString();
        logs_.append(line);
        while (logs_.size() > 2000)
            logs_.removeFirst();
        emit logAppended(line);
        return {};
    }
    case Action::clearLogDisplay:
        logs_.clear();
        return {};
    default:
        throw std::logic_error("Unimplemented workflow action.");
    }
}
void AppController::addInputs(const QVariantList& urls, QImage clipboardImage, QByteArray clipboardData) {
    if (!idle())
        return;
    QStringList paths;
    for (const auto& url : urls)
        paths.append(url.toString());
    const int original = contextIndex();
    const auto tab = tab_;
    const auto id = ++operationId_;
    setOperation("ScanningInputs", QT_TRANSLATE_NOOP("App", "Scanning input files…"));
    job_ = std::make_shared<JobControl>();
    const auto control = job_;
    auto future = QtConcurrent::run(&pool_, [this, paths, original, tab, id, control,
                                           clipboardImage=std::move(clipboardImage), clipboardData=std::move(clipboardData)] {
        QStringList images, videos;
        QString error;
        std::shared_ptr<QTemporaryDir> clipboardDirectory;
        try {
            auto incoming=paths;
            if (!clipboardImage.isNull() || !clipboardData.isEmpty()) {
                control->checkpoint();
                auto image=clipboardImage.isNull() ? QImage::fromData(clipboardData) : clipboardImage;
                if (image.isNull()) throw std::runtime_error("The clipboard URL did not contain a readable image.");
                clipboardDirectory=std::shared_ptr<QTemporaryDir>(
                    new QTemporaryDir(),
                    [](QTemporaryDir* directory) { (void)QtConcurrent::run([directory] { delete directory; }); });
                if (!clipboardDirectory->isValid()) throw std::runtime_error("Cannot create clipboard image storage.");
                const auto path=clipboardDirectory->filePath("clipboard.png");
                if (!image.save(path)) throw std::runtime_error("Cannot save the clipboard image.");
                control->checkpoint();
                incoming.append(path);
            }
            for (const auto& path : collectInputs(incoming, true)) {
                control->checkpoint();
                (isVideo(path) ? videos : images).append(path);
            }
        } catch (const std::exception& e) {
            error = QString::fromUtf8(e.what());
        }
        QMetaObject::invokeMethod(
            this,
            [this, images, videos, original, tab, id, error, clipboardDirectory] {
                if (id != operationId_ || shuttingDown_)
                    return;
                job_.reset();
                const int imageContext = tab == "upscale" ? 2 : 0, videoContext = tab == "frame-interpolation"
                                                                                      ? 4
                                                                                  : tab == "upscale" ? 3
                                                                                                     : 1;
                if (tab != "frame-interpolation") {
                    contexts_[size_t(imageContext)].queue->add(images,clipboardDirectory);
                    scanMetadata(imageContext, images);
                }
                contexts_[size_t(videoContext)].queue->add(videos);
                scanMetadata(videoContext, videos);
                if ((original % 2 == 0 && images.isEmpty() && !videos.isEmpty()) ||
                    (original % 2 == 1 && videos.isEmpty() && !images.isEmpty())) {
                    if (tab == "neural-rendering") {
                        nrMode_ = images.isEmpty() ? "Video" : "Image";
                        emit nrModeChanged();
                        emit workflowStagesChanged();
                    } else if (tab == "upscale") {
                        upscaleMode_ = images.isEmpty() ? "Video" : "Image";
                        emit upscaleModeChanged();
                    }
                }
                setOperation("Idle", error.isEmpty()
                                         ? QCoreApplication::translate("App", "Files added: %1.").arg(images.size() + videos.size())
                                         : error);
                loadSelected();
            },
            Qt::QueuedConnection);
    });
    (void)future;
}
void AppController::loadSelected() {
    if (prefetchJob_) prefetchJob_->cancel();
    const int index = contextIndex();
    auto& current = context();
    const auto path = current.queue->selectedPath();
    if (metadataJob_)
        metadataJob_->cancel();
    const auto generation = ++current.generation;
    const auto cached = current.queue->selectedIndex() >= 0
                            ? current.queue->entry(current.queue->selectedIndex())
                            : BatchEntry{};
    current.inputUrl.clear();
    current.outputUrl.clear();
    current.posterUrl.clear();
    current.error.clear();
    current.source = {};
    current.outputSize = {};
    current.outputIsVideo = false;
    current.ranges.clear();
    current.inputInfo.clear();
    current.outputInfo.clear();
    notifyContext();
    if (path.isEmpty())
        return;
    metadataJob_ = std::make_shared<JobControl>();
    const auto control = metadataJob_;
    auto future = QtConcurrent::run(&pool_, [this, index, path, generation, control, cached] {
        MediaInfo info, result;
        QImage poster, output;
        QString error, existing;
        QString posterUrl, thumbnailUrl, outputUrl;
        QByteArray posterIdentity;
        try {
            info = cached.source.width > 0 ? cached.source : probeMedia(path, control.get());
            if (info.video) poster=decodeVideoPoster(path,0,*control);
            else {
                auto source=decodeStillPreviewSource(path,previewStages_,*control);
                poster=std::move(source.pixels); posterIdentity=QByteArray("still-input/v1")+source.identity;
            }
            if (cached.state == "Completed" && QFileInfo(cached.output).isFile()) {
                existing = cached.output;
                result = cached.result.width > 0 ? cached.result : probeMedia(existing, control.get());
                if (!result.video)
                    output = decodeImage(existing, *control);
            }
            control->checkpoint();
            posterUrl = posterIdentity.isEmpty() ? previews_->store(poster) : previews_->storeImmutable(poster,posterIdentity);
            thumbnailUrl = info.video ? cached.thumbnail : QString{};
            if (thumbnailUrl.isEmpty()) {
                auto thumbnail = info.video ? decodeVideoPoster(path, 0, *control, 256)
                    : poster.scaled(256, 256, Qt::KeepAspectRatio, Qt::SmoothTransformation);
                thumbnailUrl = previews_->store(thumbnail);
            }
            if (!existing.isEmpty())
                outputUrl = result.video ? QUrl::fromLocalFile(existing).toString() : previews_->store(output);
        } catch (const std::exception& e) {
            error = QString::fromUtf8(e.what());
        }
        QMetaObject::invokeMethod(
            this,
            [this, index, path, generation, info, posterUrl, thumbnailUrl, error, result, outputUrl, existing] {
                if (shuttingDown_)
                    return;
                auto& current = contexts_[size_t(index)];
                if (current.generation != generation || current.queue->selectedPath() != path)
                    return;
                metadataJob_.reset();
                current.source = info;
                current.inputInfo = info.description();
                current.posterUrl = posterUrl;
                current.inputUrl = info.video ? QUrl::fromLocalFile(path).toString() : current.posterUrl;
                current.error = error;
                current.queue->metadata(current.queue->selectedIndex(), info, thumbnailUrl);
                if (!existing.isEmpty() && error.isEmpty()) {
                    current.outputUrl = outputUrl;
                    current.outputIsVideo = result.video;
                    current.outputSize = {result.width, result.height};
                    current.outputInfo = result.description();
                }
                if (contextIndex() == index) {
                    notifyContext();
                    if (autoPreview_ && error.isEmpty() && existing.isEmpty())
                        autoTimer_.start(previewDelay());
                }
            },
            Qt::QueuedConnection);
    });
    (void)future;
}
void AppController::scanMetadata(int index, const QStringList& paths) {
    if (paths.isEmpty())
        return;
    auto control = std::make_shared<JobControl>();
    metadataScans_.push_back(control);
    auto future = QtConcurrent::run(&pool_, [this, index, paths, control] {
        for (const auto& path : paths) {
            if (control->cancelled())
                break;
            try {
                const auto media = probeMedia(path, control.get());
                auto image = media.video ? decodeVideoPoster(path, 0, *control, 256)
                    : decodeImage(path, *control).scaled(256, 256, Qt::KeepAspectRatio, Qt::SmoothTransformation);
                const auto thumbnailUrl = previews_->store(image);
                QMetaObject::invokeMethod(
                    this,
                    [this, index, path, media, thumbnailUrl] {
                        if (shuttingDown_)
                            return;
                        auto* model = contexts_[size_t(index)].queue;
                        const auto row = model->indexOf(path);
                        if (row >= 0)
                            model->metadata(row, media, thumbnailUrl);
                    },
                    Qt::QueuedConnection);
            } catch (const Cancelled&) {
                break;
            } catch (const std::exception& error) {
                const auto message = QString::fromUtf8(error.what());
                QMetaObject::invokeMethod(
                    this,
                    [this, path, message] {
                        if (!shuttingDown_)
                            log(QFileInfo(path).fileName() + ": " + message, "metadata");
                    },
                    Qt::QueuedConnection);
            }
        }
        QMetaObject::invokeMethod(
            this, [this, control] { std::erase(metadataScans_, control); }, Qt::QueuedConnection);
    });
    (void)future;
}
bool AppController::canFastColorPreview() const {
    if (contextIndex() != 0 && contextIndex() != 1) return false;
    const auto& source = context().source;
    const auto pixels = qint64(source.width) * source.height;
    return pixels > 0 && pixels <= 8500000 && !source.hdr &&
           settings_.stages(source.video) == QStringList{"coloring"};
}
void AppController::preview(double positionMs, bool still) {
    if (!idle() || runtime_ != "Ready" || analysisJob_)
        return;
    auto &current = context();
    const auto path = current.queue->selectedPath();
    if (path.isEmpty() || current.source.width <= 0)
        return;
    if (prefetchJob_)
        prefetchJob_->cancel();
    const auto index = contextIndex();
    const auto workflowType = workflow();
    const auto info = current.source;
    const auto settings = info.video ? videoPreviewSettings(settings_) : settings_;
    const bool vfxGenerated=info.video&&settings.stages(true).contains("vfx_frame_generation");
    const bool fsrGenerated=info.video&&settings.stages(true).contains("fsr_frame_generation");
    const bool nativeRrAfterFi=info.video&&rrNeedsGeneratedHistory(settings.stages(true));
    const auto requestedPosition=quantizePlayhead(positionMs>=0?positionMs:current.playheadSeconds*1000,
        fsrGenerated?frameGenerationFrameRate(info.fps,settings,true):vfxGenerated?vfxFrameRate(info.fps,vfxSettingsForStages(settings,settings.stages(true))):info.fps,info.duration);
    const auto position=quantizePlayhead(requestedPosition.seconds*1000,info.fps,info.duration);
    const double seconds = position.seconds;
    if (workflowType == Workflow::FrameInterpolation)
        still = false;
    const bool fastColor = (!info.video || still) && canFastColorPreview();
    const auto id = ++operationId_;
    const auto generation = current.generation;
    const auto settingsRevision = settingsRevision_;
    const auto previewRevision = ++previewRequestRevision_;
    const auto posterGeneration = ++current.posterGeneration;
    job_ = std::make_shared<JobControl>();
    const auto control = job_;
    autoTimer_.stop();
    scrubTimer_.stop();
    previewDirty_ = false;
    current.error.clear();
    progress_ = 0;
    emit overallProgressChanged();
    fastPreview_ = fastColor;
    setOperation("PreviewRunning", QT_TRANSLATE_NOOP("App", "Rendering preview…"));
    QElapsedTimer previewElapsed;
    previewElapsed.start();
    auto future = QtConcurrent::run(&previewPool_, [this, index, path, settings, info, seconds, still,
                                                    workflowType, id, generation, settingsRevision,
                                                    previewRevision, posterGeneration, position, control,
                                                    fastColor, previewElapsed, vfxGenerated, fsrGenerated, nativeRrAfterFi, requestedPosition] {
        QJsonObject profile{{"queue_ms", double(previewElapsed.nsecsElapsed()) / 1e6},
                            {"frame", qint64(position.number)}};
        QElapsedTimer phase;
        phase.start();
        QImage input, output;
        QString clip, error, detail;
        double clipSeconds = 0;
        QSize size;
        QString outputUrl, posterUrl;
        QByteArray inputIdentity, outputIdentity;
        bool prefetchSource = false, timelinePacking = false;
        try {
            if (info.video && !still) {
                const QString duration = index == 4   ? settings.frame_interpolation_preview_length
                                         : index == 3 ? settings.upscale_preview_length
                                                      : settings.nr_preview_length;
                clipSeconds = std::min(duration.toDouble(), std::max(0., info.duration - seconds));
                const auto effective = workflowSettings(settings, workflowType, true);
                const auto stages = estimatePipeline(info, settings, workflowType).stages;
                auto identity = QByteArray("manual-clip/v1") + previewFileIdentity(path) +
                                previewFileIdentity(Paths::instance().ffmpeg()) +
                                QJsonDocument(QJsonObject{{"seconds", seconds},
                                                          {"length", clipSeconds},
                                                          {"frame", qint64(position.number)},
                                                          {"fps", info.fps},
                                                          {"workflow", int(workflowType)}})
                                    .toJson(QJsonDocument::Compact);
                const auto keys =
                    previewStageKeys(identity, effective, info, stages, true, Paths::instance().root);
                const auto key = keys.isEmpty()
                                     ? QCryptographicHash::hash(identity, QCryptographicHash::Sha256)
                                     : keys.back();
                if (auto cached = previewVideos_.find(key)) {
                    clip = cached->path;
                    size = {cached->info.width, cached->info.height};
                    detail = cached->info.description();
                } else {
                    clip = previewClips_.filePath("preview-" + QUuid::createUuid().toString(QUuid::WithoutBraces) + ".mkv");
                    ProcessingTemporaryDirectory sourceDirectory(ProcessingCacheKind::Scratch);
                    (void)sourceDirectory.filePath("source-preview.mkv");
                    if (!sourceDirectory.isValid())
                        throw std::runtime_error("Cannot create the preview source directory.");
                    const bool rrHistory=stages.contains("dlss_ray_reconstruction")||stages.contains("fsr_frame_generation");
                    const auto previewSource = rrHistory?path:
                        extractVideoPreviewSource(path, info, sourceDirectory.path(), seconds, clipSeconds,
                                                  false, *control, settings.ffmpeg_device);
                    Pipeline pipeline;
                    const auto result =
                        pipeline.render({previewSource, clip, settings, workflowType, true, rrHistory?seconds:0, clipSeconds},
                                        *control, [this, id](const Progress &progress) {
                                            QMetaObject::invokeMethod(
                                                this,
                                                [this, id, progress] {
                                                    if (id == operationId_) {
                                                        progress_ = progress.fraction;
                                                        status_ = progress.message;
                                                        emit overallProgressChanged();
                                                        emit statusMessageChanged();
                                                    }
                                                },
                                                Qt::QueuedConnection);
                                        });
                    size = {result.info.width, result.info.height};
                    detail = result.info.description();
                    previewClips_.cacheRoot()->publishFile(clip);
                    control->checkpoint();
                    previewVideos_.store(key, clip, result.info);
                }
            } else {
                auto effective = workflowSettings(settings, workflowType, info.video);
                const auto estimate = estimatePipeline(info, settings, workflowType);
                auto stages = estimate.stages;
                if(!nativeRrAfterFi)stages.removeAll("frame_generation");
                // Live auxiliary files can change independently of controls;
                // retain pixel-based provider identity for those outputs.
                const bool immutableOutput =
                    (!stages.contains("neural_model") ||
                     effective.nr_mask.value("path").toString().isEmpty()) &&
                    (!stages.contains("coloring") ||
                     (effective.lut_path.isEmpty() && effective.lut_reference_image.isEmpty())) &&
                    (!stages.contains("reshade") ||
                     reshadeSettingsForMode(effective, info.video).reshade_preset_path.isEmpty());
                int rotation = info.metadata.value("tags").toObject().value("rotate").toString().toInt();
                for (const auto &side : info.metadata.value("side_data_list").toArray())
                    if (side.toObject().contains("rotation"))
                        rotation = side.toObject().value("rotation").toInt();
                
                const bool hdrPreview = info.hdr || stages.contains("rtx_video_hdr");
                const bool fastHdr =
                    (!info.hdr || info.transfer == "smpte2084") &&
                    (stages == QStringList{"neural_model"} || stages == QStringList{"rtx_video_hdr"});
                const bool directVideoStill =
                    stages.size() == 1 && !stages.contains("dlss_ray_reconstruction") &&
                    !stages.contains("reshade") && rotation % 360 == 0 && (!hdrPreview || fastHdr);
                if(fsrGenerated){
                    auto source=videoFrames_.frame(path,info,position,*control,false,0,false);input=source.pixels;prefetchSource=true;
                    const auto identity=source.identity+QByteArray::number(requestedPosition.seconds,'g',17)+"/isolated-fsr/v1";
                    const auto keys=previewStageKeys(identity,effective,info,stages,true,Paths::instance().root);
                    if(auto cached=previewStages_.find(keys.back()))output=std::move(cached->pixels);
                    else{output=previewIsolatedFsr(path,effective,info,requestedPosition.seconds,*control);previewStages_.store(keys.back(),{output,false,false});}
                    if(immutableOutput)outputIdentity="video-output/isolated-fsr/v1"+keys.back();
                } else if(info.video&&hasVfxStages(stages)){
                    auto source=videoFrames_.frame(path,info,position,*control,false,gpuOrdinal(settings.ai_gpu_uuid),settings.ffmpeg_device!="cpu");input=source.pixels;prefetchSource=true;
                    const auto identity=source.identity+QByteArray::number(requestedPosition.seconds,'g',17)+"/isolated-vfx/v1";
                    const auto keys=previewStageKeys(identity,effective,info,stages,true,Paths::instance().root);
                    if(auto cached=previewStages_.find(keys.back()))output=std::move(cached->pixels);
                    else{output=previewIsolatedVfx(path,effective,info,requestedPosition.seconds,*control);previewStages_.store(keys.back(),{output,false,false});}
                    if(immutableOutput)outputIdentity="video-output/isolated-vfx/v1"+keys.back();
                } else if (fastColor && rotation % 360 == 0) {
                    auto identity =
                        QByteArray("fast-color-source/v1") + previewFileIdentity(path) +
                        previewFileIdentity(Paths::instance().ffmpeg()) +
                        QJsonDocument(QJsonObject{{"video", info.video},
                                                  {"seconds", seconds},
                                                  {"format", info.video ? "rgba64le" : info.pixelFormat},
                                                  {"fps", info.fps}})
                            .toJson(QJsonDocument::Compact);
                    const auto sourceKey = QCryptographicHash::hash(identity, QCryptographicHash::Sha256);
                    if (!info.video) {
                        auto source = decodeStillPreviewSource(path, previewStages_, *control);
                        input = std::move(source.pixels);
                        identity += source.identity;
                        inputIdentity = QByteArray("still-input/v1") + source.identity;
                    } else if (auto cached = previewStages_.find(sourceKey))
                        input = std::move(cached->pixels);
                    else {
                        input = info.video ? decodeTimelineFrame(path, info, seconds, *control,
                                                                 TimelinePrecision::Unorm16)
                                           : decodeImage(path, *control);
                        previewStages_.store(sourceKey, {input, false, false});
                    }
                    identity = QByteArray("fast-color-composed/v2") + identity +
                               (info.video ? previewPixelIdentity(input, *control) : QByteArray{});
                    const auto keys = previewStageKeys(identity, effective, info, {"coloring"}, true,
                                                       Paths::instance().root);
                    if (!info.video && immutableOutput)
                        outputIdentity = QByteArray("still-output/v1") + keys.back();
                    if (auto cached = previewStages_.find(keys.back()))
                        output = std::move(cached->pixels);
                    else {
                        output = colorStillPreview(input, effective, *control);
                        control->checkpoint();
                        previewStages_.store(keys.back(), {output, false, false});
                    }
                } else if (info.video && !directVideoStill) {
                    if (!hdrPreview || hasVfxStages(stages) || stages.contains("dlss_ray_reconstruction") ||
                        stages.contains("reshade")) {
                        auto source = videoFrames_.frame(path, info, position, *control, false,
                                                         gpuOrdinal(settings.ai_gpu_uuid),
                                                         settings.ffmpeg_device == "auto");
                        input = std::move(source.pixels);
                        prefetchSource = true;
                        profile["source_cached"] = source.cached;
                        profile["source_ms"] = double(phase.nsecsElapsed()) / 1e6;
                        phase.restart();
                        if (!stages.isEmpty())
                            NativeEngines::instance().prepare(gpuOrdinal(settings.ai_gpu_uuid), stages);
                        if (!videoStill_)
                            videoStill_ = std::make_unique<VideoStillPreview>();
                        std::vector<TimedImage> temporal;
                        if(!stages.contains("dlss_ray_reconstruction")&&(stages.contains("dlss_super_resolution")||stages.contains("reshade")||vfxGenerated)) {
                            const int count=reconstructionHistoryFrames(effective,stages);
                            temporal=videoFrames_.temporalContext(path,info,position,source.absoluteSeconds,count,
                                *control,gpuOrdinal(settings.ai_gpu_uuid),settings.ffmpeg_device=="auto",vfxGenerated||nativeRrAfterFi);
                        }
                        output = videoStill_->process(input, effective, info, stages, previewStages_,
                                                      source.identity, *control,temporal,(vfxGenerated||nativeRrAfterFi)?requestedPosition.seconds+info.presentationStartSeconds():stages.contains("dlss_ray_reconstruction")?source.absoluteSeconds:-1,path);
                        if(hdrPreview){auto colors=info;colors.hdr=true;if(stages.contains("rtx_video_hdr")){colors.primaries="bt2020";colors.transfer="smpte2084";}output=toneMapVideoPreview(output,colors,*control,settings.ffmpeg_device);}
                        profile["processing_ms"] = double(phase.nsecsElapsed()) / 1e6;
                        const auto keys = previewStageKeys(source.identity, effective, info, stages, true,
                                                           Paths::instance().root);
                        if (immutableOutput)
                            outputIdentity = QByteArray("video-output/v2") +
                                             (keys.isEmpty() ? source.identity : keys.back());
                    } else {
                        const auto identity = QByteArray("processed-video-still/v1") +
                                              previewFileIdentity(path) +
                                              QJsonDocument(QJsonObject{{"seconds", seconds},
                                                                        {"frame", qint64(position.number)},
                                                                        {"fps", info.fps},
                                                                        {"workflow", int(workflowType)}})
                                                  .toJson(QJsonDocument::Compact);
                        const auto keys =
                            previewStageKeys(identity, effective, info, stages, true, Paths::instance().root);
                        if (!keys.isEmpty()) {
                            if (auto cached = previewStages_.find(keys.back()))
                                output = std::move(cached->pixels);
                        }
                        if (output.isNull()) {
                            ProcessingTemporaryDirectory directory(ProcessingCacheKind::Scratch);
                            (void)directory.filePath("source-preview.mkv");
                            if (!directory.isValid())
                                throw std::runtime_error("Cannot create the still preview directory.");
                            const auto source =
                                extractVideoPreviewSource(path, info, directory.path(), seconds, 0, true,
                                                          *control, settings.ffmpeg_device);
                            profile["source_ms"] = double(phase.nsecsElapsed()) / 1e6;
                            phase.restart();
                            const auto rendered = directory.filePath("processed.mkv");
                            effective.video_enabled_stages = stages;
                            const auto result = Pipeline{}.render(
                                {source, rendered, effective, Workflow::NeuralRendering, true}, *control);
                            profile["processing_ms"] = double(phase.nsecsElapsed()) / 1e6;
                            phase.restart();
                            output = decodeProcessedVideoPreview(rendered, result.info, *control,
                                                                 settings.ffmpeg_device);
                            profile["display_decode_ms"] = double(phase.nsecsElapsed()) / 1e6;
                            control->checkpoint();
                            if (!keys.isEmpty())
                                previewStages_.store(keys.back(), {output, false, false});
                        }
                    }
                } else {
                    QByteArray sourceIdentity;
                    if (info.video) {
                        auto source = videoFrames_.frame(path, info, position, *control, true,
                                                         gpuOrdinal(settings.ai_gpu_uuid), false);
                        input = std::move(source.pixels);
                        sourceIdentity = std::move(source.identity);
                        prefetchSource = true;
                        timelinePacking = true;
                        profile["source_cached"] = source.cached;
                        profile["source_ms"] = double(phase.nsecsElapsed()) / 1e6;
                        phase.restart();
                    } else {
                        auto source = decodeStillPreviewSource(path, previewStages_, *control);
                        input = std::move(source.pixels);
                        sourceIdentity = std::move(source.identity);
                    }
                    if (!stages.isEmpty())
                        NativeEngines::instance().prepare(gpuOrdinal(settings.ai_gpu_uuid), stages);
                    FramePipeline pipeline(effective, info, stages);
                    auto identity = sourceIdentity + QByteArray::number(position.number) +
                                    QByteArray::number(info.fps, 'g', 17);
                    if (!info.video) {
                        inputIdentity = QByteArray("still-input/v1") + sourceIdentity;
                        const auto keys =
                            previewStageKeys(identity, effective, info, stages, true, Paths::instance().root);
                        if (immutableOutput)
                            outputIdentity =
                                QByteArray("still-output/v1") +
                                (keys.isEmpty()
                                     ? QCryptographicHash::hash(identity, QCryptographicHash::Sha256)
                                     : keys.back());
                    } else if (immutableOutput) {
                        const auto keys =
                            previewStageKeys(identity, effective, info, stages, true, Paths::instance().root);
                        outputIdentity =
                            QByteArray("video-output/v2") + (keys.isEmpty() ? identity : keys.back());
                    }
                    pipeline.configurePreviewCache(previewStages_, std::move(identity));
                    output = pipeline.process(input, *control, 0, true);
                    if (info.video && hdrPreview) {
                        auto colors = info;
                        if (stages.contains("rtx_video_hdr")) {
                            colors.hdr = true;
                            colors.primaries = "bt2020";
                            colors.transfer = "smpte2084";
                        }
                        output = toneMapVideoPreview(output, colors, *control, settings.ffmpeg_device);
                    }
                    profile["processing_ms"] = double(phase.nsecsElapsed()) / 1e6;
                }
                size = output.size();
                detail = QString::number(size.width()) + QChar(0xD7) + QString::number(size.height());
            }
            control->checkpoint();
            profile["render_ms"] = double(previewElapsed.nsecsElapsed()) / 1e6;
            phase.restart();
            outputUrl = !clip.isEmpty()            ? QUrl::fromLocalFile(clip).toString()
                        : outputIdentity.isEmpty() ? previews_->store(output)
                                                   : previews_->storeImmutable(output, outputIdentity);
            if (!input.isNull() && !info.video)
                posterUrl = inputIdentity.isEmpty() ? previews_->store(input)
                                                    : previews_->storeImmutable(input, inputIdentity);
            profile["store_ms"] = double(phase.nsecsElapsed()) / 1e6;
            profile["backend_ms"] = double(previewElapsed.nsecsElapsed()) / 1e6;
            profile["source_cache_bytes"] = double(videoFrames_.allocatedBytes());
            profile["stage_cache_bytes"] = double(previewStages_.allocatedBytes());
            profile["provider_cache_bytes"] = double(previews_->memoryUsage());
        } catch (const std::exception &e) {
            error = QString::fromUtf8(e.what());
        }
        QMetaObject::invokeMethod(
            this,
            [this, index, id, generation, settingsRevision, previewRevision, posterGeneration, outputUrl,
             posterUrl, clip, error, detail, seconds, clipSeconds, position, size, profile, prefetchSource,
             timelinePacking, path, info, settings] {
                if (id != operationId_ || shuttingDown_)
                    return;
                auto &current = contexts_[size_t(index)];
                job_.reset();
                const bool currentResult = current.generation == generation &&
                                           settingsRevision_ == settingsRevision &&
                                           previewRequestRevision_ == previewRevision;
                if (currentResult) {
                    current.error = error;
                    if (error.isEmpty()) {
                        current.outputUrl = outputUrl;
                        current.outputIsVideo = !clip.isEmpty();
                        current.outputSize = size;
                        current.outputInfo = detail;
                        current.previewSeconds = seconds;
                        current.previewFrame = int(position.number);
                        if (!posterUrl.isEmpty() && current.posterGeneration == posterGeneration) {
                            current.posterUrl = posterUrl;
                            if (!current.source.video)
                                current.inputUrl = current.posterUrl;
                        }
                        if (!clip.isEmpty()) {
                            current.ranges.append(QVariantMap{{"start", seconds},
                                                              {"end", seconds + clipSeconds},
                                                              {"url", current.outputUrl}});
                            while (current.ranges.size() > 32)
                                current.ranges.removeFirst();
                        } else
                            current.ranges.clear();
                    }
                }
                progress_ = currentResult && error.isEmpty() ? 1 : 0;
                fastPreview_ = false;
                emit vfxStatusChanged();
                emit overallProgressChanged();
                setOperation("Idle", currentResult ? error.isEmpty() ? QT_TRANSLATE_NOOP("App", "Preview ready.") : error
                                                   : QT_TRANSLATE_NOOP("App", "Preview updated; waiting for the latest request."));
                cleanupCaches();
                if (contextIndex() == index)
                    notifyContext();
                if (currentResult && error.isEmpty()) {
                    lastPreviewProfile_ = profile;
                    if (qEnvironmentVariableIntValue("VE_PREVIEW_PROFILE"))
                        log(QString::fromUtf8(QJsonDocument(profile).toJson(QJsonDocument::Compact)),
                            "preview-performance");
                    emit previewFinished(profile);
                    if (prefetchSource && autoPreview_ && idle() && !previewDirty_ &&
                        !scrubTimer_.isActive() && !autoTimer_.isActive()) {
                        if (prefetchJob_)
                            prefetchJob_->cancel();
                        prefetchJob_ = std::make_shared<JobControl>();
                        const auto prefetchControl = prefetchJob_;
                        (void)QtConcurrent::run(&previewPool_, [this, path, info, position, settings,
                                                                timelinePacking, prefetchControl] {
                            try {
                                videoFrames_.prefetch(path, info, position, *prefetchControl, timelinePacking,
                                                      gpuOrdinal(settings.ai_gpu_uuid),
                                                      !timelinePacking && settings.ffmpeg_device == "auto");
                            } catch (const std::exception &) {
                                // A new request can interrupt background decoding at any point.
                            }
                        });
                    }
                }
                if (previewDirty_ && autoPreview_) {
                    previewDirty_ = false;
                    if (!scrubTimer_.isActive() && !autoTimer_.isActive())
                        autoTimer_.start(0);
                }
            },
            Qt::QueuedConnection);
    });
    (void)future;
}
void AppController::refreshPoster(double positionMs) {
    const auto index = contextIndex();
    auto &current = context();
    const auto path = current.queue->selectedPath();
    if (shuttingDown_ || path.isEmpty() || !current.source.video)
        return;
    const auto generation = current.generation;
    const auto posterGeneration = ++current.posterGeneration;
    const auto seconds = std::isfinite(positionMs) ? std::max(0., positionMs / 1000) : 0.;
    if (current.posterJob)
        current.posterJob->cancel();
    auto control = std::make_shared<JobControl>();
    current.posterJob = control;
    metadataScans_.push_back(control);
    auto future = QtConcurrent::run(&pool_, [this, index, path, generation, posterGeneration, seconds,
                                             control] {
        QString url, error;
        try {
            auto image = decodeVideoPoster(path, seconds, *control);
            control->checkpoint();
            url = previews_->store(image);
        } catch (const std::exception& e) {
            error = QString::fromUtf8(e.what());
        }
        QMetaObject::invokeMethod(this, [this, index, path, generation, posterGeneration, control, url, error] {
            std::erase(metadataScans_, control);
            auto& current = contexts_[size_t(index)];
            if (shuttingDown_ || current.generation != generation || current.posterGeneration != posterGeneration || current.queue->selectedPath() != path)
                return;
            if (!error.isEmpty()) {
                if (!control->cancelled())
                    log("Poster refresh failed: " + error);
                return;
            }
            current.posterUrl = url;
            if (contextIndex() == index)
                emit previewPosterUrlChanged();
        }, Qt::QueuedConnection);
    });
    (void)future;
}
void AppController::batch(const QString& destinationMode, const QString& folder) {
    if (prefetchJob_) prefetchJob_->cancel();
    const int index = contextIndex();
    auto* queue = context().queue;
    const auto paths = queue->paths();
    const auto settings = settings_;
    const auto workflowType = workflow();
    const auto id = ++operationId_;
    queue->setPaused(false);
    queue->resetStates();
    job_ = std::make_shared<JobControl>();
    const auto control = job_;
    autoTimer_.stop();
    scrubTimer_.stop();
    progress_ = 0;
    emit overallProgressChanged();
    setOperation("BatchRunning", QT_TRANSLATE_NOOP("App", "Processing batch…"));
    auto future = QtConcurrent::run(&pool_, [this, index, paths, settings, workflowType, destinationMode,
                                             folder, id, control] {
        auto release = QtConcurrent::run(&previewPool_, [this] {
            videoFrames_.clear();
            videoStill_.reset();
        });
        release.waitForFinished();
        int successes = 0, failures = 0;
        bool cancelled = false;
        for (qsizetype row = 0; row < paths.size(); ++row) {
            if (control->cancelled()) {
                cancelled = true;
                break;
            }
            const auto path = paths[row];
            QMetaObject::invokeMethod(
                this,
                [this, index, row, id] {
                    if (id == operationId_)
                        contexts_[size_t(index)].queue->update(int(row), "Running", 0, "Processing");
                },
                Qt::QueuedConnection);
            try {
                const auto directory = destinationMode == "input"    ? QFileInfo(path).absolutePath()
                                       : destinationMode == "folder" ? folder
                                                                     : Paths::instance().at("outputs");
                const auto output = renderOutputPath(path, directory, settings, workflowType, isVideo(path));
                Pipeline pipeline;
                const auto result = pipeline.render(
                    {path, output, settings, workflowType, false, 0, 0, &previewStages_}, *control,
                    [this, index, row, count = paths.size(), id](const Progress& metrics) {
                        QMetaObject::invokeMethod(
                            this,
                            [this, index, row, count, id, metrics] {
                                if (id != operationId_)
                                    return;
                                auto* model = contexts_[size_t(index)].queue;
                                const bool paused = job_ && job_->paused();
                                model->update(int(row), paused ? QT_TRANSLATE_NOOP("App", "Paused") : "Running", metrics.fraction,
                                              paused ? QString(QT_TRANSLATE_NOOP("App", "Paused")) : metrics.message);
                                model->metrics(int(row), metrics.frames, metrics.totalFrames, metrics.fps);
                                progress_ = (row + metrics.fraction) / double(count);
                                exportPhase_ = paused ? QString(QT_TRANSLATE_NOOP("App", "Paused")) : metrics.message;
                                emit overallProgressChanged();
                                emit exportPhaseChanged();
                            },
                            Qt::QueuedConnection);
                    });
                ++successes;
                QMetaObject::invokeMethod(
                    this,
                    [this, index, row, id, result] {
                        if (id == operationId_)
                            contexts_[size_t(index)].queue->completed(int(row), result.output, result.info);
                    },
                    Qt::QueuedConnection);
            } catch (const Cancelled&) {
                cancelled = true;
                QMetaObject::invokeMethod(
                    this,
                    [this, index, row, id] {
                        if (id == operationId_)
                            contexts_[size_t(index)].queue->update(int(row), "Cancelled", 0, "Cancelled");
                    },
                    Qt::QueuedConnection);
                break;
            } catch (const std::exception& e) {
                ++failures;
                const auto error = QString::fromUtf8(e.what());
                QMetaObject::invokeMethod(
                    this,
                    [this, index, row, id, error] {
                        if (id == operationId_) {
                            contexts_[size_t(index)].queue->update(int(row), "Failed", 0, error);
                            log(error, "render");
                        }
                    },
                    Qt::QueuedConnection);
            }
        }
        QMetaObject::invokeMethod(
            this,
            [this, index, id, successes, failures, cancelled] {
                if (id != operationId_ || shuttingDown_)
                    return;
                job_.reset();
                for (auto& current : contexts_)
                    current.queue->setPaused(false);
                progress_ = cancelled ? progress_ : 1;
                exportPhase_.clear();
                emit overallProgressChanged();
                emit exportPhaseChanged();
                emit vfxStatusChanged();
                setOperation("Idle", QCoreApplication::translate("App", "%1 complete, %2 failed.")
                                         .arg(successes)
                                         .arg(failures)
                                         + (cancelled ? ' ' + QCoreApplication::translate("App", "Batch stopped by user.") : QString{}));
                if (contextIndex() == index && successes > 0)
                    loadSelected();
            },
            Qt::QueuedConnection);
    });
    (void)future;
}
void AppController::stopOperation() {
    autoTimer_.stop();
    scrubTimer_.stop();
    if (job_) {
        job_->cancel();
        setOperation(operation_.startsWith("Batch") ? "BatchCancelling" : "PreviewCancelling", QT_TRANSLATE_NOOP("App", "Stopping..."));
    }
    if (analysisJob_)
        analysisJob_->cancel();
}
void AppController::shutdownAll() {
    if (shuttingDown_)
        return;
    shuttingDown_ = true;
    const bool settingsPending = saveTimer_.isActive();
    saveTimer_.stop();
    autoTimer_.stop();
    scrubTimer_.stop();
    cleanupTimer_.stop();
    cacheRefreshTimer_.stop();
    cacheTrimTimer_.stop();
    if (runtimeJob_)
        runtimeJob_->cancel();
    if (prefetchJob_)
        prefetchJob_->cancel();
    if (job_)
        job_->cancel();
    if (metadataJob_)
        metadataJob_->cancel();
    if (analysisJob_)
        analysisJob_->cancel();
    for (auto& scan : metadataScans_)
        scan->cancel();
    if (settingsPending)
        saveSettings();
    desktop_.sync();
}
} // namespace ve
