#pragma once
#include "media.hpp"
#include "packet_stream.hpp"
#include <memory>
#include <optional>

namespace ve {
struct D3dVideoFrame {
    std::shared_ptr<void> owner;
    QSize size;
    void* texture=nullptr;void* readyFence=nullptr;quint64 readyValue=0;
    uint32_t pixelFormat=0,colorMatrix=1,colorRange=0,chromaLocation=1;
    qint64 pts=0,duration=0;TimeBase timeBase;
};
struct D3dVideoUnavailable:std::runtime_error{using std::runtime_error::runtime_error;};
class D3dVideoDecoder {
  public:
    D3dVideoDecoder(const QString& path,void* device,double startSeconds,JobControl& control);
    ~D3dVideoDecoder();
    [[nodiscard]] std::optional<D3dVideoFrame> read();
    [[nodiscard]] double timeOrigin() const;
  private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
// Some drivers require reference-only D3D12 decode allocations which older
// FFmpeg builds cannot recycle correctly. This fallback shares native D3D11
// decode textures and copies their selected planes on the GPU into D3D12.
class D3d11VideoDecoder {
  public:
    D3d11VideoDecoder(const QString& path,void* device,void* queue,double startSeconds,JobControl& control);
    ~D3d11VideoDecoder();
    [[nodiscard]] std::optional<D3dVideoFrame> read();
    [[nodiscard]] double timeOrigin() const;
  private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
}
