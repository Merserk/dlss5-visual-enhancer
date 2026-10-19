#include "vfx_video.hpp"
#include "vfx_native.hpp"
#include "native_engines.hpp"
#include "frame_stream.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/log.hpp"
#include "media/av_api.hpp"
#include "media/frame_time.hpp"
#include "media/host_video.hpp"
#include "media/video_backend.hpp"
#include "media/video_delivery.hpp"
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonObject>
#include "infrastructure/scratch.hpp"
#include <algorithm>
#include <limits>
#include <utility>

namespace ve {
namespace {
TimeBase frameRateBase(double fps){auto rate=av::Api::get().av_d2q(fps,1000000);if(rate.num<=0||rate.den<=0)throw std::runtime_error("Invalid VFX output clock.");return {rate.den,rate.num};}
struct SourceFrame {
    std::optional<TimedImage> host;
    std::optional<CudaVideoFrame> device;
    MediaInfo colors;
    VfxSurface surface;
    qint64 surfacePts=0,surfaceDuration=0;
    TimeBase surfaceBase;
    [[nodiscard]] VfxFrameDescV2 descriptor()const{return surface?surface.descriptor():device?vfxDescriptor(*device,colors):vfxDescriptor(host->image,colors);}
    [[nodiscard]] qint64 pts()const{return surface?surfacePts:device?device->pts:host->pts;}
    [[nodiscard]] qint64 duration()const{return surface?surfaceDuration:device?device->duration:host->duration;}
    [[nodiscard]] TimeBase base()const{return surface?surfaceBase:device?device->timeBase:host->timeBase;}
};
using Source=std::function<std::optional<SourceFrame>()>;
struct TimedSurface {VfxSurface pixels;FrameTime time,duration;TimeBase base; qint64 pts=0,span=0;};
class VfxClock {
 public:
    VfxClock(Settings settings,MediaInfo info,Source source,JobControl& control)
        :settings_(std::move(settings)),info_(std::move(info)),source_(std::move(source)),control_(control),generation_(settings_.vfx_frame_generation_enabled){
        const auto rateBase=frameRateBase(vfxFrameRate(info_.fps>0?info_.fps:30.,settings_));
        outputBase_={1,60000000};interval_=FrameTime(rateBase.numerator,rateBase.denominator);
    }
    std::optional<TimedSurface> read(){
        control_.checkpoint();
        if(!previous_){previous_=pull();if(!previous_)return {};origin_=previous_->time;if(generation_)next_=pull();}
        if(!generation_){
            auto selected=std::exchange(previous_,std::nullopt);auto output=*selected;return output;
        }
        const auto target=origin_+interval_.fraction(index_,1);
        while(next_&&target>next_->time){previous_=std::move(next_);next_=pull();}
        if(!next_&&target>=previous_->time+previous_->duration)return {};
        VfxSurface selected=previous_->pixels;
        if(next_&&target>previous_->time){
            if(target==next_->time)selected=next_->pixels;
            else{const auto between=next_->time-previous_->time;const auto elapsed=target-previous_->time;selected=graph_->generate(previous_->pixels,next_->pixels,float(elapsed.seconds()/between.seconds()));}
        }
        auto span=interval_;
        if(!next_){const auto remaining=previous_->time+previous_->duration-target;if(remaining<span)span=remaining;}
        const auto relative=target-origin_;const auto pts=relative.ticks(outputBase_.numerator,outputBase_.denominator);
        const auto end=(relative+span).ticks(outputBase_.numerator,outputBase_.denominator);++index_;
        return TimedSurface{selected,target,span,outputBase_,pts,std::max<qint64>(1,end-pts)};
    }
    [[nodiscard]] MediaInfo outputInfo()const{return graph_?graph_->outputInfo():info_;}
    [[nodiscard]] QVariantMap performance()const{return graph_?graph_->performance():QVariantMap{};}
    [[nodiscard]] TimeBase outputBase()const{return outputBase_;}
 private:
    std::optional<TimedSurface> pull(){
        auto frame=source_();if(!frame)return {};
        control_.checkpoint();
        if(!graph_){auto colors=frame->colors;if(frame->host){colors.width=frame->host->image.width();colors.height=frame->host->image.height();}graph_=std::make_unique<VideoEffects>(settings_,colors,control_);}
        const auto base=frame->base();if(frame->pts()<0||base.numerator<=0||base.denominator<=0)throw std::runtime_error("Invalid VFX source timestamp.");
        FrameTime time(frame->pts()*base.numerator,base.denominator);
        const auto sourceBase=frameRateBase(info_.fps>0?info_.fps:30.);
        FrameTime duration=frame->duration()>0?FrameTime(frame->duration()*base.numerator,base.denominator):FrameTime(sourceBase.numerator,sourceBase.denominator);
        if(previous_&&time<=previous_->time)throw std::runtime_error("VFX source presentation timestamps must increase.");
        auto pixels=graph_->spatial(frame->descriptor(),!started_);started_=true;
        return TimedSurface{std::move(pixels),time,duration,base,frame->pts(),frame->duration()>0?frame->duration():std::max<qint64>(1,std::llround(duration.seconds()*base.denominator/base.numerator))};
    }
    Settings settings_;MediaInfo info_;Source source_;JobControl& control_;
    bool generation_,started_=false;
    std::unique_ptr<VideoEffects> graph_;
    std::optional<TimedSurface> previous_,next_;
    FrameTime origin_,interval_;
    TimeBase outputBase_;
    qint64 index_=0;
};
}
TimedFrameSource vfxFrameSource(Settings settings,MediaInfo info,TimedFrameSource source,JobControl& control){
    auto reader=[source=std::move(source),info]()mutable->std::optional<SourceFrame>{
        auto frame=source();if(!frame)return {};
        frame->image=frame->image.convertToFormat(frame->image.depth()>32?QImage::Format_RGBA64:QImage::Format_RGBA8888);
        return SourceFrame{std::move(frame),{},info};
    };
    auto clock=std::make_shared<VfxClock>(std::move(settings),info,std::move(reader),control);
    return [clock,info,&control,reported=false]()mutable->std::optional<TimedImage>{
        control.checkpoint();auto frame=clock->read();if(!frame){if(!reported){ve::info("vfx",QString::fromUtf8(QJsonDocument(QJsonObject::fromVariantMap(clock->performance())).toJson(QJsonDocument::Compact)));reported=true;}return {};}
        auto output=clock->outputInfo();
        return TimedImage{frame->pixels.toHost(output.hdr||output.precisionBits()>8),frame->pts,frame->span,frame->base};
    };
}
bool renderVfxVideo(const RenderRequest& request,const MediaInfo& input,const Settings& configured,
    const PipelineEstimate& estimate,const QString& destination,JobControl& control,const ProgressCallback& progress){
    if(!hasVfxStages(estimate.stages)||estimate.stages.contains("frame_generation"))return false;
    auto settings=vfxSettingsForStages(configured,estimate.stages);const auto gpu=gpuOrdinal(settings.ai_gpu_uuid);
    if(estimate.stages.contains("rtx_video_hdr"))NativeEngines::instance().prepare(gpu,{"rtx_video_hdr"});
    VideoEffects::initialize(gpu);
    if(estimate.stages.contains("super_resolution"))info("vfx","RTX Video VSR cards share NVIDIA VFX SDK 1.3 in this combined workflow to avoid incompatible NGX versions.");
    const auto duration=request.seconds>0?std::min(request.seconds,std::max(0.,input.duration-request.startSeconds)):std::max(0.,input.duration-request.startSeconds);
    auto outputInfo=input;outputInfo.width=estimate.width;outputInfo.height=estimate.height;outputInfo.fps=estimate.fps;outputInfo.hdr=estimate.hdr;
    retainVfxPixelAspect(outputInfo,input);
    outputInfo.metadata.insert("output_chroma_location","left");
    if(settings.vfx_frame_generation_enabled)outputInfo.variableFrameRate=false;
    if(estimate.stages.contains("rtx_video_hdr")){
        outputInfo.primaries="bt2020";outputInfo.transfer="smpte2084";outputInfo.sourceBitDepth=10;
    }
    outputInfo.colorSpace=outputInfo.hdr?"bt2020nc":"bt709";outputInfo.range="tv";
    if(request.preview){settings.codec="FFV1 Lossless RGB 10-bit";settings.container="MKV";}
    ScratchDirectory directory(QStringLiteral("vfx-XXXXXX"));if(!directory.isValid())throw std::runtime_error("Cannot create VFX delivery directory.");
    const auto video=directory.filePath(settings.codec.startsWith("ProRes")?"video.mov":"video.mkv");
    const bool sole=std::all_of(estimate.stages.begin(),estimate.stages.end(),[](const QString& stage){return isVfxStage(stage);});
    const bool sameGpu=settings.video_gpu_uuid=="auto"||gpuOrdinal(settings.video_gpu_uuid)==gpu;
    const auto encodeGpu=settings.video_gpu_uuid=="auto"?gpu:gpuOrdinal(settings.video_gpu_uuid);
    const bool cudaCodec=settings.codec.contains("NVIDIA NVENC")||(settings.ffmpeg_device!="cpu"&&QStringList{"H.264","H.265","AV1"}.contains(settings.codec));
    std::unique_ptr<CudaVideoDecoder> decoder;std::optional<CudaVideoFrame> first;
    if(sole&&settings.ffmpeg_device!="cpu")try{decoder=std::make_unique<CudaVideoDecoder>(request.input,gpu,request.startSeconds,control);first=decoder->read();}catch(const CudaUnavailable& error){info("vfx",QString("Using host decoder: ")+QString::fromUtf8(error.what()));decoder.reset();}
    QElapsedTimer elapsed;elapsed.start();qint64 written=0;double actualDuration=0;std::optional<double> sourceOrigin;
    if(decoder&&first){
        sourceOrigin=decoder->timeOrigin();
        Source source=[&]()mutable->std::optional<SourceFrame>{auto frame=first?std::exchange(first,std::nullopt):decoder->read();if(!frame)return {};frame->rotation=input.clockwiseRotation();if(duration>0&&double(frame->pts)*frame->timeBase.numerator/frame->timeBase.denominator>=duration-1e-9)return {};return SourceFrame{{},std::move(frame),input};};
        std::vector<std::shared_ptr<VfxClock>> clocks;
        auto stageColors=input;
        for(qsizetype index=0;index<estimate.stages.size();){
            const auto group=vfxStageGroup(estimate.stages,index,configured);index+=group.size();
            const auto selected=vfxSettingsForStages(configured,group);
            auto clock=std::make_shared<VfxClock>(selected,stageColors,std::move(source),control);clocks.push_back(clock);
            source=[clock]()->std::optional<SourceFrame>{auto frame=clock->read();if(!frame)return {};return SourceFrame{{},{},clock->outputInfo(),std::move(frame->pixels),frame->pts,frame->span,frame->base};};
            const auto size=vfxSpatialSize({stageColors.width,stageColors.height},selected);stageColors.width=size.width();stageColors.height=size.height();stageColors.fps=vfxFrameRate(stageColors.fps,selected);
        }
        const auto clock=clocks.back();
        const auto base=settings.vfx_frame_generation_enabled?clock->outputBase():decoder->timeBase();std::unique_ptr<CudaVideoEncoder> gpuEncoder;std::unique_ptr<CudaFramePool> pool;std::unique_ptr<HostVideoEncoder> hostEncoder;
        if(cudaCodec&&sameGpu&&!request.preview){
            if(!settings.codec.contains("NVIDIA NVENC"))settings.codec+=" (NVIDIA NVENC)";
            const auto codec=settings.codec.startsWith("H.264")?QString("h264_nvenc"):settings.codec.startsWith("H.265")?QString("hevc_nvenc"):QString("av1_nvenc");
            gpuEncoder=std::make_unique<CudaVideoEncoder>(video,codec,settings,outputInfo,base,decoder->device(),gpu,control);
            pool=std::make_unique<CudaFramePool>(decoder->device(),QSize(estimate.width,estimate.height),gpuEncoder->pixelFormat()==VideoPixelFormat::P010,estimate.hdr);
        }else hostEncoder=std::make_unique<HostVideoEncoder>(video,settings.codec,settings,outputInfo,encodeGpu,control,request.preview?HostEncodePurpose::NativePreview:HostEncodePurpose::Delivery);
        while(auto frame=clock->read()){
            control.checkpoint();if(duration>0&&frame->time.seconds()>=duration-1e-9)break;
            if(gpuEncoder){auto pixels=frame->pixels.toVideo(*pool,gpuEncoder->pixelFormat()==VideoPixelFormat::P010,outputInfo);pixels.pts=frame->pts;pixels.duration=frame->span;pixels.timeBase=frame->base;gpuEncoder->write(pixels);}
            else hostEncoder->write(frame->pixels.toHost(outputInfo.hdr||outputInfo.precisionBits()>8),frame->pts,frame->span,frame->base);
            ++written;actualDuration=std::max(actualDuration,(frame->time+frame->duration).seconds());
            if(progress)progress({duration>0?std::min(.98,actualDuration/duration):0.,QT_TRANSLATE_NOOP("App", "Processing NVIDIA VFX"),written,qint64(std::ceil(duration*estimate.fps)),elapsed.elapsed()>0?written*1000./elapsed.elapsed():0.});
        }
        if(gpuEncoder)gpuEncoder->finish();else hostEncoder->finish();
        info("vfx",QString("Path: CUDA decode -> VFX -> ")+(gpuEncoder?"NVENC":"host encoder"));
        QVariantMap metrics;for(const auto& stage:clocks){const auto values=stage->performance();for(auto it=values.begin();it!=values.end();++it)if(it.key()!="source_frames")metrics[it.key()]=metrics.value(it.key()).toDouble()+it.value().toDouble();}
        metrics["source_frames"]=clocks.front()->performance().value("source_frames");
        info("vfx",QString::fromUtf8(QJsonDocument(QJsonObject::fromVariantMap(metrics)).toJson(QJsonDocument::Compact)));
    }else{
        HostVideoDecoder host(request.input,input,gpu,request.startSeconds,settings.ffmpeg_device!="cpu",control);
        TimedFrameSource source=[&]()->std::optional<TimedImage>{auto frame=host.read();if(frame&&!sourceOrigin)sourceOrigin=host.timeOrigin();if(frame&&duration>0&&frame->seconds()>=duration-1e-9)return {};return frame;};
        FrameStreamPipeline pipeline(settings,input,estimate.stages,std::move(source),control);
        HostVideoEncoder encoder(video,settings.codec,settings,outputInfo,encodeGpu,control,request.preview?HostEncodePurpose::NativePreview:HostEncodePurpose::Delivery);
        while(auto frame=pipeline.read()){
            control.checkpoint();if(duration>0&&frame->seconds()>=duration-1e-9)break;
            encoder.write(frame->image,frame->pts,frame->duration,frame->timeBase);++written;actualDuration=std::max(actualDuration,frame->seconds()+frame->duration*double(frame->timeBase.numerator)/frame->timeBase.denominator);
            if(progress)progress({duration>0?std::min(.98,actualDuration/duration):0.,QT_TRANSLATE_NOOP("App", "Processing NVIDIA VFX"),written,qint64(std::ceil(duration*estimate.fps)),elapsed.elapsed()>0?written*1000./elapsed.elapsed():0.});
        }
        encoder.finish();info("vfx","Path: host boundaries with GPU VFX processing");
    }
    if(!written)throw std::runtime_error("No video frames were processed by VFX.");
    info("vfx",vfxReport(settings));info("vfx",VideoEffects::diagnostics());
    muxDelivery(video,destination,request,input,settings,duration>0?std::min(duration,actualDuration):actualDuration,control,sourceOrigin);return true;
}
}
