#pragma once
// Independent Windows x64 ReShade ABI 2.
// Motion estimation and video depth stabilization use AMD FidelityFX Optical
// Flow 1.1.2 exclusively on all GPUs; NVIDIA Optical Flow is never used.
#include <stddef.h>
#include <stdint.h>

#if defined(VE_RS_BRIDGE_BUILD)
#define VE_RS_API __declspec(dllexport)
#else
#define VE_RS_API __declspec(dllimport)
#endif
#define VE_RS_CALL __cdecl

#ifdef __cplusplus
extern "C" {
#endif

enum {
    VE_RESHADE_ABI_VERSION = 2,
    VE_RESHADE_FORMAT_RGBA16F = 0,
    VE_RESHADE_FORMAT_RGBA8 = 1,
    VE_RESHADE_FORMAT_RGBA16_UNORM = 2,
    VE_RESHADE_FORMAT_NV12 = 3,
    VE_RESHADE_FORMAT_P010 = 4
};

enum {
    VE_RESHADE_PRECISION_AUTO = 0,
    VE_RESHADE_PRECISION_FP16 = 1,
    VE_RESHADE_PRECISION_UNORM8_SRGB = 2
};

enum {
    VE_RESHADE_TRANSFER_SRGB = 0,
    VE_RESHADE_TRANSFER_BT709 = 1,
    VE_RESHADE_TRANSFER_LINEAR = 2,
    VE_RESHADE_TRANSFER_GAMMA22 = 3,
    VE_RESHADE_TRANSFER_GAMMA28 = 4,
    VE_RESHADE_TRANSFER_PQ = 5,
    VE_RESHADE_TRANSFER_HLG = 6
};

enum {
    VE_RESHADE_HDR_AUTO = 0,
    VE_RESHADE_HDR_AWARE = 1,
    VE_RESHADE_HDR_SDR_ADAPTER = 2,
    VE_RESHADE_HDR_REJECT_UNSUPPORTED = 3
};

enum {
    VE_RESHADE_DEPTH_AUTO = 0,
    VE_RESHADE_DEPTH_OFF = 1,
    VE_RESHADE_DEPTH_AI = 2
};

enum {
    VE_RESHADE_TEMPORAL_QUALITY = 0,
    // Value 1 is retired and rejected; preserve the ABI 2 value of Off.
    VE_RESHADE_TEMPORAL_SPATIAL_OFF = 2
};

typedef struct VeReShadeCapabilities {
    uint32_t size;
    uint32_t abi;
    uint32_t available;
    uint32_t vendor_id;
    uint32_t device_id;
    uint32_t adapter_luid_low;
    int32_t adapter_luid_high;
    uint32_t shader_model;
    uint32_t max_texture_dimension;
    uint32_t host_format_mask;
    uint32_t cuda_interop_available;
    uint32_t d3d12_native_available;
    uint32_t depth_available;
    uint32_t amd_optical_flow_available;
    uint32_t dedicated_vram_mib;
    uint32_t reserved[1];
} VeReShadeCapabilities;

typedef struct VeReShadePresetDesc {
    uint32_t size;
    uint32_t abi;
    const char* preset_path_utf8;
    const char* shader_roots_utf8;
    const char* texture_roots_utf8;
    const char* overrides_json_utf8;
    uint32_t preview_width;
    uint32_t preview_height;
} VeReShadePresetDesc;

typedef struct VeReShadeSessionDesc {
    uint32_t size;
    uint32_t abi;
    uint32_t width;
    uint32_t height;
    uint32_t still_image;
    uint32_t precision_policy;
    uint32_t color_transfer;
    uint32_t hdr_policy;
    uint32_t depth_mode;
    uint32_t depth_resolution_bound;
    uint32_t temporal_policy;
    float temporal_strength;
    float mix;
    uint32_t preserve_alpha;
    uint32_t reserved_output; // Must be zero; retained for ABI 2 structure layout.
    uint32_t memory_budget_mib;
    uint64_t deterministic_seed;
    const char* preset_path_utf8;
    const char* shader_roots_utf8;
    const char* texture_roots_utf8;
    const char* overrides_json_utf8;
} VeReShadeSessionDesc;

typedef struct VeReShadeHostFrameDesc {
    uint32_t size;
    uint32_t abi;
    const void* input_pixels;
    uint32_t input_stride;
    uint32_t input_format;
    void* output_pixels;
    uint32_t output_stride;
    uint32_t output_format;
    uint32_t reset;
    uint32_t reserved;
    uint64_t frame_ordinal;
    int64_t pts_ns;
    int64_t duration_ns;
} VeReShadeHostFrameDesc;

typedef struct VeReShadeCudaYuvDesc {
    uint32_t size;
    uint32_t abi;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format; // VE_RESHADE_FORMAT_NV12 or VE_RESHADE_FORMAT_P010
    uint32_t output_format; // VE_RESHADE_FORMAT_NV12, VE_RESHADE_FORMAT_P010, or VE_RESHADE_FORMAT_RGBA16F
    uint64_t y_pointer;
    uint64_t uv_pointer;
    uint32_t y_stride;
    uint32_t uv_stride;
    uint32_t color_matrix; // 0=601, 1=709, 2=2020
    uint32_t color_range;  // 0=limited, 1=full
    uint32_t chroma_location; // 0..6 libav chroma location
    uint32_t rotation; // 0, 90, 180, 270
    uint32_t reset;
    uint32_t reserved;
    uint64_t frame_ordinal;
    int64_t pts_ns;
    int64_t duration_ns;
    uint64_t reserved_tail;
} VeReShadeCudaYuvDesc;

typedef struct VeReShadeCudaRgbDesc {
    uint32_t size;
    uint32_t abi;
    uint32_t width;
    uint32_t height;
    uint64_t pointer;
    uint32_t stride;
    uint32_t output_format; // VE_RESHADE_FORMAT_RGBA16F, VE_RESHADE_FORMAT_NV12, or VE_RESHADE_FORMAT_P010
    uint32_t color_matrix;
    uint32_t color_range;
    uint32_t reset;
    uint32_t reserved;
    uint64_t frame_ordinal;
    int64_t pts_ns;
    int64_t duration_ns;
} VeReShadeCudaRgbDesc;

typedef struct VeReShadeCudaSurfaceDesc {
    uint32_t size;
    uint32_t abi;
    uint32_t width;
    uint32_t height;
    uint32_t pixel_format;
    uint32_t stride;
    uint64_t y_pointer;
    uint64_t uv_pointer;
} VeReShadeCudaSurfaceDesc;

typedef struct VeReShadeD3d12FrameDesc {
    uint32_t size;
    uint32_t abi;
    void* input_resource;
    uint32_t input_state;
    uint32_t output_state;
    void* output_resource;
    uint32_t reset;
    uint32_t reserved;
    uint64_t frame_ordinal;
    int64_t pts_ns;
    int64_t duration_ns;
} VeReShadeD3d12FrameDesc;

typedef struct VeReShadeFrameResult {
    uint32_t size;
    uint32_t abi;
    uint32_t render_width;
    uint32_t render_height;
    uint32_t selected_precision;
    uint32_t active_techniques;
    uint32_t depth_evaluated;
    uint32_t amd_flow_dispatched;
    uint32_t amd_flow_history_valid;
    uint32_t history_reset;
    uint64_t host_upload_bytes;
    uint64_t host_download_bytes;
    uint64_t gpu_copy_bytes;
    double input_ms;
    double depth_ms;
    double flow_ms;
    double effect_ms;
    double output_ms;
    double total_ms;
} VeReShadeFrameResult;

VE_RS_API uint32_t VE_RS_CALL reshade_abi_version(void);
VE_RS_API const char* VE_RS_CALL reshade_version(void);
VE_RS_API const char* VE_RS_CALL reshade_optical_flow_backend(void);
VE_RS_API int VE_RS_CALL reshade_init_v2(int gpu_ordinal, const wchar_t* runtime_dir,
                                         const wchar_t* app_data_dir, char* error, int capacity);
VE_RS_API int VE_RS_CALL reshade_capabilities(VeReShadeCapabilities* out, char* error, int capacity);
VE_RS_API int VE_RS_CALL reshade_status(char* json, int capacity);
VE_RS_API int VE_RS_CALL reshade_inspect_preset(const VeReShadePresetDesc* desc,
                                                char* json_out, int json_capacity,
                                                char* error, int error_capacity);
VE_RS_API int VE_RS_CALL reshade_export_preset_copy(const VeReShadePresetDesc* desc,
                                                    const char* destination_path_utf8,
                                                    char* error, int error_capacity);
VE_RS_API void* VE_RS_CALL reshade_session_create(const VeReShadeSessionDesc* desc,
                                                  char* error, int capacity);
VE_RS_API int VE_RS_CALL reshade_session_reset(void* session, char* error, int capacity);
VE_RS_API int VE_RS_CALL reshade_session_status(void* session, char* json, int capacity);
VE_RS_API void VE_RS_CALL reshade_session_release(void* session);
VE_RS_API int VE_RS_CALL reshade_session_d3d12_device(void* session, void** out_device);
VE_RS_API int VE_RS_CALL reshade_session_d3d12_queue(void* session, void** out_queue);

VE_RS_API int VE_RS_CALL reshade_process_host(void* session, const VeReShadeHostFrameDesc* frame,
                                              VeReShadeFrameResult* result, char* error, int capacity);
VE_RS_API int VE_RS_CALL reshade_process_d3d12(void* session, const VeReShadeD3d12FrameDesc* frame,
                                               VeReShadeFrameResult* result, char* error, int capacity);

#ifdef __cplusplus
}
static_assert(sizeof(VeReShadeCapabilities) == 64, "Unexpected VeReShadeCapabilities layout");
static_assert(sizeof(VeReShadePresetDesc) == 48, "Unexpected VeReShadePresetDesc layout");
static_assert(sizeof(VeReShadeSessionDesc) == 104, "Unexpected VeReShadeSessionDesc layout");
static_assert(sizeof(VeReShadeHostFrameDesc) == 72, "Unexpected VeReShadeHostFrameDesc layout");
static_assert(sizeof(VeReShadeCudaYuvDesc) == 104, "Unexpected VeReShadeCudaYuvDesc layout");
static_assert(sizeof(VeReShadeCudaRgbDesc) == 72, "Unexpected VeReShadeCudaRgbDesc layout");
static_assert(sizeof(VeReShadeCudaSurfaceDesc) == 40, "Unexpected VeReShadeCudaSurfaceDesc layout");
static_assert(sizeof(VeReShadeD3d12FrameDesc) == 64, "Unexpected VeReShadeD3d12FrameDesc layout");
static_assert(sizeof(VeReShadeFrameResult) == 112, "Unexpected VeReShadeFrameResult layout");
#endif
