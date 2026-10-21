#pragma once
#include "frame_time.hpp"
#include "packet_stream.hpp"
#include <numeric>

namespace ve {
struct TemporalClockAlignment {FrameTime phase,relativeStart;std::int64_t firstIndex=0;};
inline TemporalClockAlignment alignTemporalClock(std::int64_t originTicks,TimeBase source,TimeBase target){
    if(source.numerator<=0||source.denominator<=0||target.numerator<=0||target.denominator<=0||originTicks==INT64_MIN)throw std::invalid_argument("Invalid temporal clock alignment");
    const auto origin=FrameTime(originTicks<0?-originTicks:originTicks).fraction(source.numerator,source.denominator);
    const auto first=originTicks>=0?origin.ceilTicks(target.numerator,target.denominator):0;
    const auto aligned=FrameTime(target.numerator,target.denominator).fraction(first,1);
    return{originTicks>=0?aligned-origin:aligned+origin,aligned,first};
}
// A midpoint SDK can only synthesize a dyadic time grid. Pick the smallest
// depth that covers the requested rate and bounds selection error to 1/16 of
// an output interval. Exact dyadic rates need no unnecessary extra cascades.
// Variable-rate intervals can be up to twice nominal before a discontinuity
// reset, and can have arbitrary phase relative to the delivery clock.
inline int midpointCascadeDepth(TimeBase sourceInterval,TimeBase targetInterval,bool variableRate=false) {
    if(sourceInterval.numerator<=0||sourceInterval.denominator<=0||targetInterval.numerator<=0||targetInterval.denominator<=0)
        throw std::invalid_argument("Frame-rate conversion requires positive rational intervals");
    const FrameTime source(sourceInterval.numerator,sourceInterval.denominator),target(targetInterval.numerator,targetInterval.denominator);
    if(target>=source&&!variableRate)return 0;
    std::int64_t numerator=std::int64_t(sourceInterval.numerator)*targetInterval.denominator;
    std::int64_t denominator=std::int64_t(sourceInterval.denominator)*targetInterval.numerator;
    const auto common=std::gcd(numerator,denominator);numerator/=common;denominator/=common;
    for(int depth=0;depth<=10;++depth){
        const std::int64_t samples=std::int64_t(1)<<depth;
        if(source.fraction(1,samples)>target)continue;
        const auto phases=numerator/std::gcd(numerator,samples);
        const auto error=variableRate?source.fraction(1,samples):source.fraction(phases/2,phases).fraction(1,samples);
        if(error<=target.fraction(1,16))return depth;
    }
    throw std::runtime_error("Requested frame rate exceeds the supported temporal cascade depth");
}
// Inspect the actual output clock over an observed source interval. Header
// averages can report CFR even when presentation timestamps later vary.
inline int midpointIntervalDepth(FrameTime previous,FrameTime current,TimeBase targetInterval,FrameTime offset,int minimum){
    if(current<=previous||targetInterval.numerator<=0||targetInterval.denominator<=0||minimum<0||minimum>10)
        throw std::invalid_argument("Invalid observed interpolation interval");
    const auto duration=current-previous,target=FrameTime(targetInterval.numerator,targetInterval.denominator),tolerance=target.fraction(1,16);
    const auto first=previous>=offset?(previous-offset).ceilTicks(targetInterval.numerator,targetInterval.denominator):0;
    for(int depth=minimum;depth<=10;++depth){
        const auto grid=duration.fraction(1,std::int64_t(1)<<depth);if(grid>target)continue;bool fits=true;
        for(auto index=first;;++index){
            const auto point=offset+target.fraction(index,1);if(point>current)break;
            const auto relative=point-previous,rounded=relative.roundedTo(grid);const auto error=relative>=rounded?relative-rounded:rounded-relative;
            if(error>tolerance){fits=false;break;}
        }
        if(fits)return depth;
    }
    throw std::runtime_error("Observed frame timing exceeds the supported temporal cascade depth");
}
}
