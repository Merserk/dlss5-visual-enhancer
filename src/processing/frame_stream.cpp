#include "frame_stream.hpp"
#include "media/resolution.hpp"
#include "infrastructure/paths.hpp"
#include "pipeline.hpp"
#include "preview_cache.hpp"
#include "vfx_video.hpp"
#include "vfx_native.hpp"
#include <cmath>
#include <utility>

namespace ve {
namespace {
struct Stage {
    Settings settings;
    MediaInfo info;
    QString name;
    TimedFrameSource source;
    JobControl &control;
    bool finalOutput, rgb10Input;
    qint64 index = 0;
    std::optional<double> previousSeconds;
    std::unique_ptr<FramePipeline> native;
    std::unique_ptr<VideoFilterStage> filter;
    std::unique_ptr<VideoFilterStage> normalizer;
    PreviewFrameCache *cache = nullptr;
    QByteArray key;
    bool delivered = false;
    bool cacheOutput = true;
    std::optional<TimedImage> read() {
        if (cache && delivered)
            return {};
        if (cache && cacheOutput)
            if (auto found = cache->find(key)) {
                control.checkpoint();
                delivered = true;
                return TimedImage{std::move(found->pixels), 0, 1, {1, 30}};
            }
        auto frame = next();
        if (cache && frame) {
            control.checkpoint();
            delivered = true;
            if (cacheOutput)
                cache->store(key, {frame->image, info.hdr || name == "rtx_video_hdr", false});
        }
        return frame;
    }
    std::optional<TimedImage> next() {
        control.checkpoint();
        if (filter)
            return filter->read();
        auto frame = normalizer ? normalizer->read() : source();
        if (!frame)
            return {};
        const bool sharedVsr=name=="super_resolution"&&hasVfxStages(settings.stages(true));
        if ((name=="rtx_video_hdr" || name=="super_resolution"&&!sharedVsr) && !normalizer) {
            info.width = frame->image.width();
            info.height = frame->image.height();
            info.pixelFormat = rgb10Input ? "gbrp10le" : frame->image.depth() > 32 ? "rgba64le" : "rgba";
            auto input = [first = std::move(frame), this]() mutable -> std::optional<TimedImage> {
                if (first)
                    return std::exchange(first, std::nullopt);
                return source();
            };
            normalizer = std::make_unique<VideoFilterStage>(QStringList{"rtx_normalization"}, info, settings,
                                                            std::move(input), control);
            frame = normalizer->read();
            if (!frame)
                return {};
            info.pixelFormat = frame->image.depth() > 32 ? "gbrp10le" : "rgba";
        }
        if (QStringList{"scale_method", "coloring", "cas_sharpening", "grain"}.contains(name)) {
            info.width = frame->image.width();
            info.height = frame->image.height();
            auto input = [first = std::move(frame), this]() mutable -> std::optional<TimedImage> {
                if (first)
                    return std::exchange(first, std::nullopt);
                return source();
            };
            filter = std::make_unique<VideoFilterStage>(QStringList{name}, info, settings, std::move(input),
                                                        control);
            return filter->read();
        }
        const double seconds=frame->seconds();
        if((name=="dlss_ray_reconstruction"||name=="reshade")&&(!std::isfinite(seconds)||
           (previousSeconds&&seconds<=*previousSeconds)))
            throw std::runtime_error("Ray Reconstruction/ReShade requires finite, increasing presentation timestamps.");
        if (!native) {
            native = std::make_unique<FramePipeline>(settings, info, QStringList{name}, finalOutput,
                                                     bool(normalizer));
            if (cache)
                native->reuseResetPreviewResources();
        }
        const double interval=previousSeconds && seconds>*previousSeconds ? seconds-*previousSeconds
            : frame->duration>0 ? frame->duration*double(frame->timeBase.numerator)/frame->timeBase.denominator
            : info.fps>0 ? 1./info.fps : 1./60;
        const bool gap=(name=="dlss_ray_reconstruction"||name=="reshade")&&previousSeconds&&
            interval>std::max(.25,info.fps>0?4./info.fps:.25);
        frame->image = native->process(frame->image, control, index, index == 0 || gap,
                                       float(std::clamp(interval*1000.,.01,1000.)), seconds);
        previousSeconds=seconds;
        ++index;
        return frame;
    }
};
} // namespace
FrameStreamPipeline::FrameStreamPipeline(Settings settings, MediaInfo info, const QStringList &requestedStages,
                                         TimedFrameSource source, JobControl &control, bool finalOutput,
                                         PreviewFrameCache *cache, QByteArray identity)
    : source_(std::move(source)) {
    const auto stages=normalizeVfxStages(requestedStages,settings);
    const auto keys = cache ? previewStageKeys(std::move(identity), settings, info, stages, finalOutput,
                                               Paths::instance().root)
                            : QList<QByteArray>{};
    for (qsizetype index = 0; index < stages.size(); ++index) {
        const auto &name = stages[index];
        const bool final = finalOutput && index + 1 == stages.size();
        if(isVfxStage(name)){
            const auto group=vfxStageGroup(stages,index,settings);index+=group.size()-1;
            const auto selected=vfxSettingsForStages(settings,group);
            source_=vfxFrameSource(selected,info,std::move(source_),control);
            if(cache){
                auto read=source_;auto key=keys[index];auto delivered=std::make_shared<bool>(false);
                source_=[read,cache,key,delivered,&control,selected,info]()mutable->std::optional<TimedImage>{
                    if(*delivered)return {};control.checkpoint();*delivered=true;
                    if(auto found=cache->find(key))return TimedImage{std::move(found->pixels),0,1,{1,30}};
                    auto frame=read();if(frame)cache->store(key,{frame->image,info.hdr,false});return frame;
                };
            }
            auto size=vfxSpatialSize({info.width,info.height},selected);info.width=size.width();info.height=size.height();info.fps=vfxFrameRate(info.fps,selected);
            info.pixelFormat=info.hdr||info.precisionBits()>8?"gbrp10le":"rgba";info.colorSpace="gbr";info.range="pc";
            continue;
        }
        auto stage = std::make_shared<Stage>(
            Stage{settings, info, name, source_, control, final, info.pixelFormat == "gbrp10le"});
        stage->cache = cache;
        if (cache)
            stage->key = keys[index];
        // The single-frame caller caches the final display boundary. Avoid a
        // second full-resolution copy of that output, preserving intermediates
        // when downstream controls change.
        if (cache)
            stage->cacheOutput = index + 1 < stages.size();
        source_ = [stage] { return stage->read(); };
        if(name=="scale_method"){info.width=checkedDimension(info.width*settings.upscaling_factor,DimensionRounding::EvenCeil);info.height=checkedDimension(info.height*settings.upscaling_factor,DimensionRounding::EvenCeil);}
        else if(name=="super_resolution"){auto size=rtxOutputSize({info.width,info.height},settings,true);info.width=size.width();info.height=size.height();}
        else if(name=="dlss_super_resolution"){auto size=dlssOutputSize({info.width,info.height},settings.upscale_dlss_mode,true);info.width=size.width();info.height=size.height();}
        if (name == "rtx_video_hdr") {
            info.hdr = true;
            info.primaries = "bt2020";
            info.transfer = "smpte2084";
        }
        if (name == "super_resolution" && !hasVfxStages(stages)) {
            info.primaries = "bt709";
            info.transfer = "bt709";
        }
        if (QStringList{"scale_method", "coloring", "cas_sharpening", "grain", "super_resolution",
                        "rtx_video_hdr"}
                .contains(name) ||
            name == "dlss_super_resolution" && !final)
            info.pixelFormat = "gbrp10le";
        else if(name=="dlss_ray_reconstruction"||name=="reshade"){
            info.pixelFormat="rgba64le";info.sourceBitDepth=16;
        }else if (name == "neural_model" || name == "dlss_super_resolution")
            info.pixelFormat =
                info.pixelFormat == "rgba" || info.pixelFormat.contains("8") || info.pixelFormat == "yuv420p"
                    ? "rgba"
                    : "rgba64le";
        info.colorSpace = "gbr";
        info.range = "pc";
    }
}
std::optional<TimedImage> FrameStreamPipeline::read() { return source_(); }
} // namespace ve
