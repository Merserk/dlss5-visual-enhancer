#include "controller.hpp"
#include "presentation/preview_player.hpp"
#include "infrastructure/log.hpp"
#include <QCoreApplication>
#include <QDateTime>
#include <QFileInfo>
#include <QLocale>
#include <QUrl>
#include <QtConcurrent/QtConcurrentRun>
#include <algorithm>

namespace ve {
namespace {
QString fileKey(const QString& path) {
    return QDir::cleanPath(QDir::fromNativeSeparators(path)).toCaseFolded();
}
QString gigabytes(qint64 bytes) {
    if (bytes > 0 && bytes < 10000000) return QString("<%1 GB").arg(QLocale().toString(.01, 'f', 2));
    return QString("%1 GB").arg(QLocale().toString(double(bytes) / 1e9, 'f', 2));
}
}
bool AppController::cacheMaintenanceSafe() const {
    return idle() && runtime_ != "Initializing" && !job_ && !metadataJob_ && !analysisJob_ && !runtimeInitializationStarted_ &&
        metadataScans_.empty() && !lutSaving_ && pool_.activeThreadCount() == 0 &&
        previewPool_.activeThreadCount() == 0 && !autoTimer_.isActive() && !scrubTimer_.isActive();
}
QSet<QString> AppController::protectedProcessingFiles() const {
    QSet<QString> result;
    const auto add = [&](const QString& path) { if (!path.isEmpty()) result.insert(fileKey(path)); };
    for (const auto& current : contexts_) {
        for (const auto& path : current.queue->paths()) add(path);
        for (const auto& url : {current.inputUrl, current.outputUrl, current.posterUrl})
            if (QUrl(url).isLocalFile()) add(QUrl(url).toLocalFile());
    }
    for (const auto& path : PreviewPlayer::processingFilesInUse()) add(path);
    add(settings_.nr_mask.value("path").toString());
    add(settings_.lut_path);
    add(settings_.lut_reference_image);
    return result;
}
void AppController::updateProcessingCache(bool clean, bool automatic) {
    if (shuttingDown_) return;
    if (cacheBusy_ && (!clean || automatic || cacheCleaning_ || cacheCleanPending_)) return;
    if (clean && !cacheMaintenanceSafe()) {
        if (!automatic) {
            cacheStatus_ = QCoreApplication::translate("App", "Processing is active. Try again when it finishes.");
            emit processingCacheChanged();
        }
        return;
    }
    if (cacheBusy_) {
        // A manual click during a read-only scan must not be lost. Reserve the
        // cleanup until that scan finishes; idle() blocks new processing while
        // the request is pending, just as it does during the cleanup itself.
        cacheCleanPending_ = true;
        cacheStatus_.clear();
        emit processingCacheChanged();
        emit operationStateChanged();
        return;
    }
    const auto protectedPaths = protectedProcessingFiles();
    previewClips_.cacheRoot()->protect(protectedPaths);
    if (clean && !automatic) {
        // Detach obsolete timeline ranges before background removal; currently
        // displayed, prewarmed and retiring decoder sources are protected above.
        for (auto& current : contexts_)
            current.ranges.removeIf([&](const QVariant& value) {
                const QUrl url(value.toMap().value("url").toString());
                return url.isLocalFile() && !protectedPaths.contains(fileKey(url.toLocalFile()));
            });
        emit previewRangesChanged();
    }
    cacheBusy_ = true;
    cacheCleaning_ = clean;
    if (clean && !automatic) cacheStatus_.clear();
    emit processingCacheChanged();
    emit operationStateChanged();
    const auto owner = previewClips_.cacheRoot();
    (void)QtConcurrent::run(&cachePool_, [this, clean, automatic, protectedPaths, owner] {
        ProcessingCacheCleanup removed;
        ProcessingCacheSnapshot snapshot;
        QString error;
        bool scanned = false;
        try {
            if (clean) {
                if (!automatic) {
                    // Retire processing-stage caches on their owning worker.
                    // Visible provider images and playback sources stay retained.
                    auto retired = QtConcurrent::run(&previewPool_, [this] {
                        previewStages_.clear(); videoFrames_.clear(); videoStill_.reset();
                    });
                    retired.waitForFinished();
                    QHash<QString, qint64> before;
                    for (const auto& path : previews_->cacheFiles())
                        if (QFileInfo::exists(path)) before.insert(path, QFileInfo(path).size());
                    previews_->clearUnused();
                    previews_->waitForCleanup();
                    for (auto it = before.begin(); it != before.end(); ++it)
                        if (!QFileInfo::exists(it.key())) {
                            removed.removedBytes += it.value(); ++removed.removedFiles;
                            removed.removedPaths.append(it.key());
                        }
                }
                QList<QFileInfo> clips;
                qint64 clipBytes = 0;
                for (const auto& path : owner->files()) {
                    const QFileInfo file(path);
                    if (!file.isFile()) continue;
                    clipBytes += file.size(); clips.append(file);
                }
                std::ranges::sort(clips, {}, [](const QFileInfo& file) { return file.lastModified(); });
                const auto budget = std::max<qint64>(0, PreviewProvider::diskBudget - previews_->diskUsage());
                const auto threshold = QDateTime::currentDateTimeUtc().addSecs(-24 * 3600);
                for (const auto& clip : clips) {
                    if (protectedPaths.contains(fileKey(clip.absoluteFilePath())) ||
                        (automatic && clipBytes <= budget && clip.lastModified().toUTC() >= threshold)) continue;
                    if (owner->removeFile(clip.absoluteFilePath())) {
                        clipBytes -= clip.size(); removed.removedBytes += clip.size(); ++removed.removedFiles;
                        removed.removedPaths.append(clip.absoluteFilePath());
                    } else ++removed.skipped;
                }
                if (!automatic) removed.append(ProcessingCache::instance().cleanAbandonedAndShaders(protectedPaths));
            }
            snapshot = ProcessingCache::instance().snapshot(protectedPaths);
            scanned = true;
        } catch (const std::exception& exception) { error = QString::fromUtf8(exception.what()); }
        QMetaObject::invokeMethod(this, [this, clean, automatic, removed, snapshot, scanned, error] {
            if (shuttingDown_) return;
            cacheBusy_ = cacheCleaning_ = false;
            if (scanned) { cacheSnapshot_ = snapshot; cacheReady_ = true; }
            if (!removed.removedPaths.isEmpty()) {
                previewVideos_.removePaths(removed.removedPaths);
                for (auto& current : contexts_)
                    current.ranges.removeIf([&](const QVariant& value) {
                        return removed.removedPaths.contains(QUrl(value.toMap().value("url").toString()).toLocalFile());
                    });
                emit previewRangesChanged();
            }
            if (!error.isEmpty()) {
                cacheStatus_ = QCoreApplication::translate("App", "Cache maintenance failed: %1").arg(error);
                log(cacheStatus_, "cache");
            } else if (clean && !automatic) {
                cacheStatus_ = QCoreApplication::translate("App", "Removed %1. Remaining: %2.")
                    .arg(gigabytes(removed.removedBytes), gigabytes(snapshot.bytes));
                if (removed.skipped || snapshot.skipped || snapshot.bytes > 0)
                    cacheStatus_ += ' ' + QCoreApplication::translate("App", "Some files are in use or could not be removed.");
                log(QString("Removed %1 processing cache files (%2 bytes); %3 bytes remain; %4 skipped.")
                    .arg(removed.removedFiles).arg(removed.removedBytes).arg(snapshot.bytes).arg(removed.skipped), "cache");
            }
            if (cacheCleanPending_) {
                cacheCleanPending_ = false;
                updateProcessingCache(true); // Recheck safety and fresh protected paths.
            }
            emit processingCacheChanged();
            emit operationStateChanged();
        }, Qt::QueuedConnection);
    });
}
} // namespace ve
