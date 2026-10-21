#include "d3d_amf.hpp"
#include "d3d_video.hpp"
#include "av_api.hpp"
#include "host_video.hpp"
#include "configuration/settings.hpp"
#include "processing/fsr_native.hpp"
#include <d3d11_4.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
extern "C" {
#include <libavutil/hwcontext_d3d11va.h>
}
#include <cmath>
#include <map>

namespace ve {
namespace {
using Microsoft::WRL::ComPtr;
void requireDx(HRESULT status,const char* operation){if(FAILED(status))throw D3dVideoUnavailable(std::string(operation)+" (HRESULT "+std::to_string(uint32_t(status))+")");}
}
struct D3dSharedEncoder::Impl {
    av::Api& api=av::Api::get();JobControl& control;MediaInfo info;
    ComPtr<ID3D12Device> device;ComPtr<ID3D11Device> device11;ComPtr<ID3D11DeviceContext> context11;
    AVFormatContext* format=nullptr;AVCodecContext* codec=nullptr;AVStream* stream=nullptr;AVPacket* packet=nullptr;
    AVBufferRef* hardware=nullptr;AVBufferRef* pool=nullptr;AVBufferRef* qsvDevice=nullptr;AVBufferRef* qsvPool=nullptr;
    bool ten=false,finished=false,intel=false;quint64 sent=0,received=0;
    std::map<qint64,qint64> durations;
    struct Shared {ComPtr<ID3D11Texture2D> texture;ComPtr<ID3D12Resource> resource;};
    std::map<ID3D11Texture2D*,Shared> shared;
    Impl(const QString& path,const QString& name,const Settings& settings,const MediaInfo& output,void* d3dDevice,JobControl& job,bool allIntra)
        :control(job),info(output),device(static_cast<ID3D12Device*>(d3dDevice)){
        AVDictionary* options=nullptr;
        try{
            if(!device||(info.width&1)||(info.height&1)||info.width<2||info.height<2)throw D3dVideoUnavailable("AMF requires a DirectX device and even video dimensions");
            const bool h264=name.startsWith("h264",Qt::CaseInsensitive)||name.startsWith("H.264");const bool av1=name.startsWith("av1",Qt::CaseInsensitive);
            if(h264&&info.hdr)throw D3dVideoUnavailable("HDR AMD output requires HEVC or AV1");
            ComPtr<IDXGIFactory4> factory;requireDx(CreateDXGIFactory2(0,IID_PPV_ARGS(&factory)),"Create AMF adapter factory");
            ComPtr<IDXGIAdapter1> adapter;requireDx(factory->EnumAdapterByLuid(device->GetAdapterLuid(),IID_PPV_ARGS(&adapter)),"Select AMD encoder adapter");
            DXGI_ADAPTER_DESC1 desc{};requireDx(adapter->GetDesc1(&desc),"Describe shared DirectX encoder adapter");
            if(desc.VendorId!=0x1002&&desc.VendorId!=0x8086)throw D3dVideoUnavailable("Shared DirectX encoding requires an AMD or Intel graphics adapter");
            intel=desc.VendorId==0x8086;
            const auto encoderName=intel?(h264?"h264_qsv":av1?"av1_qsv":"hevc_qsv"):(h264?"h264_amf":av1?"av1_amf":"hevc_amf");
            const auto* encoder=api.avcodec_find_encoder_by_name(encoderName);if(!encoder)throw D3dVideoUnavailable("Bundled shared DirectX hardware encoder is unavailable");
            requireDx(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,D3D11_CREATE_DEVICE_VIDEO_SUPPORT|D3D11_CREATE_DEVICE_BGRA_SUPPORT,nullptr,0,D3D11_SDK_VERSION,&device11,nullptr,&context11),"Create DirectX AMF device");
            ComPtr<ID3D11Multithread> multithread;requireDx(context11.As(&multithread),"Protect AMD video context");multithread->SetMultithreadProtected(TRUE);
            hardware=api.av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);if(!hardware)throw std::bad_alloc{};
            auto* dx=reinterpret_cast<AVD3D11VADeviceContext*>(reinterpret_cast<AVHWDeviceContext*>(hardware->data)->hwctx);dx->device=device11.Get();dx->device->AddRef();
            av::check(api.av_hwdevice_ctx_init(hardware),"Initialize AMF DirectX device");
            ten=!h264&&(info.hdr||settings.hdr_mode||info.precisionBits()>8);
            pool=api.av_hwframe_ctx_alloc(hardware);if(!pool)throw std::bad_alloc{};auto* frames=reinterpret_cast<AVHWFramesContext*>(pool->data);
            frames->format=AV_PIX_FMT_D3D11;frames->sw_format=ten?AV_PIX_FMT_P010LE:AV_PIX_FMT_NV12;frames->width=info.width;frames->height=info.height;frames->initial_pool_size=0;
            auto* frameContext=reinterpret_cast<AVD3D11VAFramesContext*>(frames->hwctx);frameContext->BindFlags=D3D11_BIND_SHADER_RESOURCE;
            frameContext->MiscFlags=D3D11_RESOURCE_MISC_SHARED|D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
            av::check(api.av_hwframe_ctx_init(pool),"Initialize shared DirectX AMF frame pool");
            if(intel){
                // FFmpeg 8.1 dynamic QSV mappings retain the individual D3D11
                // texture; DIRECT rejects any route requiring host image copies.
                av::check(api.av_hwdevice_ctx_create_derived(&qsvDevice,AV_HWDEVICE_TYPE_QSV,hardware,0),"Derive Intel QSV from selected DirectX adapter");
                av::check(api.av_hwframe_ctx_create_derived(&qsvPool,AV_PIX_FMT_QSV,qsvDevice,pool,AV_HWFRAME_MAP_READ|AV_HWFRAME_MAP_DIRECT),"Derive direct Intel hardware frame pool");
                auto probe=av::frame();av::check(api.av_hwframe_get_buffer(pool,probe.get(),0),"Probe Intel shared surface");
                if(reinterpret_cast<intptr_t>(probe->data[1])!=0)throw D3dVideoUnavailable("Intel DirectX pool requires unsupported array slices");
                target(reinterpret_cast<ID3D11Texture2D*>(probe->data[0]));(void)mapQsv(probe.get());
            }
            codec=api.avcodec_alloc_context3(encoder);if(!codec)throw std::bad_alloc{};codec->width=info.width;codec->height=info.height;
            codec->pix_fmt=intel?AV_PIX_FMT_QSV:AV_PIX_FMT_D3D11;codec->time_base={1,60000000};codec->framerate=api.av_d2q(info.fps,1000000);
            codec->gop_size=int(std::llround(info.fps*10));codec->max_b_frames=-1;codec->qmin=-1;codec->qmax=-1;
            codec->hw_frames_ctx=api.av_buffer_ref(intel?qsvPool:pool);if(!codec->hw_frames_ctx)throw std::bad_alloc{};
            const auto aspect=info.outputSampleAspectRatio();codec->sample_aspect_ratio={aspect.width(),aspect.height()};codec->color_range=AVCOL_RANGE_MPEG;codec->colorspace=info.hdr?AVCOL_SPC_BT2020_NCL:AVCOL_SPC_BT709;
            const auto primaries=api.av_color_primaries_from_name(info.primaries.toUtf8().constData()),transfer=api.av_color_transfer_from_name(info.transfer.toUtf8().constData());
            codec->color_primaries=AVColorPrimaries(primaries<0?(info.hdr?9:1):primaries);codec->color_trc=AVColorTransferCharacteristic(transfer<0?(info.hdr?16:1):transfer);codec->chroma_sample_location=AVCHROMA_LOC_LEFT;
            const int quality=settings.quality=="Max"?1:settings.quality=="Best"?20:settings.quality=="Good"?24:28;
            if(intel){
                api.av_dict_set(&options,"preset","veryslow",0);codec->global_quality=quality;codec->max_b_frames=3;
                if(h264)api.av_dict_set(&options,"look_ahead","1",0);
            }else{
                api.av_dict_set(&options,"usage","transcoding",0);api.av_dict_set(&options,"quality","quality",0);api.av_dict_set(&options,"rc","cqp",0);
                api.av_dict_set(&options,"qp_i",QByteArray::number(quality).constData(),0);api.av_dict_set(&options,"qp_p",QByteArray::number(quality).constData(),0);
                if(h264)api.av_dict_set(&options,"qp_b",QByteArray::number(quality).constData(),0);
            }
            if(allIntra){codec->gop_size=1;codec->max_b_frames=0;}
            api.av_dict_set(&options,"async_depth","4",0);
            av::check(api.avformat_alloc_output_context2(&format,nullptr,"mp4",path.toUtf8().constData()),"Create AMD video muxer");format->avoid_negative_ts=AVFMT_AVOID_NEG_TS_DISABLED;
            if(format->oformat->flags&AVFMT_GLOBALHEADER)codec->flags|=AV_CODEC_FLAG_GLOBAL_HEADER;
            av::check(api.avcodec_open2(codec,encoder,&options),"Open AMD DirectX hardware encoder");api.av_dict_free(&options);
            stream=api.avformat_new_stream(format,nullptr);if(!stream)throw std::bad_alloc{};stream->time_base=codec->time_base;stream->avg_frame_rate=codec->framerate;
            av::check(api.avcodec_parameters_from_context(stream->codecpar,codec),"Describe AMD encoded stream");retainStaticHdrMetadata(stream->codecpar,info);
            av::check(api.avio_open(&format->pb,path.toUtf8().constData(),AVIO_FLAG_WRITE),"Open AMD video destination");api.av_dict_set(&options,"video_track_timescale","60000000",0);
            av::check(api.avformat_write_header(format,&options),"Write AMD video header");api.av_dict_free(&options);packet=api.av_packet_alloc();if(!packet)throw std::bad_alloc{};
        }catch(...){api.av_dict_free(&options);release();throw;}
    }
    void release(){api.av_packet_free(&packet);api.avcodec_free_context(&codec);shared.clear();api.av_buffer_unref(&qsvPool);api.av_buffer_unref(&qsvDevice);api.av_buffer_unref(&pool);api.av_buffer_unref(&hardware);
        if(format){if(format->pb)api.avio_closep(&format->pb);api.avformat_free_context(format);format=nullptr;}}
    ~Impl(){release();}
    av::Frame mapQsv(const AVFrame* source){
        auto mapped=av::frame();mapped->format=AV_PIX_FMT_QSV;mapped->hw_frames_ctx=api.av_buffer_ref(qsvPool);if(!mapped->hw_frames_ctx)throw std::bad_alloc{};
        av::check(api.av_hwframe_map(mapped.get(),source,AV_HWFRAME_MAP_READ|AV_HWFRAME_MAP_DIRECT),"Map shared DirectX texture directly to Intel QSV");
        av::check(api.av_frame_copy_props(mapped.get(),source),"Retain Intel mapped frame properties");return mapped;
    }
    ID3D12Resource* target(ID3D11Texture2D* texture){
        if(const auto found=shared.find(texture);found!=shared.end())return found->second.resource.Get();
        if(shared.size()>=64)throw std::runtime_error("AMD hardware encoder exceeded its bounded shared surface pool");
        ComPtr<IDXGIResource1> dxgi;requireDx(texture->QueryInterface(IID_PPV_ARGS(&dxgi)),"Share AMD encoder texture");HANDLE handle=nullptr;
        requireDx(dxgi->CreateSharedHandle(nullptr,DXGI_SHARED_RESOURCE_READ|DXGI_SHARED_RESOURCE_WRITE,nullptr,&handle),"Create AMD encoder shared handle");
        Shared entry;entry.texture=texture;const auto status=device->OpenSharedHandle(handle,IID_PPV_ARGS(&entry.resource));CloseHandle(handle);requireDx(status,"Open AMD encoder texture in DirectX 12");
        auto* resource=entry.resource.Get();shared.emplace(texture,std::move(entry));return resource;
    }
    void packets(){for(;;){control.checkpoint();const auto status=api.avcodec_receive_packet(codec,packet);if(status==AVERROR(EAGAIN)||status==AVERROR_EOF)return;
        av::check(status,"Receive AMD video packet");const auto found=durations.find(packet->pts);if(found==durations.end())throw std::runtime_error("AMD encoded timestamp does not match submitted input");
        packet->duration=found->second;durations.erase(found);api.av_packet_rescale_ts(packet,codec->time_base,stream->time_base);packet->stream_index=stream->index;
        av::check(api.av_interleaved_write_frame(format,packet),"Mux AMD video packet");api.av_packet_unref(packet);++received;}}
};
D3dSharedEncoder::D3dSharedEncoder(const QString& path,const QString& codec,const Settings& settings,const MediaInfo& info,void* device,JobControl& control,bool allIntra):impl_(std::make_unique<Impl>(path,codec,settings,info,device,control,allIntra)){}
D3dSharedEncoder::~D3dSharedEncoder()=default;
QString D3dSharedEncoder::diagnostics()const{return QString("%1; shared DirectX 12/11; quality; %2-bit; %3 shared GPU surfaces").arg(impl_->intel?"Intel QSV direct hardware mapping":"AMD AMF").arg(impl_->ten?10:8).arg(impl_->shared.size());}
void D3dSharedEncoder::write(const FsrSurface& surface,qint64 pts,qint64 duration,TimeBase base,bool forceIntra){
    auto& s=*impl_;s.control.checkpoint();if(s.finished)throw std::logic_error("Cannot write after AMD encoding finishes");
    auto frame=av::frame();av::check(s.api.av_hwframe_get_buffer(s.pool,frame.get(),0),"Allocate AMD hardware video surface");
    if(reinterpret_cast<intptr_t>(frame->data[1])!=0)throw std::runtime_error("AMD encoder pool returned an unsupported array slice");
    VeFsrVideoOutput output{};output.size=sizeof(output);output.abi=VE_FSR_ABI_VERSION;output.width=uint32_t(s.info.width);output.height=uint32_t(s.info.height);
    output.texture=s.target(reinterpret_cast<ID3D11Texture2D*>(frame->data[0]));output.resource_state=D3D12_RESOURCE_STATE_COMMON;output.pixel_format=s.ten?VE_FSR_P010:VE_FSR_NV12;
    output.transfer=fsrTransfer(s.info.transfer);output.primaries=fsrPrimaries(s.info.primaries);output.color_matrix=s.info.hdr?2u:1u;output.color_range=0;output.chroma_location=1;surface.writeVideo(output);
    frame->pts=s.api.av_rescale_q(pts,{base.numerator,base.denominator},s.codec->time_base);frame->duration=s.api.av_rescale_q(duration,{base.numerator,base.denominator},s.codec->time_base);
    if(frame->duration<=0)throw std::runtime_error("AMD video frame duration must be positive");
    frame->color_range=s.codec->color_range;frame->colorspace=s.codec->colorspace;frame->color_primaries=s.codec->color_primaries;frame->color_trc=s.codec->color_trc;frame->chroma_location=AVCHROMA_LOC_LEFT;
    if(forceIntra)frame->pict_type=AV_PICTURE_TYPE_I;copyStaticHdrMetadata(frame.get(),s.stream->codecpar);
    if(!s.durations.emplace(frame->pts,frame->duration).second)throw std::runtime_error("AMD video timestamps must be unique");
    auto mapped=s.intel?s.mapQsv(frame.get()):av::Frame{};
    av::check(s.api.avcodec_send_frame(s.codec,s.intel?mapped.get():frame.get()),"Submit shared DirectX hardware video surface");++s.sent;s.packets();
    if(s.sent-s.received>64)throw std::runtime_error("AMD encoder exceeded its bounded pending frame count");
}
void D3dSharedEncoder::finish(){auto& s=*impl_;if(s.finished)return;s.control.checkpoint();av::check(s.api.avcodec_send_frame(s.codec,nullptr),"Drain AMD hardware encoder");s.packets();
    if(s.sent!=s.received||!s.durations.empty())throw std::runtime_error("AMD encoder lost submitted frames");av::check(s.api.av_write_trailer(s.format),"Finish AMD video");s.finished=true;}
}
