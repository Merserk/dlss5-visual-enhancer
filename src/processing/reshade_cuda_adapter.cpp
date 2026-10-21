#ifndef _CRT_SECURE_NO_WARNINGS
#define _CRT_SECURE_NO_WARNINGS
#endif
#include "reshade_cuda_adapter.hpp"
#include "reshade_cuda_kernels_ptx.h"
#include <cuda.h>
#include <Windows.h>
#include <dxgi1_6.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <mutex>
#include <vector>

namespace ve {
namespace {

using CuExternalMemoryHandleDesc = CUDA_EXTERNAL_MEMORY_HANDLE_DESC;
using CuExternalMemoryBufferDesc = CUDA_EXTERNAL_MEMORY_BUFFER_DESC;
using CuPrimaryRetainFn = CUresult(__cdecl*)(CUcontext*, CUdevice);
using CuPrimaryReleaseFn = CUresult(__cdecl*)(CUdevice);
using CuPrimaryStateFn = CUresult(__cdecl*)(CUdevice, unsigned*, int*);
using CuPrimaryFlagsFn = CUresult(__cdecl*)(CUdevice, unsigned);
using CuCtxGetFn = CUresult(__cdecl*)(CUcontext*);
using CuCtxSetFn = CUresult(__cdecl*)(CUcontext);
using CuImportFn = CUresult(__cdecl*)(CUexternalMemory*, const CuExternalMemoryHandleDesc*);
using CuMapFn = CUresult(__cdecl*)(uint64_t*, CUexternalMemory, const CuExternalMemoryBufferDesc*);
using CuDestroyFn = CUresult(__cdecl*)(CUexternalMemory);
using CuModuleLoadFn = CUresult(__cdecl*)(CUmodule*, const void*);
using CuModuleFunctionFn = CUresult(__cdecl*)(CUfunction*, CUmodule, const char*);
using CuLaunchFn = CUresult(__cdecl*)(CUfunction, unsigned, unsigned, unsigned, unsigned, unsigned, unsigned, unsigned, void*, void**, void**);
using CuSyncFn = CUresult(__cdecl*)(void*);
using CuAllocFn = CUresult(__cdecl*)(uint64_t*, size_t);
using CuFreeFn = CUresult(__cdecl*)(uint64_t);
using CuErrorFn = CUresult(__cdecl*)(CUresult, const char**);
using CuInitFn = int(__cdecl*)(unsigned);
using CuDeviceGetFn = int(__cdecl*)(int*, int);
using CuLuidFn = int(__cdecl*)(char*, unsigned*, int);

struct SharedBuffer {
    Microsoft::WRL::ComPtr<ID3D12Heap> heap;
    Microsoft::WRL::ComPtr<ID3D12Resource> resource;
    CUexternalMemory external = nullptr;
    uint64_t pointer = 0;
    uint64_t heap_bytes = 0;
};

struct SurfacePool;

struct CudaSurface {
    uint64_t pointer = 0;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t stride = 0;
    uint32_t format = 0;
    std::atomic<uint32_t> references{1};
    SurfacePool* pool = nullptr;
};

struct SurfacePool {
    std::vector<CudaSurface*> slots;
    bool closed = false;
};

static constexpr size_t MAX_OUTPUT_SURFACES = 64;

struct SessionBuffers {
    ID3D12Device* device = nullptr;
    uint32_t width = 0;
    uint32_t height = 0;
    D3D12_PLACED_SUBRESOURCE_FOOTPRINT cuda_fp{};
    uint64_t cuda_buffer_bytes = 0;

    SharedBuffer cuda_input;
    SharedBuffer cuda_output;

    uint64_t chroma_full = 0;
    uint64_t chroma_vertical = 0;

    SurfacePool surface_pool;
};

} // namespace

struct ReShadeCudaAdapter::Impl {
    std::mutex mutex;
    HMODULE cuda = nullptr;
    int ordinal = -1;
    CUdevice cuda_device = 0;
    CUcontext cuda_context = nullptr;
    CUmodule cuda_module = nullptr;

    CuCtxGetFn ctx_get = nullptr;
    CuCtxSetFn ctx_set = nullptr;
    CuImportFn import_mem = nullptr;
    CuMapFn map_mem = nullptr;
    CuDestroyFn destroy_mem = nullptr;
    CuModuleLoadFn module_load = nullptr;
    CuModuleFunctionFn module_function = nullptr;
    CuLaunchFn launch = nullptr;
    CuSyncFn sync = nullptr;
    CuAllocFn alloc = nullptr;
    CuFreeFn free_mem = nullptr;
    CuErrorFn error_string = nullptr;
    CuPrimaryReleaseFn primary_release = nullptr;

    CUfunction nv12_in = nullptr;
    CUfunction p010_in = nullptr;
    CUfunction yuv_prepare = nullptr;
    CUfunction yuv_vertical = nullptr;
    CUfunction nv12_out = nullptr;
    CUfunction p010_out = nullptr;
    CUfunction rgba16f_copy = nullptr;

    decltype(&reshade_process_d3d12) process_d3d12_fn = nullptr;
    decltype(&reshade_session_d3d12_device) session_device_fn = nullptr;

    std::map<void*, SessionBuffers> sessions;
    bool ready = false;

    template<class T>
    bool loadSymbol(T& out, const char* name) {
        out = reinterpret_cast<T>(GetProcAddress(cuda, name));
        return out != nullptr;
    }

    bool setError(const std::string& msg, char* error, int capacity) {
        if (error && capacity > 0) {
            strncpy_s(error, size_t(capacity), msg.c_str(), _TRUNCATE);
        }
        return false;
    }

    struct ContextGuard {
        CUcontext prev = nullptr;
        CuCtxSetFn set_fn = nullptr;
        bool active = false;

        ContextGuard(CUcontext target, CuCtxGetFn get_fn, CuCtxSetFn s_fn) : set_fn(s_fn) {
            if (target && get_fn && set_fn && get_fn(&prev) == CUDA_SUCCESS) {
                if (set_fn(target) == CUDA_SUCCESS) {
                    active = true;
                }
            }
        }
        ~ContextGuard() {
            if (active && set_fn) {
                set_fn(prev);
            }
        }
    };

    bool createSharedBuffer(ID3D12Device* device, uint64_t bytes, D3D12_RESOURCE_STATES state, SharedBuffer& out, char* error, int capacity) {
        out.heap_bytes = (bytes + 0xffffull) & ~0xffffull;
        D3D12_HEAP_DESC heap_desc{};
        heap_desc.SizeInBytes = out.heap_bytes;
        heap_desc.Properties.Type = D3D12_HEAP_TYPE_DEFAULT;
        heap_desc.Flags = D3D12_HEAP_FLAG_SHARED;
        HRESULT hr = device->CreateHeap(&heap_desc, IID_PPV_ARGS(&out.heap));
        if (FAILED(hr)) {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "CreateHeap failed with HRESULT 0x%08X", (unsigned)hr);
            return setError(buf, error, capacity);
        }
        D3D12_RESOURCE_DESC desc{};
        desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
        desc.Width = bytes;
        desc.Height = 1;
        desc.DepthOrArraySize = 1;
        desc.MipLevels = 1;
        desc.SampleDesc.Count = 1;
        desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
        hr = device->CreatePlacedResource(out.heap.Get(), 0, &desc, state, nullptr, IID_PPV_ARGS(&out.resource));
        if (FAILED(hr)) {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "CreatePlacedResource failed with HRESULT 0x%08X", (unsigned)hr);
            return setError(buf, error, capacity);
        }
        HANDLE handle = nullptr;
        hr = device->CreateSharedHandle(out.heap.Get(), nullptr, GENERIC_ALL, nullptr, &handle);
        if (FAILED(hr)) {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "CreateSharedHandle failed with HRESULT 0x%08X", (unsigned)hr);
            return setError(buf, error, capacity);
        }
        CuExternalMemoryHandleDesc mem{};
        mem.type = CU_EXTERNAL_MEMORY_HANDLE_TYPE_D3D12_HEAP;
        mem.handle.win32.handle = handle;
        mem.size = out.heap_bytes;
        mem.flags = 0;
        const CUresult res = import_mem(&out.external, &mem);
        CloseHandle(handle);
        if (res != CUDA_SUCCESS) {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "cuImportExternalMemory failed with code %d", (int)res);
            return setError(buf, error, capacity);
        }
        CuExternalMemoryBufferDesc buf_desc{};
        buf_desc.offset = 0;
        buf_desc.size = bytes;
        buf_desc.flags = 0;
        const CUresult map_res = map_mem(&out.pointer, out.external, &buf_desc);
        if (map_res != CUDA_SUCCESS) {
            char buf[128];
            std::snprintf(buf, sizeof(buf), "cuExternalMemoryGetMappedBuffer failed with code %d", (int)map_res);
            return setError(buf, error, capacity);
        }
        return true;
    }

    bool ensureSessionBuffers(void* sessionHandle, SessionBuffers*& out_s, char* error, int capacity) {
        auto it = sessions.find(sessionHandle);
        if (it != sessions.end()) {
            out_s = &it->second;
            return true;
        }
        if (!session_device_fn) {
            return setError("ReShade D3D12 device query function is not set.", error, capacity);
        }
        ID3D12Device* dev = nullptr;
        if (session_device_fn(sessionHandle, reinterpret_cast<void**>(&dev)) != 1 || !dev) {
            return setError("Cannot query D3D12 device from ReShade session.", error, capacity);
        }

        SessionBuffers s{};
        s.device = dev;
        auto ins = sessions.emplace(sessionHandle, std::move(s));
        out_s = &ins.first->second;
        return true;
    }

    bool setupSessionDimensions(SessionBuffers& s, uint32_t width, uint32_t height, char* error, int capacity) {
        if (s.width == width && s.height == height && s.cuda_input.resource && s.cuda_output.resource) {
            return true;
        }
        s.width = width;
        s.height = height;

        D3D12_RESOURCE_DESC tex_desc{};
        tex_desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
        tex_desc.Width = width;
        tex_desc.Height = height;
        tex_desc.DepthOrArraySize = 1;
        tex_desc.MipLevels = 1;
        tex_desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
        tex_desc.SampleDesc.Count = 1;
        s.device->GetCopyableFootprints(&tex_desc, 0, 1, 0, &s.cuda_fp, nullptr, nullptr, &s.cuda_buffer_bytes);

        if (!createSharedBuffer(s.device, s.cuda_buffer_bytes, D3D12_RESOURCE_STATE_COPY_SOURCE, s.cuda_input, error, capacity) ||
            !createSharedBuffer(s.device, s.cuda_buffer_bytes, D3D12_RESOURCE_STATE_COPY_DEST, s.cuda_output, error, capacity)) {
            return false;
        }
        return true;
    }

    CudaSurface* acquireSurface(SessionBuffers& s, uint32_t format) {
        for (auto* slot : s.surface_pool.slots) {
            if (slot->format == format && slot->references.load() == 0) {
                slot->references.store(1);
                return slot;
            }
        }
        if (s.surface_pool.slots.size() >= MAX_OUTPUT_SURFACES) {
            return nullptr;
        }
        auto* surface = new (std::nothrow) CudaSurface();
        if (!surface) return nullptr;
        surface->width = s.width;
        surface->height = s.height;
        surface->format = format;
        surface->pool = &s.surface_pool;
        const uint32_t elem_bytes = (format == VE_RESHADE_FORMAT_RGBA16F) ? 8u
                                  : (format == VE_RESHADE_FORMAT_P010) ? 2u : 1u;
        surface->stride = (s.width * elem_bytes + 255u) & ~255u;
        const uint64_t bytes = uint64_t(surface->stride) *
            (format == VE_RESHADE_FORMAT_RGBA16F ? s.height : (s.height + (s.height + 1) / 2));
        if (alloc(&surface->pointer, size_t(bytes)) != CUDA_SUCCESS) {
            delete surface;
            return nullptr;
        }
        s.surface_pool.slots.push_back(surface);
        return surface;
    }

    void freeSharedBuffer(SharedBuffer& buf) {
        if (buf.pointer && destroy_mem && buf.external) {
            destroy_mem(buf.external);
            buf.external = nullptr;
            buf.pointer = 0;
        }
        buf.resource.Reset();
        buf.heap.Reset();
    }

    void releaseSessionBuffersInternal(SessionBuffers& s) {
        ContextGuard guard(cuda_context, ctx_get, ctx_set);
        freeSharedBuffer(s.cuda_input);
        freeSharedBuffer(s.cuda_output);
        if (s.chroma_full && free_mem) {
            free_mem(s.chroma_full);
            s.chroma_full = 0;
        }
        if (s.chroma_vertical && free_mem) {
            free_mem(s.chroma_vertical);
            s.chroma_vertical = 0;
        }
        for (auto* surface : s.surface_pool.slots) {
            if (surface->pointer && free_mem) {
                free_mem(surface->pointer);
            }
            delete surface;
        }
        s.surface_pool.slots.clear();
    }
};

ReShadeCudaAdapter::ReShadeCudaAdapter() : impl_(std::make_unique<Impl>()) {}
ReShadeCudaAdapter::~ReShadeCudaAdapter() = default;

ReShadeCudaAdapter& ReShadeCudaAdapter::instance() {
    static ReShadeCudaAdapter s_instance;
    return s_instance;
}

bool ReShadeCudaAdapter::isAvailable() const {
    return impl_->ready;
}

bool ReShadeCudaAdapter::initialize(int ordinal) {
    std::lock_guard lock(impl_->mutex);
    if (impl_->ready && impl_->ordinal == ordinal) {
        return true;
    }

    if (!impl_->cuda) {
        impl_->cuda = LoadLibraryA("nvcuda.dll");
        if (!impl_->cuda) {
            return false;
        }
    }

    CuPrimaryRetainFn retain = nullptr;
    CuPrimaryStateFn primary_state = nullptr;
    CuPrimaryFlagsFn primary_flags = nullptr;
    CuInitFn cu_init = nullptr;
    CuDeviceGetFn cu_dev_get = nullptr;

    if (!impl_->loadSymbol(cu_init, "cuInit") ||
        !impl_->loadSymbol(cu_dev_get, "cuDeviceGet") ||
        !impl_->loadSymbol(retain, "cuDevicePrimaryCtxRetain") ||
        !impl_->loadSymbol(impl_->primary_release, "cuDevicePrimaryCtxRelease") ||
        !impl_->loadSymbol(primary_state, "cuDevicePrimaryCtxGetState") ||
        !impl_->loadSymbol(primary_flags, "cuDevicePrimaryCtxSetFlags_v2") ||
        !impl_->loadSymbol(impl_->ctx_get, "cuCtxGetCurrent") ||
        !impl_->loadSymbol(impl_->ctx_set, "cuCtxSetCurrent") ||
        !impl_->loadSymbol(impl_->import_mem, "cuImportExternalMemory") ||
        !impl_->loadSymbol(impl_->map_mem, "cuExternalMemoryGetMappedBuffer") ||
        !impl_->loadSymbol(impl_->destroy_mem, "cuDestroyExternalMemory") ||
        !impl_->loadSymbol(impl_->module_load, "cuModuleLoadData") ||
        !impl_->loadSymbol(impl_->module_function, "cuModuleGetFunction") ||
        !impl_->loadSymbol(impl_->launch, "cuLaunchKernel") ||
        !impl_->loadSymbol(impl_->sync, "cuStreamSynchronize") ||
        !impl_->loadSymbol(impl_->alloc, "cuMemAlloc_v2") ||
        !impl_->loadSymbol(impl_->free_mem, "cuMemFree_v2") ||
        !impl_->loadSymbol(impl_->error_string, "cuGetErrorString")) {
        return false;
    }

    if (cu_init(0) != 0 || cu_dev_get(&impl_->cuda_device, ordinal) != 0) {
        return false;
    }

    unsigned flags = 0;
    int active = 0;
    if (primary_state(impl_->cuda_device, &flags, &active) != CUDA_SUCCESS) {
        return false;
    }
    if (!active) {
        primary_flags(impl_->cuda_device, 0x04u); // CU_CTX_SCHED_BLOCKING_SYNC
    }
    if (retain(&impl_->cuda_context, impl_->cuda_device) != CUDA_SUCCESS) {
        return false;
    }

    Impl::ContextGuard guard(impl_->cuda_context, impl_->ctx_get, impl_->ctx_set);
    if (impl_->module_load(&impl_->cuda_module, kReShadeKernelPtx) != CUDA_SUCCESS ||
        impl_->module_function(&impl_->nv12_in, impl_->cuda_module, "rs_nv12_to_rgba16f") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->p010_in, impl_->cuda_module, "rs_p010_to_rgba16f") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->yuv_prepare, impl_->cuda_module, "rs_prepare_420") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->yuv_vertical, impl_->cuda_module, "rs_filter_420_vertical") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->nv12_out, impl_->cuda_module, "rs_420_uv_to_nv12") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->p010_out, impl_->cuda_module, "rs_420_uv_to_p010") != CUDA_SUCCESS ||
        impl_->module_function(&impl_->rgba16f_copy, impl_->cuda_module, "rs_copy_rgba16f") != CUDA_SUCCESS) {
        return false;
    }

    // Dynamic bind of ReShade D3D12 bridge functions from neuroframe_engine_reshade.dll
    const HMODULE rs_dll = GetModuleHandleW(L"neuroframe_engine_reshade.dll");
    if (rs_dll) {
        impl_->process_d3d12_fn = reinterpret_cast<decltype(&reshade_process_d3d12)>(
            GetProcAddress(rs_dll, "reshade_process_d3d12"));
        impl_->session_device_fn = reinterpret_cast<decltype(&reshade_session_d3d12_device)>(
            GetProcAddress(rs_dll, "reshade_session_d3d12_device"));
    }

    impl_->ordinal = ordinal;
    impl_->ready = true;
    return true;
}

int ReShadeCudaAdapter::processCudaYuv(void* sessionHandle,
                                      const VeReShadeCudaYuvDesc* frame,
                                      void** outSurface,
                                      VeReShadeFrameResult* result,
                                      char* error,
                                      int capacity) {
    std::lock_guard lock(impl_->mutex);
    if (outSurface) *outSurface = nullptr;
    if (!impl_->ready) {
        return impl_->setError("ReShade CUDA adapter is not initialized.", error, capacity) ? 1 : 0;
    }
    if (!sessionHandle || !frame || !outSurface || !result ||
        frame->size < sizeof(VeReShadeCudaYuvDesc) || frame->abi != VE_RESHADE_ABI_VERSION ||
        result->size < sizeof(VeReShadeFrameResult) || result->abi != VE_RESHADE_ABI_VERSION ||
        !frame->y_pointer || !frame->uv_pointer ||
        (frame->pixel_format != VE_RESHADE_FORMAT_NV12 && frame->pixel_format != VE_RESHADE_FORMAT_P010) ||
        (frame->output_format != VE_RESHADE_FORMAT_NV12 &&
         frame->output_format != VE_RESHADE_FORMAT_P010 &&
         frame->output_format != VE_RESHADE_FORMAT_RGBA16F)) {
        return impl_->setError("Invalid VeReShadeCudaYuvDesc parameters.", error, capacity) ? 1 : 0;
    }

    Impl::ContextGuard guard(impl_->cuda_context, impl_->ctx_get, impl_->ctx_set);

    SessionBuffers* s = nullptr;
    if (!impl_->ensureSessionBuffers(sessionHandle, s, error, capacity) ||
        !impl_->setupSessionDimensions(*s, frame->width, frame->height, error, capacity)) {
        return 0;
    }

    // 1. Launch CUDA YUV -> RGBA16F into shared input staging buffer
    uint64_t y_ptr = frame->y_pointer;
    uint64_t uv_ptr = frame->uv_pointer;
    uint64_t dst_ptr = s->cuda_input.pointer;
    int ys = int(frame->y_stride);
    int uvs = int(frame->uv_stride);
    int sw = int(frame->width);
    int sh = int(frame->height);
    size_t pitch = s->cuda_fp.Footprint.RowPitch;
    int ow = int(s->width);
    int oh = int(s->height);
    int mat = int(frame->color_matrix);
    int rng = int(frame->color_range);
    int rot = int(frame->rotation);
    int chr = int(frame->chroma_location);
    void* args[] = {&y_ptr, &uv_ptr, &ys, &uvs, &sw, &sh, &dst_ptr, &pitch, &ow, &oh, &mat, &rng, &rot, &chr};

    const CUfunction in_kernel = (frame->pixel_format == VE_RESHADE_FORMAT_P010) ? impl_->p010_in : impl_->nv12_in;
    if (impl_->launch(in_kernel, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, args, nullptr) != CUDA_SUCCESS ||
        impl_->sync(nullptr) != CUDA_SUCCESS) {
        return impl_->setError("CUDA YUV input kernel conversion failed.", error, capacity) ? 1 : 0;
    }

    // 2. Process frame on D3D12 ReShade session
    if (!impl_->process_d3d12_fn) {
        const HMODULE rs_dll = GetModuleHandleW(L"neuroframe_engine_reshade.dll");
        if (rs_dll) {
            impl_->process_d3d12_fn = reinterpret_cast<decltype(&reshade_process_d3d12)>(
                GetProcAddress(rs_dll, "reshade_process_d3d12"));
        }
    }
    if (!impl_->process_d3d12_fn) {
        return impl_->setError("reshade_process_d3d12 function export unavailable.", error, capacity) ? 1 : 0;
    }

    VeReShadeD3d12FrameDesc d3d_frame{};
    d3d_frame.size = sizeof(d3d_frame);
    d3d_frame.abi = VE_RESHADE_ABI_VERSION;
    d3d_frame.input_resource = s->cuda_input.resource.Get();
    d3d_frame.output_resource = s->cuda_output.resource.Get();
    d3d_frame.input_state = D3D12_RESOURCE_STATE_COPY_SOURCE;
    d3d_frame.output_state = D3D12_RESOURCE_STATE_COPY_DEST;
    d3d_frame.frame_ordinal = frame->frame_ordinal;
    d3d_frame.pts_ns = frame->pts_ns;
    d3d_frame.duration_ns = frame->duration_ns;
    d3d_frame.reset = frame->reset;

    if (impl_->process_d3d12_fn(sessionHandle, &d3d_frame, result, error, capacity) != 1) {
        return 0;
    }

    // 3. Convert shared output buffer into requested format in pooled surface
    auto* surface = impl_->acquireSurface(*s, frame->output_format);
    if (!surface) {
        return impl_->setError("Failed to acquire ReShade CUDA surface.", error, capacity) ? 1 : 0;
    }

    uint64_t src = s->cuda_output.pointer;
    uint64_t dst = surface->pointer;
    size_t src_pitch = s->cuda_fp.Footprint.RowPitch;
    size_t dst_pitch = surface->stride;
    int w = int(s->width);
    int h = int(s->height);
    bool ok = false;

    if (frame->output_format == VE_RESHADE_FORMAT_RGBA16F) {
        void* copy_args[] = {&src, &src_pitch, &dst, &dst_pitch, &w, &h};
        ok = (impl_->launch(impl_->rgba16f_copy, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, copy_args, nullptr) == CUDA_SUCCESS);
    } else {
        uint64_t uv = dst + uint64_t(surface->stride) * s->height;
        int stride = int(surface->stride);
        int out_p010 = (frame->output_format == VE_RESHADE_FORMAT_P010) ? 1 : 0;
        size_t chroma_pitch = size_t(s->width) * 8;
        if ((!s->chroma_full && impl_->alloc(&s->chroma_full, chroma_pitch * s->height) != CUDA_SUCCESS) ||
            (!s->chroma_vertical && impl_->alloc(&s->chroma_vertical, chroma_pitch * ((s->height + 1) / 2)) != CUDA_SUCCESS)) {
            surface->references.store(0);
            return impl_->setError("Allocate ReShade chroma scratch buffer failed.", error, capacity) ? 1 : 0;
        }
        void* prep_args[] = {&src, &src_pitch, &dst, &stride, &w, &h, &mat, &rng, &s->chroma_full, &chroma_pitch, &out_p010};
        void* vert_args[] = {&s->chroma_full, &chroma_pitch, &s->chroma_vertical, &chroma_pitch, &w, &h};
        void* horiz_args[] = {&s->chroma_vertical, &chroma_pitch, &uv, &stride, &w, &h};
        ok = (impl_->launch(impl_->yuv_prepare, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, prep_args, nullptr) == CUDA_SUCCESS) &&
             (impl_->launch(impl_->yuv_vertical, (s->width + 15) / 16, ((s->height + 1) / 2 + 15) / 16, 1, 16, 16, 1, 0, nullptr, vert_args, nullptr) == CUDA_SUCCESS) &&
             (impl_->launch(out_p010 ? impl_->p010_out : impl_->nv12_out, ((s->width + 1) / 2 + 15) / 16, ((s->height + 1) / 2 + 15) / 16, 1, 16, 16, 1, 0, nullptr, horiz_args, nullptr) == CUDA_SUCCESS);
    }

    if (!ok || impl_->sync(nullptr) != CUDA_SUCCESS) {
        surface->references.store(0);
        return impl_->setError("ReShade CUDA output conversion failed.", error, capacity) ? 1 : 0;
    }

    *outSurface = surface;
    return 1;
}

int ReShadeCudaAdapter::processCudaRgb(void* sessionHandle,
                                      const VeReShadeCudaRgbDesc* frame,
                                      void** outSurface,
                                      VeReShadeFrameResult* result,
                                      char* error,
                                      int capacity) {
    std::lock_guard lock(impl_->mutex);
    if (outSurface) *outSurface = nullptr;
    if (!impl_->ready) {
        return impl_->setError("ReShade CUDA adapter is not initialized.", error, capacity) ? 1 : 0;
    }
    if (!sessionHandle || !frame || !outSurface || !result ||
        frame->size < sizeof(VeReShadeCudaRgbDesc) || frame->abi != VE_RESHADE_ABI_VERSION ||
        result->size < sizeof(VeReShadeFrameResult) || result->abi != VE_RESHADE_ABI_VERSION ||
        !frame->pointer ||
        (frame->output_format != VE_RESHADE_FORMAT_RGBA16F &&
         frame->output_format != VE_RESHADE_FORMAT_NV12 &&
         frame->output_format != VE_RESHADE_FORMAT_P010)) {
        return impl_->setError("Invalid VeReShadeCudaRgbDesc parameters.", error, capacity) ? 1 : 0;
    }

    Impl::ContextGuard guard(impl_->cuda_context, impl_->ctx_get, impl_->ctx_set);

    SessionBuffers* s = nullptr;
    if (!impl_->ensureSessionBuffers(sessionHandle, s, error, capacity) ||
        !impl_->setupSessionDimensions(*s, frame->width, frame->height, error, capacity)) {
        return 0;
    }

    // 1. Copy input CUDA RGBA16F into shared input staging buffer
    uint64_t src_ptr = frame->pointer;
    uint64_t dst_ptr = s->cuda_input.pointer;
    size_t src_pitch = frame->stride;
    size_t dst_pitch = s->cuda_fp.Footprint.RowPitch;
    int w = int(s->width);
    int h = int(s->height);
    void* copy_in_args[] = {&src_ptr, &src_pitch, &dst_ptr, &dst_pitch, &w, &h};

    if (impl_->launch(impl_->rgba16f_copy, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, copy_in_args, nullptr) != CUDA_SUCCESS ||
        impl_->sync(nullptr) != CUDA_SUCCESS) {
        return impl_->setError("CUDA RGBA16F input copy failed.", error, capacity) ? 1 : 0;
    }

    // 2. Process frame on D3D12 ReShade session
    if (!impl_->process_d3d12_fn) {
        const HMODULE rs_dll = GetModuleHandleW(L"neuroframe_engine_reshade.dll");
        if (rs_dll) {
            impl_->process_d3d12_fn = reinterpret_cast<decltype(&reshade_process_d3d12)>(
                GetProcAddress(rs_dll, "reshade_process_d3d12"));
        }
    }
    if (!impl_->process_d3d12_fn) {
        return impl_->setError("reshade_process_d3d12 function export unavailable.", error, capacity) ? 1 : 0;
    }

    VeReShadeD3d12FrameDesc d3d_frame{};
    d3d_frame.size = sizeof(d3d_frame);
    d3d_frame.abi = VE_RESHADE_ABI_VERSION;
    d3d_frame.input_resource = s->cuda_input.resource.Get();
    d3d_frame.output_resource = s->cuda_output.resource.Get();
    d3d_frame.input_state = D3D12_RESOURCE_STATE_COPY_SOURCE;
    d3d_frame.output_state = D3D12_RESOURCE_STATE_COPY_DEST;
    d3d_frame.frame_ordinal = frame->frame_ordinal;
    d3d_frame.pts_ns = frame->pts_ns;
    d3d_frame.duration_ns = frame->duration_ns;
    d3d_frame.reset = frame->reset;

    if (impl_->process_d3d12_fn(sessionHandle, &d3d_frame, result, error, capacity) != 1) {
        return 0;
    }

    // 3. Convert shared output buffer into requested format in pooled surface
    auto* surface = impl_->acquireSurface(*s, frame->output_format);
    if (!surface) {
        return impl_->setError("Failed to acquire ReShade CUDA surface.", error, capacity) ? 1 : 0;
    }

    uint64_t src = s->cuda_output.pointer;
    uint64_t dst = surface->pointer;
    bool ok = false;

    if (frame->output_format == VE_RESHADE_FORMAT_RGBA16F) {
        size_t out_dst_pitch = surface->stride;
        void* copy_out_args[] = {&src, &dst_pitch, &dst, &out_dst_pitch, &w, &h};
        ok = (impl_->launch(impl_->rgba16f_copy, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, copy_out_args, nullptr) == CUDA_SUCCESS);
    } else {
        uint64_t uv = dst + uint64_t(surface->stride) * s->height;
        int stride = int(surface->stride);
        int mat = int(frame->color_matrix);
        int rng = int(frame->color_range);
        int out_p010 = (frame->output_format == VE_RESHADE_FORMAT_P010) ? 1 : 0;
        size_t chroma_pitch = size_t(s->width) * 8;
        if ((!s->chroma_full && impl_->alloc(&s->chroma_full, chroma_pitch * s->height) != CUDA_SUCCESS) ||
            (!s->chroma_vertical && impl_->alloc(&s->chroma_vertical, chroma_pitch * ((s->height + 1) / 2)) != CUDA_SUCCESS)) {
            surface->references.store(0);
            return impl_->setError("Allocate ReShade chroma scratch buffer failed.", error, capacity) ? 1 : 0;
        }
        void* prep_args[] = {&src, &dst_pitch, &dst, &stride, &w, &h, &mat, &rng, &s->chroma_full, &chroma_pitch, &out_p010};
        void* vert_args[] = {&s->chroma_full, &chroma_pitch, &s->chroma_vertical, &chroma_pitch, &w, &h};
        void* horiz_args[] = {&s->chroma_vertical, &chroma_pitch, &uv, &stride, &w, &h};
        ok = (impl_->launch(impl_->yuv_prepare, (s->width + 15) / 16, (s->height + 15) / 16, 1, 16, 16, 1, 0, nullptr, prep_args, nullptr) == CUDA_SUCCESS) &&
             (impl_->launch(impl_->yuv_vertical, (s->width + 15) / 16, ((s->height + 1) / 2 + 15) / 16, 1, 16, 16, 1, 0, nullptr, vert_args, nullptr) == CUDA_SUCCESS) &&
             (impl_->launch(out_p010 ? impl_->p010_out : impl_->nv12_out, ((s->width + 1) / 2 + 15) / 16, ((s->height + 1) / 2 + 15) / 16, 1, 16, 16, 1, 0, nullptr, horiz_args, nullptr) == CUDA_SUCCESS);
    }

    if (!ok || impl_->sync(nullptr) != CUDA_SUCCESS) {
        surface->references.store(0);
        return impl_->setError("ReShade CUDA output conversion failed.", error, capacity) ? 1 : 0;
    }

    *outSurface = surface;
    return 1;
}

int ReShadeCudaAdapter::surfaceDesc(void* s, VeReShadeCudaSurfaceDesc* desc) {
    auto* surface = static_cast<CudaSurface*>(s);
    if (!surface || !desc) return 0;
    desc->size = sizeof(VeReShadeCudaSurfaceDesc);
    desc->abi = VE_RESHADE_ABI_VERSION;
    desc->width = surface->width;
    desc->height = surface->height;
    desc->pixel_format = surface->format;
    desc->stride = surface->stride;
    desc->y_pointer = surface->pointer;
    desc->uv_pointer = (surface->format == VE_RESHADE_FORMAT_RGBA16F)
        ? 0
        : (surface->pointer + uint64_t(surface->stride) * surface->height);
    return 1;
}

void ReShadeCudaAdapter::surfaceRetain(void* s) {
    auto* surface = static_cast<CudaSurface*>(s);
    if (surface) {
        surface->references.fetch_add(1);
    }
}

void ReShadeCudaAdapter::surfaceRelease(void* s) {
    auto* surface = static_cast<CudaSurface*>(s);
    if (!surface) return;
    if (surface->references.fetch_sub(1) <= 1) {
        surface->references.store(0);
    }
}

void ReShadeCudaAdapter::releaseSessionBuffers(void* sessionHandle) {
    std::lock_guard lock(impl_->mutex);
    auto it = impl_->sessions.find(sessionHandle);
    if (it != impl_->sessions.end()) {
        impl_->releaseSessionBuffersInternal(it->second);
        impl_->sessions.erase(it);
    }
}

} // namespace ve
