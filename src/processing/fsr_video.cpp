#include "fsr_video.hpp"
#include "fsr_temporal.hpp"
#include "fsr_worker.hpp"
#include "media/temporal_cadence.hpp"
#include "native_engines.hpp"
#include "reshade_native.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/scratch.hpp"
#include "media/av_api.hpp"
#include "media/d3d_video.hpp"
#include "media/d3d_nvenc.hpp"
#include "media/d3d_amf.hpp"
#include "media/host_video.hpp"
#include "media/video_delivery.hpp"
#include <QElapsedTimer>
#include <QJsonDocument>
#include <QJsonArray>
#include <cmath>
#include <limits>
#include <mutex>

namespace ve {
namespace {
std::mutex diagnosticMutex;QString lastDiagnostic;
void prepareCards(const Settings& settings,const QStringList& stages){
    if(stages.contains("neural_model")||stages.contains("dlss_super_resolution")||stages.contains("dlss_ray_reconstruction")||stages.contains("super_resolution")||stages.contains("rtx_video_hdr"))
        NativeEngines::instance().prepare(gpuOrdinal(settings.ai_gpu_uuid),stages);
    if(stages.contains("reshade"))ReShadeEngine::initialize(gpuOrdinal(settings.ai_gpu_uuid));
}
class Input {
  public:
    Input(const RenderRequest& request,const MediaInfo& colors,const std::shared_ptr<FsrDevice>& device,bool host,JobControl& control):colors_(colors){
        const auto make11=[&]{eleven_=std::make_unique<D3d11VideoDecoder>(request.input,device->nativeDevice(),device->nativeQueue(),request.startSeconds,control);gpu_=eleven_->read();};
        if(!host){
            try{if(device->capabilities().vendor_id==0x1002)make11();else{twelve_=std::make_unique<D3dVideoDecoder>(request.input,device->nativeDevice(),request.startSeconds,control);gpu_=twelve_->read();}}
            catch(const Cancelled&){throw;}
            catch(const std::exception& first){
                const auto reason=QString::fromUtf8(first.what());twelve_.reset();eleven_.reset();
                try{make11();}
                catch(const Cancelled&){throw;}
                catch(const std::exception& fallback){eleven_.reset();info("fsr",reason+"; shared DirectX decode unavailable: "+QString::fromUtf8(fallback.what()));}
            }
        }
        if(!twelve_&&!eleven_){
            host_=std::make_unique<HostVideoDecoder>(request.input,colors,0,request.startSeconds,false,control,HostDecodePrecision::Unorm16);
            image_=host_->read();mode_=host?"Host boundary for preceding cards":"Software decode compatibility boundary";
        }else mode_=eleven_?"D3D11 decode, shared GPU textures to D3D12":"D3D12 hardware decode";
        if(!available())throw std::runtime_error("FSR source contains no decodable video frames");
        origin_=twelve_?twelve_->timeOrigin():eleven_?eleven_->timeOrigin():host_->timeOrigin();
    }
    bool available()const{return gpu_.has_value()||image_.has_value();}
    void next(){if(twelve_)gpu_=twelve_->read();else if(eleven_)gpu_=eleven_->read();else image_=host_->read();}
    qint64 pts()const{return gpu_?gpu_->pts:image_->pts;}
    qint64 duration()const{return gpu_?gpu_->duration:image_->duration;}
    TimeBase base()const{return gpu_?gpu_->timeBase:image_->timeBase;}
    double origin()const{return origin_;}
    QString mode()const{return mode_;}
    QImage hostImage()const{if(!image_)throw std::logic_error("FSR input has no host image");return image_->image;}
    bool hardware()const{return gpu_.has_value();}
    VeFsrFrame descriptor()const{
        if(!gpu_)throw std::logic_error("FSR input has no DirectX surface");const auto& input=*gpu_;
        VeFsrFrame frame{};frame.size=sizeof(frame);frame.abi=VE_FSR_ABI_VERSION;frame.memory_type=VE_FSR_MEMORY_D3D12;frame.pixel_format=input.pixelFormat;
        frame.width=uint32_t(input.size.width());frame.height=uint32_t(input.size.height());frame.texture=input.texture;frame.ready_fence=input.readyFence;frame.ready_value=input.readyValue;
        frame.transfer=fsrTransfer(colors_.transfer);frame.primaries=fsrPrimaries(colors_.primaries);frame.rotation=uint32_t(colors_.clockwiseRotation());
        frame.color_matrix=input.colorMatrix;frame.color_range=input.colorRange;frame.chroma_location=input.chromaLocation;
        frame.pts=input.pts;frame.duration=input.duration;frame.timebase_numerator=input.timeBase.numerator;frame.timebase_denominator=input.timeBase.denominator;return frame;
    }
  private:
    MediaInfo colors_;QString mode_;double origin_=0;
    std::unique_ptr<D3dVideoDecoder> twelve_;std::unique_ptr<D3d11VideoDecoder> eleven_;std::unique_ptr<HostVideoDecoder> host_;
    std::optional<D3dVideoFrame> gpu_;std::optional<TimedImage> image_;
};
}
QString fsrVideoDiagnostics(){std::lock_guard lock(diagnosticMutex);return lastDiagnostic;}
bool renderFsrVideo(const RenderRequest& request,const MediaInfo& input,const Settings& configured,const PipelineEstimate& estimate,
    const QString& destination,JobControl& control,const ProgressCallback& progress){
    const auto position=estimate.stages.indexOf("fsr_frame_generation");if(position<0)return false;
    QElapsedTimer startup;startup.start();
    if(!input.video||input.fps<=0)throw std::runtime_error("FSR Frame Generation requires video with a valid frame rate");
    auto settings=configured;const auto before=estimate.stages.mid(0,position),after=estimate.stages.mid(position+1);
    if(request.preview){settings.codec="H.265";settings.quality="Max";settings.container="MKV";}
    auto colors=fsrVideoColors(input);if(colors.hdr&&!settings.hdr_mode)throw std::runtime_error(QT_TRANSLATE_NOOP("App", "HDR FSR input requires HDR Mode and a 10-bit output codec"));
    auto device=std::make_shared<FsrDevice>(settings.fsr_adapter_id);
    const auto deviceReadyMs=startup.elapsed();
    auto beforeSettings=settings;beforeSettings.video_enabled_stages=before;
    const auto preceding=estimatePipeline(colors,beforeSettings,Workflow::NeuralRendering);
    auto pixels=colors;pixels.width=preceding.width;pixels.height=preceding.height;pixels.hdr=preceding.hdr;
    if(before.contains("rtx_video_hdr")){pixels.primaries="bt2020";pixels.transfer="smpte2084";pixels.sourceBitDepth=16;}
    pixels.colorSpace="gbr";pixels.range="pc";pixels.sourceBitDepth=16;pixels.pixelFormat="rgba64le";
    const auto target=frameGenerationFrameRate(preceding.fps,settings,true);const auto rate=av::Api::get().av_d2q(input.fps,1000000);
    const TimeBase sourceInterval{rate.den,rate.num},targetInterval=frameGenerationTimeBase(preceding.fps,settings,true);
    const FrameTime step(targetInterval.numerator,targetInterval.denominator),nominal(sourceInterval.numerator,sourceInterval.denominator);
    Input decoder(request,colors,device,!before.isEmpty(),control);
    const auto decoderReadyMs=startup.elapsed();
    const auto sourceBase=decoder.base();
    const auto sourceOrigin=qint64(std::llround(decoder.origin()*sourceBase.denominator/sourceBase.numerator));
    const auto sourceStart=input.metadata.contains("start_pts")?input.metadata.value("start_pts").toVariant().toLongLong():qint64(std::llround(input.presentationStartSeconds()*sourceBase.denominator/sourceBase.numerator));
    const auto relativeOrigin=sourceOrigin-sourceStart;
    const auto alignment=alignTemporalClock(relativeOrigin,sourceBase,targetInterval);const auto firstOutputIndex=alignment.firstIndex;
    const auto alignedTime=alignment.relativeStart,phase=alignment.phase;
    const double outputOrigin=input.presentationStartSeconds()+alignedTime.seconds();
    prepareCards(settings,before+after);
    std::unique_ptr<FramePipeline> pre,post;
    if(!before.isEmpty())pre=std::make_unique<FramePipeline>(settings,colors,before,false);
    auto temporalColors=pixels;temporalColors.fps=target;
    if(!after.isEmpty())post=std::make_unique<FramePipeline>(settings,temporalColors,after,true);
    auto output=temporalColors;output.width=estimate.width;output.height=estimate.height;output.hdr=estimate.hdr;output.sourceBitDepth=16;output.pixelFormat="rgba64le";
    if(after.contains("rtx_video_hdr")){output.primaries="bt2020";output.transfer="smpte2084";}
    ScratchDirectory directory(QStringLiteral("fsr-video-XXXXXX"));if(!directory.isValid())throw std::runtime_error("Cannot create FSR processing directory");
    const auto video=directory.filePath("video.mp4");
    std::unique_ptr<D3dNvEncoder> nvidia;std::unique_ptr<D3dSharedEncoder> amd;
    QString encoderMode;const bool compressed=settings.codec.startsWith("H.264")||settings.codec.startsWith("H.265")||settings.codec.startsWith("AV1");
    if(!compressed)throw std::runtime_error(QT_TRANSLATE_NOOP("App", "FSR Frame Generation requires GPU encoding with H.264, HEVC or AV1. Select a supported codec."));
    // Independently encoded preview frames permit accurate trimming and parked
    // comparisons without a second encode or a preceding GOP's reference state.
    if(device->capabilities().vendor_id==0x10de)nvidia=std::make_unique<D3dNvEncoder>(video,settings.codec,settings,output,device->nativeDevice(),device->nativeQueue(),control,request.preview);
    else if(device->capabilities().vendor_id==0x1002||device->capabilities().vendor_id==0x8086)amd=std::make_unique<D3dSharedEncoder>(video,settings.codec,settings,output,device->nativeDevice(),control,request.preview);
    else throw D3dVideoUnavailable("GPU encoding is unavailable for the selected FSR DirectX adapter");
    const auto encoderReadyMs=startup.elapsed();
    const auto maximumNits=fsrMaximumLuminance(pixels);
    FsrTemporalCascade cascade(device,{pixels.width,pixels.height},pixels.hdr,sourceInterval,targetInterval,input.variableFrameRate,maximumNits,phase);
    const auto temporalReadyMs=startup.elapsed();qint64 firstOutputMs=-1;
    QElapsedTimer elapsed;elapsed.start();qint64 decoded=0;FrameTime actualDuration;quint64 readback=0,postUpload=0;std::optional<int> outputSegment;
    const double decodedOriginSeconds=double(relativeOrigin)*sourceBase.numerator/sourceBase.denominator;
    const auto requestedEnd=request.seconds>0?std::min(input.duration,request.startSeconds+request.seconds):input.duration;
    const auto requestedDuration=std::max(0.,requestedEnd-decodedOriginSeconds);
    const auto expected=std::max(qint64(0),qint64(std::ceil((requestedDuration-phase.seconds())*target-1e-9)));
    if(request.seconds>0&&expected==0)throw std::runtime_error("The requested interval contains no output frames at the selected frame rate");
    const auto write=[&](const FsrTemporalCascade::Sample& selected,qint64 index){
        control.checkpoint();const auto pts=step.fraction(index,1).ticks(1,60000000),end=step.fraction(index+1,1).ticks(1,60000000);
        const bool reset=!outputSegment||*outputSegment!=selected.segment;outputSegment=selected.segment;
        auto surface=selected.pixels;
        if(post){
            auto image=surface.toHost(pixels);readback+=quint64(image.sizeInBytes());
            image=post->process(image,control,index+firstOutputIndex,reset,float(step.seconds()*1000),step.fraction(index+firstOutputIndex,1).seconds());
            surface=device->uploadImage(image,output);postUpload+=quint64(image.width())*image.height()*8;
        }
        if(nvidia)nvidia->write(surface,pts,end-pts,{1,60000000},reset);
        else amd->write(surface,pts,end-pts,{1,60000000},reset);
        if(firstOutputMs<0)firstOutputMs=startup.elapsed();
        if(progress)progress({std::min(.98,double(index+1)/std::max(qint64(1),expected)),QT_TRANSLATE_NOOP("App","FSR Frame Generation"),index+1,expected,elapsed.elapsed()>0?(index+1)*1000./elapsed.elapsed():0});
    };
    NearestFrameWriter<FsrTemporalCascade::Sample,decltype(write)> writer(request.seconds>0?expected:std::numeric_limits<qint64>::max(),step,write,phase);
    const auto consume=[&](FsrTemporalCascade::Sample sample){control.checkpoint();writer.push(std::move(sample));};
    while(decoder.available()){
        control.checkpoint();const auto time=FrameTime(decoder.pts()).fraction(decoder.base().numerator,decoder.base().denominator);
        const bool trailingAnchor=request.seconds>0&&time.seconds()>=requestedDuration-1e-9;
        if(decoder.hardware())cascade.push(decoder.descriptor(),consume,decoded==0);
        else{auto image=decoder.hostImage();if(pre){const auto globalTime=std::max(0.,decodedOriginSeconds+time.seconds());image=pre->process(image,control,qint64(std::llround(globalTime*input.fps)),decoded==0,float(nominal.seconds()*1000),globalTime);}cascade.push(image,pixels,decoder.pts(),decoder.duration(),decoder.base(),consume,decoded==0);}
        const auto duration=decoder.duration()>0?FrameTime(decoder.duration()).fraction(decoder.base().numerator,decoder.base().denominator):nominal;
        actualDuration=std::max(actualDuration,time+duration);++decoded;if(trailingAnchor)break;decoder.next();
    }
    if(!decoded)throw std::runtime_error("No FSR input frames within the requested interval");
    const double duration=std::max(0.,(request.seconds>0?std::min(actualDuration.seconds(),requestedDuration):actualDuration.seconds())-phase.seconds());writer.finish(qint64(std::ceil(duration*target-1e-9)));
    if(nvidia){nvidia->finish();encoderMode=nvidia->diagnostics();}else{amd->finish();encoderMode=amd->diagnostics();}
    const auto stats=cascade.statistics();QJsonObject report{{"provider",QString::fromUtf8(device->capabilities().provider_name)},{"adapter",QString::fromUtf8(device->capabilities().adapter_name)},
        {"decoder",decoder.mode()},{"encoder",encoderMode},{"source_time_origin",outputOrigin},{"decoded_time_origin",decoder.origin()},{"output_phase_seconds",phase.seconds()},{"source_frames",decoded},{"output_frames",writer.written()},{"levels",stats.levels},
        {"generated",qint64(stats.generated)},{"cuts",qint64(stats.sceneCuts)},{"duplicates",qint64(stats.duplicates)},{"cadence_promotions",stats.cadencePromotions},
        {"startup_milliseconds",QJsonObject{{"device_ready",deviceReadyMs},{"decoder_ready",decoderReadyMs},
            {"encoder_ready",encoderReadyMs},{"temporal_ready",temporalReadyMs},{"first_output",firstOutputMs}}},
        {"image_upload_bytes",qint64(stats.uploadBytes+postUpload)},{"image_readback_bytes",qint64(stats.downloadBytes+readback)},
        {"maximum_luminance_nits",maximumNits},{"transfer",output.transfer},{"primaries",output.primaries},{"output_sample_aspect_ratio",output.metadata.value("output_sample_aspect_ratio")},
        {"color_assumptions",colors.metadata.value("ve_fsr_color_assumptions")}};
    const bool strictModules=std::none_of(estimate.stages.begin(),estimate.stages.end(),[](const QString& stage){return isVfxStage(stage)||QStringList{"neural_model","dlss_super_resolution","dlss_ray_reconstruction","super_resolution","rtx_video_hdr","reshade"}.contains(stage);});
    report.insert("modules",fsrAuditLoadedModules(strictModules));
    const auto text=QString::fromUtf8(QJsonDocument(report).toJson(QJsonDocument::Compact));{std::lock_guard lock(diagnosticMutex);lastDiagnostic=text;}info("fsr",text);
    muxDelivery(video,destination,request,colors,settings,duration,control,request.preview?std::nullopt:std::optional(outputOrigin));return true;
}
}
