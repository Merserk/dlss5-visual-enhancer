#pragma once
#include <cstdint>
#include <memory>
#include <string>
#include <utility>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include "processing/reshade/reshade_bridge.h"

namespace ve {

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

class ReShadeCudaAdapter {
public:
    static ReShadeCudaAdapter& instance();

    bool isAvailable() const;
    bool initialize(int ordinal);

    int processCudaYuv(void* sessionHandle,
                       const VeReShadeCudaYuvDesc* frame,
                       void** outSurface,
                       VeReShadeFrameResult* result,
                       char* error,
                       int capacity);

    int processCudaRgb(void* sessionHandle,
                       const VeReShadeCudaRgbDesc* frame,
                       void** outSurface,
                       VeReShadeFrameResult* result,
                       char* error,
                       int capacity);

    int surfaceDesc(void* surface, VeReShadeCudaSurfaceDesc* desc);
    void surfaceRetain(void* surface);
    void surfaceRelease(void* surface);

    void releaseSessionBuffers(void* sessionHandle);

private:
    ReShadeCudaAdapter();
    ~ReShadeCudaAdapter();

    struct Impl;
    std::unique_ptr<Impl> impl_;
};

} // namespace ve
