#pragma once
#include "fsr_native.hpp"
#include "media/temporal_stream.hpp"
#include <functional>

namespace ve {
class FsrTemporalCascade {
  public:
    using Sample=TemporalSample<FsrSurface>;
    using Consumer=std::function<void(Sample)>;
    struct Statistics {quint64 sourceFrames=0,generated=0,sceneCuts=0,duplicates=0,uploadBytes=0,downloadBytes=0;double milliseconds=0;int levels=0,cadencePromotions=0;};
    FsrTemporalCascade(std::shared_ptr<FsrDevice> device,QSize size,bool hdr,TimeBase sourceInterval,
        TimeBase targetInterval,bool variableRate=false,float maximumNits=1000,FrameTime clockOffset={});
    ~FsrTemporalCascade();
    void push(const VeFsrFrame& frame,const Consumer& consume,bool reset=false);
    void push(const QImage& image,const MediaInfo& colors,qint64 pts,qint64 duration,TimeBase base,const Consumer& consume,bool reset=false);
    [[nodiscard]] Statistics statistics() const;
  private:
    struct Impl;std::unique_ptr<Impl> impl_;
};
}
