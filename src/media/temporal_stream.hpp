#pragma once
#include "frame_time.hpp"
#include <algorithm>
#include <vector>

namespace ve {
template<class Surface> struct TemporalSample {Surface pixels;FrameTime time;int segment=0;};
// Common temporal publication contract for native DLSS and FSR results.
// Each call owns just one SDK result; consumers stream it into the next level.
template<class Surface,class Result>
std::vector<TemporalSample<Surface>> publishTemporalResult(std::optional<TemporalSample<Surface>>& previous,
    Result result,FrameTime time,int segment,int generated) {
    if(previous){
        segment=std::max(segment,previous->segment);
        if(result.sceneCut||result.reset)segment=std::max(segment,previous->segment+1);
    }
    std::vector<TemporalSample<Surface>> frames;
    if(previous&&!result.reset&&!result.sceneCut){
        if(generated<0||result.generated.size()!=size_t(generated))throw std::runtime_error("Native interpolator returned an incomplete temporal grid");
        const auto interval=time-previous->time;int index=0;
        for(auto& surface:result.generated)frames.push_back({std::move(surface),previous->time+interval.fraction(++index,generated+1),segment});
    }
    previous=TemporalSample<Surface>{std::move(result.source),time,segment};frames.push_back(*previous);return frames;
}
}
