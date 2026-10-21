#include "video_delivery.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "infrastructure/scratch.hpp"
#include "media/image_metadata.hpp"
#include "processing/vfx_native.hpp"
#include <QFile>

namespace ve {
QStringList vfxSubtitleArguments(const MediaInfo& source,const Settings& settings){
    QStringList result;int outputIndex=0;
    const auto subtitles=source.metadata.value("source_subtitles").toArray();
    const QStringList textCodecs{"subrip","ass","ssa","webvtt","mov_text","text","microdvd","subviewer","subviewer1","sami","realtext","jacosub","vplayer","pjs","stl","ttml"};
    for(const auto& value:subtitles){
        const auto subtitle=value.toObject();const auto codec=subtitle.value("codec_name").toString();
        if(settings.container!="MKV"&&!textCodecs.contains(codec))
            throw std::runtime_error(QString("VFX subtitle codec %1 cannot be preserved in %2. Choose MKV.").arg(codec,settings.container).toStdString());
        result<<"-map"<<"1:"+QString::number(subtitle.value("index").toInt())<<"-c:s:"+QString::number(outputIndex++)
              <<(settings.container=="MKV"||codec=="mov_text"?"copy":"mov_text");
    }
    if(!subtitles.isEmpty()&&settings.container=="MKV")result<<"-map"<<"1:t?"<<"-c:t"<<"copy";
    return result;
}
QString neuralRenderNote(const Settings& settings) {
    const auto number = [](double value) { return QString::number(value, 'g', 8); };
    const auto scale = settings.upscaling_factor == 1 ? QString("Source (Original)")
                                                      : number(settings.upscaling_factor * 100) + '%';
    return "Visual Enhancer:\nDLSS 5 Neural Rendering Settings - NR Style - " + settings.nr_style +
           ", NR Intensity - " + number(settings.nr_intensity) + ", NR Passes - " +
           QString::number(settings.nr_passes) + ", Local Tone Strength - " +
           number(settings.local_tone_strength) + ", Local Structure Strength - " +
           number(settings.local_structure_strength) + ", Skin Structure Strength - " +
           number(settings.skin_structure_strength) +
           (settings.automatic_mask ? QString{} : QString(" (inactive - Automatic Mask Off)")) +
           ", Automatic Mask - " + (settings.automatic_mask ? "On" : "Off") + ", NR Control Mask - " +
           (settings.nr_mask.isEmpty() ? "Off" : "On") + ", GPU Memory Path - On, Scale - " + scale;
}
void muxDelivery(const QString& video, const QString& destination, const RenderRequest& request,
                 const MediaInfo& source, const Settings& settings, double duration, JobControl& control,
                 std::optional<double> sourceTimeOrigin) {
    QStringList mux{"-v", "error", "-y"};
    if (sourceTimeOrigin) mux << "-copyts";
    mux << "-i" << video;
    if (request.preview) {
        mux << "-map" << "0:v:0" << "-an" << "-c:v" << "copy" << "-map_metadata" << "0";
    } else {
    if (sourceTimeOrigin) mux << "-itsoffset" << QString::number(-*sourceTimeOrigin, 'f', 12);
    else if (request.startSeconds > 0) mux << "-ss" << QString::number(request.startSeconds, 'f', 9);
    mux << "-i" << request.input << "-map" << "0:v:0" << "-map" << "1:a?" << "-c:v" << "copy"
        << "-map_metadata" << "1" << "-map_chapters" << "1";
    mux << audioEncoderArguments(request.input, settings.container, source, control);
    if(hasVfxStages(settings.stages(true)))mux<<vfxSubtitleArguments(source,settings);
    }
    if (duration > 0) mux << "-t" << QString::number(duration, 'f', 9);
    if(hasVfxStages(settings.stages(true))){
        auto pixels=probeMedia(video,&control);retainVfxPixelAspect(pixels,source);const auto aspect=pixels.outputSampleAspectRatio();
        mux<<"-aspect"<<QString::number(double(pixels.width)*aspect.width()/(double(pixels.height)*aspect.height()),'g',15);
        if(settings.stages(true).contains("vfx_frame_generation"))mux<<"-r"<<QString::number(estimatePipeline(source,request.settings,request.workflow).fps,'g',15);
    }
    // NUT does not retain the full-range RGB tags of software FFV1. Declare
    // that packed-frame contract on the delivery stream as well.
    if (settings.codec.startsWith("FFV1")) {
        mux << "-colorspace" << "0" << "-color_range" << "pc";
        const auto stages=settings.stages(true);
        const bool convertedHdr=stages.contains("rtx_video_hdr");
        const bool rtxSdr=stages.contains("super_resolution")&&!hasVfxStages(stages);
        const auto primaries=convertedHdr ? QString("bt2020") : rtxSdr ? QString("bt709") : source.primaries;
        const auto transfer=convertedHdr ? QString("smpte2084") : rtxSdr ? QString("bt709") : source.transfer;
        if (!primaries.isEmpty() && primaries!="unknown") mux << "-color_primaries" << primaries;
        if (!transfer.isEmpty() && transfer!="unknown") mux << "-color_trc" << transfer;
    }
    if (!request.preview && settings.container == "MP4") mux << "-movflags" << "+faststart";
    std::unique_ptr<ScratchDirectory> metadataDirectory;
    if (!request.preview && settings.container != "MOV" && settings.stages(true).contains("neural_model")) {
        // Inherited comments can exceed Windows' process command-line limit.
        // FFmpeg's file-loaded option preserves the complete UTF-8 argument.
        metadataDirectory = std::make_unique<ScratchDirectory>("delivery-metadata-XXXXXX");
        if (!metadataDirectory->isValid())
            throw std::runtime_error("Cannot create the delivery metadata directory.");
        QFile metadataFile(metadataDirectory->filePath("comment.txt"));
        const auto argument = ("comment=" + mergeRenderDescription(
            source.metadata.value("source_format").toObject().value("tags").toObject().value("comment").toString(), neuralRenderNote(settings))).toUtf8();
        if (!metadataFile.open(QIODevice::WriteOnly) || metadataFile.write(argument) != argument.size() || !metadataFile.flush())
            throw std::runtime_error(("Cannot write delivery metadata: " + metadataFile.errorString()).toStdString());
        metadataFile.close();
        mux << "-/metadata" << metadataFile.fileName();
    }
    mux << "-fps_mode" << "passthrough" << "-avoid_negative_ts" << "disabled"
        << "-metadata:s:v:0" << "rotate=0" << destination;
    const auto result = runProcess(Paths::instance().ffmpeg(), mux, &control, 0);
    if (result.exitCode) throw std::runtime_error(result.error.toStdString());
}
} // namespace ve
