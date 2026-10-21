#include "fsr_native.hpp"
#include "infrastructure/paths.hpp"
#include <QDir>
#include <QFileInfo>
#include <QMap>
#include <QJsonArray>
#include <array>
#include <algorithm>
#include <cmath>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <stdexcept>

namespace ve {
namespace {
template<class T> using ComPtr=Microsoft::WRL::ComPtr<T>;
QString runtimeDirectory(){const auto override=qEnvironmentVariable("VE_FSR_RUNTIME_DIRECTORY");return override.isEmpty()?Paths::instance().at("bin/runtime/fsrfg"):QFileInfo(override).absoluteFilePath();}
class Api {
  public:
    template<class T> T load(const char* name){auto* function=GetProcAddress(module_,name);if(!function)throw std::runtime_error("FSR engine is missing export "+std::string(name));return reinterpret_cast<T>(function);}
    Api(){
        const auto path=QDir::toNativeSeparators(runtimeDirectory()+"/neuroframe_engine_fsr_frame_generation.dll").toStdWString();
        module_=LoadLibraryExW(path.c_str(),nullptr,LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR|LOAD_LIBRARY_SEARCH_SYSTEM32);
        if(!module_)throw std::runtime_error("Cannot load AMD FSR Frame Generation engine: Windows error "+std::to_string(GetLastError()));
        try{
        abi=load<decltype(abi)>("fsrfg_abi_version");if(abi()!=VE_FSR_ABI_VERSION)throw std::runtime_error("The AMD FSR engine uses an incompatible ABI");
#define VE_FSR_BIND(member,symbol) member=load<decltype(member)>(symbol)
        VE_FSR_BIND(createDevice,"fsrfg_device_create");VE_FSR_BIND(releaseDevice,"fsrfg_device_release");VE_FSR_BIND(capabilities,"fsrfg_device_capabilities");
        VE_FSR_BIND(nativeDevice,"fsrfg_device_d3d12");VE_FSR_BIND(nativeQueue,"fsrfg_device_queue");VE_FSR_BIND(createSession,"fsrfg_session_create");
        VE_FSR_BIND(releaseSession,"fsrfg_session_release");VE_FSR_BIND(process,"fsrfg_process");VE_FSR_BIND(describe,"fsrfg_surface_describe");
        VE_FSR_BIND(releaseSurface,"fsrfg_surface_release");VE_FSR_BIND(read,"fsrfg_surface_read");VE_FSR_BIND(writeVideo,"fsrfg_surface_to_video");
        VE_FSR_BIND(upload,"fsrfg_surface_upload");
#undef VE_FSR_BIND
        }catch(...){FreeLibrary(module_);module_=nullptr;throw;}
    }
    static Api& get(){static Api api;return api;}
    decltype(&fsrfg_abi_version) abi=nullptr;
    decltype(&fsrfg_device_create) createDevice=nullptr;decltype(&fsrfg_device_release) releaseDevice=nullptr;
    decltype(&fsrfg_device_capabilities) capabilities=nullptr;decltype(&fsrfg_device_d3d12) nativeDevice=nullptr;decltype(&fsrfg_device_queue) nativeQueue=nullptr;
    decltype(&fsrfg_session_create) createSession=nullptr;decltype(&fsrfg_session_release) releaseSession=nullptr;decltype(&fsrfg_process) process=nullptr;
    decltype(&fsrfg_surface_describe) describe=nullptr;decltype(&fsrfg_surface_release) releaseSurface=nullptr;
    decltype(&fsrfg_surface_read) read=nullptr;decltype(&fsrfg_surface_to_video) writeVideo=nullptr;
    decltype(&fsrfg_surface_upload) upload=nullptr;
  private:
    HMODULE module_=nullptr; // Keep code resident through worker/global teardown.
};
template<class T> T record(){T value{};value.size=sizeof(T);value.abi=VE_FSR_ABI_VERSION;return value;}
void require(bool okay,const std::array<char,4096>& error){if(!okay)throw std::runtime_error(error[0]?error.data():"AMD FSR operation failed");}
QString adapterKey(const DXGI_ADAPTER_DESC1& desc,uint32_t occurrence){
    return QString("dxgi:%1:%2:%3:%4:%5").arg(desc.VendorId,4,16,QLatin1Char('0')).arg(desc.DeviceId,4,16,QLatin1Char('0'))
        .arg(desc.SubSysId,8,16,QLatin1Char('0')).arg(desc.Revision,2,16,QLatin1Char('0')).arg(occurrence);
}
}
std::vector<FsrAdapter> detectFsrAdapters(JobControl* control){
    ComPtr<IDXGIFactory6> factory;if(FAILED(CreateDXGIFactory2(0,IID_PPV_ARGS(&factory))))throw std::runtime_error("Cannot enumerate DirectX graphics adapters");
    std::vector<FsrAdapter> result;QMap<QString,uint32_t> seen;
    for(uint32_t index=0;;++index){
        if(control)control->checkpoint();ComPtr<IDXGIAdapter1> adapter;const auto status=factory->EnumAdapters1(index,&adapter);if(status==DXGI_ERROR_NOT_FOUND)break;
        if(FAILED(status))throw std::runtime_error("Cannot read a DirectX graphics adapter");DXGI_ADAPTER_DESC1 desc{};
        if(FAILED(adapter->GetDesc1(&desc))||(desc.Flags&DXGI_ADAPTER_FLAG_SOFTWARE))continue;
        const auto hardware=adapterKey(desc,0);const auto id=adapterKey(desc,seen[hardware]++);
        FsrAdapter item{index,desc.VendorId,desc.DeviceId,desc.AdapterLuid.LowPart,desc.AdapterLuid.HighPart,desc.DedicatedVideoMemory,id,QString::fromWCharArray(desc.Description),{},false};
        try{
            auto& api=Api::get();auto create=record<VeFsrDeviceDesc>();create.adapter_index=index;const auto directory=QDir::toNativeSeparators(runtimeDirectory()).toStdWString();create.runtime_directory=directory.c_str();
            std::array<char,4096> error{};auto* device=api.createDevice(&create,error.data(),uint32_t(error.size()));
            if(!device)item.unavailableReason=QString::fromUtf8(error.data());
            else{auto caps=record<VeFsrCapabilities>();item.available=api.capabilities(device,&caps)!=0;item.unavailableReason=QString::fromUtf8(caps.unavailable_reason);api.releaseDevice(device);}
        }catch(const std::exception& e){item.unavailableReason=QString::fromUtf8(e.what());}
        result.push_back(std::move(item));
    }
    return result;
}
uint32_t fsrAdapterIndex(const QString& selection){
    if(selection=="auto")return UINT32_MAX;
    bool number=false;const auto index=selection.toUInt(&number);if(number)return index;
    for(const auto& adapter:detectFsrAdapters())if(adapter.id.compare(selection,Qt::CaseInsensitive)==0)return adapter.index;
    throw std::runtime_error("Selected FSR DirectX adapter is unavailable");
}
uint32_t fsrTransfer(const QString& transfer){
    if(transfer=="iec61966-2-1")return VE_FSR_SRGB;if(transfer=="bt709"||transfer=="smpte170m"||transfer=="bt2020-10"||transfer=="bt2020-12")return VE_FSR_BT709;
    if(transfer=="linear")return VE_FSR_LINEAR;if(transfer=="gamma22"||transfer=="bt470m")return VE_FSR_GAMMA22;if(transfer=="gamma28"||transfer=="bt470bg")return VE_FSR_GAMMA28;
    if(transfer=="smpte2084")return VE_FSR_PQ;if(transfer=="arib-std-b67")return VE_FSR_HLG;throw std::runtime_error("FSR video transfer function is unsupported or unspecified");
}
uint32_t fsrPrimaries(const QString& primaries){
    if(primaries=="bt2020")return VE_FSR_BT2020;if(primaries=="bt709")return VE_FSR_PRIMARIES_BT709;
    if(primaries=="smpte170m"||primaries=="smpte240m")return VE_FSR_SMPTE_C;
    if(primaries=="bt470bg")return VE_FSR_BT470BG;if(primaries=="bt470m")return VE_FSR_BT470M;
    throw std::runtime_error("FSR video color primaries are unsupported or unspecified");
}
float fsrMaximumLuminance(const MediaInfo& colors){
    if(!colors.hdr)return 100;
    double maximum=0;
    const auto luminance=[](const QJsonValue& value){
        const auto parts=value.toVariant().toString().split('/');
        if(parts.size()==2){const auto denominator=parts[1].toDouble();return denominator>0?parts[0].toDouble()/denominator:0.;}
        return value.toVariant().toDouble();
    };
    for(const auto& value:colors.metadata.value("side_data_list").toArray()){
        const auto side=value.toObject();const auto type=side.value("side_data_type").toString();
        const auto nits=type=="Mastering display metadata"?luminance(side.value("max_luminance")):
            type=="Content light level metadata"?luminance(side.value("max_content")):0.;
        if(std::isfinite(nits)&&nits>0&&nits<=10000)maximum=std::max(maximum,nits);
    }
    return float(maximum>0?std::clamp(maximum,100.,10000.):colors.transfer=="arib-std-b67"?1000.:10000.);
}
MediaInfo fsrVideoColors(MediaInfo colors){
    const auto missing=[](const QString& value){return value.isEmpty()||value=="unknown"||value=="unspecified";};
    QJsonArray assumptions;
    if(missing(colors.transfer)){
        if(colors.hdr||colors.primaries=="bt2020"||colors.colorSpace.startsWith("bt2020"))
            throw std::runtime_error("FSR requires an explicit transfer function for HDR or BT.2020 input");
        colors.transfer="bt709";assumptions.append("Unspecified SDR transfer: BT.709 assumed");
    }
    if(missing(colors.primaries)){
        colors.primaries=colors.hdr||colors.colorSpace.startsWith("bt2020")?"bt2020":
            colors.colorSpace=="smpte170m"?"smpte170m":colors.colorSpace=="bt470bg"?"bt470bg":"bt709";
        assumptions.append("Unspecified color primaries: "+colors.primaries+" assumed");
    }
    if(missing(colors.colorSpace)){colors.colorSpace=colors.hdr?"bt2020nc":"bt709";assumptions.append("Unspecified video matrix: "+colors.colorSpace+" assumed");}
    if(missing(colors.range)){colors.range="tv";assumptions.append("Unspecified video range: limited assumed; decoder frame tags take precedence");}
    (void)fsrTransfer(colors.transfer);(void)fsrPrimaries(colors.primaries);
    if(!colors.metadata.contains("output_sample_aspect_ratio")){
        const auto parts=colors.metadata.value("sample_aspect_ratio").toString().split(':');
        int numerator=parts.value(0).toInt(),denominator=parts.value(1).toInt();
        if(numerator<=0||denominator<=0)numerator=denominator=1;
        if(colors.clockwiseRotation()%180)std::swap(numerator,denominator);
        colors.metadata.insert("output_sample_aspect_ratio",QString::number(numerator)+':'+QString::number(denominator));
    }
    if(!assumptions.isEmpty())colors.metadata.insert("ve_fsr_color_assumptions",assumptions);
    return colors;
}
QByteArray fsrAdapterCacheIdentity(){
    static const QByteArray identity=[](){
        QByteArray bytes("fsr-dxgi-hardware/v1");ComPtr<IDXGIFactory6> factory;if(FAILED(CreateDXGIFactory2(0,IID_PPV_ARGS(&factory))))return bytes;
        for(uint32_t index=0;;++index){ComPtr<IDXGIAdapter1> adapter;if(factory->EnumAdapters1(index,&adapter)==DXGI_ERROR_NOT_FOUND)break;if(!adapter)continue;
            DXGI_ADAPTER_DESC1 desc{};if(FAILED(adapter->GetDesc1(&desc))||(desc.Flags&DXGI_ADAPTER_FLAG_SOFTWARE))continue;LARGE_INTEGER driver{};adapter->CheckInterfaceSupport(__uuidof(IDXGIDevice),&driver);
            bytes+=adapterKey(desc,0).toUtf8()+'|'+QByteArray::number(desc.AdapterLuid.HighPart)+':'+QByteArray::number(desc.AdapterLuid.LowPart)+':'+QByteArray::number(driver.QuadPart)+';';
        }return bytes;
    }();return identity;
}
FsrDevice::FsrDevice(const QString& selection){
    auto& api=Api::get();auto desc=record<VeFsrDeviceDesc>();desc.adapter_index=fsrAdapterIndex(selection);const auto directory=QDir::toNativeSeparators(runtimeDirectory()).toStdWString();desc.runtime_directory=directory.c_str();
    std::array<char,4096> error{};handle_=api.createDevice(&desc,error.data(),uint32_t(error.size()));require(handle_!=nullptr,error);
    capabilities_=record<VeFsrCapabilities>();if(!api.capabilities(handle_,&capabilities_)){api.releaseDevice(handle_);handle_=nullptr;throw std::runtime_error(capabilities_.unavailable_reason);}
}
FsrDevice::~FsrDevice(){if(handle_)Api::get().releaseDevice(handle_);}
void* FsrDevice::nativeDevice()const{return Api::get().nativeDevice(handle_);}
void* FsrDevice::nativeQueue()const{return Api::get().nativeQueue(handle_);}
struct FsrSurface::Storage{VeFsrSurface* handle=nullptr;VeFsrSurfaceDesc description{};~Storage(){if(handle)Api::get().releaseSurface(handle);}};
FsrSurface::FsrSurface(VeFsrSurface* handle){
    if(!handle)throw std::runtime_error("FSR returned an empty surface");auto release=Api::get().releaseSurface;
    std::unique_ptr<VeFsrSurface,decltype(release)> guard(handle,release);state_=std::make_shared<Storage>();state_->handle=guard.release();state_->description=record<VeFsrSurfaceDesc>();
    if(!Api::get().describe(handle,&state_->description))throw std::runtime_error("FSR returned an invalid surface descriptor");
}
FsrSurface FsrDevice::uploadImage(const QImage& image,const MediaInfo& colors)const{
    const auto pixels=image.convertToFormat(QImage::Format_RGBA64);if(pixels.isNull())throw std::invalid_argument("FSR post-processing image is empty");
    auto frame=record<VeFsrFrame>();frame.memory_type=VE_FSR_MEMORY_HOST;frame.pixel_format=VE_FSR_RGBA16;
    frame.width=uint32_t(pixels.width());frame.height=uint32_t(pixels.height());frame.row_bytes=uint32_t(pixels.bytesPerLine());frame.pixels=pixels.constBits();
    frame.transfer=fsrTransfer(colors.transfer);frame.primaries=fsrPrimaries(colors.primaries);
    std::array<char,4096> error{};auto* surface=Api::get().upload(handle_,&frame,error.data(),uint32_t(error.size()));require(surface!=nullptr,error);return FsrSurface(surface);
}
const VeFsrSurfaceDesc& FsrSurface::description()const{if(!state_)throw std::runtime_error("FSR surface is empty");return state_->description;}
QImage FsrSurface::toHost(const MediaInfo& colors)const{
    const auto& desc=description();QImage image(int(desc.width),int(desc.height),QImage::Format_RGBA64);if(image.isNull())throw std::bad_alloc{};
    std::array<char,4096> error{};require(Api::get().read(state_->handle,VE_FSR_RGBA16,fsrTransfer(colors.transfer),fsrPrimaries(colors.primaries),image.bits(),uint32_t(image.bytesPerLine()),error.data(),uint32_t(error.size()))!=0,error);return image;
}
void FsrSurface::writeVideo(const VeFsrVideoOutput& output)const{(void)description();std::array<char,4096> error{};require(Api::get().writeVideo(state_->handle,&output,error.data(),uint32_t(error.size()))!=0,error);}
FsrInterpolation::FsrInterpolation(std::shared_ptr<FsrDevice> device,QSize size,bool hdr,int poolSize,float maximumNits):device_(std::move(device)){
    if(!device_)throw std::invalid_argument("FSR requires an owned GPU device");auto desc=record<VeFsrSessionDesc>();desc.width=uint32_t(size.width());desc.height=uint32_t(size.height());desc.hdr=hdr;desc.surface_pool_size=uint32_t(poolSize);desc.maximum_luminance_nits=hdr?maximumNits:100.f;
    std::array<char,4096> error{};handle_=Api::get().createSession(device_->handle_,&desc,error.data(),uint32_t(error.size()));require(handle_!=nullptr,error);
}
FsrInterpolation::~FsrInterpolation(){if(handle_)Api::get().releaseSession(handle_);}
FsrInterpolation::Result FsrInterpolation::evaluate(const VeFsrFrame& input,bool reset){
    auto native=record<VeFsrResult>();std::array<char,4096> error{};require(Api::get().process(handle_,&input,uint32_t(reset),&native,error.data(),uint32_t(error.size()))!=0,error);
    Result result;result.reset=native.reset!=0;result.sceneCut=native.scene_cut!=0;result.duplicate=native.duplicate!=0;result.uploadBytes=native.upload_bytes;result.downloadBytes=native.download_bytes;result.milliseconds=native.total_milliseconds;
    auto release=Api::get().releaseSurface;std::unique_ptr<VeFsrSurface,decltype(release)> source(native.source,release),generated(native.generated,release);
    result.source=FsrSurface(source.release());if(native.generated_count){if(native.generated_count!=1)throw std::runtime_error("FSR returned an invalid intermediate frame count");result.generated.push_back(FsrSurface(generated.release()));}return result;
}
FsrInterpolation::Result FsrInterpolation::evaluate(const QImage& image,const MediaInfo& colors,qint64 pts,qint64 duration,TimeBase base,bool reset){
    const auto pixels=image.convertToFormat(QImage::Format_RGBA64);auto frame=record<VeFsrFrame>();frame.memory_type=VE_FSR_MEMORY_HOST;frame.pixel_format=VE_FSR_RGBA16;frame.width=uint32_t(pixels.width());frame.height=uint32_t(pixels.height());frame.row_bytes=uint32_t(pixels.bytesPerLine());frame.pixels=pixels.constBits();
    frame.transfer=fsrTransfer(colors.transfer);frame.primaries=fsrPrimaries(colors.primaries);frame.pts=pts;frame.duration=duration;frame.timebase_numerator=base.numerator;frame.timebase_denominator=base.denominator;return evaluate(frame,reset);
}
FsrInterpolation::Result FsrInterpolation::evaluate(const FsrSurface& input,qint64 pts,qint64 duration,TimeBase base,bool reset){
    const auto& desc=input.description();auto frame=record<VeFsrFrame>();frame.memory_type=VE_FSR_MEMORY_D3D12;frame.pixel_format=desc.pixel_format;frame.width=desc.width;frame.height=desc.height;frame.texture=desc.texture;frame.resource_state=desc.resource_state;
    frame.transfer=desc.transfer;frame.primaries=desc.primaries;frame.pts=pts;frame.duration=duration;frame.timebase_numerator=base.numerator;frame.timebase_denominator=base.denominator;return evaluate(frame,reset);
}
}
