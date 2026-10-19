#include "pipeline.hpp"
#include "effects.hpp"
#include "frame_stream.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/heap_resources.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "media/frame_batch.hpp"
#include "media/resolution.hpp"
#include "media/frame_time.hpp"
#include "media/av_api.hpp"
#include "media/native_frame.hpp"
#include "media/cuda_video.hpp"
#include "media/native_rgb.hpp"
#include "media/video_preview.hpp"
#include "media/host_video.hpp"
#include "media/video_delivery.hpp"
#include "interpolation_video.hpp"
#include "media/image_metadata.hpp"
#include "media/image_alpha.hpp"
#include "media/packet_stream.hpp"
#include "media/video_backend.hpp"
#include "native_engines.hpp"
#include "neural_video_delivery.hpp"
#include "temporal_guide.hpp"
#include "preview_cache.hpp"
#include "vfx_native.hpp"
#include "vfx_worker.hpp"
#include <QCoreApplication>
#include "vfx_video.hpp"
#include <QColorSpace>
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QSaveFile>
#include <QScopeGuard>
#include "infrastructure/scratch.hpp"
#include <QUuid>
#include <QtGui/qrgbafloat.h>
#include <algorithm>
#include <cmath>
#include <deque>

namespace ve {
namespace {
int even(double value) {
    return checkedDimension(value, DimensionRounding::EvenNearest);
}
QString imageExtension(const QString& format) {
    return QMap<QString, QString>{
        {"PNG", "png"}, {"JPEG", "jpg"}, {"WebP", "webp"}, {"AVIF", "avif"}, {"TIFF", "tiff"}}
        .value(format, "png");
}
bool renderCudaVideo(const RenderRequest& request, const MediaInfo& source, const Settings& settings,
                     const PipelineEstimate& estimate, const QString& destination, JobControl& control,
                     const ProgressCallback& progress) {
    if (request.preview || estimate.stages != QStringList{"neural_model"} || source.width % 2 || source.height % 2)
        return false;
    const int ordinal = gpuOrdinal(settings.ai_gpu_uuid);
    const auto devices = detectGpus();
    const auto selected = std::ranges::find_if(devices, [ordinal](const Gpu& gpu) { return gpu.index == ordinal; });
    if (selected == devices.end() ||
        (settings.video_gpu_uuid != "auto" && settings.video_gpu_uuid != selected->uuid) ||
        (settings.ffmpeg_device != "auto" && settings.ffmpeg_device != "cpu" &&
         settings.ffmpeg_device.mid(7).compare(selected->uuid.mid(4), Qt::CaseInsensitive) != 0))
        return false;
    ScratchDirectory temporary(QStringLiteral("cuda-render-XXXXXX"));
    if (!temporary.isValid()) throw std::runtime_error("Cannot create the GPU render directory.");
    const auto video = temporary.filePath(settings.codec.startsWith("ProRes") ? "video.mov" : "video.nut");
    const auto codec = settings.codec.startsWith("H.264") ? QString("h264_nvenc")
        : settings.codec.startsWith("H.265") ? QString("hevc_nvenc") : QString("av1_nvenc");
    const bool cudaEncoding = settings.codec.contains("NVIDIA NVENC") ||
        (settings.ffmpeg_device != "cpu" && QStringList{"H.264", "H.265", "AV1"}.contains(settings.codec));
    qint64 count = 0;
    double actualDuration = 0;
    double sourceTimeOrigin = 0;
    const double duration = request.seconds > 0 ? std::min(request.seconds, source.duration - request.startSeconds)
                                                : source.duration - request.startSeconds;
    QElapsedTimer timer;
    timer.start();
    try {
        // Open the primary-context decoder before NGX, as the reference does.
        CudaVideoDecoder decoder(request.input, ordinal, request.startSeconds, control);
        auto first = decoder.read();
        if (!first) throw std::runtime_error("Source contains no decodable video frames.");
        sourceTimeOrigin=decoder.timeOrigin();
        std::unique_ptr<CudaVideoEncoder> encoder;
        std::unique_ptr<HostVideoEncoder> hostEncoder;
        if (cudaEncoding) encoder = std::make_unique<CudaVideoEncoder>(video, codec, settings, source, decoder.timeBase(), decoder.device(), ordinal, control);
        else {
            hostEncoder = std::make_unique<HostVideoEncoder>(video, settings.codec, settings, source, ordinal, control);
        }
        NativeEngines::instance().prepare(ordinal, estimate.stages);
        NativeEngines::NeuralRendering session;
        const auto rotation = source.clockwiseRotation();
        info("video", "CUDA/NVDEC -> Neural Rendering ABI 11 -> " + (cudaEncoding ? codec + " (device frames)" : settings.codec + " (host delivery)"));
        auto frame = std::move(first);
        qint64 previous = -1;
        while (frame) {
            control.checkpoint();
            const double seconds = frame->pts * double(frame->timeBase.numerator) / frame->timeBase.denominator;
            if (request.seconds > 0 && seconds >= duration - 1e-9) break;
            if (frame->pts <= previous) throw std::runtime_error("Video has non-increasing presentation timestamps.");
            previous = frame->pts;
            frame->rotation = uint32_t(rotation);
            const bool reset = session.sceneReset(*frame);
            if (encoder) {
                auto output = encoder->allocate();
                session.process(*frame, output, settings, reset);
                encoder->write(output);
            } else {
                const bool wide = source.pixelFormat.contains("10") || source.pixelFormat.contains("12") || source.pixelFormat.contains("16");
                const auto pixels = session.processHost(*frame, settings, reset, wide);
                hostEncoder->write(pixels, frame->pts, frame->duration, frame->timeBase);
            }
            ++count;
            actualDuration = std::max(actualDuration, seconds + (frame->duration > 0
                ? frame->duration*double(frame->timeBase.numerator)/frame->timeBase.denominator : 1/source.fps));
            if (progress) progress({std::min(.98, duration > 0 ? seconds / duration : .1), QT_TRANSLATE_NOOP("App", "Processing video"),
                count, source.frames, timer.elapsed() > 0 ? count * 1000. / timer.elapsed() : 0});
            frame = decoder.read();
        }
        if (encoder) encoder->finish(); else hostEncoder->finish();
        // All encoder references to output surfaces are flushed before the NR lease closes.
    } catch (const CudaUnavailable& error) {
        if (count) throw;
        control.checkpoint();
        info("video", "GPU path unavailable; using the bounded host path: " + QString::fromUtf8(error.what()));
        return false;
    }
    if (count <= 0) throw std::runtime_error("The GPU render produced no frames.");
    control.checkpoint();
    if (request.seconds > 0) actualDuration = std::min(actualDuration, request.seconds);
    muxDelivery(video, destination, request, source, settings, actualDuration, control,
        request.preview ? std::nullopt : std::optional(sourceTimeOrigin));
    return true;
}
bool renderRayReconstructionVideo(const RenderRequest& request,const MediaInfo& source,const Settings& settings,
                                  const PipelineEstimate& estimate,const QString& destination,JobControl& control,
                                  const ProgressCallback& progress) {
    if(request.preview || estimate.stages!=QStringList{"dlss_ray_reconstruction"} ||
       (source.width|source.height|estimate.width|estimate.height)&1) return false;
    const bool nvenc=settings.codec.contains("NVIDIA NVENC") || (settings.ffmpeg_device!="cpu" &&
        QStringList{"H.264","H.265","AV1"}.contains(settings.codec));
    // The device encoder signals 709 limited SDR or 2020 limited HDR. Other
    // source color spaces retain their established host conversion boundary.
    if(!nvenc || source.range=="pc" ||
       (source.hdr ? source.primaries!="bt2020" || source.colorSpace!="bt2020nc"
                   : !QStringList{"bt709","unknown","unspecified",""}.contains(source.primaries) ||
                     !QStringList{"bt709","unknown","unspecified",""}.contains(source.colorSpace)))return false;
    if(!source.hdr&&!QStringList{"bt709","gamma22","bt470m","unknown","unspecified",""}.contains(source.transfer))return false;
    const int ordinal=gpuOrdinal(settings.ai_gpu_uuid);
    const auto devices=detectGpus();
    const auto selected=std::ranges::find_if(devices,[ordinal](const Gpu& gpu){return gpu.index==ordinal;});
    if(selected==devices.end() || (settings.video_gpu_uuid!="auto"&&settings.video_gpu_uuid!=selected->uuid) ||
       (settings.ffmpeg_device!="auto"&&settings.ffmpeg_device!="cpu"&&
        settings.ffmpeg_device.mid(7).compare(selected->uuid.mid(4),Qt::CaseInsensitive)!=0))return false;
    ScratchDirectory temporary(QStringLiteral("rr-cuda-render-XXXXXX"));
    if(!temporary.isValid())throw std::runtime_error("Cannot create the Ray Reconstruction render directory.");
    const auto video=temporary.filePath("video.mp4");
    const auto codec=settings.codec.startsWith("H.264")?QString("h264_nvenc"):
        settings.codec.startsWith("H.265")?QString("hevc_nvenc"):QString("av1_nvenc");
    const double duration=request.seconds>0?std::min(request.seconds,source.duration-request.startSeconds):source.duration-request.startSeconds;
    qint64 count=0;double actualDuration=0,origin=0;
    QElapsedTimer timer;timer.start();
    try {
        CudaVideoDecoder decoder(request.input,ordinal,request.startSeconds,control);
        auto frame=decoder.read();
        if(!frame)throw std::runtime_error("Source contains no decodable video frames.");
        origin=decoder.timeOrigin();
        auto colors=source;colors.width=estimate.width;colors.height=estimate.height;colors.hdr=estimate.hdr;
        // RR reconstruction retains FP16 internally and requests a 10-bit
        // encoder surface for HEVC/AV1, including 8-bit source material.
        colors.pixelFormat="p010le";
        NativeEngines::instance().prepare(ordinal,estimate.stages);
        const quint32 transfer=source.transfer=="smpte2084"?5u:source.transfer=="arib-std-b67"?6u:
            source.transfer=="linear"?2u:QStringList{"iec61966-2-1","srgb"}.contains(source.transfer)?0u:
            QStringList{"gamma22","bt470m"}.contains(source.transfer)?3u:
            QStringList{"gamma28","bt470bg"}.contains(source.transfer)?4u:1u;
        NativeEngines::RayReconstruction session({source.width,source.height},
            settings.rr_optical_flow_quality,false,true,transfer);
        // Construct encoder after the session; its delayed references flush
        // and release before the session during normal and exceptional exits.
        CudaVideoEncoder encoder(video,codec,settings,colors,decoder.timeBase(),decoder.device(),ordinal,control);
        const bool p010=encoder.pixelFormat()==VideoPixelFormat::P010;
        CudaFramePool pool(decoder.device(),{estimate.width,estimate.height},p010,source.hdr);
        info("video","NVDEC -> DLSS Ray Reconstruction/NVOFA -> "+codec+" (owned device surfaces)");
        qint64 previous=-1;
        while(frame){
            control.checkpoint();
            const double seconds=frame->pts*double(frame->timeBase.numerator)/frame->timeBase.denominator;
            if(request.seconds>0&&seconds>=duration-1e-9)break;
            if(frame->pts<=previous)throw std::runtime_error("Video has non-increasing presentation timestamps.");
            const float delta=previous>=0?float((frame->pts-previous)*1000.*frame->timeBase.numerator/frame->timeBase.denominator):
                source.fps>0?float(1000./source.fps):0.f;
            const bool gap=count>0&&delta>std::max(250.f,source.fps>0?float(4000./source.fps):250.f);
            previous=frame->pts;frame->rotation=uint32_t(source.clockwiseRotation());
            // Decoder metadata is authoritative when present. The transport
            // uses the encoder's matching, explicitly selected output matrix.
            if(frame->colorMatrix!=(source.hdr?2u:1u)||frame->colorRange!=0)
                throw CudaUnavailable("RR device delivery requires matching 709/2020 limited-range planes.");
            auto output=session.process(*frame,pool,p010,count==0||gap,quint32(count),std::clamp(delta,.01f,1000.f));
            control.checkpoint();encoder.write(output);++count;
            actualDuration=std::max(actualDuration,seconds+(frame->duration>0?
                frame->duration*double(frame->timeBase.numerator)/frame->timeBase.denominator:1/source.fps));
            if(progress)progress({std::min(.98,duration>0?seconds/duration:.1),QT_TRANSLATE_NOOP("App","Processing video"),
                count,source.frames,timer.elapsed()>0?count*1000./timer.elapsed():0});
            frame=decoder.read();
        }
        encoder.finish();
        info("dlssrr","Device delivery counters: "+session.diagnostics());
    }catch(const CudaUnavailable& error){
        if(count)throw;
        control.checkpoint();info("dlssrr","CUDA delivery unavailable; using host composition: "+QString::fromUtf8(error.what()));
        return false;
    }
    if(count<=0)throw std::runtime_error("Ray Reconstruction produced no video frames.");
    control.checkpoint();if(request.seconds>0)actualDuration=std::min(actualDuration,request.seconds);
    muxDelivery(video,destination,request,source,settings,actualDuration,control,
        request.preview?std::nullopt:std::optional(origin));
    return true;
}
class OutputTransaction {
  public:
    explicit OutputTransaction(const QString& destination) : target_(destination) {
        const QFileInfo file(target_);
        QDir().mkpath(file.absolutePath());
        temporary_ =
            file.dir().filePath('.' + file.completeBaseName() + ".render-" +
                                QUuid::createUuid().toString(QUuid::WithoutBraces) + '.' + file.suffix());
    }
    ~OutputTransaction() {
        if (!committed_)
            QFile::remove(temporary_);
    }
    [[nodiscard]] QString path() const { return temporary_; }
    void commit() {
        if (QFileInfo::exists(target_))
            throw std::runtime_error("Output already exists. Choose a different filename.");
        if (!QFile::rename(temporary_, target_))
            throw std::runtime_error("Cannot finalize the output file.");
        committed_ = true;
    }

  private:
    QString target_, temporary_;
    bool committed_ = false;
};
QImage pqFromScRgb(const QImage& input) {
    auto frame = input.convertToFormat(QImage::Format_RGBA32FPx4);
    auto pq = [](float n) {
        const float p = std::pow(std::max(0.f, n / 10000.f), 2610.f / 16384.f);
        return std::pow((3424.f / 4096.f + 2413.f / 128.f * p) / (1 + 2392.f / 128.f * p), 2523.f / 32.f);
    };
    for (int y = 0; y < frame.height(); ++y) {
        auto* row = reinterpret_cast<QRgbaFloat32*>(frame.scanLine(y));
        for (int x = 0; x < frame.width(); ++x) {
            const float r = row[x].red() * 80, g = row[x].green() * 80, b = row[x].blue() * 80;
            row[x] = QRgbaFloat32{pq(.627404f * r + .329283f * g + .043313f * b),
                                  pq(.069097f * r + .919540f * g + .011362f * b),
                                  pq(.016391f * r + .088013f * g + .895595f * b), row[x].alpha()};
        }
    }
    return frame.convertToFormat(QImage::Format_RGBA64);
}
struct InterpolationGrid {
    using Surface=NativeEngines::Interpolation::Surface;
    struct Frame { Surface pixels; FrameTime time; int segment=0; };
    struct Output { QImage pixels; Surface surface; FrameTime time; };
    struct Stage {
        std::unique_ptr<NativeEngines::Interpolation> session;
        std::optional<Frame> previous;
        int generated=1;
        std::vector<Frame> push(NativeEngines::Interpolation::Result result, FrameTime time, int segment) {
            if (previous && result.sceneCut) segment=previous->segment+1;
            std::vector<Frame> frames;
            if (previous && !result.reset) {
                const auto interval=time-previous->time;
                int index=0;
                for (auto& surface : result.generated)
                    frames.push_back({std::move(surface),previous->time+interval.fraction(++index,generated+1),segment});
            }
            previous=Frame{std::move(result.source),time,segment};
            frames.push_back(*previous);
            return frames;
        }
    };
    std::vector<Stage> stages;
    FrameTime nominal;
    bool hdr=false;
    bool rgb10=false;
    std::unique_ptr<HostRgb10Boundary> rgbBoundary;
    std::unique_ptr<VideoFilterStage> hdrInput;
    MediaInfo hdrColors;
    std::deque<CudaVideoFrame> preparedHdr;
    bool highPrecision=false;
    quint32 mediaTransfer=1;
    InterpolationGrid(const MediaInfo& source, const Settings& settings, double target, QSize size, bool inputRgb10,bool wideRgb=false) {
        hdr=source.hdr;
        rgb10=inputRgb10;
        highPrecision=wideRgb;mediaTransfer=nativeMediaTransfer(source.transfer);
        if (rgb10 && !hdr) rgbBoundary=std::make_unique<HostRgb10Boundary>();
        if (hdr&&!highPrecision) {
            hdrColors=source;
            hdrColors.colorSpace="bt2020nc"; hdrColors.range="tv";
            hdrColors.primaries="bt2020"; hdrColors.transfer="smpte2084";
            hdrInput=std::make_unique<VideoFilterStage>(QStringList{"fi_hdr_input"},size,hdrColors,settings);
        }
        const auto rate=av::Api::get().av_d2q(source.fps>0 ? source.fps : 30.,1000000);
        nominal=FrameTime(rate.den,rate.num);
        if (target <= source.fps)
            return;
        const double ratio = target / source.fps;
        const int nativeMax = NativeEngines::Interpolation::nativeMaximum();
        const int integer = int(std::lround(ratio));
        const bool exact = !source.variableFrameRate && std::abs(ratio - integer) < 1e-7 && integer >= 2 &&
                           integer <= nativeMax;
        if (settings.frame_interpolation_engine == "Native DLSSG" && !exact)
            throw std::runtime_error("Target FPS is outside the native DLSSG grid. Choose Auto or Cascade.");
        if (exact && settings.frame_interpolation_engine != "Cascade") {
            stages.push_back({std::make_unique<NativeEngines::Interpolation>(size, integer-1, source.hdr,
                settings.frame_interpolation_optical_flow_quality,highPrecision,64),{},integer-1});
        } else {
            const int levels = std::abs(ratio - 2) < 1e-7                                  ? 1
                               : std::abs(ratio - 4) < 1e-7                                ? 2
                               : !source.variableFrameRate && std::abs(ratio - 2.5) < 1e-7 ? 4
                                                                                           : 3;
            for (int level = 0; level < levels; ++level)
                stages.push_back({std::make_unique<NativeEngines::Interpolation>(
                    size,1,source.hdr,settings.frame_interpolation_optical_flow_quality,highPrecision,level==levels-1 ? 64 : 0),{},1});
        }
    }
    void prepare(std::span<const QImage> pixels, JobControl& control) {
        if (!hdrInput || stages.empty()) return;
        if (!preparedHdr.empty()) throw std::logic_error("Interpolation inputs were not consumed.");
        auto converted=hdrInput->processP010(pixels,control,rgb10);
        for (auto& frame : converted) preparedHdr.push_back(std::move(frame));
    }
    std::vector<Output> push(const QImage& pixels, FrameTime time, JobControl& control) {
        const auto input=rgbBoundary ? highPrecision?decodedRgb10Rgba(pixels):rgbBoundary->decodeRgba8(pixels,control) : pixels;
        if (stages.empty()) return {{input.convertToFormat(hdr||highPrecision ? QImage::Format_RGBA64 : QImage::Format_RGBA8888),{},time}};
        auto& first=stages.front();
        int segment=first.previous ? first.previous->segment : 0;
        if (first.previous && (time<=first.previous->time || time-first.previous->time>nominal.fraction(2,1))) ++segment;
        const bool reset=first.previous && segment!=first.previous->segment;
        if (hdrInput && preparedHdr.empty()) throw std::logic_error("Missing HDR interpolation input.");
        auto result=hdrInput ? first.session->evaluate(preparedHdr.front(),hdrColors,reset,true)
            : first.session->evaluate(input,reset,true,mediaTransfer);
        if (hdrInput) preparedHdr.pop_front();
        auto frames=first.push(std::move(result),time,segment);
        for (size_t level=1; level<stages.size(); ++level) {
            auto& stage=stages[level];
            std::vector<Frame> expanded;
            for (auto& frame : frames) {
                control.checkpoint();
                auto produced=stage.push(stage.session->evaluate(frame.pixels,
                    stage.previous && frame.segment!=stage.previous->segment,false),frame.time,frame.segment);
                expanded.insert(expanded.end(),std::make_move_iterator(produced.begin()),std::make_move_iterator(produced.end()));
            }
            frames=std::move(expanded);
        }
        std::vector<Output> output;
        // Retain surfaces until cadence selection. Only the selected frames
        // cross a host boundary when a downstream stage needs CPU pixels.
        for (auto& frame : frames) output.push_back({{},std::move(frame.pixels),frame.time});
        return output;
    }
};
} // namespace
QSize dlssOutputSize(QSize input, const QString& mode, bool video) {
    requireResolution(input, "DLSS input texture", maximumDlssTextureDimension);
    const double factor =
        QMap<QString, double>{
            {"DLAA", 1}, {"Quality", 1.5}, {"Balanced", 1.724}, {"Performance", 2}, {"Ultra Performance", 3}}
            .value(mode, 0);
    if (!factor || input.width() < 32 || input.height() < 32)
        throw std::runtime_error("DLSS input must be at least 32 by 32 pixels and use a supported mode.");
    if (input.width()*factor > maximumDlssTextureDimension || input.height()*factor > maximumDlssTextureDimension)
        throw std::runtime_error("DLSS output exceeds the 16384-pixel texture limit.");
    const int width =
        checkedDimension(input.width() * factor, video ? DimensionRounding::EvenCeil : DimensionRounding::Ceil);
    const int height =
        checkedDimension(input.height() * factor, video ? DimensionRounding::EvenCeil : DimensionRounding::Ceil);
    if (std::max(width, height) > 16384)
        throw std::runtime_error("DLSS output exceeds the 16384-pixel texture limit.");
    return {width, height};
}
QSize rtxOutputSize(QSize input, const Settings& settings, bool video) {
    requireResolution(input, "RTX input");
    const QString mode = video ? settings.upscale_size_mode : settings.upscale_image_size_mode;
    const double factor = video ? settings.upscale_scale_factor : settings.upscale_image_scale_factor;
    const bool locked = video ? settings.upscale_aspect_lock : settings.upscale_image_aspect_lock;
    const double width = mode == "Scale factor"
                             ? input.width() * factor
                             : double(video ? settings.upscale_width : settings.upscale_image_width);
    const double height = mode == "Scale factor" ? input.height() * factor
                          : locked ? width * input.height() / input.width()
                                   : double(video ? settings.upscale_height : settings.upscale_image_height);
    const auto rounding = video ? DimensionRounding::EvenCeil : DimensionRounding::Ceil;
    QSize output(checkedDimension(width, rounding), checkedDimension(height, rounding));
    if (output.width() < input.width() || output.height() < input.height())
        throw std::runtime_error(
            "RTX Super Resolution output must be at least the input size.");
    return output;
}
Settings workflowSettings(Settings s, Workflow workflow, bool video) {
    if (workflow == Workflow::NeuralRendering)
        return s;
    if (workflow == Workflow::FrameInterpolation) {
        s.video_enabled_stages = {"frame_generation"};
        s.codec = s.frame_interpolation_codec;
        s.container = s.frame_interpolation_container;
        s.quality = s.frame_interpolation_quality;
        s.hdr_mode = s.frame_interpolation_hdr_mode;
        s.video_rename_mode = s.frame_interpolation_rename_mode;
        s.video_custom_suffix = s.frame_interpolation_custom_suffix;
        return s;
    }
    const auto engine = video ? s.upscale_engine : s.upscale_image_engine;
    if (video) {
        s.video_enabled_stages = {};
        if (engine == "DLSS")
            s.video_enabled_stages.append("dlss_super_resolution");
        else {
            if (s.upscale_vsr_enabled)
                s.video_enabled_stages.append("super_resolution");
            if (s.upscale_hdr_enabled)
                s.video_enabled_stages.append("rtx_video_hdr");
        }
        s.codec = s.upscale_codec;
        s.container = s.upscale_container;
        s.quality = s.upscale_quality;
        s.hdr_mode = s.upscale_hdr_enabled;
        s.video_rename_mode = s.upscale_rename_mode;
        s.video_custom_suffix = s.upscale_custom_suffix;
    } else {
        s.image_enabled_stages = {engine == "DLSS" ? "dlss_super_resolution" : "super_resolution"};
        s.image_format = s.upscale_image_output_format;
        s.image_quality = s.upscale_image_quality;
        s.image_rename_mode = s.upscale_image_rename_mode;
        s.image_custom_suffix = s.upscale_image_custom_suffix;
    }
    return s;
}
Settings videoPreviewSettings(Settings settings) {
    settings.codec = settings.upscale_codec = settings.frame_interpolation_codec = "FFV1 Lossless RGB 10-bit";
    settings.container = settings.upscale_container = settings.frame_interpolation_container = "MKV";
    settings.quality = settings.upscale_quality = settings.frame_interpolation_quality = "Auto (Default)";
    settings.hdr_mode = settings.frame_interpolation_hdr_mode = true;
    return settings;
}
PipelineEstimate estimatePipeline(const MediaInfo& info, const Settings& raw, Workflow workflow) {
    requireResolution({info.width, info.height}, "Input media");
    if (auto valid = raw.validate(); !valid)
        throw std::runtime_error(valid.error().toStdString());
    const auto settings = workflowSettings(raw, workflow, info.video);
    PipelineEstimate result{info.width, info.height, info.fps, info.hdr, settings.stages(info.video)};
    if(result.stages.contains("vfx_frame_generation")&&result.stages.contains("frame_generation"))
        throw std::runtime_error("Use one frame-generation engine per workflow. Disable DLSS Frame Generation or VFX Video Frame Generation.");
    for (const auto& stage : result.stages) {
        QSize size(result.width, result.height);
        if (stage == "neural_model" || stage == "frame_generation")
            requireResolution(size, stage == "neural_model" ? "DLSS Neural Rendering texture" : "DLSS Frame Generation texture",
                              maximumDlssTextureDimension);
        if (stage == "neural_model" && std::min(size.width(), size.height()) < 64)
            throw std::runtime_error("DLSS Neural Rendering requires at least 64 by 64 input.");
        else if (stage == "scale_method") {
            if (info.video && result.hdr)
                throw std::runtime_error(
                    "Scaling cannot process HDR video at this position. Move it before HDR conversion.");
            size = {even(size.width() * settings.upscaling_factor),
                    even(size.height() * settings.upscaling_factor)};
            if (std::min(size.width(), size.height()) < 64)
                throw std::runtime_error("The requested output is below the supported 64 by 64 minimum.");
        } else if (stage == "dlss_super_resolution") {
            if (info.video && result.hdr)
                throw std::runtime_error("DLSS Super Resolution cannot process HDR video at this position. "
                                         "Move it before HDR conversion.");
            size = dlssOutputSize(
                size, info.video ? settings.upscale_dlss_mode : settings.upscale_image_dlss_mode, info.video);
        } else if (stage == "dlss_ray_reconstruction") {
            if(info.video && result.hdr && !QStringList{"smpte2084","arib-std-b67"}.contains(info.transfer) && !result.stages.contains("rtx_video_hdr"))
                throw std::runtime_error("Ray Reconstruction requires explicit PQ or HLG signaling for HDR video.");
        } else if (stage == "super_resolution") {
            if (info.video && result.hdr)
                throw std::runtime_error("RTX Super Resolution cannot process HDR video at this position. "
                                         "Move it before HDR conversion.");
            size = rtxOutputSize(size, settings, info.video);
        } else if(isVfxStage(stage)){
            if(!info.video)throw std::runtime_error("NVIDIA Video Effects is video-only.");
            (void)vfxSubtitleArguments(info,settings);
            const auto selected=vfxSettingsForStages(settings,{stage});
            if(!selected.vfx_vsr_enabled&&!selected.vfx_upscale_enabled&&!selected.vfx_frame_generation_enabled)
                throw std::runtime_error("The legacy NVIDIA VFX preset has no enabled effect.");
            if(result.hdr&&selected.vfx_upscale_enabled)
                throw std::runtime_error("This video effect requires SDR at this position. Move its card before HDR conversion.");
            size=vfxSpatialSize(size,selected);result.fps=vfxFrameRate(result.fps,selected);
        } else if (stage == "rtx_video_hdr") {
            if (result.hdr)
                throw std::runtime_error("RTX Video HDR requires SDR at this position.");
            result.hdr = true;
        } else if (stage == "frame_generation") {
            const auto target = targetFrameRate(settings.frame_interpolation_target_fps);
            if (!info.video || result.fps <= 0 ||
                (workflow == Workflow::NeuralRendering && target <= result.fps))
                throw std::runtime_error("Frame Generation target FPS must exceed the frame rate entering "
                                         "this card. Choose a higher target FPS.");
            result.fps = target;
        } else if (stage == "cas_sharpening" && result.hdr && settings.cas_sharpness > 0)
            throw std::runtime_error(
                "Sharpening requires SDR video at this position. Move it before HDR conversion.");
        else if (stage == "coloring") {
            if (!settings.lut_path.isEmpty() && !QFileInfo(settings.lut_path).isFile())
                throw std::runtime_error("The selected .cube LUT is unavailable.");
        }
        requireResolution(size, "Processing stage output");
        result.width = size.width();
        result.height = size.height();
    }
    if (info.video && !containersForCodec(settings.codec).contains(settings.container))
        throw std::runtime_error("The selected codec does not support this container.");
    if (!info.video && settings.image_format == "WebP")
        requireResolution({result.width, result.height}, "WebP format", 16383);
    if (info.video && (result.width % 2 || result.height % 2) && !settings.codec.startsWith("FFV1"))
        throw std::runtime_error("Video export requires even output dimensions.");
    if (info.video && result.hdr && !settings.hdr_mode)
        throw std::runtime_error("HDR output requires 10-bit HDR Mode in Export Settings.");
    if (info.video && result.hdr && !hdrSupported(settings.codec))
        throw std::runtime_error("HDR requires a 10-bit or higher video codec.");
    return result;
}
QString renderOutputPath(const QString& input, const QString& directory, const Settings& raw,
                         Workflow workflow, bool video) {
    const auto settings = workflowSettings(raw, workflow, video);
    const QFileInfo source(input);
    const QString rename = video ? settings.video_rename_mode : settings.image_rename_mode,
                  suffix = video ? settings.video_custom_suffix : settings.image_custom_suffix;
    const QString extension = video ? settings.container.toLower() : imageExtension(settings.image_format);
    QString stem = source.completeBaseName();
    if (rename == "Custom") {
        static const QRegularExpression invalid("[<>:\"/\\\\|?*\\x00-\\x1f]");
        if (suffix.isEmpty() || suffix.contains(invalid) || suffix.endsWith(' ') || suffix.endsWith('.'))
            throw std::runtime_error("Custom suffix is empty or contains invalid filename characters.");
        stem += suffix;
    } else if (rename == "Auto")
        stem += '_' +
                (workflow == Workflow::FrameInterpolation ? QString("Frame_Interpolation")
                 : workflow == Workflow::Upscale          ? QString("Upscale")
                                                          : QString("Neural_Rendering")) +
                '_' + QDateTime::currentDateTime().toString("yyyyMMdd-HHmmss") + '_' +
                QUuid::createUuid().toString(QUuid::WithoutBraces).left(8);
    return uniqueOutputPath(QDir(directory.isEmpty() ? Paths::instance().at("outputs") : directory)
                                .filePath(stem + '.' + extension));
}
namespace {
uint32_t dlssSourceTransfer(const MediaInfo& info) {
    if(info.transfer=="smpte2084")return 5;
    if(info.transfer=="arib-std-b67")return 6;
    if(!info.video || QStringList{"iec61966-2-1","srgb"}.contains(info.transfer))return 0;
    if(info.transfer=="linear")return 2;
    if(QStringList{"gamma22","bt470m"}.contains(info.transfer))return 3;
    if(QStringList{"gamma28","bt470bg"}.contains(info.transfer))return 4;
    return 1;
}
bool dlssNativeAlpha(const QImage& input) {
    if(!input.hasAlphaChannel())return false;
    const auto image=input.convertToFormat(QImage::Format_RGBA64);
    for(int y=0;y<image.height();++y){
        const auto* row=reinterpret_cast<const QRgba64*>(image.constScanLine(y));
        for(int x=0;x<image.width();++x)if(row[x].alpha()!=65535)return true;
    }
    return false;
}
}
struct FramePipeline::Impl {
    Settings settings;
    MediaInfo info;
    QStringList stages;
    std::unique_ptr<NativeEngines::NeuralRendering> nr;
    std::vector<std::unique_ptr<NativeEngines::SuperResolution>> sr;
    std::vector<std::unique_ptr<NativeEngines::RayReconstruction>> rr;
    std::vector<std::unique_ptr<NativeEngines::RtxVideo>> rtx;
    std::vector<std::unique_ptr<VideoEffects>> vfx;
    std::vector<TemporalGuide> guides;
    std::vector<std::unique_ptr<VideoFilterStage>> filters;
    std::unique_ptr<HostRgb10Boundary> rgbBoundary;
    bool finalOutput;
    bool rtxInputNormalized;
    PreviewFrameCache* previewCache=nullptr;
    bool resetPreviewReuse=false;
    QList<QByteArray> previewKeys;
    Impl(Settings s, MediaInfo media, QStringList active, bool final,bool normalized)
        : settings(std::move(s)), info(std::move(media)), stages(normalizeVfxStages(active,settings)), finalOutput(final),rtxInputNormalized(normalized) {
        sr.resize(size_t(stages.size()));
        rr.resize(size_t(stages.size()));
        rtx.resize(size_t(stages.size()));
        vfx.resize(size_t(stages.size()));
        guides.resize(size_t(stages.size()));
        filters.resize(size_t(stages.size()));
    }
};
FramePipeline::FramePipeline(Settings s, MediaInfo input, QStringList stages, bool finalOutput,bool normalized)
    : impl_(std::make_unique<Impl>(std::move(s), std::move(input), std::move(stages), finalOutput,normalized)) {}
FramePipeline::~FramePipeline() = default;
void FramePipeline::configurePreviewCache(PreviewFrameCache& cache, QByteArray identity) {
    impl_->resetPreviewReuse=true;
    impl_->previewCache=&cache;
    impl_->previewKeys=previewStageKeys(std::move(identity),impl_->settings,impl_->info,impl_->stages,
        impl_->finalOutput,Paths::instance().root);
}
void FramePipeline::reuseResetPreviewResources() { impl_->resetPreviewReuse=true; }
QImage FramePipeline::process(const QImage& input, JobControl& control, qint64 frame, bool reset, float frameTimeMs) {
    return processBatch({&input, 1}, control, frame, reset, frameTimeMs).front();
}
std::vector<QImage> FramePipeline::processBatch(std::span<const QImage> input, JobControl& control,
                                                qint64 firstFrame, bool reset, float frameTimeMs) {
    ProcessingCacheActivity cacheActivity;
    std::vector<QImage> images{input.begin(), input.end()};
    if (images.empty())
        return images;
    auto& state = *impl_;
    const auto& s = state.settings;
    auto workingInfo=state.info;
    bool hdr = workingInfo.hdr;
    bool rgb10 = workingInfo.pixelFormat == "gbrp10le";
    const bool cacheable=state.previewCache && input.size()==1 && reset;
    for (qsizetype index = 0; index < state.stages.size(); ++index) {
        control.checkpoint();
        const auto cacheKey=cacheable ? QCryptographicHash::hash(state.previewKeys[index]+
            QByteArray::number(firstFrame),QCryptographicHash::Sha256) : QByteArray{};
        if (cacheable) {
            if (auto cached=state.previewCache->find(cacheKey)) {
                images={std::move(cached->pixels)}; hdr=cached->hdr; rgb10=cached->rgb10;
                continue;
            }
        }
        const auto remember=[&] {
            if (cacheable) {
                control.checkpoint();
                state.previewCache->store(cacheKey,{images.front(),hdr,rgb10});
            }
        };
        const auto& stage = state.stages[index];
        const auto stageInputInfo=workingInfo;
        const QStringList filterStages{"scale_method", "cas_sharpening", "grain", "coloring"};
        if (workingInfo.video && filterStages.contains(stage)) {
            qsizetype last = index;
            auto& filter = state.filters[size_t(index)];
            if (!filter) {
                auto colors = workingInfo; colors.hdr = hdr;
                if (hdr && !workingInfo.hdr) { colors.primaries = "bt2020"; colors.transfer = "smpte2084"; }
                filter = std::make_unique<VideoFilterStage>(state.stages.mid(index, last-index+1), images.front().size(), colors, s, quint64(firstFrame));
            }
            images = filter->process(images, control, rgb10);
            rgb10 = true;
            index = last;
            remember();
            continue;
        }
        if (stage == "scale_method") {
            const auto size = images.front().size();
            images = resizeFrames(
                images, {even(size.width() * s.upscaling_factor), even(size.height() * s.upscaling_factor)},
                workingInfo.video ? s.video_scaling_filter : s.image_scaling_filter, control, false,
                s.ffmpeg_device);
            remember();
            continue;
        }
        if (stage == "cas_sharpening") {
            images = sharpenFrames(images, s, control,
                                   workingInfo.video ? workingInfo.transfer == "iec61966-2-1" ? "srgb"
                                                      : workingInfo.transfer == "linear"     ? "linear"
                                                                                            : "bt709"
                                                    : "srgb");
            remember();
            continue;
        }
        if (stage == "grain") {
            images = grainFrames(images, s, control, quint64(firstFrame), hdr, workingInfo.video);
            remember();
            continue;
        }
        const bool sharedVsr=stage=="super_resolution"&&hasVfxStages(s.stages(true));
        if (workingInfo.video && !state.rtxInputNormalized && (stage=="rtx_video_hdr" || stage=="super_resolution"&&!sharedVsr)) {
            auto& normalizer=state.filters[size_t(index)];
            if (!normalizer) {
                auto colors=workingInfo;
                colors.pixelFormat=rgb10 ? "gbrp10le" : images.front().depth()>32 ? "rgba64le" : "rgba";
                normalizer=std::make_unique<VideoFilterStage>(QStringList{"rtx_normalization"},images.front().size(),colors,s);
            }
            images=normalizer->process(images,control,rgb10);
            rgb10=images.front().depth()>32;
        }
        for (size_t frame = 0; frame < images.size(); ++frame) {
            control.checkpoint();
            auto& image = images[frame];
            if (workingInfo.video && rgb10 && (stage == "neural_model" || stage == "dlss_super_resolution" || stage == "dlss_ray_reconstruction"))
                image = decodedRgb10Rgba(image);
            const bool resetFrame = reset && frame == 0;
            if (stage == "neural_model") {
                if (!state.nr)
                    state.nr = std::make_unique<NativeEngines::NeuralRendering>();
                const bool resetHistory = workingInfo.video
                    ? state.guides[size_t(index)].reset(image, .24, resetFrame) : resetFrame;
                image = state.nr->process(image, s, workingInfo.video, resetHistory, state.resetPreviewReuse && reset);
            }
            else if (stage == "dlss_super_resolution") {
                auto& session = state.sr[size_t(index)];
                if (!session)
                    session = std::make_unique<NativeEngines::SuperResolution>(
                        image.size(),
                        dlssOutputSize(image.size(),
                                       workingInfo.video ? s.upscale_dlss_mode : s.upscale_image_dlss_mode,
                                       workingInfo.video),
                        workingInfo.video ? s.upscale_dlss_mode : s.upscale_image_dlss_mode,
                        workingInfo.video ? s.upscale_dlss_preset : s.upscale_image_dlss_preset,
                        s.upscale_optical_flow_quality, !workingInfo.video,
                        !workingInfo.video && s.image_bit_depth==16, dlssSourceTransfer(workingInfo),
                        !workingInfo.video && dlssNativeAlpha(image));
                const bool resetHistory = workingInfo.video
                    ? state.guides[size_t(index)].resetMedia(image, resetFrame) : resetFrame;
                const float delta=frameTimeMs>0 ? frameTimeMs : workingInfo.fps>0 ? float(1000./workingInfo.fps) : 0.f;
                image = session->process(image, resetHistory, quint32(firstFrame + qint64(frame)), delta);
            } else if(stage=="dlss_ray_reconstruction"){
                auto& session=state.rr[size_t(index)];
                const bool alpha=!workingInfo.video&&dlssNativeAlpha(image);
                auto rrColors=workingInfo;
                if(hdr&&!workingInfo.hdr){rrColors.hdr=true;rrColors.primaries="bt2020";rrColors.transfer="smpte2084";}
                if(!session)session=std::make_unique<NativeEngines::RayReconstruction>(
                    image.size(),
                    s.rr_optical_flow_quality,!workingInfo.video,!workingInfo.video&&
                        (s.image_bit_depth==16||index+1<state.stages.size()||!state.finalOutput),
                    dlssSourceTransfer(rrColors),alpha);
                const bool resetHistory=resetFrame;
                const float delta=frameTimeMs>0?frameTimeMs:workingInfo.fps>0?float(1000./workingInfo.fps):0.f;
                image=session->process(image,resetHistory,quint32(firstFrame+qint64(frame)),delta);
            } else if(isVfxStage(stage)){
                auto colors=stageInputInfo;colors.width=image.width();colors.height=image.height();colors.hdr=hdr;
                const auto selected=vfxSettingsForStages(s,{stage});
                auto& effect=state.vfx[size_t(index)];if(!effect)effect=std::make_unique<VideoEffects>(selected,colors,control);
                auto pixels=image.convertToFormat(image.depth()>32?QImage::Format_RGBA64:QImage::Format_RGBA8888);
                image=effect->process(vfxDescriptor(pixels,colors),resetFrame).toHost(colors.hdr||pixels.depth()>32);
                workingInfo=effect->outputInfo();
            } else if (stage == "super_resolution" || stage == "rtx_video_hdr") {
                const auto alphaSource = image;
                const bool convertHdr = stage == "rtx_video_hdr";
                if(workingInfo.video&&hasVfxStages(s.stages(true))&&!convertHdr){
                    // Use the VFX VSR path for this card in a combined workflow.
                    // RTX Video HDR continues through its own NGX bridge.
                    auto shared=s;shared.vfx_vsr_enabled=true;
                    shared.vfx_upscale_enabled=false;shared.vfx_frame_generation_enabled=false;
                    shared.vfx_vsr_mode=s.upscale_vsr_quality;shared.vfx_vsr_strength=1;
                    const auto output=rtxOutputSize(image.size(),s,true);
                    shared.vfx_size_mode="Custom dimensions";shared.vfx_width=output.width();shared.vfx_height=output.height();shared.vfx_aspect_lock=false;
                    auto colors=workingInfo;colors.width=image.width();colors.height=image.height();colors.hdr=false;
                    auto& effect=state.vfx[size_t(index)];if(!effect)effect=std::make_unique<VideoEffects>(shared,colors,control);
                    auto pixels=image.convertToFormat(image.depth()>32?QImage::Format_RGBA64:QImage::Format_RGBA8888);
                    image=effect->process(vfxDescriptor(pixels,colors),resetFrame).toHost(true);workingInfo=effect->outputInfo();
                    continue;
                }
                auto& session = state.rtx[size_t(index)];
                if (!session)
                    session = std::make_unique<NativeEngines::RtxVideo>(
                        image.size(),
                        convertHdr ? image.size() : rtxOutputSize(image.size(), s, workingInfo.video), s,
                        convertHdr, workingInfo.video, image.depth()>32);
                image = session->process(image);
                if (convertHdr && session->linearHdr())
                    image = pqFromScRgb(image);
                // The RTX still bridge already reconstructs its RGBA8 alpha.
                // A 16-bit source needs the separate mask to retain its depth.
                if (!workingInfo.video && alphaSource.depth()>32)
                    restoreImageAlpha(image, alphaSource,control);
            } else if (stage == "coloring") {
                image = colorImage(image, s, control);
            }
        }
        if (workingInfo.video && (stage=="super_resolution" || stage=="rtx_video_hdr")) rgb10=true;
        if (stage == "rtx_video_hdr")
            hdr = true;
        const bool final = index+1 == state.stages.size() && state.finalOutput;
        if (workingInfo.video && stage == "dlss_super_resolution" && !final) {
            if (!state.rgbBoundary) state.rgbBoundary = std::make_unique<HostRgb10Boundary>();
            for (auto& image : images) image = state.rgbBoundary->convert(image, control);
            rgb10 = true;
        } else if (stage == "neural_model" || stage == "dlss_super_resolution" || stage == "dlss_ray_reconstruction") rgb10 = false;
        remember();
    }
    if (!workingInfo.video)
        for (size_t index = 0; index < images.size(); ++index) {
            auto& image = images[index];
            image.setColorSpace(input[index].colorSpace());
            image.setDotsPerMeterX(input[index].dotsPerMeterX());
            image.setDotsPerMeterY(input[index].dotsPerMeterY());
        }
    return images;
}
RenderResult Pipeline::render(const RenderRequest& request, JobControl& control,
                              const ProgressCallback& progress) {
    ProcessingCacheActivity cacheActivity;
    // Construct first so that job-local frames, encoders, processes and sessions
    // are destroyed before unused allocator pages are reclaimed on this worker.
    const auto reclaimCaches=qScopeGuard([] { reclaimIdleHeapCaches(); });
    QElapsedTimer elapsed;
    elapsed.start();
    const auto initiallyPaused = control.pausedTime();
    control.checkpoint();
    const auto activeElapsed = [&] {
        return std::max<qint64>(0, elapsed.elapsed() - (control.pausedTime() - initiallyPaused).count());
    };
    auto report = [&](double value, const QString& text, qint64 frames = 0, qint64 total = 0) {
        const auto milliseconds = activeElapsed();
        if (progress)
            progress({value, text, frames, total, milliseconds > 0 ? frames * 1000. / milliseconds : 0});
    };
    report(.01, QT_TRANSLATE_NOOP("App", "Loading source metadata"));
    const auto input = probeMedia(request.input, &control);
    const auto requestedSettings = request.preview && input.video ? videoPreviewSettings(request.settings) : request.settings;
    auto settings = workflowSettings(requestedSettings, request.workflow, input.video);
    const auto estimate = estimatePipeline(input, requestedSettings, request.workflow);
    if(request.preview&&input.video&&request.startSeconds>0&&!request.temporalContextIncluded&&
       estimate.stages.contains("dlss_ray_reconstruction")) {
        ScratchDirectory context(QStringLiteral("rr-preview-context-XXXXXX"));
        if(!context.isValid())throw std::runtime_error("Cannot create Ray Reconstruction preview history.");
        const double start=reconstructionContextStart(request.input,input,request.startSeconds,
            reconstructionHistoryFrames(settings,estimate.stages),control,gpuOrdinal(settings.ai_gpu_uuid),settings.ffmpeg_device=="auto");
        const double prefix=request.startSeconds-start;
        auto warmed=request;warmed.output=context.filePath("warmed.mkv");warmed.startSeconds=start;
        warmed.seconds=request.seconds>0?prefix+request.seconds:0;warmed.temporalContextIncluded=true;
        const auto result=render(warmed,control,progress);
        const double origin=result.info.metadata.value("ve_source_time_origin").toDouble(start);
        const double discard=std::max(0.,request.startSeconds+input.presentationStartSeconds()-origin);
        OutputTransaction output(request.output);
        QStringList trim{"-v","error","-y","-ss",QString::number(discard,'f',12),"-i",warmed.output,
                         "-map","0:v:0","-an","-c:v","copy","-map_metadata","0"};
        if(request.seconds>0)trim<<"-t"<<QString::number(request.seconds,'f',12);
        trim<<"-avoid_negative_ts"<<"make_zero"<<output.path();
        const auto trimmed=runProcess(Paths::instance().ffmpeg(),trim,&control,0);
        if(trimmed.exitCode)throw std::runtime_error(trimmed.error.toStdString());
        control.checkpoint();output.commit();
        auto outputInfo=probeMedia(request.output,&control);
        info("dlssrr",QString("Preview discarded %1 s of bounded RR context (maximum 32 source frames).").arg(discard,0,'g',12));
        return {request.input,request.output,Log::instance().sessionPath(),outputInfo,activeElapsed()/1000.};
    }
    if(input.video&&hasVfxStages(estimate.stages)&&!request.isolatedVfxWorker)return renderIsolatedVfx(request,control,progress);
    OutputTransaction output(request.output);
    if (input.video && (renderVfxVideo(request,input,settings,estimate,output.path(),control,progress) ||
                       renderRayReconstructionVideo(request,input,settings,estimate,output.path(),control,progress) ||
                       renderCudaVideo(request, input, settings, estimate, output.path(), control, progress) ||
                       renderInterpolationVideo(request, input, settings, estimate, output.path(), control, progress))) {
        output.commit();
        const auto result = probeMedia(request.output, &control);
        report(1, QT_TRANSLATE_NOOP("App", "Video complete"), result.frames, result.frames);
        info("render", request.input + " -> " + request.output);
        return {request.input, request.output, Log::instance().sessionPath(), result, activeElapsed() / 1000.};
    }
    if (estimate.stages.contains("neural_model") || estimate.stages.contains("dlss_super_resolution") || estimate.stages.contains("dlss_ray_reconstruction") ||
        estimate.stages.contains("super_resolution") || estimate.stages.contains("frame_generation") ||
        estimate.stages.contains("rtx_video_hdr"))
        NativeEngines::instance().prepare(gpuOrdinal(settings.ai_gpu_uuid), estimate.stages);
    if (!input.video) {
        report(.05, QT_TRANSLATE_NOOP("App", "Decoding image"));
        const auto source = request.stillSources ? decodeStillPreviewSource(request.input, *request.stillSources, control).pixels
                                                 : decodeImage(request.input, control);
        FramePipeline pipeline(settings, input, estimate.stages);
        report(.2, QT_TRANSLATE_NOOP("App", "Processing image"));
        auto result = pipeline.process(source, control, 0, true);
        report(.9, QT_TRANSLATE_NOOP("App", "Saving image"));
        encodeImage(output.path(), result, settings.image_format, settings.image_quality,
                    settings.image_bit_depth, control, request.input,
                    estimate.stages.contains("neural_model") ? neuralRenderNote(settings) : QString{});
        output.commit();
        report(1, QT_TRANSLATE_NOOP("App", "Image complete"));
        return {request.input, request.output, Log::instance().sessionPath(),
                probeMedia(request.output, &control), activeElapsed() / 1000.};
    }
    const auto fiPosition = estimate.stages.indexOf("frame_generation");
    const auto before = fiPosition < 0 ? estimate.stages : estimate.stages.mid(0, fiPosition);
    auto after = fiPosition < 0 ? QStringList{} : estimate.stages.mid(fiPosition + 1);
    MediaInfo intermediate = input;
    const auto beforeEstimate = [&] {
        Settings s = settings;
        s.video_enabled_stages = before;
        return estimatePipeline(input, s, Workflow::NeuralRendering);
    }();
    intermediate.width = beforeEstimate.width;
    intermediate.height = beforeEstimate.height;
    intermediate.hdr = beforeEstimate.hdr;
    if(before.contains("rtx_video_hdr")){intermediate.primaries="bt2020";intermediate.transfer="smpte2084";}
    const bool rrInterpolationPrecision=fiPosition>=0&&
        ((!before.isEmpty()&&before.back()=="dlss_ray_reconstruction")||(!after.isEmpty()&&after.front()=="dlss_ray_reconstruction"));
    const double sourceFps = input.fps > 0 ? input.fps : 30.,
                 targetFps =
                     fiPosition < 0 ? sourceFps : targetFrameRate(settings.frame_interpolation_target_fps);
    const double duration =
        request.seconds > 0 ? std::min(request.seconds, std::max(0., input.duration - request.startSeconds))
                            : std::max(0., input.duration - request.startSeconds);
    const qint64 total = static_cast<qint64>(std::ceil(duration * targetFps - 1e-8));
    double actualDuration = 0;
    const bool nativeStages = std::ranges::any_of(estimate.stages, [](const QString& stage) {
        return QStringList{"neural_model", "dlss_super_resolution", "dlss_ray_reconstruction", "super_resolution", "rtx_video_hdr",
                           "frame_generation"}.contains(stage);
    });
    const auto codec = request.preview ? QString("FFV1 Lossless RGB 10-bit")
        : nativeStages ? nativeDeliveryCodec(settings) : settings.codec;
    const bool nativeNeuralDelivery = estimate.stages.size() > 1 && estimate.stages.back() == "neural_model" &&
        codec.contains("NVIDIA NVENC") && gpuOrdinal(settings.video_gpu_uuid) == gpuOrdinal(settings.ai_gpu_uuid);
    const bool dlssMedia = before.contains("dlss_super_resolution")||before.contains("dlss_ray_reconstruction");
    const bool nativeDecoder = dlssMedia || !before.isEmpty() && QStringList{"neural_model", "super_resolution", "rtx_video_hdr"}.contains(before.front())
        || (before.isEmpty() && fiPosition == 0);
    const bool sourceWide = input.precisionBits() > 8;
    std::unique_ptr<HostVideoDecoder> hostDecoder;
    std::unique_ptr<CudaVideoDecoder> neuralDecoder;
    std::unique_ptr<NativeEngines::NeuralRendering> neuralSource;
    std::unique_ptr<NativeEngines::RtxVideo> rtxSource;
    std::optional<CudaVideoFrame> neuralFirst;
    auto preStages = before;
    std::unique_ptr<VideoFilterStage> filteredSource;
    const QStringList sourceFilters{"scale_method", "cas_sharpening", "grain", "coloring"};
    if (!dlssMedia && !before.isEmpty() && sourceFilters.contains(before.front())) {
        int count = 1;
        filteredSource = std::make_unique<VideoFilterStage>(before.mid(0, count), input, settings,
            request.input, request.startSeconds, control);
        preStages = before.mid(count);
    }
    if (!before.isEmpty() && QStringList{"neural_model","super_resolution","rtx_video_hdr"}.contains(before.front())) {
        try {
            neuralDecoder = std::make_unique<CudaVideoDecoder>(request.input, gpuOrdinal(settings.ai_gpu_uuid), request.startSeconds, control);
            neuralFirst = neuralDecoder->read();
            if (!neuralFirst) throw std::runtime_error("The source contains no decodable video frames.");
            if (before.front()=="neural_model") neuralSource = std::make_unique<NativeEngines::NeuralRendering>();
            else {
                const bool hdr=before.front()=="rtx_video_hdr";
                const QSize source(input.width,input.height);
                rtxSource=std::make_unique<NativeEngines::RtxVideo>(source,hdr ? source : rtxOutputSize(source,settings,true),
                    settings,hdr,true,sourceWide);
            }
            preStages.removeFirst();
        } catch (const CudaUnavailable& error) {
            neuralDecoder.reset();
            info("video", "Native source decode unavailable: " + QString::fromUtf8(error.what()));
        }
    }
    auto preInfo = input;
    if (filteredSource) { preInfo.pixelFormat="gbrp10le"; preInfo.colorSpace="gbr"; preInfo.range="pc"; }
    if (rtxSource) {
        preInfo.pixelFormat="gbrp10le"; preInfo.colorSpace="gbr"; preInfo.range="pc";
        preInfo.hdr=before.front()=="rtx_video_hdr";
        preInfo.primaries=preInfo.hdr ? "bt2020" : "bt709"; preInfo.transfer=preInfo.hdr ? "smpte2084" : "bt709";
    }
    auto postInfo=intermediate;
    if(fiPosition>=0)postInfo.fps=targetFps;
    if (fiPosition>=0) { postInfo.pixelFormat=rrInterpolationPrecision?"rgba64le":postInfo.hdr ? "gbrp10le" : "rgba";
        if(rrInterpolationPrecision)postInfo.sourceBitDepth=16;postInfo.colorSpace="gbr"; postInfo.range="pc"; }
    if (nativeNeuralDelivery) {
        if (fiPosition < 0) preStages.removeLast();
        else after.removeLast();
    }
    FramePipeline post(settings, postInfo, after, !nativeNeuralDelivery);
    std::unique_ptr<PipeProcess> decoder;
    std::unique_ptr<FramePacketReader> reader;
    if (filteredSource || neuralDecoder) {
        // The first NR card consumes the original NV12/P010 planes. Packing
        // through a CPU RGB conversion before NR changes the native result.
    } else if (nativeDecoder) {
        // SDR FI consumes swscale's direct RGBA8 decode. Expanding RGB10 to
        // RGBA16 first and reducing it with Qt rounds source codes differently;
        // the native temporal model amplifies those input differences.
        const auto precision = before.isEmpty() && fiPosition == 0 && !input.hdr
            ? HostDecodePrecision::Unorm8 : HostDecodePrecision::Source;
        hostDecoder = std::make_unique<HostVideoDecoder>(request.input, input, gpuOrdinal(settings.ai_gpu_uuid),
            request.startSeconds, true, control, precision);
    } else {
    const auto decodePlan = planVideoDecoder(settings.ffmpeg_device, request.input, control,
        sourceWide ? "rgba64le" : "rgba", input.clockwiseRotation());
    QStringList decodeArgs{"-v", "error", "-copyts"};
    decodeArgs << decodePlan.inputOptions;
    if (request.startSeconds > 0)
        decodeArgs << "-ss" << QString::number(request.startSeconds, 'f', 6);
    decodeArgs << "-i" << request.input;
    if (request.seconds > 0)
        decodeArgs << "-t" << QString::number(request.seconds, 'f', 6);
    if (!decodePlan.filter.isEmpty())
        decodeArgs << "-vf" << decodePlan.filter;
    decodeArgs << "-map" << "0:v:0" << "-an" << "-fps_mode" << "passthrough" << "-enc_time_base:v" << "demux"
               << "-c:v" << "rawvideo" << "-pix_fmt" << (sourceWide ? "rgba64le" : "rgba") << "-f" << "nut" << "-write_index" << "0"
               << "-flush_packets" << "1" << "pipe:1";
    decoder = std::make_unique<PipeProcess>(Paths::instance().ffmpeg(), decodeArgs,PipeProcess::OutputMode::Backpressured);
    reader = std::make_unique<FramePacketReader>(*decoder, control, sourceWide ? RawPixelFormat::Rgba64 : RawPixelFormat::Rgba8);
    }
    const auto readSource = [&]() -> std::optional<TimedImage> {
        if (filteredSource) return filteredSource->read();
        if (!neuralDecoder) return hostDecoder ? hostDecoder->read() : reader->read();
        auto frame = neuralFirst ? std::exchange(neuralFirst, std::nullopt) : neuralDecoder->read();
        if (!frame) return {};
        frame->rotation = uint32_t(input.clockwiseRotation());
        auto image = rtxSource ? rtxSource->process(*frame,input)
            : neuralSource->processHost(*frame, settings, neuralSource->sceneReset(*frame), sourceWide);
        return TimedImage{std::move(image), frame->pts, frame->duration, frame->timeBase};
    };
    std::optional<double> sourceTimeOrigin;
    std::optional<qint64> rawOrigin;
    const auto read=[&]() -> std::optional<TimedImage> {
        auto frame=readSource();
        if (!frame) return {};
        if (neuralDecoder) sourceTimeOrigin=neuralDecoder->timeOrigin();
        else if (hostDecoder) sourceTimeOrigin=hostDecoder->timeOrigin();
        else {
            if (!rawOrigin) { rawOrigin=frame->pts; sourceTimeOrigin=frame->seconds(); }
            frame->pts-=*rawOrigin;
        }
        if (request.seconds>0 && frame->seconds()>=duration-1e-9) return {};
        actualDuration=std::max(actualDuration,frame->seconds()+(frame->duration>0
            ? frame->duration*double(frame->timeBase.numerator)/frame->timeBase.denominator : 1/sourceFps));
        return frame;
    };
    FrameStreamPipeline pre(settings,preInfo,preStages,read,control,fiPosition<0 && !nativeNeuralDelivery);
    const auto decodedTimeBase = filteredSource ? filteredSource->timeBase() : neuralDecoder ? neuralDecoder->timeBase()
        : hostDecoder ? hostDecoder->timeBase() : reader->timeBase();
    auto known = [](QString value, const QString& fallback) {
        return value.isEmpty() || value == "unknown" || value == "unspecified" || value == "reserved"
                   ? fallback
                   : value;
    };
    const bool rtxSdr=estimate.stages.contains("super_resolution")&&!hasVfxStages(estimate.stages);
    const auto primaries = estimate.stages.contains("rtx_video_hdr")
                               ? QString("bt2020")
                               : rtxSdr ? QString("bt709")
                               : known(input.primaries, estimate.hdr ? "bt2020" : "unknown");
    const auto transfer = estimate.stages.contains("rtx_video_hdr")
                              ? QString("smpte2084")
                              : rtxSdr ? QString("bt709")
                              : known(input.transfer, estimate.hdr ? "smpte2084" : "unknown");
    ScratchDirectory encodedTemporary(QStringLiteral("host-delivery-XXXXXX"));
    if (!encodedTemporary.isValid()) throw std::runtime_error("Cannot create the host delivery directory.");
    const auto encodedVideo = encodedTemporary.filePath(codec.startsWith("ProRes") ? "video.mov" : request.preview ? "video.mkv" : "video.nut");
    auto encodedInfo = input;
    encodedInfo.width = estimate.width; encodedInfo.height = estimate.height;
    encodedInfo.fps = targetFps; encodedInfo.hdr = estimate.hdr;
    encodedInfo.primaries = primaries; encodedInfo.transfer = transfer;
    if(hasVfxStages(estimate.stages))retainVfxPixelAspect(encodedInfo,input);
    if (!estimate.stages.isEmpty() && estimate.stages.back() != "neural_model" && estimate.stages.back() != "frame_generation"
        && estimate.stages.back() != "dlss_super_resolution" && estimate.stages.back() != "dlss_ray_reconstruction")
        encodedInfo.pixelFormat = "gbrp10le";
    else if (encodedInfo.pixelFormat == "gbrp10le" && !estimate.stages.isEmpty() &&
             QStringList{"neural_model","dlss_super_resolution","dlss_ray_reconstruction"}.contains(estimate.stages.back()))
        // The input cache was planar RGB10; the native result is packed
        // RGBA16 and needs the encoder's genuine swscale packing boundary.
        encodedInfo.pixelFormat = "rgba64le";
    if(!estimate.stages.isEmpty()&&estimate.stages.back()=="dlss_ray_reconstruction"){
        encodedInfo.pixelFormat="rgba64le";encodedInfo.sourceBitDepth=16;
    }
    if (fiPosition>=0 && after.isEmpty()) {
        encodedInfo.pixelFormat=rrInterpolationPrecision?"rgba64le":intermediate.hdr ? "gbrp10le" : "rgba";
        if(rrInterpolationPrecision)encodedInfo.sourceBitDepth=16;
    }
    const auto timeBase = fiPosition < 0 ? decodedTimeBase
                          : settings.frame_interpolation_target_fps.contains('.')
                              ? TimeBase{1001, int(std::lround(targetFps * 1001))}
                              : TimeBase{1, int(std::lround(targetFps))};
    std::unique_ptr<HostVideoEncoder> hostEncoder;
    std::unique_ptr<NeuralVideoDelivery> neuralDelivery;
    std::shared_ptr<void> interpolationDevice;
    std::unique_ptr<CudaFramePool> interpolationPool;
    std::unique_ptr<CudaVideoEncoder> interpolationEncoder;
    const bool nativeInterpolationDelivery = fiPosition >= 0 && after.isEmpty() && targetFps > sourceFps &&
        codec.contains("NVIDIA NVENC") && gpuOrdinal(settings.video_gpu_uuid) == gpuOrdinal(settings.ai_gpu_uuid);
    if (nativeInterpolationDelivery) {
        const int ordinal = gpuOrdinal(settings.ai_gpu_uuid);
        interpolationDevice = cudaPrimaryDevice(ordinal);
        const auto encoder = codec.startsWith("H.264") ? QString("h264_nvenc")
            : codec.startsWith("H.265") ? QString("hevc_nvenc") : QString("av1_nvenc");
        interpolationEncoder = std::make_unique<CudaVideoEncoder>(encodedVideo, encoder, settings, encodedInfo,
            timeBase, interpolationDevice.get(), ordinal, control);
        interpolationPool = std::make_unique<CudaFramePool>(interpolationDevice.get(),
            QSize(encodedInfo.width, encodedInfo.height),
            interpolationEncoder->pixelFormat() == VideoPixelFormat::P010, encodedInfo.hdr);
    } else if (nativeNeuralDelivery) {
        const auto preceding = estimate.stages[estimate.stages.size()-2];
        const bool rgb10 = preceding != "frame_generation" || estimate.hdr;
        neuralDelivery = std::make_unique<NeuralVideoDelivery>(encodedVideo, codec, settings, encodedInfo,
            gpuOrdinal(settings.ai_gpu_uuid), rgb10, control);
    } else hostEncoder = std::make_unique<HostVideoEncoder>(encodedVideo, codec, settings, encodedInfo,
        gpuOrdinal(settings.video_gpu_uuid), control,
        request.preview ? HostEncodePurpose::NativePreview : HostEncodePurpose::Delivery);
    // Ordinary quality targets need temporal compression. Forcing every
    // filtered frame to be an IDR inflated files despite the CQ/CRF target.
    // Preserve the established Max and lossless/intra-only delivery behavior.
    const bool qualityEncoding=codec.startsWith("H.264") || codec.startsWith("H.265") || codec.startsWith("AV1");
    const bool filteredDelivery=(!qualityEncoding || settings.quality == "Max") && !estimate.stages.isEmpty() &&
        QStringList{"scale_method","coloring","cas_sharpening","grain"}.contains(estimate.stages.back());
    std::unique_ptr<InterpolationGrid> interpolation;
    if (fiPosition >= 0)
        interpolation = std::make_unique<InterpolationGrid>(intermediate, settings, targetFps,
            QSize(intermediate.width, intermediate.height), !before.isEmpty()
                && before.back() != "neural_model"&&before.back()!="dlss_ray_reconstruction",rrInterpolationPrecision);
    const auto batchSize =
        videoBatchCapacity({std::max(input.width, estimate.width), std::max(input.height, estimate.height)});
    struct CachedPacket {
        QImage pixels;
        qint64 pts, duration;
        TimeBase timeBase;
    };
    std::deque<CachedPacket> ready;
    qint64 preFrame = 0;
    auto nextProcessed = [&]() -> std::optional<TimedImage> {
        if (!interpolation) return pre.read();
        if (ready.empty()) {
            std::vector<TimedImage> packets;
            std::vector<QImage> images;
            packets.reserve(batchSize);
            images.reserve(batchSize);
            for (size_t index = 0; index < batchSize; ++index) {
                auto item = pre.read();
                if (!item)
                    break;
                images.push_back(item->image);
                packets.push_back(std::move(*item));
            }
            if (packets.empty())
                return std::nullopt;
            auto processed = std::move(images);
            preFrame += qint64(processed.size());
            if (interpolation) interpolation->prepare(processed,control);
            for (size_t index = 0; index < packets.size(); ++index) {
                auto pixels = std::move(processed[index]);
                ready.push_back({std::move(pixels),
                                 packets[index].pts, packets[index].duration, packets[index].timeBase});
            }
        }
        auto result = std::move(ready.front());
        ready.pop_front();
        return TimedImage{std::move(result.pixels), result.pts, result.duration, result.timeBase};
    };
    auto packet = nextProcessed();
    if (!packet)
        throw std::runtime_error("Source contains no decodable video frames.");
    auto previous = packet->image;
    qint64 outputIndex = 0, written = 0;
    std::vector<QImage> pending;
    std::vector<std::pair<qint64, qint64>> timestamps;
    pending.reserve(batchSize);
    timestamps.reserve(batchSize);
    auto flush = [&] {
        if (pending.empty())
            return;
        std::vector<QImage> images;
        images.reserve(pending.size());
        for (const auto& frame : pending) {
            control.checkpoint();
            images.push_back(frame);
        }
        pending.clear();
        auto results = post.processBatch(images, control, written, written == 0);
        for (size_t index = 0; index < results.size(); ++index) {
            if (neuralDelivery) neuralDelivery->write(results[index], timestamps[index].first, timestamps[index].second, timeBase);
            else hostEncoder->write(results[index], timestamps[index].first, timestamps[index].second, timeBase,filteredDelivery);
            ++written;
            report(total > 0 ? std::min(.98, written / double(total)) : .1, QT_TRANSLATE_NOOP("App", "Processing video"), written,
                   total);
        }
        pending.clear();
        timestamps.clear();
    };
    auto writeFrame = [&](const QImage& pixels, qint64 pts, qint64 frameDuration) {
        if (fiPosition<0) {
            if (neuralDelivery) neuralDelivery->write(pixels,pts,frameDuration,timeBase);
            else hostEncoder->write(pixels,pts,frameDuration,timeBase,filteredDelivery);
            ++outputIndex; ++written;
            report(total>0 ? std::min(.98,written/double(total)) : .1,QT_TRANSLATE_NOOP("App", "Processing video"),written,total);
            return;
        }
        pending.push_back(pixels);
        timestamps.emplace_back(pts, frameDuration);
        ++outputIndex;
        if (pending.size() >= batchSize)
            flush();
    };
    auto emitInterpolated=[&](const InterpolationGrid::Output& selected,qint64 index) {
        if (interpolationEncoder) {
            auto frame = selected.surface.toVideo(*interpolationPool,
                interpolationEncoder->pixelFormat() == VideoPixelFormat::P010, encodedInfo.hdr ? 2u : 1u, 0);
            frame.pts = index; frame.duration = 1; frame.timeBase = timeBase;
            interpolationEncoder->write(frame);
            ++outputIndex; ++written;
            report(total>0 ? std::min(.98,written/double(total)) : .1,QT_TRANSLATE_NOOP("App", "Processing video"),written,total);
        } else {
            const auto pixels = selected.surface
                ? interpolation->hdr&&!interpolation->highPrecision ? selected.surface.toHostRgb10() : selected.surface.toHost()
                : selected.pixels;
            writeFrame(pixels,index,1);
        }
    };
    NearestFrameWriter<InterpolationGrid::Output,decltype(emitInterpolated)> writer(
        request.seconds>0 ? total : std::numeric_limits<qint64>::max(),
        FrameTime(timeBase.numerator,timeBase.denominator),emitInterpolated);
    while (true) {
        control.checkpoint();
        if (fiPosition < 0) {
            writeFrame(previous, packet->pts, packet->duration);
            packet = nextProcessed();
            if (!packet)
                break;
            previous = packet->image;
            continue;
        }
        auto frames=interpolation->push(packet->image,
            FrameTime(packet->pts*packet->timeBase.numerator,packet->timeBase.denominator),control);
        for (auto& frame : frames) writer.push(std::move(frame));
        packet=nextProcessed();
        if (!packet) {
            writer.finish(qint64(std::ceil((request.seconds>0 ? std::min(actualDuration,request.seconds) : actualDuration)*targetFps-1e-9)));
            break;
        }
    }
    flush();
    report(.99, QT_TRANSLATE_NOOP("App", "Finalizing export"), outputIndex, total);
    if (interpolationEncoder) { interpolationEncoder->finish(); interpolationEncoder.reset(); }
    else if (neuralDelivery) { neuralDelivery->finish(); neuralDelivery.reset(); }
    else { hostEncoder->finish(); hostEncoder.reset(); }
    if (request.preview) { settings.codec = codec; settings.container = "MKV"; }
    muxDelivery(encodedVideo, output.path(), request, input, settings,
        request.seconds > 0 ? std::min(actualDuration, request.seconds) : actualDuration, control,
        request.preview ? std::nullopt : sourceTimeOrigin);
    output.commit();
    report(1, QT_TRANSLATE_NOOP("App", "Video complete"), outputIndex, total);
    auto outputInfo = probeMedia(request.output, &control);
    if(sourceTimeOrigin)outputInfo.metadata.insert("ve_source_time_origin",*sourceTimeOrigin);
    info("render", request.input + " -> " + request.output);
    return {request.input, request.output, Log::instance().sessionPath(), outputInfo,
            activeElapsed() / 1000.};
}
} // namespace ve
