#pragma once
#include "fsr_frame_generation/fsr_bridge.h"
#include "media/media.hpp"
#include "media/packet_stream.hpp"
#include <QImage>
#include <QString>
#include <memory>
#include <vector>

namespace ve {
struct D3dVideoFrame;
struct FsrAdapter {
    uint32_t index=0,vendor=0,device=0,luidLow=0;int32_t luidHigh=0;
    quint64 memoryBytes=0;
    QString id,name,unavailableReason;
    bool available=false;
};
[[nodiscard]] std::vector<FsrAdapter> detectFsrAdapters(JobControl* control=nullptr);
[[nodiscard]] uint32_t fsrAdapterIndex(const QString& selection);
[[nodiscard]] uint32_t fsrTransfer(const QString& transfer);
[[nodiscard]] uint32_t fsrPrimaries(const QString& primaries);
[[nodiscard]] float fsrMaximumLuminance(const MediaInfo& colors);
// Make missing SDR/HDR color tags explicit before passing images to the SDK.
// Assumptions are recorded in metadata for diagnostics and delivery reports.
[[nodiscard]] MediaInfo fsrVideoColors(MediaInfo colors);
[[nodiscard]] QByteArray fsrAdapterCacheIdentity();
class FsrSurface;
class FsrDevice {
  public:
    explicit FsrDevice(const QString& selection="auto");
    ~FsrDevice();
    FsrDevice(const FsrDevice&)=delete;FsrDevice& operator=(const FsrDevice&)=delete;
    [[nodiscard]] void* nativeDevice() const;
    [[nodiscard]] void* nativeQueue() const;
    [[nodiscard]] FsrSurface uploadImage(const QImage& image,const MediaInfo& colors) const;
    [[nodiscard]] const VeFsrCapabilities& capabilities() const {return capabilities_;}
  private:
    VeFsrDevice* handle_=nullptr;VeFsrCapabilities capabilities_{};
    friend class FsrInterpolation;
};
class FsrSurface {
  public:
    FsrSurface()=default;
    [[nodiscard]] explicit operator bool() const{return bool(state_);}
    [[nodiscard]] const VeFsrSurfaceDesc& description() const;
    [[nodiscard]] QImage toHost(const MediaInfo& colors) const;
    void writeVideo(const VeFsrVideoOutput& output) const;
  private:
    struct Storage;std::shared_ptr<Storage> state_;
    explicit FsrSurface(VeFsrSurface* handle);
    friend class FsrInterpolation;
    friend class FsrDevice;
};
class FsrInterpolation {
  public:
    struct Result {
        FsrSurface source;std::vector<FsrSurface> generated;
        bool reset=false,sceneCut=false,duplicate=false;
        quint64 uploadBytes=0,downloadBytes=0;double milliseconds=0;
    };
    FsrInterpolation(std::shared_ptr<FsrDevice> device,QSize size,bool hdr,int poolSize=16,float maximumNits=1000);
    ~FsrInterpolation();
    FsrInterpolation(const FsrInterpolation&)=delete;FsrInterpolation& operator=(const FsrInterpolation&)=delete;
    [[nodiscard]] Result evaluate(const VeFsrFrame& input,bool reset=false);
    [[nodiscard]] Result evaluate(const QImage& input,const MediaInfo& colors,qint64 pts,qint64 duration,TimeBase base,bool reset=false);
    [[nodiscard]] Result evaluate(const FsrSurface& input,qint64 pts,qint64 duration,TimeBase base,bool reset=false);
  private:
    std::shared_ptr<FsrDevice> device_;VeFsrSession* handle_=nullptr;
};
}
