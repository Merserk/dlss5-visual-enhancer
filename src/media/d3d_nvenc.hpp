#pragma once
#include "media.hpp"
#include "packet_stream.hpp"
#include <memory>

namespace ve {
struct Settings;
class FsrSurface;
// Uses the driver's DirectX interface. Does not load or initialize CUDA.
class D3dNvEncoder {
  public:
    D3dNvEncoder(const QString& path,const QString& codec,const Settings& settings,
        const MediaInfo& output,void* device,void* queue,JobControl& control,bool allIntra=false);
    ~D3dNvEncoder();
    void write(const FsrSurface& surface,qint64 pts,qint64 duration,TimeBase timeBase,bool forceIntra=false);
    void finish();
    [[nodiscard]] bool tenBit() const;
    [[nodiscard]] QString diagnostics() const;
  private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
}
