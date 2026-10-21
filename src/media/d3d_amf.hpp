#pragma once
#include "media.hpp"
#include "packet_stream.hpp"
#include <memory>
namespace ve {
struct Settings;
class FsrSurface;
// AMD AMF or Intel QSV, selected by the supplied DirectX device's adapter.
// QSV is derived from this exact D3D11 device and requires a direct GPU map.
class D3dSharedEncoder {
  public:
    D3dSharedEncoder(const QString& path,const QString& codec,const Settings& settings,
        const MediaInfo& output,void* device,JobControl& control,bool allIntra=false);
    ~D3dSharedEncoder();
    void write(const FsrSurface& surface,qint64 pts,qint64 duration,TimeBase timeBase,bool forceIntra=false);
    void finish();
    [[nodiscard]] QString diagnostics() const;
  private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
}
