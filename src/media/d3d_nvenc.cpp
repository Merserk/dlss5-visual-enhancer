#include "d3d_nvenc.hpp"
#include "d3d_video.hpp"
#include "av_api.hpp"
#include "host_video.hpp"
#include "configuration/settings.hpp"
#include "processing/fsr_native.hpp"
#include <nvEncodeAPI.h>
#include <d3d12.h>
#include <wrl/client.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <deque>
#include <limits>
#include <map>

namespace ve {
namespace {
using Microsoft::WRL::ComPtr;
void dxCheck(HRESULT status,const char* operation){if(FAILED(status))throw std::runtime_error(std::string(operation)+" (HRESULT "+std::to_string(uint32_t(status))+")");}
struct Event {
    HANDLE handle=CreateEventW(nullptr,FALSE,FALSE,nullptr);
    Event(){if(!handle)throw std::runtime_error("Cannot allocate NVENC completion event");}
    ~Event(){CloseHandle(handle);}
};
constexpr AVRational clockBase{1,60000000};
}
struct D3dNvEncoder::Impl {
    av::Api& av=av::Api::get();JobControl& control;MediaInfo info;
    ComPtr<ID3D12Device> device;ComPtr<ID3D12CommandQueue> queue;ComPtr<ID3D12Fence> inputFence,outputFence;
    HMODULE library=nullptr;NV_ENCODE_API_FUNCTION_LIST nv{};void* encoder=nullptr;
    NV_ENC_CONFIG config{};NV_ENC_INITIALIZE_PARAMS init{};GUID codec{};
    bool ten=false,finished=false,eos=false,quarantined=false,initialized=false;
    uint64_t sent=0,received=0,fenceValue=0;size_t capacity=0;
    AVFormatContext* format=nullptr;AVStream* stream=nullptr;AVPacket* packet=nullptr;
    std::map<qint64,qint64> durations;
    std::array<std::vector<uint8_t>,2> hdrBytes;
    std::vector<NV_ENC_SEI_PAYLOAD> hdrPayloads;
    struct Slot {
        ComPtr<ID3D12Resource> input,output;
        NV_ENC_REGISTERED_PTR inputRegistration=nullptr,outputRegistration=nullptr;
        NV_ENC_INPUT_PTR mappedInput=nullptr,mappedOutput=nullptr;
        NV_ENC_INPUT_RESOURCE_D3D12 in{};NV_ENC_OUTPUT_RESOURCE_D3D12 out{};
        Event event;bool eventRegistered=false,locked=false,completed=false;
    };
    std::vector<std::unique_ptr<Slot>> pool_;
    std::deque<Slot*> pending,free;
    void check(NVENCSTATUS status,const char* operation) const {
        if(status==NV_ENC_SUCCESS)return;
        const char* detail=encoder&&nv.nvEncGetLastErrorString?nv.nvEncGetLastErrorString(encoder):nullptr;
        throw D3dVideoUnavailable(std::string(operation)+" (NVENC "+std::to_string(status)+")"+(detail?std::string(": ")+detail:""));
    }
    int cap(NV_ENC_CAPS id){NV_ENC_CAPS_PARAM query{};query.version=NV_ENC_CAPS_PARAM_VER;query.capsToQuery=id;int result=0;check(nv.nvEncGetEncodeCaps(encoder,codec,&query,&result),"Query DirectX NVENC capability");return result;}
    Impl(const QString& path,const QString& name,const Settings& settings,const MediaInfo& output,
        void* d3dDevice,void* d3dQueue,JobControl& job,bool allIntra):control(job),info(output),device(static_cast<ID3D12Device*>(d3dDevice)),queue(static_cast<ID3D12CommandQueue*>(d3dQueue)) {
        AVDictionary* options=nullptr;
        try {
            if(!device||!queue||info.width<2||info.height<2||(info.width&1)||(info.height&1)||!(info.fps>0))throw D3dVideoUnavailable("DirectX NVENC requires a device, queue and even video dimensions");
            library=LoadLibraryExW(L"nvEncodeAPI64.dll",nullptr,LOAD_LIBRARY_SEARCH_SYSTEM32);
            if(!library)throw D3dVideoUnavailable("NVIDIA's DirectX encoder driver is unavailable");
            const auto create=reinterpret_cast<NVENCSTATUS(NVENCAPI*)(NV_ENCODE_API_FUNCTION_LIST*)>(GetProcAddress(library,"NvEncodeAPICreateInstance"));
            const auto maximum=reinterpret_cast<NVENCSTATUS(NVENCAPI*)(uint32_t*)>(GetProcAddress(library,"NvEncodeAPIGetMaxSupportedVersion"));
            if(!create||!maximum)throw D3dVideoUnavailable("NVIDIA encoder driver exports are incomplete");
            uint32_t driver=0;check(maximum(&driver),"Query NVENC driver API");
            if(driver<((NVENCAPI_MAJOR_VERSION<<4)|NVENCAPI_MINOR_VERSION))throw D3dVideoUnavailable("DirectX encoding requires NVIDIA Video Codec API 12.2 or newer");
            nv.version=NV_ENCODE_API_FUNCTION_LIST_VER;check(create(&nv),"Load DirectX NVENC API");
            NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS open{};open.version=NV_ENC_OPEN_ENCODE_SESSION_EX_PARAMS_VER;
            open.device=device.Get();open.deviceType=NV_ENC_DEVICE_TYPE_DIRECTX;open.apiVersion=NVENCAPI_VERSION;
            check(nv.nvEncOpenEncodeSessionEx(&open,&encoder),"Open DirectX 12 NVENC session");
            const bool h264=name.startsWith("h264",Qt::CaseInsensitive)||name.startsWith("H.264");
            const bool av1=name.startsWith("av1",Qt::CaseInsensitive);
            codec=h264?NV_ENC_CODEC_H264_GUID:av1?NV_ENC_CODEC_AV1_GUID:NV_ENC_CODEC_HEVC_GUID;
            uint32_t count=0;check(nv.nvEncGetEncodeGUIDCount(encoder,&count),"Count NVENC codecs");
            std::vector<GUID> guids(count);check(nv.nvEncGetEncodeGUIDs(encoder,guids.data(),count,&count),"Enumerate NVENC codecs");
            if(std::none_of(guids.begin(),guids.end(),[&](const GUID& value){return value==codec;}))throw D3dVideoUnavailable("The selected NVIDIA GPU cannot encode this codec");
            if(info.width>cap(NV_ENC_CAPS_WIDTH_MAX)||info.height>cap(NV_ENC_CAPS_HEIGHT_MAX))throw D3dVideoUnavailable("Output exceeds this NVIDIA encoder's dimensions");
            ten=!h264&&(info.hdr||settings.hdr_mode||info.precisionBits()>8);
            if(ten&&!cap(NV_ENC_CAPS_SUPPORT_10BIT_ENCODE))throw D3dVideoUnavailable("The selected NVIDIA encoder cannot preserve 10-bit video");
            if(h264&&info.hdr)throw D3dVideoUnavailable("HDR DirectX output requires HEVC or AV1");
            const auto pixel=ten?NV_ENC_BUFFER_FORMAT_YUV420_10BIT:NV_ENC_BUFFER_FORMAT_NV12;
            check(nv.nvEncGetInputFormatCount(encoder,codec,&count),"Count NVENC surface formats");
            std::vector<NV_ENC_BUFFER_FORMAT> formats(count);check(nv.nvEncGetInputFormats(encoder,codec,formats.data(),count,&count),"Enumerate NVENC surface formats");
            if(std::find(formats.begin(),formats.end(),pixel)==formats.end())throw D3dVideoUnavailable("The selected NVIDIA encoder cannot accept the required DirectX surface");
            NV_ENC_PRESET_CONFIG preset{};preset.version=NV_ENC_PRESET_CONFIG_VER;preset.presetCfg.version=NV_ENC_CONFIG_VER;
            check(nv.nvEncGetEncodePresetConfigEx(encoder,codec,NV_ENC_PRESET_P7_GUID,NV_ENC_TUNING_INFO_HIGH_QUALITY,&preset),"Select NVENC P7 high-quality preset");
            config=preset.presetCfg;config.version=NV_ENC_CONFIG_VER;
            config.profileGUID=h264?NV_ENC_H264_PROFILE_HIGH_GUID:av1?NV_ENC_AV1_PROFILE_MAIN_GUID:ten?NV_ENC_HEVC_PROFILE_MAIN10_GUID:NV_ENC_HEVC_PROFILE_MAIN_GUID;
            config.gopLength=std::max(1u,uint32_t(std::llround(info.fps*10)));
            config.frameIntervalP=std::min(3,cap(NV_ENC_CAPS_NUM_MAX_BFRAMES))+1;
            auto& rc=config.rcParams;rc.version=NV_ENC_RC_PARAMS_VER;rc.rateControlMode=NV_ENC_PARAMS_RC_VBR;
            rc.averageBitRate=0;rc.maxBitRate=0;rc.multiPass=NV_ENC_MULTI_PASS_DISABLED;
            const int quality=settings.quality=="Best"?2:settings.quality=="Good"?1:0;
            const int targets[3][3]={{26,22,18},{28,24,20},{34,29,24}};
            rc.targetQuality=uint8_t(settings.quality=="Max"?1:targets[h264?0:av1?2:1][quality]);rc.targetQualityLSB=0;
            if(allIntra){
                config.gopLength=1;config.frameIntervalP=1;
                rc.rateControlMode=NV_ENC_PARAMS_RC_CONSTQP;rc.constQP={1,1,1};rc.targetQuality=0;
            }
            // NVIDIA documents all AQ modes, lookahead and multipass as CUDA
            // preprocessing. Use high-quality core NVENC tools and unrestricted
            // quality-based VBR while keeping this route entirely DirectX.
            rc.enableAQ=0;rc.aqStrength=0;rc.enableTemporalAQ=0;
            rc.enableLookahead=0;rc.lookaheadDepth=0;
            const auto primaries=av.av_color_primaries_from_name(info.primaries.toUtf8().constData());
            const auto transfer=av.av_color_transfer_from_name(info.transfer.toUtf8().constData());
            NV_ENC_CONFIG_H264_VUI_PARAMETERS vui{};vui.videoSignalTypePresentFlag=1;vui.videoFormat=NV_ENC_VUI_VIDEO_FORMAT(5);vui.colourDescriptionPresentFlag=1;
            vui.colourPrimaries=NV_ENC_VUI_COLOR_PRIMARIES(primaries<0?(info.hdr?9:1):primaries);
            vui.transferCharacteristics=NV_ENC_VUI_TRANSFER_CHARACTERISTIC(transfer<0?(info.hdr?16:1):transfer);
            vui.colourMatrix=NV_ENC_VUI_MATRIX_COEFFS(info.hdr?9:1);vui.chromaSampleLocationFlag=1;vui.chromaSampleLocationTop=0;vui.chromaSampleLocationBot=0;
            const auto depth=ten?NV_ENC_BIT_DEPTH_10:NV_ENC_BIT_DEPTH_8;
            if(h264){auto& c=config.encodeCodecConfig.h264Config;c.idrPeriod=config.gopLength;c.chromaFormatIDC=1;c.h264VUIParameters=vui;c.outputBitDepth=depth;c.inputBitDepth=depth;c.repeatSPSPPS=1;}
            else if(av1){auto& c=config.encodeCodecConfig.av1Config;c.idrPeriod=config.gopLength;c.chromaFormatIDC=1;c.colorPrimaries=vui.colourPrimaries;c.transferCharacteristics=vui.transferCharacteristics;c.matrixCoefficients=vui.colourMatrix;c.colorRange=0;c.chromaSamplePosition=1;c.outputBitDepth=depth;c.inputBitDepth=depth;c.repeatSeqHdr=1;}
            else{auto& c=config.encodeCodecConfig.hevcConfig;c.idrPeriod=config.gopLength;c.chromaFormatIDC=1;c.hevcVUIParameters=vui;c.outputBitDepth=depth;c.inputBitDepth=depth;c.repeatSPSPPS=1;c.tfLevel=NV_ENC_TEMPORAL_FILTER_LEVEL_0;}
            init.version=NV_ENC_INITIALIZE_PARAMS_VER;init.encodeGUID=codec;init.presetGUID=NV_ENC_PRESET_P7_GUID;init.tuningInfo=NV_ENC_TUNING_INFO_HIGH_QUALITY;
            init.encodeWidth=uint32_t(info.width);init.encodeHeight=uint32_t(info.height);init.maxEncodeWidth=init.encodeWidth;init.maxEncodeHeight=init.encodeHeight;
            const auto aspect=info.outputSampleAspectRatio();init.darWidth=uint32_t(info.width*aspect.width());init.darHeight=uint32_t(info.height*aspect.height());
            const auto rate=av.av_d2q(info.fps,1000000);init.frameRateNum=uint32_t(rate.num);init.frameRateDen=uint32_t(rate.den);
            init.enablePTD=1;init.enableEncodeAsync=1;init.encodeConfig=&config;init.bufferFormat=pixel;
            check(nv.nvEncInitializeEncoder(encoder,&init),"Initialize DirectX 12 NVENC");initialized=true;
            capacity=size_t(rc.lookaheadDepth+config.frameIntervalP+4);
            dxCheck(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&inputFence)),"Create NVENC input fence");
            dxCheck(device->CreateFence(0,D3D12_FENCE_FLAG_NONE,IID_PPV_ARGS(&outputFence)),"Create NVENC output fence");
            av::check(av.avformat_alloc_output_context2(&format,nullptr,"mp4",path.toUtf8().constData()),"Create DirectX video muxer");format->avoid_negative_ts=AVFMT_AVOID_NEG_TS_DISABLED;
            stream=av.avformat_new_stream(format,nullptr);if(!stream)throw std::bad_alloc{};stream->time_base=clockBase;stream->avg_frame_rate=rate;
            auto* parameters=stream->codecpar;parameters->codec_type=AVMEDIA_TYPE_VIDEO;parameters->codec_id=h264?AV_CODEC_ID_H264:av1?AV_CODEC_ID_AV1:AV_CODEC_ID_HEVC;
            parameters->width=info.width;parameters->height=info.height;parameters->format=ten?AV_PIX_FMT_YUV420P10LE:AV_PIX_FMT_YUV420P;
            parameters->sample_aspect_ratio={aspect.width(),aspect.height()};parameters->color_range=AVCOL_RANGE_MPEG;
            parameters->color_space=info.hdr?AVCOL_SPC_BT2020_NCL:AVCOL_SPC_BT709;parameters->color_primaries=AVColorPrimaries(vui.colourPrimaries);parameters->color_trc=AVColorTransferCharacteristic(vui.transferCharacteristics);
            parameters->chroma_location=AVCHROMA_LOC_LEFT;parameters->video_delay=config.frameIntervalP>1?2:0;
            std::array<uint8_t,65536> sequence{};uint32_t bytes=0;NV_ENC_SEQUENCE_PARAM_PAYLOAD payload{};payload.version=NV_ENC_SEQUENCE_PARAM_PAYLOAD_VER;
            payload.spsppsBuffer=sequence.data();payload.inBufferSize=uint32_t(sequence.size());payload.outSPSPPSPayloadSize=&bytes;
            check(nv.nvEncGetSequenceParams(encoder,&payload),"Read DirectX NVENC sequence header");
            if(!bytes||bytes>sequence.size())throw D3dVideoUnavailable("NVIDIA returned an invalid sequence header");
            parameters->extradata=static_cast<uint8_t*>(av.av_malloc(bytes+AV_INPUT_BUFFER_PADDING_SIZE));if(!parameters->extradata)throw std::bad_alloc{};
            std::memcpy(parameters->extradata,sequence.data(),bytes);std::memset(parameters->extradata+bytes,0,AV_INPUT_BUFFER_PADDING_SIZE);parameters->extradata_size=int(bytes);
            retainStaticHdrMetadata(parameters,info);
            prepareHdr(parameters,av1);
            av::check(av.avio_open(&format->pb,path.toUtf8().constData(),AVIO_FLAG_WRITE),"Open DirectX video destination");
            av.av_dict_set(&options,"video_track_timescale","60000000",0);av::check(av.avformat_write_header(format,&options),"Write DirectX video header");av.av_dict_free(&options);
            packet=av.av_packet_alloc();if(!packet)throw std::bad_alloc{};
        }catch(...){av.av_dict_free(&options);release();throw;}
    }
    void prepareHdr(const AVCodecParameters* parameters,bool av1){
        const auto value=[](AVRational input,uint64_t scale,uint64_t limit){
            if(input.den<=0||input.num<0)throw std::runtime_error("Invalid static HDR rational");
            return uint32_t(std::min(limit,uint64_t(std::llround(double(input.num)*double(scale)/input.den))));
        };
        const auto append=[](std::vector<uint8_t>& bytes,uint32_t number,int count){for(int shift=(count-1)*8;shift>=0;shift-=8)bytes.push_back(uint8_t(number>>shift));};
        for(int i=0;i<parameters->nb_coded_side_data;++i){const auto& side=parameters->coded_side_data[i];
            if(side.type==AV_PKT_DATA_MASTERING_DISPLAY_METADATA){
                if(side.size<sizeof(AVMasteringDisplayMetadata))throw std::runtime_error("Truncated HDR mastering metadata");
                const auto& md=*reinterpret_cast<const AVMasteringDisplayMetadata*>(side.data);
                if(!md.has_primaries||!md.has_luminance)continue;auto& bytes=hdrBytes[0];bytes.clear();
                const auto scale=av1?65536u:50000u;const std::array<int,3> order=av1?std::array{0,1,2}:std::array{1,2,0};
                for(int color:order)for(int axis=0;axis<2;++axis)append(bytes,value(md.display_primaries[color][axis],scale,65535),2);
                for(int axis=0;axis<2;++axis)append(bytes,value(md.white_point[axis],scale,65535),2);
                append(bytes,value(md.max_luminance,av1?256:10000,UINT32_MAX),4);append(bytes,value(md.min_luminance,av1?16384:10000,UINT32_MAX),4);
                hdrPayloads.push_back({uint32_t(bytes.size()),av1?2u:137u,bytes.data()});
            }else if(side.type==AV_PKT_DATA_CONTENT_LIGHT_LEVEL){
                if(side.size<sizeof(AVContentLightMetadata))throw std::runtime_error("Truncated HDR light-level metadata");
                const auto& light=*reinterpret_cast<const AVContentLightMetadata*>(side.data);auto& bytes=hdrBytes[1];bytes.clear();
                append(bytes,std::min(light.MaxCLL,65535u),2);append(bytes,std::min(light.MaxFALL,65535u),2);
                hdrPayloads.push_back({uint32_t(bytes.size()),av1?1u:144u,bytes.data()});
            }
        }
    }
    void destroySlot(Slot& slot){
        if(slot.locked)nv.nvEncUnlockBitstream(encoder,&slot.out);
        if(slot.mappedInput)nv.nvEncUnmapInputResource(encoder,slot.mappedInput);
        if(slot.mappedOutput)nv.nvEncUnmapInputResource(encoder,slot.mappedOutput);
        if(slot.inputRegistration)nv.nvEncUnregisterResource(encoder,slot.inputRegistration);
        if(slot.outputRegistration)nv.nvEncUnregisterResource(encoder,slot.outputRegistration);
        if(slot.eventRegistered){NV_ENC_EVENT_PARAMS event{};event.version=NV_ENC_EVENT_PARAMS_VER;event.completionEvent=slot.event.handle;nv.nvEncUnregisterAsyncEvent(encoder,&event);}
    }
    void release(){
        // Destroying the session synchronizes accepted work. Keep its resources
        // alive until destruction completes, including exceptional/cancel paths.
        if(encoder){
            if(initialized&&!eos){NV_ENC_PIC_PARAMS end{};end.version=NV_ENC_PIC_PARAMS_VER;end.encodePicFlags=NV_ENC_PIC_FLAG_EOS;nv.nvEncEncodePicture(encoder,&end);eos=true;}
            for(auto& slot:pool_)destroySlot(*slot);
            nv.nvEncDestroyEncoder(encoder);encoder=nullptr;
        }
        pool_.clear();av.av_packet_free(&packet);
        if(format){if(format->pb)av.avio_closep(&format->pb);av.avformat_free_context(format);format=nullptr;}
        if(library){FreeLibrary(library);library=nullptr;}
    }
    void quiesce() noexcept {
        if(!encoder||pending.empty()||quarantined)return;
        if(!eos){NV_ENC_PIC_PARAMS end{};end.version=NV_ENC_PIC_PARAMS_VER;end.encodePicFlags=NV_ENC_PIC_FLAG_EOS;
            if(nv.nvEncEncodePicture(encoder,&end)!=NV_ENC_SUCCESS){quarantined=true;return;}eos=true;}
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(30);
        for(auto* slot:pending)while(!slot->completed){const auto status=WaitForSingleObject(slot->event.handle,50);
            if(status==WAIT_OBJECT_0){slot->completed=true;break;}
            if(status!=WAIT_TIMEOUT||FAILED(device->GetDeviceRemovedReason())||std::chrono::steady_clock::now()>deadline){quarantined=true;return;}}
    }
    ~Impl(){release();}
    Slot& allocate(){
        if(free.empty()&&pool_.size()>=capacity)receive();
        if(!free.empty()){auto* result=free.front();free.pop_front();return *result;}
        if(pool_.size()>=capacity)throw std::runtime_error("DirectX encoder exceeded its bounded frame pool");
        auto owned=std::make_unique<Slot>();auto& slot=*owned;pool_.push_back(std::move(owned));
        D3D12_HEAP_PROPERTIES heap{};heap.Type=D3D12_HEAP_TYPE_DEFAULT;
        D3D12_RESOURCE_DESC description{};description.Dimension=D3D12_RESOURCE_DIMENSION_TEXTURE2D;description.Width=uint64_t(info.width);description.Height=uint32_t(info.height);
        description.DepthOrArraySize=1;description.MipLevels=1;description.Format=ten?DXGI_FORMAT_P010:DXGI_FORMAT_NV12;description.SampleDesc.Count=1;
        dxCheck(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&description,D3D12_RESOURCE_STATE_COMMON,nullptr,IID_PPV_ARGS(&slot.input)),"Allocate DirectX NVENC input");
        const uint64_t outputBytes=std::max(uint64_t(1<<20),uint64_t(info.width)*uint64_t(info.height)*(ten?6:3));
        description={};description.Dimension=D3D12_RESOURCE_DIMENSION_BUFFER;description.Width=(outputBytes+255)&~uint64_t(255);description.Height=1;description.DepthOrArraySize=1;description.MipLevels=1;description.SampleDesc.Count=1;description.Layout=D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        heap.Type=D3D12_HEAP_TYPE_READBACK;dxCheck(device->CreateCommittedResource(&heap,D3D12_HEAP_FLAG_NONE,&description,D3D12_RESOURCE_STATE_COPY_DEST,nullptr,IID_PPV_ARGS(&slot.output)),"Allocate NVENC compressed output");
        const auto registration=[&](ID3D12Resource* resource,NV_ENC_BUFFER_USAGE usage,NV_ENC_BUFFER_FORMAT pixel,uint32_t width,uint32_t height,NV_ENC_REGISTERED_PTR& target){
            NV_ENC_REGISTER_RESOURCE request{};request.version=NV_ENC_REGISTER_RESOURCE_VER;request.resourceType=NV_ENC_INPUT_RESOURCE_TYPE_DIRECTX;request.resourceToRegister=resource;request.width=width;request.height=height;request.bufferUsage=usage;request.bufferFormat=pixel;
            check(nv.nvEncRegisterResource(encoder,&request),"Register DirectX NVENC resource");target=request.registeredResource;
        };
        registration(slot.input.Get(),NV_ENC_INPUT_IMAGE,ten?NV_ENC_BUFFER_FORMAT_YUV420_10BIT:NV_ENC_BUFFER_FORMAT_NV12,uint32_t(info.width),uint32_t(info.height),slot.inputRegistration);
        registration(slot.output.Get(),NV_ENC_OUTPUT_BITSTREAM,NV_ENC_BUFFER_FORMAT_U8,uint32_t(description.Width),1,slot.outputRegistration);
        NV_ENC_EVENT_PARAMS event{};event.version=NV_ENC_EVENT_PARAMS_VER;event.completionEvent=slot.event.handle;check(nv.nvEncRegisterAsyncEvent(encoder,&event),"Register NVENC completion event");slot.eventRegistered=true;
        return slot;
    }
    NV_ENC_INPUT_PTR map(NV_ENC_REGISTERED_PTR resource){NV_ENC_MAP_INPUT_RESOURCE request{};request.version=NV_ENC_MAP_INPUT_RESOURCE_VER;request.registeredResource=resource;check(nv.nvEncMapInputResource(encoder,&request),"Map DirectX NVENC resource");return request.mappedResource;}
    void receive(){
        if(pending.empty())throw std::logic_error("DirectX NVENC output has no pending input");auto& slot=*pending.front();
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(60);
        while(!slot.completed){control.checkpoint();const auto status=WaitForSingleObject(slot.event.handle,50);if(status==WAIT_OBJECT_0)break;
            if(status!=WAIT_TIMEOUT||FAILED(device->GetDeviceRemovedReason())||std::chrono::steady_clock::now()>deadline){quarantined=true;throw std::runtime_error("DirectX NVENC did not complete its submitted frame");}}
        slot.completed=true;
        NV_ENC_LOCK_BITSTREAM lock{};lock.version=NV_ENC_LOCK_BITSTREAM_VER;lock.outputBitstream=&slot.out;lock.doNotWait=1;
        check(nv.nvEncLockBitstream(encoder,&lock),"Read DirectX NVENC compressed output");slot.locked=true;
        if(lock.bitstreamSizeInBytes>uint32_t(std::numeric_limits<int>::max()))throw std::runtime_error("NVENC bitstream packet is too large");
        av::check(av.av_new_packet(packet,int(lock.bitstreamSizeInBytes)),"Allocate DirectX video packet");std::memcpy(packet->data,lock.bitstreamBufferPtr,lock.bitstreamSizeInBytes);
        packet->pts=qint64(lock.outputTimeStamp);
        const auto found=durations.find(packet->pts);if(found==durations.end())throw std::runtime_error("NVENC output timestamp does not match a submitted frame");packet->duration=found->second;durations.erase(found);
        // B frames are emitted in decoding order. Match the two-frame reorder
        // delay advertised by H.264/HEVC/AV1 with B frames, without shifting PTS.
        const qint64 delay=config.frameIntervalP>1?2:0;
        packet->dts=av.av_rescale_q(qint64(received)-delay,{int(init.frameRateDen),int(init.frameRateNum)},clockBase);
        packet->flags=(lock.pictureType==NV_ENC_PIC_TYPE_IDR||lock.pictureType==NV_ENC_PIC_TYPE_I)?AV_PKT_FLAG_KEY:0;
        check(nv.nvEncUnlockBitstream(encoder,&slot.out),"Unlock DirectX video packet");slot.locked=false;
        check(nv.nvEncUnmapInputResource(encoder,slot.mappedInput),"Release DirectX NVENC input mapping");slot.mappedInput=nullptr;
        check(nv.nvEncUnmapInputResource(encoder,slot.mappedOutput),"Release DirectX NVENC output mapping");slot.mappedOutput=nullptr;
        av.av_packet_rescale_ts(packet,clockBase,stream->time_base);packet->stream_index=stream->index;
        av::check(av.av_interleaved_write_frame(format,packet),"Mux DirectX NVENC packet");av.av_packet_unref(packet);
        pending.pop_front();free.push_back(&slot);++received;
    }
};
D3dNvEncoder::D3dNvEncoder(const QString& path,const QString& codec,const Settings& settings,const MediaInfo& output,void* device,void* queue,JobControl& control,bool allIntra)
    :impl_(std::make_unique<Impl>(path,codec,settings,output,device,queue,control,allIntra)){}
D3dNvEncoder::~D3dNvEncoder(){if(impl_)impl_->quiesce();if(impl_&&impl_->quarantined){
    // A failed fence cannot establish resource safety. Worker termination reclaims
    // the driver session; do not release resources still referenced by GPU work.
    (void)impl_.release();
}}
bool D3dNvEncoder::tenBit()const{return impl_->ten;}
QString D3dNvEncoder::diagnostics()const{return QString("NVENC DirectX 12; P7 HQ; %1-bit; %2 B frames; lookahead %3; GPU frame pool %4/%5").arg(impl_->ten?10:8).arg(impl_->config.frameIntervalP-1).arg(impl_->config.rcParams.lookaheadDepth).arg(impl_->pool_.size()).arg(impl_->capacity);}
void D3dNvEncoder::write(const FsrSurface& surface,qint64 pts,qint64 duration,TimeBase timeBase,bool forceIntra){
    auto& s=*impl_;s.control.checkpoint();if(s.eos)throw std::logic_error("Cannot write after DirectX NVENC drain");auto& slot=s.allocate();
    VeFsrVideoOutput output{};output.size=sizeof(output);output.abi=VE_FSR_ABI_VERSION;output.width=uint32_t(s.info.width);output.height=uint32_t(s.info.height);
    output.texture=slot.input.Get();output.resource_state=D3D12_RESOURCE_STATE_COMMON;output.pixel_format=s.ten?VE_FSR_P010:VE_FSR_NV12;
    output.transfer=fsrTransfer(s.info.transfer);output.primaries=fsrPrimaries(s.info.primaries);output.color_matrix=s.info.hdr?2u:1u;output.color_range=0;output.chroma_location=1;
    surface.writeVideo(output);const auto value=++s.fenceValue;dxCheck(s.queue->Signal(s.inputFence.Get(),value),"Signal DirectX encoder input readiness");
    slot.mappedInput=s.map(slot.inputRegistration);slot.mappedOutput=s.map(slot.outputRegistration);
    slot.in={};slot.in.version=NV_ENC_INPUT_RESOURCE_D3D12_VER;slot.in.pInputBuffer=slot.mappedInput;slot.in.inputFencePoint.version=NV_ENC_FENCE_POINT_D3D12_VER;
    slot.in.inputFencePoint.pFence=s.inputFence.Get();slot.in.inputFencePoint.waitValue=value;slot.in.inputFencePoint.bWait=1;
    slot.out={};slot.out.version=NV_ENC_OUTPUT_RESOURCE_D3D12_VER;slot.out.pOutputBuffer=slot.mappedOutput;slot.out.outputFencePoint.version=NV_ENC_FENCE_POINT_D3D12_VER;
    slot.out.outputFencePoint.pFence=s.outputFence.Get();slot.out.outputFencePoint.signalValue=value;slot.out.outputFencePoint.bSignal=1;
    NV_ENC_PIC_PARAMS picture{};picture.version=NV_ENC_PIC_PARAMS_VER;picture.inputWidth=uint32_t(s.info.width);picture.inputHeight=uint32_t(s.info.height);picture.frameIdx=uint32_t(s.sent);
    picture.inputBuffer=&slot.in;picture.outputBitstream=&slot.out;picture.bufferFmt=s.ten?NV_ENC_BUFFER_FORMAT_YUV420_10BIT:NV_ENC_BUFFER_FORMAT_NV12;picture.pictureStruct=NV_ENC_PIC_STRUCT_FRAME;picture.completionEvent=slot.event.handle;
    picture.inputTimeStamp=uint64_t(s.av.av_rescale_q(pts,{timeBase.numerator,timeBase.denominator},clockBase));picture.inputDuration=uint64_t(s.av.av_rescale_q(duration,{timeBase.numerator,timeBase.denominator},clockBase));
    if(!picture.inputDuration)throw std::runtime_error("DirectX encoder frame duration must be positive");
    if(forceIntra)picture.encodePicFlags=NV_ENC_PIC_FLAG_FORCEIDR;
    if(!s.hdrPayloads.empty()&&(s.sent%s.config.gopLength==0||forceIntra)){
        if(s.codec==NV_ENC_CODEC_AV1_GUID){picture.codecPicParams.av1PicParams.obuPayloadArray=s.hdrPayloads.data();picture.codecPicParams.av1PicParams.obuPayloadArrayCnt=uint32_t(s.hdrPayloads.size());}
        else if(s.codec==NV_ENC_CODEC_HEVC_GUID){picture.codecPicParams.hevcPicParams.seiPayloadArray=s.hdrPayloads.data();picture.codecPicParams.hevcPicParams.seiPayloadArrayCnt=uint32_t(s.hdrPayloads.size());}
    }
    if(!s.durations.emplace(qint64(picture.inputTimeStamp),qint64(picture.inputDuration)).second)throw std::runtime_error("DirectX encoder timestamps must be unique");
    slot.completed=false;s.pending.push_back(&slot);
    const auto status=s.nv.nvEncEncodePicture(s.encoder,&picture);if(status!=NV_ENC_SUCCESS&&status!=NV_ENC_ERR_NEED_MORE_INPUT){s.quarantined=true;const auto operation=std::string("Submit DirectX NVENC frame ")+std::to_string(s.sent)+" (HDR messages "+std::to_string(s.hdrPayloads.size())+")";s.check(status,operation.c_str());}
    ++s.sent;
    // With B frames one successful submission can make several earlier output
    // buffers available. Drain every completed buffer, in encoding order.
    while(!s.pending.empty()){
        auto* front=s.pending.front();const auto ready=WaitForSingleObject(front->event.handle,0);
        if(ready==WAIT_TIMEOUT)break;if(ready!=WAIT_OBJECT_0)throw std::runtime_error("Cannot inspect DirectX encoder completion");front->completed=true;s.receive();
    }
}
void D3dNvEncoder::finish(){auto& s=*impl_;if(s.finished)return;s.control.checkpoint();NV_ENC_PIC_PARAMS end{};end.version=NV_ENC_PIC_PARAMS_VER;end.encodePicFlags=NV_ENC_PIC_FLAG_EOS;
    s.check(s.nv.nvEncEncodePicture(s.encoder,&end),"Drain DirectX NVENC");s.eos=true;while(!s.pending.empty())s.receive();
    if(s.sent!=s.received||!s.durations.empty())throw std::runtime_error("DirectX encoder lost submitted frames");av::check(s.av.av_write_trailer(s.format),"Finish DirectX video");s.finished=true;
}
}
