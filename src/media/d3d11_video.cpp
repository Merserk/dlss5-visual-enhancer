#include "d3d_video.hpp"
#include "av_api.hpp"
#include "fsr_frame_generation/fsr_bridge.h"
#include <d3d11_4.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
extern "C" {
#include <libavutil/hwcontext_d3d11va.h>
}
#include <algorithm>
#include <array>
#include <chrono>
#include <vector>

namespace ve {
namespace {
using Microsoft::WRL::ComPtr;
void checkDx(HRESULT status,const char* operation){if(FAILED(status))throw D3dVideoUnavailable(std::string(operation)+" (HRESULT "+std::to_string(uint32_t(status))+")");}
}
struct D3d11VideoDecoder::Impl {
    av::Api& api=av::Api::get();JobControl& control;AVFormatContext* format=nullptr;AVCodecContext* codec=nullptr;
    AVBufferRef* hardware=nullptr;AVPacket* packet=nullptr;int stream=-1;bool draining=false,unsafe=false;
    double start=0,timestampOffset=0;qint64 origin=AV_NOPTS_VALUE;std::string poolError;
    ComPtr<ID3D12Device> device;ComPtr<ID3D12CommandQueue> queue;ComPtr<ID3D12Fence> ready;
    ComPtr<ID3D11Device> device11;ComPtr<ID3D11DeviceContext> context11;ComPtr<ID3D11DeviceContext4> context4;ComPtr<ID3D11Fence> decodeFence;
    uint64_t sequence=0;
    struct Output {ComPtr<ID3D12Resource> texture;ComPtr<ID3D11Texture2D> shared11;ComPtr<ID3D12Fence> fence;av::Frame decoded;uint64_t value=0;};
    std::vector<std::shared_ptr<Output>> buffers;
    static AVPixelFormat select(AVCodecContext* codec,const AVPixelFormat* formats) noexcept {
        auto& self=*static_cast<Impl*>(codec->opaque);
        try{
            for(auto* current=formats;*current!=AV_PIX_FMT_NONE;++current)if(*current==AV_PIX_FMT_D3D11){
                AVBufferRef* pool=nullptr;av::check(self.api.avcodec_get_hw_frames_parameters(codec,codec->hw_device_ctx,*current,&pool),"Describe shared D3D11 decoder pool");
                try{auto* frames=reinterpret_cast<AVHWFramesContext*>(pool->data);auto* dx=reinterpret_cast<AVD3D11VAFramesContext*>(frames->hwctx);
                    dx->MiscFlags=0;
                    frames->initial_pool_size=std::max(frames->initial_pool_size,16)+4;
                    av::check(self.api.av_hwframe_ctx_init(pool),"Allocate shared D3D11 decoding textures");
                }catch(...){self.api.av_buffer_unref(&pool);throw;}
                self.api.av_buffer_unref(&codec->hw_frames_ctx);codec->hw_frames_ctx=pool;return *current;
            }
            self.poolError="The source codec has no DirectX 11 hardware decoder";
        }catch(const std::exception& error){self.poolError=error.what();}catch(...){self.poolError="Cannot allocate shared D3D11 decoder surfaces";}
        return AV_PIX_FMT_NONE;
    }
    Impl(const QString& path,void* d3dDevice,void* d3dQueue,double seconds,JobControl& job)
        :control(job),start(seconds),device(static_cast<ID3D12Device*>(d3dDevice)),queue(static_cast<ID3D12CommandQueue*>(d3dQueue)){
        try{
            if(!device||!queue)throw D3dVideoUnavailable("DirectX decoding requires a device and queue");
            ComPtr<IDXGIFactory4> factory;ComPtr<IDXGIAdapter1> adapter;
            checkDx(CreateDXGIFactory2(0,IID_PPV_ARGS(&factory)),"Create DirectX decoding factory");checkDx(factory->EnumAdapterByLuid(device->GetAdapterLuid(),IID_PPV_ARGS(&adapter)),"Select DirectX decoding adapter");
            checkDx(D3D11CreateDevice(adapter.Get(),D3D_DRIVER_TYPE_UNKNOWN,nullptr,D3D11_CREATE_DEVICE_VIDEO_SUPPORT,nullptr,0,D3D11_SDK_VERSION,&device11,nullptr,&context11),"Create shared DirectX decoding device");
            ComPtr<ID3D11Multithread> multithread;checkDx(context11.As(&multithread),"Protect DirectX decoder context");multithread->SetMultithreadProtected(TRUE);
            ComPtr<ID3D11Device5> device5;checkDx(device11.As(&device5),"Open DirectX decoder fence API");checkDx(context11.As(&context4),"Open DirectX decoder queue API");
            checkDx(device5->CreateFence(0,D3D11_FENCE_FLAG_SHARED,IID_PPV_ARGS(&decodeFence)),"Create shared decode fence");HANDLE handle=nullptr;
            checkDx(decodeFence->CreateSharedHandle(nullptr,GENERIC_ALL,nullptr,&handle),"Share decoder completion fence");
            const auto opened=device->OpenSharedHandle(handle,IID_PPV_ARGS(&ready));CloseHandle(handle);checkDx(opened,"Open decoder fence in DirectX 12");
            hardware=api.av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D11VA);if(!hardware)throw std::bad_alloc{};
            auto* dx=reinterpret_cast<AVD3D11VADeviceContext*>(reinterpret_cast<AVHWDeviceContext*>(hardware->data)->hwctx);dx->device=device11.Get();dx->device->AddRef();
            av::check(api.av_hwdevice_ctx_init(hardware),"Initialize shared DirectX decoder device");
            format=api.avformat_alloc_context();if(!format)throw std::bad_alloc{};format->interrupt_callback={[](void* context){return static_cast<JobControl*>(context)->cancelled()?1:0;},&control};
            av::check(api.avformat_open_input(&format,path.toUtf8().constData(),nullptr,nullptr),"Open DirectX decoder source");av::check(api.avformat_find_stream_info(format,nullptr),"Probe DirectX decoder source");
            stream=api.av_find_best_stream(format,AVMEDIA_TYPE_VIDEO,-1,-1,nullptr,0);av::check(stream,"Find DirectX decoder video stream");
            const auto* decoder=api.avcodec_find_decoder(format->streams[stream]->codecpar->codec_id);if(!decoder)throw D3dVideoUnavailable("Source video decoder is unavailable");
            bool supported=false;for(int i=0;;++i){const auto* config=api.avcodec_get_hw_config(decoder,i);if(!config)break;if(config->device_type==AV_HWDEVICE_TYPE_D3D11VA&&config->pix_fmt==AV_PIX_FMT_D3D11)supported=true;}
            if(!supported)throw D3dVideoUnavailable("Source codec has no DirectX hardware decoder");
            codec=api.avcodec_alloc_context3(decoder);if(!codec)throw std::bad_alloc{};av::check(api.avcodec_parameters_to_context(codec,format->streams[stream]->codecpar),"Describe shared DirectX decoder");
            codec->pkt_timebase=format->streams[stream]->time_base;codec->hw_device_ctx=api.av_buffer_ref(hardware);if(!codec->hw_device_ctx)throw std::bad_alloc{};
            codec->opaque=this;codec->get_format=select;codec->thread_count=1;av::check(api.avcodec_open2(codec,decoder,nullptr),"Open shared DirectX hardware decoder");
            packet=api.av_packet_alloc();if(!packet)throw std::bad_alloc{};const auto* track=format->streams[stream];
            if(track->start_time!=AV_NOPTS_VALUE)timestampOffset=track->start_time*av_q2d(track->time_base);else if(format->start_time!=AV_NOPTS_VALUE)timestampOffset=double(format->start_time)/AV_TIME_BASE;
            if(start>0)av::check(api.avformat_seek_file(format,-1,INT64_MIN,qint64((timestampOffset+std::max(0.,start-.002))*AV_TIME_BASE),INT64_MAX,0),"Seek shared DirectX decoder");
        }catch(...){release();throw;}
    }
    void wait(uint64_t value,bool cancellable){
        if(!value)return;if(ready->GetCompletedValue()==UINT64_MAX){unsafe=true;throw std::runtime_error("DirectX decoder device was removed");}if(ready->GetCompletedValue()>=value)return;
        HANDLE event=CreateEventW(nullptr,FALSE,FALSE,nullptr);if(!event){unsafe=true;throw std::runtime_error("Cannot wait for decoder GPU copy");}
        const auto status=ready->SetEventOnCompletion(value,event);if(FAILED(status)){CloseHandle(event);unsafe=true;checkDx(status,"Wait for decoder copy fence");}
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(30);
        try{for(;;){if(cancellable)control.checkpoint();const auto result=WaitForSingleObject(event,50);if(result==WAIT_OBJECT_0)break;
            if(result!=WAIT_TIMEOUT||FAILED(device->GetDeviceRemovedReason())||std::chrono::steady_clock::now()>deadline){unsafe=true;throw std::runtime_error("Decoder GPU copy did not finish");}}}
        catch(...){CloseHandle(event);throw;}CloseHandle(event);
    }
    void release(){api.av_packet_free(&packet);api.avcodec_free_context(&codec);api.av_buffer_unref(&hardware);if(format)api.avformat_close_input(&format);}
    ~Impl(){release();}
    TimeBase base()const{const auto value=format->streams[stream]->time_base;return{value.num,value.den};}
    std::shared_ptr<Output> copy(av::Frame frame){
        wait(sequence,true);for(auto& buffer:buffers)if(buffer->value<=ready->GetCompletedValue())buffer->decoded.reset();
        std::shared_ptr<Output> target;for(auto& buffer:buffers)if(buffer.use_count()==1){target=buffer;break;}
        if(!target){if(buffers.size()>=4)throw std::runtime_error("Shared DirectX decoder exhausted its bounded output pool");target=std::make_shared<Output>();buffers.push_back(target);}
        auto* texture=reinterpret_cast<ID3D11Texture2D*>(frame->data[0]);const auto slice=uint32_t(reinterpret_cast<uintptr_t>(frame->data[1]));
        D3D11_TEXTURE2D_DESC source{};texture->GetDesc(&source);if(source.Format!=DXGI_FORMAT_NV12&&source.Format!=DXGI_FORMAT_P010)throw D3dVideoUnavailable("Shared DirectX decoder requires NV12 or P010");
        if(slice>=source.ArraySize)throw std::runtime_error("Decoded DirectX slice is outside the texture array");
        if(target->texture){const auto desc=target->texture->GetDesc();if(desc.Width!=source.Width||desc.Height!=source.Height||desc.Format!=source.Format){target->texture.Reset();target->shared11.Reset();}}
        if(!target->texture){auto desc=source;desc.ArraySize=1;desc.MipLevels=1;desc.BindFlags=D3D11_BIND_SHADER_RESOURCE;desc.MiscFlags=D3D11_RESOURCE_MISC_SHARED|D3D11_RESOURCE_MISC_SHARED_NTHANDLE;
            checkDx(device11->CreateTexture2D(&desc,nullptr,&target->shared11),"Allocate shared decoded video texture");
            ComPtr<IDXGIResource1> shared;checkDx(target->shared11.As(&shared),"Open decoded texture sharing");HANDLE handle=nullptr;
            checkDx(shared->CreateSharedHandle(nullptr,DXGI_SHARED_RESOURCE_READ|DXGI_SHARED_RESOURCE_WRITE,nullptr,&handle),"Share decoded GPU texture");
            const auto opened=device->OpenSharedHandle(handle,IID_PPV_ARGS(&target->texture));CloseHandle(handle);checkDx(opened,"Open decoded texture in DirectX 12");}
        // Decoder array allocations have driver-specific restrictions. Copy the
        // selected slice into a normal shared texture before crossing APIs.
        context11->CopySubresourceRegion(target->shared11.Get(),0,0,0,0,texture,slice,nullptr);
        const auto value=sequence+1;target->decoded=std::move(frame);target->fence=ready;target->value=value;sequence=value;
        const auto signaled=context4->Signal(decodeFence.Get(),value);if(FAILED(signaled))unsafe=true;checkDx(signaled,"Signal copied DirectX decode");context11->Flush();return target;
    }
};
D3d11VideoDecoder::D3d11VideoDecoder(const QString& path,void* device,void* queue,double start,JobControl& control):impl_(std::make_unique<Impl>(path,device,queue,start,control)){}
D3d11VideoDecoder::~D3d11VideoDecoder(){if(impl_){try{impl_->wait(impl_->sequence,false);}catch(...){impl_->unsafe=true;}if(impl_->unsafe)(void)impl_.release();}}
double D3d11VideoDecoder::timeOrigin()const{if(impl_->origin==AV_NOPTS_VALUE)throw std::logic_error("Read a decoded frame before its time origin");const auto base=impl_->base();return impl_->origin*double(base.numerator)/base.denominator;}
std::optional<D3dVideoFrame> D3d11VideoDecoder::read(){
    auto& s=*impl_;for(;;){s.control.checkpoint();auto frame=av::frame();const auto status=s.api.avcodec_receive_frame(s.codec,frame.get());
        if(status>=0){if(frame->format!=AV_PIX_FMT_D3D11||!frame->data[0])throw D3dVideoUnavailable("Decoder did not return DirectX 11 GPU video");
            const auto pts=frame->best_effort_timestamp!=AV_NOPTS_VALUE?frame->best_effort_timestamp:frame->pts;if(pts==AV_NOPTS_VALUE)throw std::runtime_error("DirectX decoded frame has no presentation timestamp");
            const auto base=s.base();if(pts*double(base.numerator)/base.denominator+.002<s.start+s.timestampOffset)continue;if(s.origin==AV_NOPTS_VALUE)s.origin=pts;
            D3dVideoFrame result;result.size={frame->width,frame->height};result.pts=pts-s.origin;result.duration=frame->duration;result.timeBase=base;
            result.colorMatrix=frame->colorspace==AVCOL_SPC_BT2020_NCL?2u:frame->colorspace==AVCOL_SPC_FCC?3u:frame->colorspace==AVCOL_SPC_SMPTE240M?4u:frame->colorspace==AVCOL_SPC_SMPTE170M||frame->colorspace==AVCOL_SPC_BT470BG?0u:1u;
            result.colorRange=frame->color_range==AVCOL_RANGE_JPEG?1u:0u;result.chromaLocation=frame->chroma_location==AVCHROMA_LOC_UNSPECIFIED?1u:uint32_t(frame->chroma_location);
            auto target=s.copy(std::move(frame));result.texture=target->texture.Get();result.readyFence=target->fence.Get();result.readyValue=target->value;
            result.pixelFormat=target->texture->GetDesc().Format==DXGI_FORMAT_P010?VE_FSR_P010:VE_FSR_NV12;result.owner=std::move(target);return result;
        }
        if(status==AVERROR_EOF)return{};if(status!=AVERROR(EAGAIN))av::check(status,"Receive shared DirectX frame");if(s.draining)throw std::runtime_error("Shared DirectX decoder did not finish draining");
        for(;;){s.api.av_packet_unref(s.packet);const auto read=s.api.av_read_frame(s.format,s.packet);if(read==AVERROR_EOF){av::check(s.api.avcodec_send_packet(s.codec,nullptr),"Drain shared DirectX decoder");s.draining=true;break;}
            av::check(read,"Read shared DirectX packet");if(s.packet->stream_index==s.stream){const auto sent=s.api.avcodec_send_packet(s.codec,s.packet);if(sent<0&&!s.poolError.empty())throw D3dVideoUnavailable(s.poolError);av::check(sent,"Submit shared DirectX decoding packet");break;}}
    }
}
}
