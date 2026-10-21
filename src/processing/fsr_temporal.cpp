#include "fsr_temporal.hpp"
#include "media/temporal_cadence.hpp"

namespace ve {
struct FsrTemporalCascade::Impl {
    struct Stage {std::unique_ptr<FsrInterpolation> session;std::optional<Sample> previous;std::optional<int> inputSegment;};
    std::vector<Stage> stages;Statistics stats;FrameTime nominal,clockOffset;TimeBase targetInterval;
    std::shared_ptr<FsrDevice> device;QSize size;bool hdr;float nits;
    Impl(std::shared_ptr<FsrDevice> selectedDevice,QSize selectedSize,bool selectedHdr,TimeBase source,TimeBase target,bool variable,float selectedNits,FrameTime offset)
        :nominal(source.numerator,source.denominator),clockOffset(offset),targetInterval(target),device(std::move(selectedDevice)),size(selectedSize),hdr(selectedHdr),nits(selectedNits){
        stats.levels=midpointCascadeDepth(source,target,variable);
        for(int i=0;i<std::max(1,stats.levels);++i)stages.push_back({std::make_unique<FsrInterpolation>(device,size,hdr,16,nits),{}});
    }
    void adapt(FrameTime time){
        if(!stages.front().previous)return;const auto depth=midpointIntervalDepth(stages.front().previous->time,time,targetInterval,clockOffset,stats.levels);
        if(depth<=stats.levels)return;
        const auto oldCount=stages.size();const auto tail=stages.back().previous;
        while(stages.size()<size_t(std::max(1,depth)))stages.push_back({std::make_unique<FsrInterpolation>(device,size,hdr,16,nits),{}});
        stats.levels=depth;++stats.cadencePromotions;
        // Seed only the added contexts from the preceding anchor. Avoid
        // publishing a duplicate timestamp to the delivery sampler.
        if(tail)feed(oldCount,*tail,[](Sample){});
    }
    void account(const FsrInterpolation::Result& result,bool source){
        stats.generated+=result.generated.size();stats.uploadBytes+=result.uploadBytes;stats.downloadBytes+=result.downloadBytes;stats.milliseconds+=result.milliseconds;
        if(source){++stats.sourceFrames;stats.sceneCuts+=result.sceneCut;stats.duplicates+=result.duplicate;}
    }
    std::pair<bool,int> discontinuity(FrameTime time,bool reset)const{
        const auto& previous=stages.front().previous;if(!previous)return{true,0};
        if(time<=previous->time)throw std::runtime_error("FSR source presentation timestamps must increase");
        const bool discontinuity=reset||time-previous->time>nominal.fraction(2,1);
        return{discontinuity,previous->segment+(discontinuity?1:0)};
    }
    void feed(size_t level,Sample sample,const Consumer& consume){
        if(level==stages.size()){consume(std::move(sample));return;}
        auto& stage=stages[level];const auto interval=stage.previous?sample.time-stage.previous->time:nominal.fraction(1,std::int64_t(1)<<level);
        const auto pts=sample.time.ticks(1,60000000),duration=std::max(std::int64_t(1),interval.ticks(1,60000000));
        auto result=stage.session->evaluate(sample.pixels,pts,duration,{1,60000000},!stage.inputSegment||sample.segment!=*stage.inputSegment);stage.inputSegment=sample.segment;account(result,false);
        for(auto& frame:publishTemporalResult(stage.previous,std::move(result),sample.time,sample.segment,1))feed(level+1,std::move(frame),consume);
    }
    void publish(FsrInterpolation::Result result,FrameTime time,int segment,const Consumer& consume){
        if(!result.reset&&!result.sceneCut)adapt(time);
        account(result,true);if(stats.levels==0)result.generated.clear();
        for(auto& sample:publishTemporalResult(stages.front().previous,std::move(result),time,segment,stats.levels==0?0:1))feed(1,std::move(sample),consume);
    }
};
FsrTemporalCascade::FsrTemporalCascade(std::shared_ptr<FsrDevice> device,QSize size,bool hdr,TimeBase source,TimeBase target,bool variable,float nits,FrameTime offset)
    :impl_(std::make_unique<Impl>(std::move(device),size,hdr,source,target,variable,nits,offset)){}
FsrTemporalCascade::~FsrTemporalCascade()=default;
FsrTemporalCascade::Statistics FsrTemporalCascade::statistics()const{return impl_->stats;}
void FsrTemporalCascade::push(const VeFsrFrame& frame,const Consumer& consume,bool reset){
    if(frame.pts<0||frame.timebase_numerator<=0||frame.timebase_denominator<=0)throw std::runtime_error("Invalid FSR frame presentation time");
    const FrameTime time=FrameTime(frame.pts).fraction(frame.timebase_numerator,frame.timebase_denominator);
    const auto [discontinuity,segment]=impl_->discontinuity(time,reset);impl_->publish(impl_->stages.front().session->evaluate(frame,discontinuity),time,segment,consume);
}
void FsrTemporalCascade::push(const QImage& image,const MediaInfo& colors,qint64 pts,qint64 duration,TimeBase base,const Consumer& consume,bool reset){
    if(pts<0||base.numerator<=0||base.denominator<=0)throw std::runtime_error("Invalid FSR host frame presentation time");
    const FrameTime time=FrameTime(pts).fraction(base.numerator,base.denominator);const auto [discontinuity,segment]=impl_->discontinuity(time,reset);
    impl_->publish(impl_->stages.front().session->evaluate(image,colors,pts,duration,base,discontinuity),time,segment,consume);
}
}
