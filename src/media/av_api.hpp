#pragma once
#include "av_library.hpp"
#include <memory>
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/pixdesc.h>
#include <libavutil/mathematics.h>
#include <libavutil/imgutils.h>
#include <libavutil/mastering_display_metadata.h>
#include <libswscale/swscale.h>
}

namespace ve::av {
struct Api {
#define AV_FN(name) decltype(&::name) name = AvLibrary::instance().resolve<decltype(&::name)>(#name)
    AV_FN(avformat_version); AV_FN(avcodec_version); AV_FN(avutil_version); AV_FN(swscale_version);
    AV_FN(avformat_alloc_context); AV_FN(avformat_open_input); AV_FN(avformat_find_stream_info);
    AV_FN(avformat_close_input); AV_FN(av_find_best_stream); AV_FN(avformat_seek_file); AV_FN(av_read_frame);
    AV_FN(av_find_input_format); AV_FN(av_malloc); AV_FN(av_free);
    AV_FN(avio_alloc_context); AV_FN(avio_context_free); 
    AV_FN(avcodec_find_decoder); AV_FN(avcodec_find_encoder_by_name); AV_FN(avcodec_get_hw_config);
    AV_FN(avcodec_get_hw_frames_parameters);
    AV_FN(avcodec_alloc_context3); AV_FN(avcodec_parameters_to_context); AV_FN(avcodec_parameters_from_context);
    AV_FN(avcodec_open2); AV_FN(avcodec_free_context); AV_FN(avcodec_send_packet); AV_FN(avcodec_receive_frame);
    AV_FN(avcodec_flush_buffers); AV_FN(avformat_flush);
    AV_FN(avcodec_send_frame); AV_FN(avcodec_receive_packet);
    AV_FN(av_frame_alloc); AV_FN(av_frame_free); AV_FN(av_frame_copy_props);
    AV_FN(av_frame_new_side_data); AV_FN(av_frame_get_side_data);
    AV_FN(av_frame_get_buffer); AV_FN(av_frame_make_writable);
    AV_FN(av_image_get_buffer_size); AV_FN(av_image_copy_to_buffer);
    AV_FN(av_buffer_create);
    AV_FN(av_packet_alloc); AV_FN(av_packet_free); AV_FN(av_packet_unref); AV_FN(av_packet_rescale_ts);
    AV_FN(av_new_packet);
    AV_FN(av_packet_side_data_new);
    AV_FN(av_buffer_ref); AV_FN(av_buffer_unref); AV_FN(av_hwdevice_ctx_create);
    AV_FN(av_hwdevice_ctx_alloc); AV_FN(av_hwdevice_ctx_init);
    AV_FN(av_hwdevice_ctx_create_derived); AV_FN(av_hwframe_ctx_create_derived); AV_FN(av_hwframe_map);
    AV_FN(av_hwframe_ctx_alloc); AV_FN(av_hwframe_ctx_init); AV_FN(av_hwframe_get_buffer);
    AV_FN(av_hwframe_transfer_data);
    AV_FN(avformat_alloc_output_context2); AV_FN(avformat_new_stream); AV_FN(avformat_write_header);
    AV_FN(av_interleaved_write_frame); AV_FN(av_write_frame); AV_FN(av_write_trailer); AV_FN(avformat_free_context);
    AV_FN(avio_open); AV_FN(avio_closep); AV_FN(av_dict_set); AV_FN(av_dict_free);
    AV_FN(avio_flush);
    AV_FN(av_strerror); AV_FN(av_d2q); AV_FN(av_rescale_q); AV_FN(av_pix_fmt_desc_get);
    AV_FN(av_get_pix_fmt); AV_FN(av_rescale_q_rnd);
    AV_FN(av_color_space_from_name); AV_FN(av_color_primaries_from_name); AV_FN(av_color_transfer_from_name);
    AV_FN(sws_alloc_context); AV_FN(sws_free_context); AV_FN(sws_scale_frame);
#undef AV_FN
    Api() {
        if ((avformat_version() >> 16) != LIBAVFORMAT_VERSION_MAJOR ||
            (avcodec_version() >> 16) != LIBAVCODEC_VERSION_MAJOR ||
            (avutil_version() >> 16) != LIBAVUTIL_VERSION_MAJOR ||
            (swscale_version() >> 16) != LIBSWSCALE_VERSION_MAJOR)
            throw std::runtime_error("Incompatible bundled libav ABI.");
    }
    static Api& get() { static Api api; return api; }
};
inline void check(int code, const char* operation) {
    if (code >= 0) return;
    char error[AV_ERROR_MAX_STRING_SIZE]{};
    Api::get().av_strerror(code, error, sizeof(error));
    throw std::runtime_error(std::string(operation) + ": " + error);
}
struct FrameDeleter { void operator()(AVFrame* frame) const { Api::get().av_frame_free(&frame); } };
using Frame = std::unique_ptr<AVFrame, FrameDeleter>;
inline Frame frame() {
    Frame result(Api::get().av_frame_alloc());
    if (!result) throw std::bad_alloc{};
    return result;
}
} // namespace ve::av
