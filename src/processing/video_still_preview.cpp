#include "video_still_preview.hpp"
#include "frame_stream.hpp"
#include "native_engines.hpp"
#include "infrastructure/paths.hpp"
#include "media/video_preview.hpp"
#include "infrastructure/scratch.hpp"
#include <utility>
#include <limits>
#include <cmath>

namespace ve {
QImage VideoStillPreview::process(const QImage &input, Settings settings, MediaInfo info,
                                  const QStringList &requestedStages, PreviewFrameCache &cache,
                                  const QByteArray &identity, JobControl &control,
                                  const std::vector<TimedImage>& temporal, double generatedSeconds,const QString& sourceMediaPath) {
    ProcessingCacheActivity cacheActivity;
    const auto stages=normalizeVfxStages(requestedStages,settings);
    // These are the exact metadata/layout boundaries that the old source FFV1
    // container supplied to the streaming video pipeline. Rotation is already
    // applied by HostVideoDecoder; all source RGB codes are retained.
    info.width = input.width();
    info.height = input.height();
    info.sourceBitDepth = info.precisionBits();
    // The former source container quantized wide layouts back to the original
    // integer depth before the streaming stages read them. Preserve that step.
    info.pixelFormat = ffv1PreviewPixelFormat(info);
    const bool nativeTemporal=stages.contains("dlss_super_resolution")||stages.contains("dlss_ray_reconstruction");
    if(nativeTemporal)info.pixelFormat=input.depth()>32?"rgba64le":"rgba";
    info.colorSpace = "gbr";
    info.range = "pc";
    info.codec = "ffv1";
    info.metadata.remove("side_data_list");
    info.metadata.remove("tags");
    info.metadata["width"] = info.width;
    info.metadata["height"] = info.height;
    auto temporalIdentity=identity;
    temporalIdentity+=QByteArray::number(generatedSeconds,'g',17);
    if(!temporal.empty()) {
        temporalIdentity+="/native-reconstruction-history/v2/";
        for(const auto& frame:temporal) {
            temporalIdentity+=previewPixelIdentity(frame.image,control);
            temporalIdentity+=QByteArray::number(frame.pts)+'/'+QByteArray::number(frame.timeBase.numerator)+'/'+QByteArray::number(frame.timeBase.denominator);
        }
    }
    const auto keys = previewStageKeys(temporalIdentity, settings, info, stages, true, Paths::instance().root);
    const auto displayKey =
        QCryptographicHash::hash(QByteArray("video-display/v1") + (keys.isEmpty() ? identity : keys.back()),
                                 QCryptographicHash::Sha256);
    if (auto cached = cache.find(displayKey))
        return std::move(cached->pixels);
    if(!sourceMediaPath.isEmpty()&&stages.contains("dlss_ray_reconstruction")){
        // Stream the context through the export graph. Retaining 32 complete
        // UHD images in RAM would either exceed the history budget or silently
        // shorten the selected history when the source precision increases.
        ScratchDirectory directory(QStringLiteral("rr-parked-context-XXXXXX"));
        if(!directory.isValid())throw std::runtime_error("Cannot create streamed RR preview context.");
        const double fps=info.fps>0?info.fps:30.;
        // Original-source callers provide an absolute presentation timestamp;
        // negative presentation origins are valid as well.
        const double selectedAbsolute=std::isfinite(generatedSeconds)?generatedSeconds:info.presentationStartSeconds();
        const double selectedRelative=std::max(0.,selectedAbsolute-info.presentationStartSeconds());
        const double start=reconstructionContextStart(sourceMediaPath,info,selectedRelative,
            reconstructionHistoryFrames(settings,stages),control,gpuOrdinal(settings.ai_gpu_uuid),settings.ffmpeg_device=="auto");
        settings.video_enabled_stages=stages;
        RenderRequest request{sourceMediaPath,directory.filePath("processed.mkv"),settings,
            Workflow::NeuralRendering,true,start,selectedRelative-start+std::max(.25,3./fps)};
        request.temporalContextIncluded=true;
        const auto result=Pipeline{}.render(request,control);
        const double origin=result.info.metadata.value("ve_source_time_origin").toDouble(info.presentationStartSeconds()+start);
        const double target=std::max(0.,selectedAbsolute-origin);
        HostVideoDecoder decoder(result.output,result.info,0,0,false,control);
        std::optional<TimedImage> selected;
        double distance=std::numeric_limits<double>::infinity();
        while(auto frame=decoder.read()){
            control.checkpoint();const auto candidate=std::abs(frame->seconds()-target);
            if(candidate<distance){distance=candidate;selected=std::move(frame);}
            else if(frame->seconds()>target)break;
        }
        if(!selected)throw std::runtime_error("Streamed RR preview returned no selected frame.");
        auto pixels=display_.display(selected->image,result.info,control);
        control.checkpoint();cache.store(displayKey,{pixels,false,false});return pixels;
    }
    if(rrNeedsGeneratedHistory(stages)){
        // RR after FG must consume the generated temporal frames as export
        // does. Preserve the decoded source context losslessly and run the
        // same clip graph rather than dropping FG from the parked preview.
        auto frames=temporal;
        if(frames.empty())frames.push_back({input,0,1,{1,30}});
        ScratchDirectory directory(QStringLiteral("rr-generated-history-XXXXXX"));
        if(!directory.isValid())throw std::runtime_error("Cannot create generated RR preview history.");
        const auto sourcePath=directory.filePath("source.mkv"),rendered=directory.filePath("processed.mkv");
        auto packing=info;packing.pixelFormat=frames.front().image.depth()>32?"rgba64le":"rgba";
        packing.sourceBitDepth=frames.front().image.depth()>32?16:8;
        const auto origin=frames.front();
        {
            HostVideoEncoder encoder(sourcePath,"FFV1",settings,packing,0,control,HostEncodePurpose::NativePreview);
            for(const auto& frame:frames){
                control.checkpoint();
                if(frame.timeBase.numerator!=origin.timeBase.numerator||frame.timeBase.denominator!=origin.timeBase.denominator)
                    throw std::runtime_error("RR preview context changed time base.");
                encoder.write(frame.image,frame.pts-origin.pts,frame.duration,frame.timeBase);
            }
            encoder.finish();
        }
        settings.video_enabled_stages=stages;
        const auto result=Pipeline{}.render({sourcePath,rendered,settings,Workflow::NeuralRendering,true},control);
        const double target=std::max(0.,(generatedSeconds>=0?generatedSeconds:frames.back().seconds())-origin.seconds());
        HostVideoDecoder decoder(rendered,result.info,0,0,false,control);
        std::optional<TimedImage> selected;
        double distance=std::numeric_limits<double>::infinity();
        while(auto frame=decoder.read()){
            control.checkpoint();const double candidate=std::abs(frame->seconds()-target);
            if(candidate<distance){distance=candidate;selected=std::move(frame);}
            else if(frame->seconds()>target)break;
        }
        if(!selected)throw std::runtime_error("Generated RR preview returned no selected frame.");
        auto pixels=display_.display(selected->image,result.info,control);
        control.checkpoint();cache.store(displayKey,{pixels,false,false});return pixels;
    }
    if (!nativeTemporal && info.sourceBitDepth > 8 && info.sourceBitDepth < 16 && !stages.isEmpty() &&
        QStringList{"scale_method", "cas_sharpening", "grain", "coloring"}.contains(stages.front())) {
        // Leading GPU filters reconstruct sub-16-bit planar source codes with
        // libplacebo before their first UNORM16 boundary. Retain that exact
        // conversion until its in-memory replacement has pixel parity.
        ScratchDirectory directory(QStringLiteral("preview-wide-XXXXXX"));
        if (!directory.isValid())
            throw std::runtime_error("Cannot create the wide preview directory.");
        const auto sourcePath = directory.filePath("source.mkv");
        auto packing = info;
        packing.pixelFormat = "rgba64le";
        {
            HostVideoEncoder encoder(sourcePath, "FFV1", settings, packing, 0, control,
                                     HostEncodePurpose::NativePreview);
            encoder.write(input, 0, 1, {1, 30});
            encoder.finish();
        }
        settings.video_enabled_stages = stages;
        const auto rendered = directory.filePath("processed.mkv");
        const auto result =
            Pipeline{}.render({sourcePath, rendered, settings, Workflow::NeuralRendering, true}, control);
        auto pixels = decodeProcessedVideoPreview(rendered, result.info, control, settings.ffmpeg_device);
        control.checkpoint();
        cache.store(displayKey, {pixels, false, false});
        return pixels;
    }
    auto source = input;
    if (!nativeTemporal && info.sourceBitDepth > 8) {
        auto packing = info;
        packing.pixelFormat = "rgba64le";
        source = display_.display(input, packing, control);
    }
    auto frames=temporal;
    if(frames.empty())frames.push_back({source,0,1,{1,30}});
    size_t next=0;
    FrameStreamPipeline pipeline(
        settings, info, stages, [&frames,&next]() -> std::optional<TimedImage> {
            if(next>=frames.size())return {};
            return frames[next++];
        }, control, true,temporal.empty()?&cache:nullptr,temporalIdentity);
    auto output = pipeline.read();
    const double generatedTarget=generatedSeconds>=0&&!frames.empty()?std::max(0.,generatedSeconds-frames.front().seconds()):-1;
    while(auto frame=pipeline.read()) {control.checkpoint();if(generatedTarget>=0&&frame->seconds()>generatedTarget+1e-7)break;output=std::move(frame);}
    if (!output)
        throw std::runtime_error("Video still preview returned no frame.");
    if (!stages.isEmpty() && stages.back() != "neural_model" && stages.back() != "dlss_super_resolution" && stages.back() != "dlss_ray_reconstruction")
        info.pixelFormat = "gbrp10le";
    else if (info.pixelFormat == "gbrp10le" && !stages.isEmpty())
        info.pixelFormat = "rgba64le";
    if (stages.contains("super_resolution") && !hasVfxStages(stages)) {
        info.primaries = "bt709";
        info.transfer = "bt709";
    }
    if(stages.contains("rtx_video_hdr")){info.hdr=true;info.primaries="bt2020";info.transfer="smpte2084";}
    info.width = output->image.width();
    info.height = output->image.height();
    auto pixels = display_.display(output->image, info, control);
    control.checkpoint();
    cache.store(displayKey, {pixels, false, false});
    return pixels;
}
} // namespace ve
