#include "cuda_video.hpp"
#include "host_video.hpp"
#include "av_api.hpp"
#include "configuration/settings.hpp"
#include "infrastructure/windows_runtime.hpp"
#include <QLibrary>
#include <utility>
#include <cmath>
#include <map>
#include <mutex>
#include <cstring>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_cuda.h>
#include <libavutil/pixdesc.h>
#include <libavutil/mathematics.h>
}

namespace ve {
namespace {
using av::Api;
using av::check;
struct CudaFrameDriver {
    QLibrary library{QString::fromStdWString(windowsSystemLibrary(L"nvcuda.dll"))};
    template<class Function> Function load(const char* name) {
        const auto function=reinterpret_cast<Function>(library.resolve(name));
        if (!function) throw CudaUnavailable("CUDA driver is missing "+std::string(name));
        return function;
    }
    decltype(&cuCtxPushCurrent) push=load<decltype(&cuCtxPushCurrent)>("cuCtxPushCurrent_v2");
    decltype(&cuCtxPopCurrent) pop=load<decltype(&cuCtxPopCurrent)>("cuCtxPopCurrent_v2");
    decltype(&cuStreamSynchronize) synchronize=load<decltype(&cuStreamSynchronize)>("cuStreamSynchronize");
};
void completeDecodedFrame(const AVFrame* frame) {
    if (frame->format!=AV_PIX_FMT_CUDA) return;
    // NVDEC's mapping/copy can still be queued when receive_frame returns.
    // Native bridges use their own nonblocking streams. Complete this frame's
    // producer stream before making its pixels available to those consumers.
    static CudaFrameDriver driver;
    if (!frame->hw_frames_ctx) throw std::runtime_error("Decoded CUDA frame has no device owner.");
    const auto* pool=reinterpret_cast<const AVHWFramesContext*>(frame->hw_frames_ctx->data);
    const auto* device=static_cast<const AVCUDADeviceContext*>(pool->device_ctx->hwctx);
    const auto push=driver.push(device->cuda_ctx);
    if (push!=CUDA_SUCCESS) throw std::runtime_error("Cannot activate the decoded CUDA frame's context.");
    const auto completed=driver.synchronize(device->stream);
    CUcontext previous=nullptr;
    const auto popped=driver.pop(&previous);
    if (completed!=CUDA_SUCCESS || popped!=CUDA_SUCCESS)
        throw std::runtime_error("Decoded CUDA frame did not complete: "+std::to_string(int(completed)));
}
AVPixelFormat selectCuda(AVCodecContext*, const AVPixelFormat* formats) {
    while (*formats != AV_PIX_FMT_NONE) {
        if (*formats == AV_PIX_FMT_CUDA)
            return *formats;
        ++formats;
    }
    return AV_PIX_FMT_NONE;
}
CudaVideoFrame ownedFrame(AVFrame* frame, TimeBase base) {
    auto& api = Api::get();
    CudaVideoFrame result;
    result.owner = std::shared_ptr<void>(frame, [&api](void* pointer) {
        auto* owned = static_cast<AVFrame*>(pointer);
        api.av_frame_free(&owned);
    });
    AVPixelFormat format = AVPixelFormat(frame->format);
    if (format == AV_PIX_FMT_CUDA && frame->hw_frames_ctx) {
        const auto* pool = reinterpret_cast<const AVHWFramesContext*>(frame->hw_frames_ctx->data);
        format = pool->sw_format;
    } else result.memory = FrameMemory::Host;
    if (format != AV_PIX_FMT_NV12 && format != AV_PIX_FMT_P010LE)
        throw CudaUnavailable("CUDA processing requires NV12 or P010 surfaces.");
    result.size = {frame->width, frame->height};
    result.format = format == AV_PIX_FMT_P010LE ? VideoPixelFormat::P010 : VideoPixelFormat::Nv12;
    for (int plane = 0; plane < 3; ++plane) {
        result.planes[plane] = reinterpret_cast<uint64_t>(frame->data[plane]);
        result.strides[plane] = uint32_t(frame->linesize[plane]);
    }
    result.colorMatrix = frame->colorspace == AVCOL_SPC_BT2020_NCL ? 2u
        : frame->colorspace == AVCOL_SPC_SMPTE170M || frame->colorspace == AVCOL_SPC_BT470BG ||
          frame->colorspace == AVCOL_SPC_SMPTE240M || frame->colorspace == AVCOL_SPC_FCC ? 0u : 1u;
    result.colorRange = frame->color_range == AVCOL_RANGE_JPEG ? 1u : 0u;
    result.chromaLocation = frame->chroma_location == AVCHROMA_LOC_UNSPECIFIED ? 1u : uint32_t(frame->chroma_location);
    result.pts = frame->pts;
    result.duration = frame->duration;
    result.timeBase = base;
    return result;
}
} // namespace
CudaVideoFrame downloadVideoFrame(const CudaVideoFrame& frame) {
    if (frame.memory == FrameMemory::Host) return frame;
    auto& api = Api::get();
    auto host = av::frame();
    auto* source = static_cast<AVFrame*>(frame.owner.get());
    check(api.av_hwframe_transfer_data(host.get(), source, 0), "Checkpoint decoded pixels");
    check(api.av_frame_copy_props(host.get(), source), "Checkpoint decoded metadata");
    host->pts = frame.pts; host->duration = frame.duration;
    auto result = ownedFrame(host.release(), frame.timeBase);
    result.rotation = frame.rotation;
    return result;
}
void prepareCudaPrimaryContext(int ordinal) {
    (void)cudaPrimaryDevice(ordinal);
}
std::shared_ptr<void> cudaPrimaryDevice(int ordinal) {
    static std::mutex mutex;
    static std::map<int, std::shared_ptr<void>> contexts;
    std::lock_guard lock(mutex);
    if (const auto found = contexts.find(ordinal); found != contexts.end()) return found->second;
    auto& api = Api::get();
    AVBufferRef* context = nullptr;
    AVDictionary* options = nullptr;
    api.av_dict_set(&options, "primary_ctx", "1", 0);
    const auto code = api.av_hwdevice_ctx_create(&context, AV_HWDEVICE_TYPE_CUDA,
        QByteArray::number(ordinal).constData(), options, 1);
    api.av_dict_free(&options);
    if (code < 0) { api.av_buffer_unref(&context); check(code, "Prepare CUDA primary context"); }
    auto owner = std::shared_ptr<void>(context, [&api](void* pointer) {
        auto* reference = static_cast<AVBufferRef*>(pointer); api.av_buffer_unref(&reference);
    });
    contexts.emplace(ordinal, owner);
    return owner;
}
struct CudaVideoDecoder::Impl {
    Api& api = Api::get();
    JobControl& control;
    AVFormatContext* format = nullptr;
    AVCodecContext* codec = nullptr;
    AVBufferRef* hardware = nullptr;
    AVPacket* packet = nullptr;
    int stream = -1;
    bool draining = false;
    double start = 0, timestampOffset=0;
    qint64 origin = AV_NOPTS_VALUE;
    Impl(const QString& path, int ordinal, double seconds, JobControl& job) : control(job), start(seconds) {
        try {
            format = api.avformat_alloc_context();
            if (!format)
                throw std::bad_alloc{};
            format->interrupt_callback = {[](void* opaque) {
                return static_cast<JobControl*>(opaque)->cancelled() ? 1 : 0;
            }, &control};
            check(api.avformat_open_input(&format, path.toUtf8().constData(), nullptr, nullptr), "Open source");
            check(api.avformat_find_stream_info(format, nullptr), "Probe source");
            prepareDecoder(ordinal);
            const auto* track=format->streams[stream];
            if(track->start_time!=AV_NOPTS_VALUE)timestampOffset=track->start_time*av_q2d(track->time_base);
            else if(format->start_time!=AV_NOPTS_VALUE)timestampOffset=double(format->start_time)/AV_TIME_BASE;
            if (start > 0)
                check(api.avformat_seek_file(format, -1, INT64_MIN, qint64((timestampOffset+std::max(0., start - .002)) * AV_TIME_BASE),
                                             INT64_MAX, 0), "Seek NVDEC source");
        } catch (const Cancelled&) { release(); throw; }
        catch (const std::exception& e) { release(); throw CudaUnavailable(e.what()); }
    }
    void prepareDecoder(int ordinal) {
            stream = api.av_find_best_stream(format, AVMEDIA_TYPE_VIDEO, -1, -1, nullptr, 0);
            check(stream, "Find video stream");
            const auto* decoder = api.avcodec_find_decoder(format->streams[stream]->codecpar->codec_id);
            bool cuda = false;
            for (int i = 0; decoder; ++i) {
                const auto* config = api.avcodec_get_hw_config(decoder, i);
                if (!config) break;
                if (config->device_type == AV_HWDEVICE_TYPE_CUDA && config->pix_fmt == AV_PIX_FMT_CUDA)
                    cuda = true;
            }
            if (!cuda)
                throw CudaUnavailable("This source codec has no NVDEC implementation.");
            AVDictionary* options = nullptr;
            api.av_dict_set(&options, "primary_ctx", "1", 0);
            const auto hardwareCode = api.av_hwdevice_ctx_create(&hardware, AV_HWDEVICE_TYPE_CUDA,
                QByteArray::number(ordinal).constData(), options, 1); // AV_CUDA_USE_PRIMARY_CONTEXT
            api.av_dict_free(&options);
            check(hardwareCode, "Create CUDA primary context");
            codec = api.avcodec_alloc_context3(decoder);
            if (!codec) throw std::bad_alloc{};
            check(api.avcodec_parameters_to_context(codec, format->streams[stream]->codecpar), "Describe decoder");
            codec->pkt_timebase = format->streams[stream]->time_base;
            codec->hw_device_ctx = api.av_buffer_ref(hardware);
            codec->get_format = selectCuda;
            // NVDEC performs the decoding on the GPU. A host frame-thread pool
            // adds no parallelism and retains Windows thread synchronization state.
            codec->thread_count = 1;
            check(api.avcodec_open2(codec, decoder, nullptr), "Open NVDEC");
            packet = api.av_packet_alloc();
            if (!packet) throw std::bad_alloc{};
    }
    void release() {
        api.av_packet_free(&packet);
        api.avcodec_free_context(&codec);
        api.av_buffer_unref(&hardware);
        if (format) api.avformat_close_input(&format);
    }
    ~Impl() { release(); }
    TimeBase base() const {
        const auto b = format->streams[stream]->time_base;
        return {b.num, b.den};
    }
};
CudaVideoDecoder::CudaVideoDecoder(const QString& path, int ordinal, double start, JobControl& control)
    : impl_(std::make_unique<Impl>(path, ordinal, start, control)) {}
CudaVideoDecoder::~CudaVideoDecoder() = default;
struct CudaFramePool::Impl {
    AVBufferRef* pool = nullptr;
    bool hdr = false;
    Impl(void* device, QSize size, bool p010, bool isHdr) : hdr(isHdr) {
        auto& api = Api::get();
        pool = api.av_hwframe_ctx_alloc(static_cast<AVBufferRef*>(device));
        if (!pool) throw std::bad_alloc{};
        auto* context = reinterpret_cast<AVHWFramesContext*>(pool->data);
        context->format = AV_PIX_FMT_CUDA; context->sw_format = p010 ? AV_PIX_FMT_P010LE : AV_PIX_FMT_NV12;
        context->width = size.width(); context->height = size.height();
        // Wrapped native surfaces already own their pixels. Allocate backing
        // CUDA buffers on demand only for callers of allocate().
        context->initial_pool_size = 0;
        const auto code = api.av_hwframe_ctx_init(pool);
        if (code < 0) { api.av_buffer_unref(&pool); check(code, "Allocate native output pool"); }
    }
    ~Impl() { Api::get().av_buffer_unref(&pool); }
};
CudaFramePool::CudaFramePool(void* device, QSize size, bool p010, bool hdr)
    : impl_(std::make_unique<Impl>(device, size, p010, hdr)) {}
CudaFramePool::~CudaFramePool() = default;
CudaVideoFrame CudaFramePool::allocate() {
    auto& s = *impl_;
    auto& api = Api::get();
    auto* frame = api.av_frame_alloc();
    if (!frame) throw std::bad_alloc{};
    const auto code = api.av_hwframe_get_buffer(s.pool, frame, 0);
    if (code < 0) { api.av_frame_free(&frame); check(code, "Allocate native output surface"); }
    frame->color_range = AVCOL_RANGE_MPEG;
    frame->colorspace = s.hdr ? AVCOL_SPC_BT2020_NCL : AVCOL_SPC_BT709;
    frame->chroma_location = AVCHROMA_LOC_LEFT;
    return ownedFrame(frame, {1, 60000000});
}
CudaVideoFrame CudaFramePool::wrap(std::shared_ptr<void> pixels, const std::array<uint64_t, 3>& planes,
    const std::array<uint32_t, 3>& strides, uint32_t matrix, uint32_t range) {
    auto& s = *impl_;
    auto& api = Api::get();
    auto owner = av::frame();
    auto* frame = owner.get();
    const auto* pool = reinterpret_cast<const AVHWFramesContext*>(s.pool->data);
    frame->format = AV_PIX_FMT_CUDA; frame->width = pool->width; frame->height = pool->height;
    frame->hw_frames_ctx = api.av_buffer_ref(s.pool);
    if (!frame->hw_frames_ctx) throw std::bad_alloc{};
    auto retained = std::make_unique<std::shared_ptr<void>>(std::move(pixels));
    frame->buf[0] = api.av_buffer_create(reinterpret_cast<uint8_t*>(planes[0]), 0,
        [](void* owner, uint8_t*) { delete static_cast<std::shared_ptr<void>*>(owner); }, retained.get(), 0);
    if (!frame->buf[0]) throw std::bad_alloc{};
    retained.release();
    for (int index=0; index<3; ++index) { frame->data[index] = reinterpret_cast<uint8_t*>(planes[index]); frame->linesize[index] = int(strides[index]); }
    frame->colorspace = matrix == 2 ? AVCOL_SPC_BT2020_NCL : matrix == 0 ? AVCOL_SPC_SMPTE170M
        : matrix == 3 ? AVCOL_SPC_FCC : matrix == 4 ? AVCOL_SPC_SMPTE240M : AVCOL_SPC_BT709;
    frame->color_range = range ? AVCOL_RANGE_JPEG : AVCOL_RANGE_MPEG;
    frame->chroma_location = AVCHROMA_LOC_LEFT;
    return ownedFrame(owner.release(), {1, 60000000});
}
TimeBase CudaVideoDecoder::timeBase() const { return impl_->base(); }
double CudaVideoDecoder::timeOrigin() const {
    const auto base=impl_->base();
    if (impl_->origin==AV_NOPTS_VALUE) throw std::logic_error("Read the first frame before its time origin.");
    return impl_->origin*double(base.numerator)/base.denominator;
}
void* CudaVideoDecoder::device() const { return impl_->hardware; }
std::optional<CudaVideoFrame> CudaVideoDecoder::read() {
    auto& s = *impl_;
    for (;;) {
        s.control.checkpoint();
        auto* frame = s.api.av_frame_alloc();
        if (!frame) throw std::bad_alloc{};
        const int status = s.api.avcodec_receive_frame(s.codec, frame);
        if (status >= 0) {
            auto output = ownedFrame(frame, s.base());
            completeDecodedFrame(static_cast<const AVFrame*>(output.owner.get()));
            if (output.pts == AV_NOPTS_VALUE)
                throw std::runtime_error("Decoded video frame has no presentation timestamp.");
            const double seconds = output.pts * double(output.timeBase.numerator) / output.timeBase.denominator;
            if (seconds + .002 < s.start+s.timestampOffset)
                continue;
            if (s.origin == AV_NOPTS_VALUE) s.origin = output.pts;
            output.pts -= s.origin;
            return output;
        }
        s.api.av_frame_free(&frame);
        if (status == AVERROR_EOF)
            return {};
        if (status != AVERROR(EAGAIN))
            check(status, "Receive NVDEC frame");
        if (s.draining)
            throw std::runtime_error("NVDEC did not finish draining.");
        for (;;) {
            s.api.av_packet_unref(s.packet);
            const int read = s.api.av_read_frame(s.format, s.packet);
            if (read == AVERROR_EOF) {
                check(s.api.avcodec_send_packet(s.codec, nullptr), "Drain NVDEC");
                s.draining = true;
                break;
            }
            check(read, "Read compressed packet");
            if (s.packet->stream_index == s.stream) {
                check(s.api.avcodec_send_packet(s.codec, s.packet), "Submit NVDEC packet");
                break;
            }
        }
    }
}
struct CudaVideoEncoder::Impl {
    Api& api = Api::get();
    JobControl& control;
    AVFormatContext* format = nullptr;
    AVCodecContext* codec = nullptr;
    AVBufferRef* pool = nullptr;
    AVPacket* packet = nullptr;
    AVStream* stream = nullptr;
    bool finished = false;
    bool explicitDurations=false;
    std::map<qint64,qint64> durations;
    Impl(const QString& path, const QString& name, const Settings& settings, const MediaInfo& info,
         TimeBase base, void* device, int ordinal, JobControl& job) : control(job) {
        explicitDurations=info.metadata.contains("output_sample_aspect_ratio");
        AVDictionary* options = nullptr;
        try {
            const auto* encoder = api.avcodec_find_encoder_by_name(name.toUtf8().constData());
            if (!encoder) throw CudaUnavailable("Bundled NVENC encoder is unavailable.");
            codec = api.avcodec_alloc_context3(encoder);
            if (!codec) throw std::bad_alloc{};
            codec->width = info.width; codec->height = info.height;
            codec->gop_size = -1; codec->max_b_frames = -1;
            codec->qmin = -1; codec->qmax = -1;
            codec->thread_count = 0; codec->thread_type = FF_THREAD_SLICE;
            codec->pix_fmt = AV_PIX_FMT_CUDA;
            Q_UNUSED(base);
            codec->time_base = {1, 60000000};
            codec->framerate = api.av_d2q(info.fps, 1000000);
            const auto aspect=info.outputSampleAspectRatio();codec->sample_aspect_ratio = {aspect.width(),aspect.height()};
            if(info.metadata.value("output_chroma_location")=="center")codec->chroma_sample_location=AVCHROMA_LOC_CENTER;
            codec->color_range = AVCOL_RANGE_MPEG;
            // Native RGB output uses the delivery matrix, independently of
            // the source YUV matrix used while decoding.
            codec->colorspace = info.hdr ? AVCOL_SPC_BT2020_NCL : AVCOL_SPC_BT709;
            const auto primaries=api.av_color_primaries_from_name(info.primaries.toUtf8().constData());
            const auto transfer=api.av_color_transfer_from_name(info.transfer.toUtf8().constData());
            codec->color_primaries=primaries<0 ? info.hdr ? AVCOL_PRI_BT2020 : AVCOL_PRI_UNSPECIFIED : AVColorPrimaries(primaries);
            codec->color_trc=transfer<0 ? info.hdr ? AVCOL_TRC_SMPTE2084 : AVCOL_TRC_UNSPECIFIED : AVColorTransferCharacteristic(transfer);
            const bool ten = name != "h264_nvenc" && (info.hdr || settings.hdr_mode ||
                info.pixelFormat.contains("10") || info.pixelFormat.contains("12") || info.pixelFormat.contains("16") ||
                info.pixelFormat.contains("64"));
            pool = api.av_hwframe_ctx_alloc(static_cast<AVBufferRef*>(device));
            if (!pool) throw std::bad_alloc{};
            auto* context = reinterpret_cast<AVHWFramesContext*>(pool->data);
            context->format = AV_PIX_FMT_CUDA;
            context->sw_format = ten ? AV_PIX_FMT_P010LE : AV_PIX_FMT_NV12;
            context->width = info.width; context->height = info.height;
            context->initial_pool_size = 4;
            check(api.av_hwframe_ctx_init(pool), "Allocate encoder CUDA pool");
            codec->hw_frames_ctx = api.av_buffer_ref(pool);
            const auto choice = name.startsWith("h264") ? QString("H.264 (NVIDIA NVENC)")
                : name.startsWith("hevc") ? QString("H.265 (NVIDIA NVENC)") : QString("AV1 (NVIDIA NVENC)");
            const auto arguments = videoEncoderArguments(choice, settings.quality, info.width, info.height, info.fps, info.hdr);
            for (qsizetype i = 0; i + 1 < arguments.size(); i += 2) {
                const auto key = arguments[i].mid(1).section(':', 0, 0);
                if (key == "b") {
                    auto value = arguments[i + 1];
                    const bool kilo = value.endsWith('k');
                    if (kilo) value.chop(1);
                    codec->bit_rate = value.toLongLong() * (kilo ? 1000 : 1);
                } else if (key != "c" && key != "pix_fmt")
                    api.av_dict_set(&options, key.toUtf8().constData(), arguments[i + 1].toUtf8().constData(), 0);
            }
            api.av_dict_set(&options, "gpu", QByteArray::number(ordinal).constData(), 0);
            // MP4 preserves explicit decode timestamps for reordered codecs.
            // NUT can infer a different DTS and shift the initial presentation.
            check(api.avformat_alloc_output_context2(&format, nullptr, "mp4",
                path.toUtf8().constData()), "Create video muxer");
            format->avoid_negative_ts=AVFMT_AVOID_NEG_TS_DISABLED;
            if (format->oformat->flags & AVFMT_GLOBALHEADER) codec->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
            check(api.avcodec_open2(codec, encoder, &options), "Open NVENC");
            api.av_dict_free(&options);
            stream = api.avformat_new_stream(format, nullptr);
            if (!stream) throw std::bad_alloc{};
            stream->time_base = codec->time_base;
            stream->avg_frame_rate = codec->framerate;
            check(api.avcodec_parameters_from_context(stream->codecpar, codec), "Describe encoded stream");
            retainStaticHdrMetadata(stream->codecpar,info);
            check(api.avio_open(&format->pb, path.toUtf8().constData(), AVIO_FLAG_WRITE), "Open video destination");
            api.av_dict_set(&options,"video_track_timescale","60000000",0);
            check(api.avformat_write_header(format, &options), "Write video header");
            api.av_dict_free(&options);
            packet = api.av_packet_alloc();
            if (!packet) throw std::bad_alloc{};
        } catch (const Cancelled&) {
            api.av_dict_free(&options); release(); throw;
        } catch (const std::exception& e) {
            api.av_dict_free(&options); release(); throw CudaUnavailable(e.what());
        }
    }
    void release() {
        api.av_packet_free(&packet);
        api.avcodec_free_context(&codec);
        api.av_buffer_unref(&pool);
        if (format) { if (format->pb) api.avio_closep(&format->pb); api.avformat_free_context(format); format = nullptr; }
    }
    ~Impl() { release(); }
    void packets() {
        for (;;) {
            control.checkpoint();
            const int code = api.avcodec_receive_packet(codec, packet);
            if (code == AVERROR(EAGAIN) || code == AVERROR_EOF) return;
            check(code, "Receive NVENC packet");
            if(explicitDurations)if(auto found=durations.find(packet->pts);found!=durations.end()){packet->duration=found->second;durations.erase(found);}
            api.av_packet_rescale_ts(packet, codec->time_base, stream->time_base);
            packet->stream_index = stream->index;
            check(api.av_interleaved_write_frame(format, packet), "Mux NVENC packet");
            api.av_packet_unref(packet);
        }
    }
};
CudaVideoEncoder::CudaVideoEncoder(const QString& path, const QString& codec, const Settings& settings,
    const MediaInfo& info, TimeBase base, void* device, int ordinal, JobControl& control)
    : impl_(std::make_unique<Impl>(path, codec, settings, info, base, device, ordinal, control)) {}
CudaVideoEncoder::~CudaVideoEncoder() = default;
CudaVideoFrame CudaVideoEncoder::allocate() {
    auto& s = *impl_;
    auto* frame = s.api.av_frame_alloc();
    if (!frame) throw std::bad_alloc{};
    const int code = s.api.av_hwframe_get_buffer(s.pool, frame, 0);
    if (code < 0) { s.api.av_frame_free(&frame); check(code, "Allocate output CUDA surface"); }
    frame->color_range = s.codec->color_range;
    frame->colorspace = s.codec->colorspace;
    frame->color_primaries = s.codec->color_primaries;
    frame->color_trc = s.codec->color_trc;
    frame->chroma_location = AVCHROMA_LOC_LEFT;
    return ownedFrame(frame, {s.codec->time_base.num, s.codec->time_base.den});
}
void CudaVideoEncoder::write(const CudaVideoFrame& frame) {
    auto& s = *impl_;
    s.control.checkpoint();
    auto* av = static_cast<AVFrame*>(frame.owner.get());
    const AVRational sourceBase{frame.timeBase.numerator, frame.timeBase.denominator};
    av->pts = s.api.av_rescale_q(frame.pts, sourceBase, s.codec->time_base);
    av->duration = s.api.av_rescale_q(frame.duration, sourceBase, s.codec->time_base);
    if(s.explicitDurations)s.durations[av->pts]=av->duration;
    if(s.codec->chroma_sample_location!=AVCHROMA_LOC_UNSPECIFIED)av->chroma_location=s.codec->chroma_sample_location;
    // NVENC reads static HDR SEI from submitted frames. Also retain the same
    // values on codec parameters for containers with native HDR metadata.
    copyStaticHdrMetadata(av,s.stream->codecpar);
    check(s.api.avcodec_send_frame(s.codec, av), "Submit NVENC frame");
    s.packets();
}
VideoPixelFormat CudaVideoEncoder::pixelFormat() const {
    const auto* pool = reinterpret_cast<const AVHWFramesContext*>(impl_->pool->data);
    return pool->sw_format == AV_PIX_FMT_P010LE ? VideoPixelFormat::P010 : VideoPixelFormat::Nv12;
}
void CudaVideoEncoder::finish() {
    auto& s = *impl_;
    if (s.finished) return;
    check(s.api.avcodec_send_frame(s.codec, nullptr), "Drain NVENC");
    s.packets();
    check(s.api.av_write_trailer(s.format), "Finish GPU video");
    s.finished = true;
}
} // namespace ve
