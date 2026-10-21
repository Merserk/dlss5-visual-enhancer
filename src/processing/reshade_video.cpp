#include "reshade_video.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/scratch.hpp"
#include "media/cuda_video.hpp"
#include "media/native_rgb.hpp"
#include "media/video_delivery.hpp"
#include "native_engines.hpp"
#include "reshade_native.hpp"
#include <QElapsedTimer>
#include <algorithm>
#include <cmath>
#include <memory>
#include <optional>

namespace ve {
bool renderReShadeVideo(const RenderRequest& request, const MediaInfo& source,
                        const Settings& settings, const PipelineEstimate& estimate,
                        const QString& destination, JobControl& control,
                        const ProgressCallback& progress) {
    const bool sole = estimate.stages == QStringList{"reshade"};
    const bool rrBefore = estimate.stages == QStringList{"dlss_ray_reconstruction", "reshade"};
    const bool rrAfter = estimate.stages == QStringList{"reshade", "dlss_ray_reconstruction"};
    if (request.preview || (!sole && !rrBefore && !rrAfter) ||
        ((source.width | source.height | estimate.width | estimate.height) & 1)) {
        return false;
    }

    const bool nvenc = settings.codec.contains("NVIDIA NVENC") ||
        (settings.ffmpeg_device != "cpu" && QStringList{"H.264", "H.265", "AV1"}.contains(settings.codec));
    if (!nvenc || source.range == "pc" ||
        (source.hdr ? source.primaries != "bt2020" || source.colorSpace != "bt2020nc"
                    : !QStringList{"bt709", "unknown", "unspecified", ""}.contains(source.primaries) ||
                      !QStringList{"bt709", "unknown", "unspecified", ""}.contains(source.colorSpace))) {
        return false;
    }
    if (!source.hdr && !QStringList{"bt709", "gamma22", "bt470m", "iec61966-2-1", "srgb", "unknown", "unspecified", ""}.contains(source.transfer)) {
        return false;
    }

    const int ordinal = gpuOrdinal(settings.ai_gpu_uuid);
    const auto devices = detectGpus();
    const auto selected = std::ranges::find_if(devices, [ordinal](const Gpu& gpu) { return gpu.index == ordinal; });
    if (selected == devices.end() ||
        (settings.video_gpu_uuid != "auto" && settings.video_gpu_uuid != selected->uuid) ||
        (settings.ffmpeg_device != "auto" && settings.ffmpeg_device != "cpu" &&
         settings.ffmpeg_device.mid(7).compare(selected->uuid.mid(4), Qt::CaseInsensitive) != 0)) {
        return false;
    }

    ScratchDirectory temporary(QStringLiteral("reshade-cuda-render-XXXXXX"));
    if (!temporary.isValid()) {
        throw std::runtime_error("Cannot create the ReShade GPU render directory.");
    }
    const auto video = temporary.filePath("video.mp4");
    const auto codec = settings.codec.startsWith("H.264") ? QString("h264_nvenc")
        : settings.codec.startsWith("H.265") ? QString("hevc_nvenc") : QString("av1_nvenc");
    const double duration = request.seconds > 0
        ? std::min(request.seconds, source.duration - request.startSeconds)
        : source.duration - request.startSeconds;

    qint64 count = 0;
    double actualDuration = 0.0;
    double origin = 0.0;
    QElapsedTimer timer;
    timer.start();

    try {
        CudaVideoDecoder decoder(request.input, ordinal, request.startSeconds, control);
        auto frame = decoder.read();
        if (!frame) {
            throw std::runtime_error("Source contains no decodable video frames.");
        }
        origin = decoder.timeOrigin();
        auto colors = source;
        colors.width = estimate.width;
        colors.height = estimate.height;
        colors.hdr = estimate.hdr;
        if (rrBefore || rrAfter || source.precisionBits() > 8 || estimate.hdr) {
            colors.pixelFormat = "p010le";
        }

        ReShadeEngine::initialize(ordinal);
        const auto caps = ReShadeEngine::capabilities(ordinal);
        if (!caps.cuda_interop_available) {
            throw CudaUnavailable("ReShade D3D12-CUDA interop is unavailable on the selected adapter.");
        }
        if (rrBefore || rrAfter) {
            NativeEngines::instance().prepare(ordinal, {"dlss_ray_reconstruction"});
        }

        const uint32_t transfer = reshadeColorTransfer(source);
        std::unique_ptr<NativeEngines::RayReconstruction> rrSession;
        if (rrBefore || rrAfter) {
            rrSession = std::make_unique<NativeEngines::RayReconstruction>(
                QSize(source.width, source.height), settings.rr_optical_flow_quality, false, true, transfer);
        }
        ReShadeSession rsSession(QSize(source.width, source.height), settings, source, false, true, true);

        CudaVideoEncoder encoder(video, codec, settings, colors, decoder.timeBase(), decoder.device(), ordinal, control);
        const bool p010 = encoder.pixelFormat() == VideoPixelFormat::P010;
        CudaFramePool pool(decoder.device(), QSize(estimate.width, estimate.height), p010, source.hdr);

        if (sole) {
            info("reshade", "NVDEC -> ReShade (D3D12 + AMD FidelityFX Optical Flow 1.1.2) -> " + codec + " (owned device surfaces)");
        } else if (rrBefore) {
            info("reshade", "NVDEC -> DLSS Ray Reconstruction -> ReShade -> " + codec + " (owned device surfaces)");
        } else {
            info("reshade", "NVDEC -> ReShade -> DLSS Ray Reconstruction -> " + codec + " (owned device surfaces)");
        }

        qint64 previous = -1;
        while (frame) {
            control.checkpoint();
            const double seconds = frame->pts * double(frame->timeBase.numerator) / frame->timeBase.denominator;
            if (request.seconds > 0 && seconds >= duration - 1e-9) {
                break;
            }
            if (frame->pts <= previous) {
                throw std::runtime_error("Video has non-increasing presentation timestamps.");
            }
            const float delta = previous >= 0
                ? float((frame->pts - previous) * 1000.0 * frame->timeBase.numerator / frame->timeBase.denominator)
                : (source.fps > 0 ? float(1000.0 / source.fps) : 33.333f);
            const bool gap = count > 0 && delta > std::max(250.0f, source.fps > 0 ? float(4000.0 / source.fps) : 250.0f);
            previous = frame->pts;
            frame->rotation = uint32_t(source.clockwiseRotation());

            if (frame->colorMatrix != (source.hdr ? 2u : 1u) || frame->colorRange != 0) {
                throw CudaUnavailable("ReShade device delivery requires matching 709/2020 limited-range planes.");
            }

            const qint64 ptsNs = qint64(std::llround(seconds * 1000000000.0));
            const qint64 durNs = std::max<qint64>(1, qint64(std::llround(double(delta) * 1000000.0)));
            const bool reset = (count == 0 || gap);

            CudaVideoFrame output;
            if (sole) {
                output = rsSession.process(*frame, pool, p010, reset, count);
            } else if (rrBefore) {
                auto rgb = rrSession->processRgb(*frame, reset, quint32(count), std::clamp(delta, 0.01f, 1000.0f));
                output = rsSession.process(rgb, pool, p010, reset, count, ptsNs, durNs);
                output.pts = frame->pts;
                output.duration = frame->duration;
                output.timeBase = frame->timeBase;
            } else {
                auto rgb = rsSession.processRgb(*frame, reset, count);
                output = rrSession->process(rgb, pool, p010, reset, quint32(count), std::clamp(delta, 0.01f, 1000.0f));
                output.pts = frame->pts;
                output.duration = frame->duration;
                output.timeBase = frame->timeBase;
            }

            control.checkpoint();
            encoder.write(output);
            ++count;
            actualDuration = std::max(actualDuration, seconds + (frame->duration > 0
                ? frame->duration * double(frame->timeBase.numerator) / frame->timeBase.denominator
                : (source.fps > 0 ? 1.0 / source.fps : 1.0 / 30.0)));
            if (progress) {
                progress({std::min(0.98, duration > 0 ? seconds / duration : 0.1),
                          QT_TRANSLATE_NOOP("App", "Processing ReShade video"),
                          count, source.frames, timer.elapsed() > 0 ? count * 1000.0 / timer.elapsed() : 0.0});
            }
            frame = decoder.read();
        }
        encoder.finish();
        info("reshade", "Device delivery counters: " + rsSession.diagnostics());
        if (rrSession) {
            info("dlssrr", "Combined RR+ReShade device counters: " + rrSession->diagnostics());
        }
    } catch (const CudaUnavailable& error) {
        if (count) {
            throw;
        }
        control.checkpoint();
        info("reshade", "CUDA delivery unavailable; using host composition: " + QString::fromUtf8(error.what()));
        return false;
    }

    if (count <= 0) {
        throw std::runtime_error("ReShade produced no video frames.");
    }
    control.checkpoint();
    if (request.seconds > 0) {
        actualDuration = std::min(actualDuration, request.seconds);
    }
    info("reshade", reshadeReport(settings, true));
    muxDelivery(video, destination, request, source, settings, actualDuration, control,
                request.preview ? std::nullopt : std::optional(origin));
    return true;
}
} // namespace ve
