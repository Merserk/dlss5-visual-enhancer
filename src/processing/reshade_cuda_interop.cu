// SPDX-License-Identifier: MIT
// Independent CUDA/D3D12 frame-boundary kernels for the ReShade native bridge.
// Compiled to this feature's embedded PTX via NVRTC.
// Note: These kernels perform only YUV/RGB memory transport; ReShade motion
// estimation uses AMD FidelityFX Optical Flow 1.1.2 on D3D12 exclusively.
#include <cuda_fp16.h>
#include <cstddef>

__device__ __forceinline__ void matrix_coefficients(int matrix, float& kr, float& kb) {
    if (matrix == 0) { kr = 0.2990f; kb = 0.1140f; }
    else if (matrix == 2) { kr = 0.2627f; kb = 0.0593f; }
    else if (matrix == 3) { kr = 0.3000f; kb = 0.1100f; }
    else if (matrix == 4) { kr = 0.2120f; kb = 0.0870f; }
    else { kr = 0.2126f; kb = 0.0722f; }
}

__device__ __forceinline__ void yuv_to_rgb(float yv, float uv, float vv, int bits,
                                            int matrix, int full, float& r, float& g, float& b) {
    float y, cb, cr;
    const float maximum = bits == 10 ? 1023.0f : 255.0f;
    if (full) {
        const float neutral = bits == 10 ? 512.0f : 128.0f;
        y = yv / maximum;
        cb = (uv - neutral) / maximum;
        cr = (vv - neutral) / maximum;
    } else if (bits == 10) {
        y = (yv - 64.0f) / 876.0f;
        cb = (uv - 512.0f) / 896.0f;
        cr = (vv - 512.0f) / 896.0f;
    } else {
        y = (yv - 16.0f) / 219.0f;
        cb = (uv - 128.0f) / 224.0f;
        cr = (vv - 128.0f) / 224.0f;
    }
    float kr, kb;
    matrix_coefficients(matrix, kr, kb);
    const float kg = 1.0f - kr - kb;
    r = y + 2.0f * (1.0f - kr) * cr;
    b = y + 2.0f * (1.0f - kb) * cb;
    g = (y - kr * r - kb * b) / kg;
    r = fminf(fmaxf(r, 0.0f), 1.0f);
    g = fminf(fmaxf(g, 0.0f), 1.0f);
    b = fminf(fmaxf(b, 0.0f), 1.0f);
}

__device__ __forceinline__ void rgb_to_yuv(float r, float g, float b, int bits,
                                            int matrix, int full, float& y, float& u, float& v) {
    r = fminf(fmaxf(r, 0.0f), 1.0f);
    g = fminf(fmaxf(g, 0.0f), 1.0f);
    b = fminf(fmaxf(b, 0.0f), 1.0f);
    float kr, kb;
    matrix_coefficients(matrix, kr, kb);
    const float kg = 1.0f - kr - kb;
    const float luma = kr * r + kg * g + kb * b;
    const float cb = (b - luma) / (2.0f * (1.0f - kb));
    const float cr = (r - luma) / (2.0f * (1.0f - kr));
    if (full) {
        const float m = bits == 10 ? 1023.0f : 255.0f;
        const float neutral = bits == 10 ? 512.0f : 128.0f;
        y = luma * m;
        u = cb * m + neutral;
        v = cr * m + neutral;
    } else if (bits == 10) {
        y = 64.0f + luma * 876.0f;
        u = 512.0f + cb * 896.0f;
        v = 512.0f + cr * 896.0f;
    } else {
        y = 16.0f + luma * 219.0f;
        u = 128.0f + cb * 224.0f;
        v = 128.0f + cr * 224.0f;
    }
}

__device__ __forceinline__ bool source_xy(int x, int y, int out_w, int out_h,
                                           int src_w, int src_h, int rotation, int& sx, int& sy) {
    int ox = x, oy = y;
    if (rotation == 90) { sx = oy; sy = src_h - 1 - ox; }
    else if (rotation == 180) { sx = src_w - 1 - ox; sy = src_h - 1 - oy; }
    else if (rotation == 270) { sx = src_w - 1 - oy; sy = ox; }
    else { sx = ox; sy = oy; }
    return sx >= 0 && sy >= 0 && sx < src_w && sy < src_h && x < out_w && y < out_h;
}

template<typename T>
__device__ __forceinline__ float sample_uv(
    const T* plane, int stride_bytes, int width, int height, float x, float y, int channel) {
    x = fminf(fmaxf(x, 0.0f), float(width - 1));
    y = fminf(fmaxf(y, 0.0f), float(height - 1));
    const int stride = stride_bytes / int(sizeof(T));
    const int x0 = int(floorf(x)), y0 = int(floorf(y));
    const int x1 = min(width - 1, x0 + 1), y1 = min(height - 1, y0 + 1);
    const float fx = x - floorf(x), fy = y - floorf(y);
    const float a = plane[size_t(y0) * stride + x0 * 2 + channel];
    const float b = plane[size_t(y0) * stride + x1 * 2 + channel];
    const float c = plane[size_t(y1) * stride + x0 * 2 + channel];
    const float d = plane[size_t(y1) * stride + x1 * 2 + channel];
    return (a + (b - a) * fx) + ((c + (d - c) * fx) - (a + (b - a) * fx)) * fy;
}

__device__ __forceinline__ void chroma_xy(int sx, int sy, int location, float& x, float& y) {
    const float cx = (location == 2 || location == 4 || location == 6) ? 0.5f : 0.0f;
    const float cy = (location == 3 || location == 4) ? 0.0f : (location == 5 || location == 6) ? 1.0f : 0.5f;
    x = (float(sx) - cx) * 0.5f;
    y = (float(sy) - cy) * 0.5f;
}

__device__ __forceinline__ void store_half4(unsigned short* dst, size_t pitch, int x, int y,
                                             float r, float g, float b, float a = 1.0f) {
    uint2 value;
    const unsigned hr = __half_as_ushort(__float2half_rn(r));
    const unsigned hg = __half_as_ushort(__float2half_rn(g));
    const unsigned hb = __half_as_ushort(__float2half_rn(b));
    const unsigned ha = __half_as_ushort(__float2half_rn(a));
    value.x = (hg << 16) | hr;
    value.y = (ha << 16) | hb;
    reinterpret_cast<uint2*>(reinterpret_cast<unsigned char*>(dst) + size_t(y) * pitch)[x] = value;
}

__device__ __forceinline__ void load_half4(const unsigned short* src, size_t pitch, int x, int y,
                                            float& r, float& g, float& b, float& a) {
    const uint2 value = reinterpret_cast<const uint2*>(
        reinterpret_cast<const unsigned char*>(src) + size_t(y) * pitch)[x];
    r = __half2float(__ushort_as_half(value.x & 0xffffu));
    g = __half2float(__ushort_as_half(value.x >> 16));
    b = __half2float(__ushort_as_half(value.y & 0xffffu));
    a = __half2float(__ushort_as_half(value.y >> 16));
}

extern "C" __global__ void rs_nv12_to_rgba16f(
    const unsigned char* yp, const unsigned char* uvp, int ys, int uvs,
    int sw, int sh, unsigned short* dst, size_t pitch, int ow, int oh,
    int matrix, int full, int rotation, int chroma) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= ow || y >= oh) return;
    int sx, sy;
    float r = 0.0f, g = 0.0f, b = 0.0f;
    if (source_xy(x, y, ow, oh, sw, sh, rotation, sx, sy)) {
        float ux, uy;
        chroma_xy(sx, sy, chroma, ux, uy);
        yuv_to_rgb(yp[size_t(sy) * ys + sx],
                   sample_uv(uvp, uvs, (sw + 1) / 2, (sh + 1) / 2, ux, uy, 0),
                   sample_uv(uvp, uvs, (sw + 1) / 2, (sh + 1) / 2, ux, uy, 1),
                   8, matrix, full, r, g, b);
    }
    store_half4(dst, pitch, x, y, r, g, b, 1.0f);
}

extern "C" __global__ void rs_p010_to_rgba16f(
    const unsigned short* yp, const unsigned short* uvp, int ys, int uvs,
    int sw, int sh, unsigned short* dst, size_t pitch, int ow, int oh,
    int matrix, int full, int rotation, int chroma) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= ow || y >= oh) return;
    int sx, sy;
    float r = 0.0f, g = 0.0f, b = 0.0f;
    if (source_xy(x, y, ow, oh, sw, sh, rotation, sx, sy)) {
        const int yi = ys / 2;
        float ux, uy;
        chroma_xy(sx, sy, chroma, ux, uy);
        yuv_to_rgb(float(yp[size_t(sy) * yi + sx] >> 6),
                   sample_uv(uvp, uvs, (sw + 1) / 2, (sh + 1) / 2, ux, uy, 0) / 64.0f,
                   sample_uv(uvp, uvs, (sw + 1) / 2, (sh + 1) / 2, ux, uy, 1) / 64.0f,
                   10, matrix, full, r, g, b);
    }
    store_half4(dst, pitch, x, y, r, g, b, 1.0f);
}

__device__ __forceinline__ float rs_mitchell(float x) {
    x = fabsf(x);
    if (x < 1.0f) return ((7.0f * x - 12.0f) * x * x + 16.0f / 3.0f) / 6.0f;
    if (x < 2.0f) return (((-7.0f / 3.0f * x + 12.0f) * x - 20.0f) * x + 32.0f / 3.0f) / 6.0f;
    return 0.0f;
}

extern "C" __global__ void rs_prepare_420(
    const unsigned short* src, size_t pitch,
    unsigned char* yp, int ys, int w, int h, int matrix, int full,
    float2* chroma, size_t chroma_pitch, int p010) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;
    float r, g, b, a, yy, u, v;
    load_half4(src, pitch, x, y, r, g, b, a);
    rgb_to_yuv(r, g, b, p010 ? 10 : 8, matrix, full, yy, u, v);
    const float maximum = p010 ? 1023.0f : 255.0f;
    const unsigned q = (unsigned)(fminf(fmaxf(yy, 0.0f), maximum) + 0.5f);
    unsigned char* row = yp + size_t(y) * ys;
    if (p010) reinterpret_cast<unsigned short*>(row)[x] = (unsigned short)(q << 6);
    else row[x] = (unsigned char)q;
    reinterpret_cast<float2*>(reinterpret_cast<unsigned char*>(chroma) + size_t(y) * chroma_pitch)[x] = make_float2(u, v);
}

extern "C" __global__ void rs_filter_420_vertical(
    const float2* source, size_t pitch,
    float2* destination, size_t destination_pitch, int w, int h) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int cy = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || cy * 2 >= h) return;
    float u = 0.0f, v = 0.0f, norm = 0.0f;
    #pragma unroll
    for (int j = 0; j < 8; ++j) {
        const int y = max(0, min(h - 1, cy * 2 + j - 3));
        const float2 value = reinterpret_cast<const float2*>(
            reinterpret_cast<const unsigned char*>(source) + size_t(y) * pitch)[x];
        const float weight = rs_mitchell((j - 3.5f) * 0.5f);
        norm += weight;
        u += weight * value.x;
        v += weight * value.y;
    }
    reinterpret_cast<float2*>(
        reinterpret_cast<unsigned char*>(destination) + size_t(cy) * destination_pitch)[x] = make_float2(u / norm, v / norm);
}

template<typename T, int BITS>
__device__ void rs_filter_420_horizontal(
    const float2* source, size_t pitch,
    T* destination, int stride, int w, int h, int cx, int cy) {
    if (cx * 2 >= w || cy * 2 >= h) return;
    float u = 0.0f, v = 0.0f, norm = 0.0f;
    const float2* row = reinterpret_cast<const float2*>(
        reinterpret_cast<const unsigned char*>(source) + size_t(cy) * pitch);
    #pragma unroll
    for (int i = 0; i < 8; ++i) {
        const float2 value = row[max(0, min(w - 1, cx * 2 + i - 3))];
        const float weight = rs_mitchell((i - 3) * 0.5f);
        norm += weight;
        u += weight * value.x;
        v += weight * value.y;
    }
    const float maximum = BITS == 10 ? 1023.0f : 255.0f;
    unsigned uq = (unsigned)(fminf(fmaxf(u / norm, 0.0f), maximum) + 0.5f);
    unsigned vq = (unsigned)(fminf(fmaxf(v / norm, 0.0f), maximum) + 0.5f);
    if (BITS == 10) { uq <<= 6; vq <<= 6; }
    T* out = destination + size_t(cy) * (stride / int(sizeof(T))) + cx * 2;
    out[0] = (T)uq;
    out[1] = (T)vq;
}

extern "C" __global__ void rs_420_uv_to_nv12(
    const float2* source, size_t pitch,
    unsigned char* destination, int stride, int w, int h) {
    rs_filter_420_horizontal<unsigned char, 8>(
        source, pitch, destination, stride, w, h,
        blockIdx.x * blockDim.x + threadIdx.x, blockIdx.y * blockDim.y + threadIdx.y);
}

extern "C" __global__ void rs_420_uv_to_p010(
    const float2* source, size_t pitch,
    unsigned short* destination, int stride, int w, int h) {
    rs_filter_420_horizontal<unsigned short, 10>(
        source, pitch, destination, stride, w, h,
        blockIdx.x * blockDim.x + threadIdx.x, blockIdx.y * blockDim.y + threadIdx.y);
}

extern "C" __global__ void rs_copy_rgba16f(
    const unsigned short* src, size_t src_pitch,
    unsigned short* dst, size_t dst_pitch, int w, int h) {
    const int x = blockIdx.x * blockDim.x + threadIdx.x;
    const int y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;
    const uint2 value = reinterpret_cast<const uint2*>(
        reinterpret_cast<const unsigned char*>(src) + size_t(y) * src_pitch)[x];
    reinterpret_cast<uint2*>(
        reinterpret_cast<unsigned char*>(dst) + size_t(y) * dst_pitch)[x] = value;
}
