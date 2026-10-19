#include "interpolation_video.hpp"
#include "neural_video_delivery.hpp"
#include "native_engines.hpp"
#include "vfx_native.hpp"
#include "media/av_api.hpp"
#include "media/cuda_video.hpp"
#include "media/native_rgb.hpp"
#include "media/frame_time.hpp"
#include "media/host_video.hpp"
#include "media/video_delivery.hpp"
#include "media/frame_batch.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/log.hpp"
#include <QElapsedTimer>
#include "infrastructure/scratch.hpp"
#include <algorithm>
#include <cmath>
#include <limits>

namespace ve {
namespace {
using Surface = NativeEngines::Interpolation::Surface;
struct TimedSurface { Surface pixels; FrameTime time; int segment = 0; };
struct InterpolationStage {
    std::unique_ptr<NativeEngines::Interpolation> session;
    std::optional<TimedSurface> previous;
    int generated = 1;
    std::vector<TimedSurface> push(NativeEngines::Interpolation::Result result, FrameTime time, int segment) {
        if (previous && result.sceneCut) segment = previous->segment + 1;
        std::vector<TimedSurface> frames;
        if (previous && !result.reset) {
            const auto interval = time - previous->time;
            int index = 0;
            for (auto& surface : result.generated)
                frames.push_back({std::move(surface), previous->time + interval.fraction(++index, generated+1), segment});
        }
        previous = TimedSurface{std::move(result.source), time, segment};
        frames.push_back(*previous);
        return frames;
    }
};
TimeBase outputTimeBase(const Settings& settings) {
    const auto fps = targetFrameRate(settings.frame_interpolation_target_fps);
    return settings.frame_interpolation_target_fps.contains('.') ? TimeBase{1001, int(std::lround(fps*1001))}
                                                                : TimeBase{1, int(std::lround(fps))};
}
// Own delivery independently of the native feature. Downstream CPU cards use
// detached, bounded batches; the GPU encoder retains only its selected surfaces.
class InterpolationDelivery {
  public:
    InterpolationDelivery(const QString& video, const Settings& settings, MediaInfo pixels,
        const PipelineEstimate& estimate, void* device, bool cudaEncoding, int ordinal, TimeBase base,
        qint64 expected, JobControl& control, ProgressCallback progress, bool preview)
        : pixels_(std::move(pixels)), settings_(settings), video_(video), ordinal_(ordinal),
          cudaEncoding_(cudaEncoding), base_(base), expected_(expected), control_(control), progress_(std::move(progress)), preview_(preview) {
        elapsed_.start();
        auto after=estimate.stages.mid(1);
        output_=pixels_;
        output_.width=estimate.width; output_.height=estimate.height; output_.hdr=estimate.hdr;
        if(hasVfxStages(after))retainVfxPixelAspect(output_,pixels_);
        if (after.contains("rtx_video_hdr")) { output_.primaries="bt2020"; output_.transfer="smpte2084"; output_.sourceBitDepth=10; }
        if (cudaEncoding_ && !settings_.codec.contains("NVIDIA NVENC")) settings_.codec+=" (NVIDIA NVENC)";
        neuralDelivery_=cudaEncoding_ && !after.isEmpty() && after.back()=="neural_model";
        if (neuralDelivery_) after.removeLast();
        if(cudaEncoding_&&!preview_&&after==QStringList{"dlss_ray_reconstruction"}){
            rr_=std::make_unique<NativeEngines::RayReconstruction>(QSize(pixels_.width,pixels_.height),
                settings_.rr_optical_flow_quality,false,true,nativeMediaTransfer(pixels_.transfer));
            after.clear();output_.pixelFormat="rgba64le";
        }
        if (!after.isEmpty()) {
            output_.pixelFormat=after.back()=="dlss_super_resolution" || after.back()=="dlss_ray_reconstruction" || after.back()=="neural_model" ? "rgba64le" : "gbrp10le";
            post_=std::make_unique<FramePipeline>(settings, pixels_, after, !neuralDelivery_);
            capacity_=videoBatchCapacity({std::max(pixels_.width, output_.width), std::max(pixels_.height, output_.height)});
        }
        if (neuralDelivery_) {
            const bool rgb10 = !after.isEmpty() || pixels_.hdr;
            neural_=std::make_unique<NeuralVideoDelivery>(video_,settings_.codec,settings_,output_,ordinal_,rgb10,control_);
        }
        cudaEncoding_ = cudaEncoding_ && after.isEmpty() && !neuralDelivery_;
        filteredDelivery_=!after.isEmpty() && QStringList{"scale_method","coloring","cas_sharpening","grain"}.contains(after.back());
        // Keep the decoder device alive for the streaming encoder.
        if (cudaEncoding_) {
            auto& api=av::Api::get();
            auto* reference=api.av_buffer_ref(static_cast<AVBufferRef*>(device));
            if (!reference) throw std::bad_alloc{};
            device_=std::shared_ptr<void>(reference,[&api](void* pointer) {
                auto* owned=static_cast<AVBufferRef*>(pointer); api.av_buffer_unref(&owned);
            });
        }
    }
    void write(const TimedSurface& selected, qint64 index) {
        control_.checkpoint();
        ensureEncoder();
        if (gpu_) {
            const bool reset=!rrSegment_||*rrSegment_!=selected.segment;
            auto frame=rr_?rr_->process(selected.pixels.toRgb(),*pool_,gpu_->pixelFormat()==VideoPixelFormat::P010,
                reset,quint32(index),float(1000.*base_.numerator/base_.denominator)):
                selected.pixels.toVideo(*pool_, gpu_->pixelFormat()==VideoPixelFormat::P010, pixels_.hdr ? 2u : 1u, 0);
            rrSegment_=selected.segment;
            frame.pts=index; frame.duration=1; frame.timeBase=base_;
            gpu_->write(frame);
        } else {
            auto image=pixels_.hdr ? selected.pixels.toHostRgb10() : selected.pixels.toHost();
            if (post_) {
                images_.push_back(std::move(image)); indices_.push_back(index);
                if (images_.size()>=capacity_) flush();
            } else if (neural_) neural_->write(image,index,1,base_);
            else host_->write(image,index,1,base_);
        }
        if (progress_) progress_({std::min(.98,double(index+1)/std::max<qint64>(1,expected_)), "Interpolating video", index+1, expected_,
            elapsed_.elapsed()>0 ? (index+1)*1000./elapsed_.elapsed() : 0});
    }
    void finish() {
        ensureEncoder();
        flush();
        if (gpu_) gpu_->finish();
        else if (neural_) neural_->finish();
        else host_->finish();
        if(rr_)ve::info("dlssrr","FG -> RR FP16 RGB device counters: "+rr_->diagnostics());
    }
  private:
    void ensureEncoder() {
        if (gpu_ || host_ || neural_) return;
        if (cudaEncoding_) {
            const auto codec=settings_.codec.startsWith("H.264") ? QString("h264_nvenc")
                : settings_.codec.startsWith("H.265") ? QString("hevc_nvenc") : QString("av1_nvenc");
            gpu_=std::make_unique<CudaVideoEncoder>(video_, codec, settings_, output_, base_, device_.get(), ordinal_, control_);
            pool_=std::make_unique<CudaFramePool>(device_.get(), QSize(output_.width, output_.height),
                gpu_->pixelFormat()==VideoPixelFormat::P010, output_.hdr);
        } else host_=std::make_unique<HostVideoEncoder>(video_, settings_.codec, settings_, output_, ordinal_, control_,
            preview_ ? HostEncodePurpose::NativePreview : HostEncodePurpose::Delivery);
    }
    void flush() {
        if (images_.empty()) return;
        auto output=post_->processBatch(images_,control_,indices_.front(),indices_.front()==0);
        for (size_t i=0; i<output.size(); ++i) {
            if (neural_) neural_->write(output[i],indices_[i],1,base_);
            else host_->write(output[i],indices_[i],1,base_,filteredDelivery_);
        }
        images_.clear(); indices_.clear();
    }
    MediaInfo pixels_, output_;
    Settings settings_;
    QString video_;
    int ordinal_;
    bool cudaEncoding_;
    bool filteredDelivery_=false;
    bool neuralDelivery_=false;
    std::shared_ptr<void> device_;
    TimeBase base_;
    qint64 expected_;
    JobControl& control_;
    ProgressCallback progress_;
    bool preview_ = false;
    QElapsedTimer elapsed_;
    size_t capacity_=1;
    std::vector<QImage> images_;
    std::vector<qint64> indices_;
    std::unique_ptr<FramePipeline> post_;
    std::unique_ptr<NativeEngines::RayReconstruction> rr_;
    std::optional<int> rrSegment_;
    std::unique_ptr<CudaFramePool> pool_;
    std::unique_ptr<HostVideoEncoder> host_;
    std::unique_ptr<CudaVideoEncoder> gpu_;
    std::unique_ptr<NeuralVideoDelivery> neural_;
};
} // namespace
bool renderInterpolationVideo(const RenderRequest& request, const MediaInfo& input, const Settings& configured,
    const PipelineEstimate& estimate, const QString& destination, JobControl& control, const ProgressCallback& progress) {
    const bool rrBefore=estimate.stages==QStringList{"dlss_ray_reconstruction","frame_generation"};
    const bool rrAfter=estimate.stages==QStringList{"frame_generation","dlss_ray_reconstruction"};
    const bool rrCombined=rrBefore||rrAfter;
    if (estimate.stages.isEmpty() || (!rrBefore&&estimate.stages.front() != "frame_generation") || input.width%2 || input.height%2)
        return false;
    auto settings=configured;
    if (request.preview) { settings.codec="FFV1 Lossless RGB 10-bit"; settings.container="MKV"; }
    const bool cudaEncoding = settings.codec.contains("NVIDIA NVENC") ||
        (settings.ffmpeg_device != "cpu" && QStringList{"H.264", "H.265", "AV1"}.contains(settings.codec));
    if(rrCombined&&(request.preview||!cudaEncoding||
        (input.hdr?input.primaries!="bt2020"||input.colorSpace!="bt2020nc"||
            !QStringList{"smpte2084","arib-std-b67"}.contains(input.transfer):
            input.primaries!="bt709"||input.colorSpace!="bt709"||
            !QStringList{"bt709","gamma22","bt470m"}.contains(input.transfer))))return false;
    const int ordinal = gpuOrdinal(settings.ai_gpu_uuid);
    const auto devices = detectGpus();
    const auto selected = std::ranges::find_if(devices, [ordinal](const Gpu& gpu) { return gpu.index == ordinal; });
    if (selected == devices.end() || (settings.video_gpu_uuid != "auto" && settings.video_gpu_uuid != selected->uuid)) return false;
    if (input.hdr && !settings.hdr_mode) throw std::runtime_error("HDR input requires Frame Interpolation HDR Mode with a 10-bit codec.");
    const auto base = outputTimeBase(settings);
    const auto fps = targetFrameRate(settings.frame_interpolation_target_fps);
    const auto sourceRate = av::Api::get().av_d2q(input.fps, 1000000);
    const double sourceDuration = !input.variableFrameRate && input.frames > 0 ? input.frames/input.fps : input.duration;
    const double duration = request.seconds > 0 ? std::min(request.seconds, sourceDuration-request.startSeconds)
                                               : sourceDuration-request.startSeconds;
    const qint64 count = qint64(std::ceil(duration*fps-1e-9));
    const double ratio = fps/input.fps;
    const int multiple = int(std::lround(ratio));
    ScratchDirectory directory(QStringLiteral("interpolation-XXXXXX"));
    if (!directory.isValid()) throw std::runtime_error("Cannot create the interpolation directory.");
    auto info = input; info.fps = fps; info.pixelFormat = info.hdr ? "gbrp10le" : "rgba";
    if(rrBefore){info.width=estimate.width;info.height=estimate.height;}
    if(rrCombined)info.pixelFormat="rgba64le";
    info.colorSpace="gbr"; info.range="pc";
    const auto video = directory.filePath(settings.codec.startsWith("ProRes") ? "video.mov" : request.preview ? "video.mkv" : "video.nut");
    QElapsedTimer elapsed; elapsed.start();
    qint64 decoded = 0;
    double actualDuration = 0;
    double sourceTimeOrigin = 0;
    try {
        auto decoder = std::make_unique<CudaVideoDecoder>(request.input, ordinal, request.startSeconds, control);
        auto frame = decoder->read();
        if (!frame) throw std::runtime_error("The source contains no decodable video frames.");
        sourceTimeOrigin=decoder->timeOrigin();
        NativeEngines::instance().prepare(ordinal, rrCombined?estimate.stages:QStringList{"frame_generation"});
        std::unique_ptr<NativeEngines::RayReconstruction> rrSource;
        if(rrBefore)rrSource=std::make_unique<NativeEngines::RayReconstruction>(QSize(input.width,input.height),
            settings.rr_optical_flow_quality,false,true,nativeMediaTransfer(input.transfer));
        const auto maximum = NativeEngines::Interpolation::nativeMaximum();
        const bool native = !input.variableFrameRate && std::abs(ratio-multiple)<1e-7 && multiple>=2 && multiple<=maximum;
        if (fps>input.fps && settings.frame_interpolation_engine == "Native DLSSG" && !native)
            throw std::runtime_error("Target FPS is outside the native DLSSG grid. Choose Auto or Cascade.");
        const int levels = fps <= input.fps ? 0 : native && settings.frame_interpolation_engine != "Cascade" ? 1
            : std::abs(ratio-2)<1e-7 ? 1 : std::abs(ratio-4)<1e-7 ? 2
            : !input.variableFrameRate && std::abs(ratio-2.5)<1e-7 ? 4 : 3;
        const int generated = native && settings.frame_interpolation_engine != "Cascade" ? multiple-1 : 1;
        std::vector<InterpolationStage> stages;
        for (int level=0; level<levels; ++level) stages.push_back({std::make_unique<NativeEngines::Interpolation>(
            QSize(info.width, info.height), generated, info.hdr, settings.frame_interpolation_optical_flow_quality,
            rrCombined, level == levels-1 ? 64 : 0), {}, generated});
        // Source resampling still needs the native RGB conversion, but generates no frames.
        if (stages.empty()) stages.push_back({std::make_unique<NativeEngines::Interpolation>(
            QSize(info.width, info.height), 1, info.hdr, settings.frame_interpolation_optical_flow_quality, rrCombined, 64), {}, 0});
        auto deliveryEstimate=estimate;if(rrBefore)deliveryEstimate.stages={"frame_generation"};
        InterpolationDelivery delivery(video, settings, info, deliveryEstimate, decoder->device(), cudaEncoding, ordinal, base, count, control, progress, request.preview);
        auto writeSelected = [&](const TimedSurface& selected, qint64 index) {
            delivery.write(selected,index);
        };
        NearestFrameWriter<TimedSurface, decltype(writeSelected)> writer(request.seconds > 0 ? count : std::numeric_limits<qint64>::max(),
            FrameTime(base.numerator, base.denominator), writeSelected);
        const FrameTime nominal(sourceRate.den, sourceRate.num);
        std::optional<FrameTime> lastTime;
        int segment = 0;
        const auto rotation = input.clockwiseRotation();
        // Feed each output immediately into the next temporal session. Each
        // level retains its neighbour; no whole pass or clip is accumulated.
        auto feed = [&](auto&& self, size_t level, TimedSurface item) -> void {
            control.checkpoint();
            if (level == stages.size()) { writer.push(std::move(item)); return; }
            auto& stage = stages[level];
            auto result = stage.session->evaluate(item.pixels,
                stage.previous && item.segment != stage.previous->segment, false);
            auto produced = stage.push(std::move(result), item.time, item.segment);
            for (auto& output : produced) self(self, level+1, std::move(output));
        };
        while (frame) {
            control.checkpoint();
            const FrameTime time(frame->pts*frame->timeBase.numerator, frame->timeBase.denominator);
            if (request.seconds>0 && time.seconds()>=duration-1e-9) break;
            if (lastTime && (time<=*lastTime || time-*lastTime>nominal.fraction(2,1))) ++segment;
            frame->rotation = uint32_t((rotation%360+360)%360);
            const bool discontinuity=stages.front().previous&&segment!=stages.front().previous->segment;
            NativeEngines::Interpolation::Result result;
            if(rrSource){
                const auto interval=lastTime?(time-*lastTime).seconds():nominal.seconds();
                auto rgb=rrSource->processRgb(*frame,!decoded||discontinuity,quint32(decoded),float(std::clamp(interval*1000.,.01,1000.)));
                result=stages.front().session->evaluate(rgb,discontinuity,true);
            }else result=stages.front().session->evaluate(*frame,input,discontinuity,true);
            if (levels == 0) result.generated.clear();
            auto items = stages.front().push(std::move(result), time, segment);
            segment = stages.front().previous->segment;
            for (auto& item : items) feed(feed, 1, std::move(item));
            const auto end = time + (frame->duration > 0 ? FrameTime(frame->duration*frame->timeBase.numerator, frame->timeBase.denominator) : nominal);
            actualDuration = std::max(actualDuration, end.seconds());
            ++decoded; lastTime = time; frame = decoder->read();
        }
        if (request.seconds > 0) actualDuration = std::min(actualDuration, request.seconds);
        writer.finish(qint64(std::ceil(actualDuration*fps-1e-9)));
        delivery.finish();
        if(rrSource)ve::info("dlssrr","RR -> FG FP16 RGB device counters: "+rrSource->diagnostics());
        ve::info("video", "Interpolation streamed through temporal stages directly to the selected encoder.");
    } catch (const CudaUnavailable& error) {
        if (decoded) throw;
        control.checkpoint();
        ve::info("video", "Interpolation NVDEC unavailable; using host frames: " + QString::fromUtf8(error.what()));
        return false;
    }
    muxDelivery(video, destination, request, input, settings, actualDuration, control,
        request.preview ? std::nullopt : std::optional(sourceTimeOrigin));
    return true;
}
} // namespace ve
