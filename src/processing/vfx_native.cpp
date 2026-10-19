#include "vfx_native.hpp"
#include "media/resolution.hpp"
#include "native_engines.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "media/media.hpp"
#include <QColor>
#include <QDir>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <mutex>
#include <numbers>
#include <stdexcept>
#include <vector>
#include <windows.h>

namespace ve {
namespace {
struct Api {
    HMODULE library=nullptr;
    std::recursive_mutex mutex;
#define FUNCTION(name) decltype(&name) name##Fn=nullptr
    FUNCTION(vfx_init); FUNCTION(vfx_status); FUNCTION(vfx_session_create); FUNCTION(vfx_session_release);
    FUNCTION(vfx_surface_create); FUNCTION(vfx_surface_frame_desc); FUNCTION(vfx_surface_release);
    FUNCTION(vfx_convert_frame_v2); FUNCTION(vfx_process_frame_v2);
#undef FUNCTION
    Api(){
        auto path=QDir::toNativeSeparators(Paths::instance().at("bin/runtime/vfx/neuroframe_engine_video_effects.dll")).toStdWString();
        library=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_APPLICATION_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
        if(!library)throw std::runtime_error("Cannot load NVIDIA VFX engine: Windows error "+std::to_string(GetLastError()));
#define LOAD(name) name##Fn=reinterpret_cast<decltype(name##Fn)>(GetProcAddress(library,#name));if(!name##Fn)throw std::runtime_error("Missing VFX engine export: " #name)
        LOAD(vfx_init);LOAD(vfx_status);LOAD(vfx_session_create);LOAD(vfx_session_release);
        LOAD(vfx_surface_create);LOAD(vfx_surface_frame_desc);LOAD(vfx_surface_release);
        LOAD(vfx_convert_frame_v2);LOAD(vfx_process_frame_v2);
#undef LOAD
        auto version=reinterpret_cast<decltype(&vfx_abi_version)>(GetProcAddress(library,"vfx_abi_version"));
        if(!version||version()!=VFX_ABI_VERSION)throw std::runtime_error("Incompatible NVIDIA VFX engine ABI.");
    }
    static Api& get(){static Api instance;return instance;}
};
template<class T> T descriptor(){T out{};out.struct_size=sizeof(out);out.abi_version=VFX_ABI_VERSION;return out;}
using Error=std::array<char,4096>;
void check(int okay,const Error& error){if(!okay)throw std::runtime_error(error[0]?error.data():"NVIDIA VFX call failed.");}
void colorDescription(VfxFrameDescV2& f,const MediaInfo& colors){
    f.color_matrix=colors.colorSpace=="bt2020nc"||colors.colorSpace=="bt2020_ncl"?2u:colors.colorSpace=="smpte170m"||colors.colorSpace=="bt470bg"?0u:1u;
    f.color_range=colors.range=="pc"?1u:0u;
    f.color_primaries=colors.primaries=="bt2020"?2u:colors.primaries=="smpte170m"||colors.primaries=="smpte240m"?3u:colors.primaries=="bt470bg"?0u:1u;
    f.color_transfer=colors.transfer=="smpte2084"?5u:colors.transfer=="arib-std-b67"?6u:colors.transfer=="linear"?4u:colors.transfer=="gamma28"||colors.transfer=="bt470bg"?3u:colors.transfer=="gamma22"||colors.transfer=="bt470m"?2u:colors.transfer=="iec61966-2-1"||colors.transfer=="srgb"?1u:0u;
}
void convert(const VfxFrameDescV2& a,const VfxFrameDescV2& b,VfxFrameResultV2* result=nullptr){
    Error error{};check(Api::get().vfx_convert_frame_v2Fn(&a,&b,result,error.data(),int(error.size())),error);
}
struct Effect {
    void* handle=nullptr;
    VfxSessionDescV2 desc{};
    explicit Effect(VfxSessionDescV2 d):desc(d){Error error{};handle=Api::get().vfx_session_createFn(&d,error.data(),int(error.size()));check(handle!=nullptr,error);}
    ~Effect(){if(handle)Api::get().vfx_session_releaseFn(handle);}
};
VfxSessionDescV2 effectDescriptor(unsigned effect,QSize input,QSize output,unsigned format,const Settings& s){
    auto d=descriptor<VfxSessionDescV2>();d.effect=effect;d.input_width=input.width();d.input_height=input.height();d.output_width=output.width();d.output_height=output.height();d.pixel_format=format;
    d.strength=float(effect==VFX_UPSCALE?s.vfx_upscale_strength:s.vfx_vsr_strength);
    d.mode=effect==VFX_FRAME_GENERATION?unsigned(s.vfx_frame_generation_model):unsigned(s.vfx_vsr_mode);
    d.flags=s.vfx_automatic_shot_detection?VFX_AUTO_SHOT_CHANGE:0u;
    return d;
}
bool restores(const Settings& s){return s.vfx_vsr_mode>=8&&s.vfx_vsr_mode<=15;}
}

struct VfxSurface::Metrics {
    quint64 uploads=0,downloads=0,frames=0;
    double effectMs=0,conversionMs=0;
    void account(const VfxFrameResultV2& r){uploads+=r.upload_bytes;downloads+=r.download_bytes;effectMs+=r.effect_milliseconds;conversionMs+=r.total_milliseconds-r.effect_milliseconds;}
};
struct VfxSurface::Storage {
    void* handle=nullptr;
    VfxFrameDescV2 desc{};
    std::shared_ptr<Metrics> metrics;
    ~Storage(){if(handle)Api::get().vfx_surface_releaseFn(handle);}
};
VfxFrameDescV2 VfxSurface::descriptor()const{if(!storage_)throw std::runtime_error("Empty VFX GPU surface.");return storage_->desc;}
QImage VfxSurface::toHost(bool wide)const{
    const auto src=descriptor();QImage pixels(QSize(int(src.width),int(src.height)),wide?QImage::Format_RGBA64:QImage::Format_RGBA8888);
    if(pixels.isNull())throw std::bad_alloc{};
    auto dst=src;dst.memory_type=VFX_HOST;dst.pixel_format=wide?VFX_RGBA16:VFX_RGBA8;dst.planes[0]=reinterpret_cast<uint64_t>(pixels.bits());dst.strides[0]=pixels.bytesPerLine();dst.reserved[1]=0;
    auto result=ve::descriptor<VfxFrameResultV2>();convert(src,dst,&result);storage_->metrics->account(result);return pixels;
}
CudaVideoFrame VfxSurface::toVideo(CudaFramePool& pool,bool p010,const MediaInfo& colors)const{
    auto output=pool.allocate();auto dst=vfxDescriptor(output,colors);dst.pixel_format=p010?VFX_P010:VFX_NV12;dst.reserved[1]=0;
    auto result=ve::descriptor<VfxFrameResultV2>();convert(descriptor(),dst,&result);storage_->metrics->account(result);output.chromaLocation=1;return output;
}
VfxFrameDescV2 vfxDescriptor(const CudaVideoFrame& input,const MediaInfo& colors){
    auto f=descriptor<VfxFrameDescV2>();f.memory_type=input.memory==FrameMemory::Cuda?VFX_CUDA:VFX_HOST;f.pixel_format=input.format==VideoPixelFormat::P010?VFX_P010:VFX_NV12;f.width=input.size.width();f.height=input.size.height();
    std::copy(input.planes.begin(),input.planes.end(),f.planes);std::copy(input.strides.begin(),input.strides.end(),f.strides);colorDescription(f,colors);f.color_matrix=input.colorMatrix;f.color_range=input.colorRange;f.rotation=input.rotation;f.reserved[0]=input.chromaLocation?input.chromaLocation-1:0;
    return f;
}
VfxFrameDescV2 vfxDescriptor(const QImage& input,const MediaInfo& colors){
    if(input.format()!=QImage::Format_RGBA8888&&input.format()!=QImage::Format_RGBA64)throw std::runtime_error("VFX host input must own packed RGBA8/RGBA16 pixels.");
    auto f=descriptor<VfxFrameDescV2>();f.memory_type=VFX_HOST;f.pixel_format=input.format()==QImage::Format_RGBA64?VFX_RGBA16:VFX_RGBA8;f.width=input.width();f.height=input.height();f.planes[0]=reinterpret_cast<uint64_t>(input.constBits());f.strides[0]=input.bytesPerLine();colorDescription(f,colors);f.color_range=1;return f;
}
QSize vfxSpatialSize(QSize input,const Settings& s){
    requireResolution(input, "VFX input");
    double w=input.width(),h=input.height();
    if(s.vfx_vsr_enabled&&!restores(s)){
        if(s.vfx_size_mode=="Scale factor"){w*=s.vfx_scale_factor;h*=s.vfx_scale_factor;}
        else{w=s.vfx_width;h=s.vfx_aspect_lock?w*input.height()/input.width():s.vfx_height;}
        w=std::ceil(w/2)*2;h=std::ceil(h/2)*2;
        if(w<input.width()||h<input.height())throw std::runtime_error("VFX VSR output must be at least the input size.");
    }
    if(s.vfx_upscale_enabled){
        if(s.vfx_vsr_enabled&&!restores(s))throw std::runtime_error("Choose VSR spatial upscaling or Lightweight Upscale in the VFX card.");
        w*=s.vfx_upscale_scale;h*=s.vfx_upscale_scale;
        if(std::abs(w-std::round(w))>1e-6||std::abs(h-std::round(h))>1e-6)throw std::runtime_error("Lightweight Upscale requires exact integral dimensions; choose a different scale.");
    }
    return {checkedDimension(std::round(w)),checkedDimension(std::round(h))};
}
double vfxFrameRate(double source,const Settings& s){
    if(!s.vfx_frame_generation_enabled)return source;
    if(source<=0)throw std::runtime_error("VFX frame generation requires valid source timestamps/FPS.");
    auto rate=s.vfx_frame_rate_mode=="Multiplier"?source*s.vfx_frame_multiplier:targetFrameRate(s.vfx_target_fps);
    if(rate<=source)throw std::runtime_error("VFX target FPS must exceed its input frame rate.");return rate;
}
QString vfxReport(const Settings& s){
    QStringList stages;
    if(s.vfx_vsr_enabled)stages<<QString("VSR mode %1 strength %2").arg(s.vfx_vsr_mode).arg(s.vfx_vsr_strength);
    if(s.vfx_upscale_enabled)stages<<QString("Lightweight Upscale %1x").arg(s.vfx_upscale_scale);
    if(s.vfx_frame_generation_enabled)stages<<QString("Video Frame Generation model %1").arg(s.vfx_frame_generation_model);
    return "NVIDIA VFX SDK 1.3.0.0: "+stages.join(" -> ");
}
void retainVfxPixelAspect(MediaInfo& output,const MediaInfo& source){
    auto ratio=source.metadata.value("sample_aspect_ratio").toString().split(':');
    int numerator=ratio.value(0).toInt(),denominator=ratio.value(1).toInt();
    if(numerator<=0||denominator<=0){numerator=1;denominator=1;}
    if(source.clockwiseRotation()%180)std::swap(numerator,denominator);
    output.metadata.insert("output_sample_aspect_ratio",QString("%1:%2").arg(numerator).arg(denominator));
}
QImage vfxDisplayAspect(const QImage& pixels,const MediaInfo& source){
    auto colors=source;retainVfxPixelAspect(colors,source);const auto ratio=colors.outputSampleAspectRatio();
    if(ratio.width()==ratio.height())return pixels;
    const double width=double(pixels.width())*ratio.width()/ratio.height();
    auto result = pixels.scaled(checkedDimension(std::round(width)),pixels.height(),Qt::IgnoreAspectRatio,Qt::SmoothTransformation);
    requireImageAllocation(result, "VFX display aspect conversion");
    return result;
}
void VideoEffects::initialize(int ordinal){
    prepareCudaPrimaryContext(ordinal);auto& api=Api::get();std::lock_guard lock(api.mutex);Error error{};
    auto root=QDir::toNativeSeparators(Paths::instance().at("bin/runtime/vfx")).toStdWString();check(api.vfx_initFn(ordinal,root.c_str(),error.data(),int(error.size())),error);
}
QString VideoEffects::diagnostics(){try{std::array<char,16384> out{};Api::get().vfx_statusFn(out.data(),int(out.size()));return QString::fromUtf8(out.data());}catch(const std::exception& e){return QString::fromUtf8(e.what());}}

struct VideoEffects::State {
    Settings settings;
    MediaInfo input,spatialColors,outputColors;
    JobControl& control;
    std::array<std::unique_ptr<Effect>,7> effects;
    std::vector<std::shared_ptr<VfxSurface::Storage>> buffers;
    std::shared_ptr<VfxSurface::Metrics> metrics=std::make_shared<VfxSurface::Metrics>();
    State(Settings s,MediaInfo i,JobControl& c):settings(std::move(s)),input(std::move(i)),spatialColors(input),outputColors(input),control(c){}
    ~State(){for(auto& effect:effects)effect.reset();buffers.clear();}
    VfxSurface acquire(QSize size,unsigned format){
        for(auto& p:buffers)if(p.use_count()==1&&p->desc.width==size.width()&&p->desc.height==size.height()&&p->desc.pixel_format==format)return VfxSurface(p);
        auto storage=std::make_shared<VfxSurface::Storage>();storage->metrics=metrics;Error error{};storage->handle=Api::get().vfx_surface_createFn(size.width(),size.height(),format,error.data(),int(error.size()));check(storage->handle!=nullptr,error);storage->desc=descriptor<VfxFrameDescV2>();check(Api::get().vfx_surface_frame_descFn(storage->handle,&storage->desc),error);buffers.push_back(storage);return VfxSurface(std::move(storage));
    }
    void account(const VfxFrameResultV2& r){metrics->account(r);}
    VfxSurface converted(const VfxFrameDescV2& source,QSize size,unsigned format){
        control.checkpoint();auto out=acquire(size,format);auto& dst=out.storage_->desc;
        // VFX consumes encoded RGB. Preserve the source gamut and transfer;
        // converting BT.709 to gamma 2.2 here darkened even retained FG frames.
        dst.color_matrix=source.color_matrix;dst.color_range=1;
        dst.color_primaries=source.color_primaries;dst.color_transfer=source.color_transfer;
        dst.reserved[1]=0;auto result=descriptor<VfxFrameResultV2>();convert(source,dst,&result);account(result);return out;
    }
    Effect& ensure(unsigned type,QSize in,QSize out,unsigned format){auto& p=effects[type];if(!p)p=std::make_unique<Effect>(effectDescriptor(type,in,out,format,settings));return *p;}
    VfxSurface run(unsigned type,const VfxSurface& src,QSize out,bool reset=false,const VfxSurface& previous={},float timestep=.5f,bool cut=false){
        control.checkpoint();auto input=src.descriptor();auto format=input.pixel_format==VFX_RGB10A2?VFX_RGB10A2:VFX_RGBA8;
        auto& effect=ensure(type,QSize(int(input.width),int(input.height)),out,format);auto output=acquire(out,format);auto dst=output.descriptor();
        auto prev=previous?previous.descriptor():VfxFrameDescV2{};
        auto params=descriptor<VfxRunParamsV2>();params.reset=reset;params.shot_change=cut;params.timestep=timestep;auto result=descriptor<VfxFrameResultV2>();Error error{};
        check(Api::get().vfx_process_frame_v2Fn(effect.handle,&input,previous?&prev:nullptr,&dst,&params,&result,error.data(),int(error.size())),error);account(result);
        output.storage_->desc.color_matrix=input.color_matrix;output.storage_->desc.color_range=1;
        output.storage_->desc.color_primaries=input.color_primaries;output.storage_->desc.color_transfer=input.color_transfer;return output;
    }
};
VideoEffects::VideoEffects(Settings settings,MediaInfo input,JobControl& control):state_(std::make_unique<State>(std::move(settings),std::move(input),control)){
    const auto& s=state_->settings;initialize(gpuOrdinal(s.ai_gpu_uuid));auto size=vfxSpatialSize({state_->input.width,state_->input.height},s);state_->spatialColors.width=size.width();state_->spatialColors.height=size.height();
    state_->outputColors=state_->spatialColors;state_->outputColors.fps=vfxFrameRate(state_->input.fps,s);
    if(s.vfx_frame_generation_enabled)state_->outputColors.variableFrameRate=false;
    if(state_->input.hdr&&s.vfx_upscale_enabled)throw std::runtime_error("Selected VFX effects require SDR at this position.");
}
VideoEffects::~VideoEffects()=default;
VfxSurface VideoEffects::spatial(const VfxFrameDescV2& input,bool reset){
    auto& s=*state_;s.control.checkpoint();const auto& settings=s.settings;QSize inSize(s.input.width,s.input.height);
    bool eight=settings.vfx_upscale_enabled;
    auto format=(!eight&&(s.input.hdr||s.input.precisionBits()>8))?VFX_RGB10A2:VFX_RGBA8;
    auto current=s.converted(input,inSize,format);
    if(settings.vfx_vsr_enabled){QSize output=restores(settings)?inSize:vfxSpatialSize(inSize,settings);current=s.run(VFX_VSR,current,output,reset);}
    if(settings.vfx_upscale_enabled)current=s.run(VFX_UPSCALE,current,vfxSpatialSize(inSize,settings),reset);
    ++s.metrics->frames;return current;
}
VfxSurface VideoEffects::generate(const VfxSurface& previous,const VfxSurface& current,float t,bool cut){auto& s=*state_;return s.run(VFX_FRAME_GENERATION,current,{s.spatialColors.width,s.spatialColors.height},false,previous,t,cut);}
VfxSurface VideoEffects::process(const VfxFrameDescV2& input,bool reset){return spatial(input,reset);}
MediaInfo VideoEffects::spatialInfo()const{return state_->spatialColors;}
MediaInfo VideoEffects::outputInfo()const{return state_->outputColors;}
QVariantMap VideoEffects::performance()const{const auto& s=*state_;const auto& m=*s.metrics;return {{"source_frames",QVariant::fromValue(m.frames)},{"upload_bytes",QVariant::fromValue(m.uploads)},{"download_bytes",QVariant::fromValue(m.downloads)},{"effect_ms",m.effectMs},{"conversion_ms",m.conversionMs},{"surface_pool_size",int(s.buffers.size())}};}
}
