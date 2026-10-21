#include "d3d_video.hpp"
#include "av_api.hpp"
#include "fsr_frame_generation/fsr_bridge.h"
extern "C" {
#include <libavutil/hwcontext_d3d12va.h>
}
#include <algorithm>
#include <cmath>

namespace ve {
namespace {
AVPixelFormat selectD3d12(AVCodecContext*,const AVPixelFormat* formats){for(auto* format=formats;*format!=AV_PIX_FMT_NONE;++format)if(*format==AV_PIX_FMT_D3D12)return *format;return AV_PIX_FMT_NONE;}
}
struct D3dVideoDecoder::Impl {
    av::Api& api=av::Api::get();JobControl& control;AVFormatContext* format=nullptr;AVCodecContext* codec=nullptr;
    AVBufferRef* hardware=nullptr;AVPacket* packet=nullptr;int stream=-1;bool draining=false;
    double start=0,timestampOffset=0;qint64 origin=AV_NOPTS_VALUE;
    Impl(const QString& path,void* device,double seconds,JobControl& job):control(job),start(seconds){
        try{
            // The public D3D12 frame descriptor gained an array-slice member in
            // FFmpeg 8.1. Require the packaged ABI rather than reading old data
            // at the new synchronization offsets.
            if(api.avutil_version()<AV_VERSION_INT(60,26,100))throw D3dVideoUnavailable("DirectX 12 video decoding requires the updated FFmpeg 8.1 media runtime");
            if(!device)throw D3dVideoUnavailable("A DirectX 12 decoding device is required");format=api.avformat_alloc_context();if(!format)throw std::bad_alloc{};
            format->interrupt_callback={[](void* context){return static_cast<JobControl*>(context)->cancelled()?1:0;},&control};
            av::check(api.avformat_open_input(&format,path.toUtf8().constData(),nullptr,nullptr),"Open DirectX video source");
            av::check(api.avformat_find_stream_info(format,nullptr),"Probe DirectX video source");stream=api.av_find_best_stream(format,AVMEDIA_TYPE_VIDEO,-1,-1,nullptr,0);av::check(stream,"Find DirectX video stream");
            const auto* decoder=api.avcodec_find_decoder(format->streams[stream]->codecpar->codec_id);bool compatible=false;
            for(int i=0;decoder;++i){const auto* config=api.avcodec_get_hw_config(decoder,i);if(!config)break;if(config->device_type==AV_HWDEVICE_TYPE_D3D12VA&&config->pix_fmt==AV_PIX_FMT_D3D12)compatible=true;}
            if(!compatible)throw D3dVideoUnavailable("The source codec has no DirectX 12 hardware decoder");
            hardware=api.av_hwdevice_ctx_alloc(AV_HWDEVICE_TYPE_D3D12VA);if(!hardware)throw std::bad_alloc{};
            auto* context=reinterpret_cast<AVHWDeviceContext*>(hardware->data);auto* dx=reinterpret_cast<AVD3D12VADeviceContext*>(context->hwctx);
            dx->device=static_cast<ID3D12Device*>(device);dx->device->AddRef();av::check(api.av_hwdevice_ctx_init(hardware),"Initialize DirectX video decoding device");
            codec=api.avcodec_alloc_context3(decoder);if(!codec)throw std::bad_alloc{};av::check(api.avcodec_parameters_to_context(codec,format->streams[stream]->codecpar),"Describe DirectX video decoder");
            codec->pkt_timebase=format->streams[stream]->time_base;codec->hw_device_ctx=api.av_buffer_ref(hardware);if(!codec->hw_device_ctx)throw std::bad_alloc{};
            codec->get_format=selectD3d12;codec->thread_count=1;av::check(api.avcodec_open2(codec,decoder,nullptr),"Open DirectX 12 hardware decoder");
            packet=api.av_packet_alloc();if(!packet)throw std::bad_alloc{};const auto* track=format->streams[stream];
            if(track->start_time!=AV_NOPTS_VALUE)timestampOffset=track->start_time*av_q2d(track->time_base);else if(format->start_time!=AV_NOPTS_VALUE)timestampOffset=double(format->start_time)/AV_TIME_BASE;
            if(start>0)av::check(api.avformat_seek_file(format,-1,INT64_MIN,qint64((timestampOffset+std::max(0.,start-.002))*AV_TIME_BASE),INT64_MAX,0),"Seek DirectX video source");
        }catch(const Cancelled&){release();throw;}catch(const std::exception& error){release();throw D3dVideoUnavailable(error.what());}
    }
    void release(){api.av_packet_free(&packet);api.avcodec_free_context(&codec);api.av_buffer_unref(&hardware);if(format)api.avformat_close_input(&format);}
    ~Impl(){release();}
    TimeBase base()const{const auto value=format->streams[stream]->time_base;return{value.num,value.den};}
};
D3dVideoDecoder::D3dVideoDecoder(const QString& path,void* device,double start,JobControl& control):impl_(std::make_unique<Impl>(path,device,start,control)){}
D3dVideoDecoder::~D3dVideoDecoder()=default;
double D3dVideoDecoder::timeOrigin()const{if(impl_->origin==AV_NOPTS_VALUE)throw std::logic_error("Read a DirectX frame before querying its time origin");const auto base=impl_->base();return impl_->origin*double(base.numerator)/base.denominator;}
std::optional<D3dVideoFrame> D3dVideoDecoder::read(){
    auto& s=*impl_;for(;;){s.control.checkpoint();auto frame=av::frame();const int status=s.api.avcodec_receive_frame(s.codec,frame.get());
        if(status>=0){
            if(frame->format!=AV_PIX_FMT_D3D12||!frame->data[0]||!frame->hw_frames_ctx)throw D3dVideoUnavailable("DirectX decoder did not return GPU video surfaces");
            const auto* pool=reinterpret_cast<const AVHWFramesContext*>(frame->hw_frames_ctx->data);
            if(pool->sw_format!=AV_PIX_FMT_NV12&&pool->sw_format!=AV_PIX_FMT_P010LE)throw D3dVideoUnavailable("DirectX FSR input requires NV12 or P010 video surfaces");
            auto* dx=reinterpret_cast<AVD3D12VAFrame*>(frame->data[0]);if(!dx->texture||!dx->sync_ctx.fence)throw std::runtime_error("DirectX decoder returned incomplete surface synchronization");
            if(dx->subresource_index!=0||(dx->flags&AV_D3D12VA_FRAME_FLAG_TEXTURE_ARRAY))throw D3dVideoUnavailable("DirectX decoder texture arrays require an explicit video slice copy");
            const auto pts=frame->best_effort_timestamp!=AV_NOPTS_VALUE?frame->best_effort_timestamp:frame->pts;if(pts==AV_NOPTS_VALUE)throw std::runtime_error("Decoded DirectX frame has no presentation timestamp");
            const auto base=s.base();if(pts*double(base.numerator)/base.denominator+.002<s.start+s.timestampOffset)continue;if(s.origin==AV_NOPTS_VALUE)s.origin=pts;
            D3dVideoFrame result;result.size={frame->width,frame->height};result.pixelFormat=pool->sw_format==AV_PIX_FMT_P010LE?VE_FSR_P010:VE_FSR_NV12;
            result.texture=dx->texture;result.readyFence=dx->sync_ctx.fence;result.readyValue=dx->sync_ctx.fence_value;result.pts=pts-s.origin;result.duration=frame->duration;result.timeBase=base;
            result.colorMatrix=frame->colorspace==AVCOL_SPC_BT2020_NCL?2u:frame->colorspace==AVCOL_SPC_FCC?3u:frame->colorspace==AVCOL_SPC_SMPTE240M?4u:
                frame->colorspace==AVCOL_SPC_SMPTE170M||frame->colorspace==AVCOL_SPC_BT470BG?0u:1u;
            result.colorRange=frame->color_range==AVCOL_RANGE_JPEG?1u:0u;result.chromaLocation=frame->chroma_location==AVCHROMA_LOC_UNSPECIFIED?1u:uint32_t(frame->chroma_location);
            result.owner=std::shared_ptr<void>(frame.release(),[](void* pointer){auto* owned=static_cast<AVFrame*>(pointer);av::Api::get().av_frame_free(&owned);});return result;
        }
        if(status==AVERROR_EOF)return{};if(status!=AVERROR(EAGAIN))av::check(status,"Receive DirectX decoded frame");if(s.draining)throw std::runtime_error("DirectX video decoder did not finish draining");
        for(;;){s.api.av_packet_unref(s.packet);const int read=s.api.av_read_frame(s.format,s.packet);
            if(read==AVERROR_EOF){av::check(s.api.avcodec_send_packet(s.codec,nullptr),"Drain DirectX decoder");s.draining=true;break;}
            av::check(read,"Read DirectX video packet");if(s.packet->stream_index==s.stream){av::check(s.api.avcodec_send_packet(s.codec,s.packet),"Submit DirectX video packet");break;}}
    }
}
}
