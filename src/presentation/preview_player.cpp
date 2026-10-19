#include "preview_player.hpp"
#include "media/timeline.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "media/media.hpp"
#include "media/packet_stream.hpp"
#include "processing/preview_cache.hpp"
#include <QVideoFrameFormat>
#include <QSet>
#include <QScopeGuard>
#include <algorithm>
#include <cmath>
#include <cstring>

namespace ve {
namespace { QSet<PreviewPlayer*> players; }
PreviewPlayer::PreviewPlayer(QObject* parent)
    : QObject(parent), thread_([this](std::stop_token stop) { decode(stop); }) {
    players.insert(this);
    timer_.setInterval(5);
    connect(&timer_, &QTimer::timeout, this, &PreviewPlayer::tick);
    timer_.start();
}
PreviewPlayer::~PreviewPlayer() {
    close();
    if (thread_.joinable()) thread_.join();
    players.remove(this);
}
QStringList PreviewPlayer::processingFilesInUse() {
    QStringList paths;
    for (auto* player : players) {
        if (!player->closed_ && player->source_.isLocalFile()) paths.append(player->source_.toLocalFile());
        std::lock_guard lock(player->mutex_);
        if (!player->decodingPath_.isEmpty()) paths.append(player->decodingPath_);
    }
    return paths;
}
void PreviewPlayer::setState(int state) {
    if (state_ != state) {
        state_ = state;
        emit playbackStateChanged(state_);
    }
}
void PreviewPlayer::setStatus(int status) {
    if (status_ != status) {
        status_ = status;
        emit mediaStatusChanged(status_);
    }
}
void PreviewPlayer::setSource(const QUrl& source) {
    if (source == source_)
        return;
    source_ = source;
    if (sink_)
        sink_->setVideoFrame({});
    position_ = duration_ = 0;
    fps_ = 0;
    completedLoops_ = 0;
    error_.clear();
    emit durationChanged(0);
    emit positionChanged(0);
    emit videoFpsChanged();
    emit errorStringChanged();
    setState(2);
    setStatus(source.isEmpty() ? 0 : 1);
    restart();
    emit sourceChanged();
}
void PreviewPlayer::setVideoOutput(QObject* output) {
    output_ = output;
    sink_ = output ? qvariant_cast<QVideoSink*>(output->property("videoSink")) : nullptr;
    if (!sink_)
        sink_ = qobject_cast<QVideoSink*>(output);
    emit videoOutputChanged();
}
void PreviewPlayer::restart() {
    std::lock_guard lock(mutex_);
    if (control_)
        control_->cancel();
    ++generation_;
    queue_.clear();
    primed_ = finished_ = false;
    request_ = Request{source_.toLocalFile(), position_, generation_};
    condition_.notify_all();
}
void PreviewPlayer::seek(int position) {
    position_ = std::clamp(position, 0, std::max(0, duration_ - 1));
    clockPosition_ = position_;
    clock_.restart();
    emit positionChanged(position_);
    restart();
}
void PreviewPlayer::play() {
    if (closed_ || source_.isEmpty())
        return;
    if (status_ == 6)
        seek(0);
    clockPosition_ = position_;
    clock_.restart();
    setState(1);
}
void PreviewPlayer::pause() {
    setState(2);
}
void PreviewPlayer::setPlaybackRate(double rate) {
    if (!std::isfinite(rate) || rate <= 0 || rate == rate_)
        return;
    rate_ = rate;
    clockPosition_ = position_;
    clock_.restart();
    emit playbackRateChanged();
}
void PreviewPlayer::stop() {
    pause();
    seek(0);
    setState(0);
}
void PreviewPlayer::close() {
    if (closed_)
        return;
    closed_ = true;
    timer_.stop();
    thread_.request_stop();
    {
        std::lock_guard lock(mutex_);
        if (control_)
            control_->cancel();
        queue_.clear();
    }
    condition_.notify_all();
}
void PreviewPlayer::tick() {
    if (closed_)
        return;
    std::optional<Frame> latest;
    bool finished = false;
    const int desired = state_ == 1 ? clockPosition_ + int(clock_.elapsed() * rate_) : position_;
    {
        std::lock_guard lock(mutex_);
        while (!queue_.empty() && (!primed_ || (state_ == 1 && queue_.front().position <= desired))) {
            latest = std::move(queue_.front());
            queue_.pop_front();
            primed_ = true;
        }
        finished = finished_ && queue_.empty();
        condition_.notify_all();
    }
    if (latest && latest->generation == generation_) {
        if (sink_)
            sink_->setVideoFrame(latest->video);
        if (position_ != latest->position) {
            position_ = latest->position;
            emit positionChanged(position_);
        }
        if (status_ == 1)
            setStatus(2);
    }
    if (finished && state_ == 1) {
        if (loops_ == -1 || ++completedLoops_ < loops_) {
            seek(0);
            play();
        } else {
            setState(0);
            setStatus(6);
        }
    }
}
void PreviewPlayer::decode(std::stop_token stop) {
    QByteArray sourceIdentity;
    MediaInfo sourceInfo;
    while (!stop.stop_requested()) {
        Request request;
        std::shared_ptr<JobControl> control;
        {
            std::unique_lock lock(mutex_);
            condition_.wait(lock, stop, [this] { return request_.has_value(); });
            if (stop.stop_requested())
                return;
            request = *request_;
            request_.reset();
            control = std::make_shared<JobControl>();
            control_ = control;
            decodingPath_ = request.path;
        }
        const auto releasePath = qScopeGuard([this] { std::lock_guard lock(mutex_); decodingPath_.clear(); });
        if (request.path.isEmpty())
            continue;
        const auto generation = request.generation;
        try {
            const auto identity = previewFileIdentity(request.path);
            if (identity != sourceIdentity) {
                auto info = probeMedia(request.path, control.get());
                control->checkpoint();
                sourceInfo = std::move(info);
                sourceIdentity = identity;
            }
            const auto info = sourceInfo;
            const int width = (info.width + 1) / 2 * 2, height = (info.height + 1) / 2 * 2;
            const double fps = info.fps > 0 ? info.fps : 30;
            QMetaObject::invokeMethod(
                this,
                [this, generation, info, fps] {
                    if (generation != generation_)
                        return;
                    duration_ = int(info.duration * 1000);
                    fps_ = fps;
                    emit durationChanged(duration_);
                    emit videoFpsChanged();
                    setStatus(2);
                },
                Qt::QueuedConnection);
            QVideoFrameFormat format(QSize(width, height), QVideoFrameFormat::Format_P010);
            format.setViewport(QRect(0, 0, info.width, info.height));
            format.setColorRange(QVideoFrameFormat::ColorRange_Video);
            format.setColorSpace(info.primaries == "bt2020" ? QVideoFrameFormat::ColorSpace_BT2020
                                                            : QVideoFrameFormat::ColorSpace_BT709);
            format.setColorTransfer(info.transfer == "smpte2084" ? QVideoFrameFormat::ColorTransfer_ST2084
                                    : info.transfer == "arib-std-b67"
                                        ? QVideoFrameFormat::ColorTransfer_STD_B67
                                        : QVideoFrameFormat::ColorTransfer_BT709);
            const auto matrix = info.primaries == "bt2020" ? QString("bt2020nc") : QString("bt709");
            const auto parked = quantizePlayhead(request.position, fps, info.duration);
            const double seekSeconds = std::max(0., parked.seconds - .002);
            PipeProcess decoder(Paths::instance().ffmpeg(),
                                {"-v",
                                 "error",
                                 "-ss",
                                 QString::number(seekSeconds, 'f', 9),
                                 "-copyts", "-start_at_zero",
                                 "-i",
                                 request.path,
                                 "-an",
                                 "-vf",
                                 "scale=in_range=auto:out_range=tv:out_color_matrix=" + matrix +
                                     ",pad=" + QString::number(width) + ':' + QString::number(height),
                                 "-fps_mode",
                                 "passthrough",
                                 "-enc_time_base:v",
                                 "demux",
                                 "-c:v",
                                 "rawvideo",
                                 "-f",
                                 "nut",
                                 "-write_index",
                                 "0",
                                 "-flush_packets",
                                 "1",
                                 "-pix_fmt",
                                 "p010le",
                                 "pipe:1"},PipeProcess::OutputMode::Backpressured);
            FramePacketReader packets(decoder, *control, RawPixelFormat::P010);
            while (!stop.stop_requested()) {
                control->checkpoint();
                const auto packet = packets.readPixels();
                if (!packet)
                    break;
                const auto& bytes = packet->pixels;
                QVideoFrame video(format);
                if (!video.map(QVideoFrame::WriteOnly))
                    throw std::runtime_error("Cannot allocate the preview display frame.");
                qsizetype offset = 0;
                for (int plane = 0; plane < 2; ++plane) {
                    const int rows = plane == 0 ? height : height / 2;
                    for (int y = 0; y < rows; ++y)
                        std::memcpy(video.bits(plane) + qsizetype(y) * video.bytesPerLine(plane),
                                    bytes.constData() + offset + qsizetype(y) * width * 2, size_t(width) * 2);
                    offset += qsizetype(rows) * width * 2;
                }
                video.unmap();
                const int position = int(std::lround(packet->seconds() * 1000));
                video.setStartTime(qint64(position) * 1000);
                video.setEndTime(qint64(position) * 1000 +
                                 (packet->duration > 0
                                      ? packet->duration * 1000000ll * packet->timeBase.numerator /
                                            packet->timeBase.denominator
                                      : qint64(1000000. / fps)));
                {
                    std::unique_lock lock(mutex_);
                    condition_.wait(lock, stop, [&] { return queue_.size() < 3 || control->cancelled(); });
                    control->checkpoint();
                    if (stop.stop_requested())
                        return;
                    if (generation != generation_)
                        break;
                    queue_.push_back({std::move(video), position, generation});
                }
            }
            {
                std::lock_guard lock(mutex_);
                if (generation == generation_)
                    finished_ = true;
            }
        } catch (const Cancelled&) {
        } catch (const std::exception& exception) {
            const auto message = QString::fromUtf8(exception.what());
            QMetaObject::invokeMethod(
                this,
                [this, generation, message] {
                    if (generation != generation_)
                        return;
                    error_ = message;
                    emit errorStringChanged();
                    emit errorOccurred(1, message);
                    setStatus(7);
                },
                Qt::QueuedConnection);
        }
    }
}
} // namespace ve
