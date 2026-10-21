#pragma once
#include "configuration/settings.hpp"
#include "infrastructure/job.hpp"
#include "media/media.hpp"
#include "media/frame_generation_rate.hpp"
#include <functional>
#include <memory>
#include <span>
#include <vector>

namespace ve {
class PreviewFrameCache;
inline int reconstructionHistoryFrames(const Settings& settings,const QStringList& stages) {
    if(stages.contains("dlss_ray_reconstruction"))return 32;
    if(stages.contains("dlss_super_resolution"))return settings.upscale_dlss_mode=="DLAA"?8:18;
    if(stages.contains("reshade")){
        const auto rs=reshadeSettingsForMode(settings,true);
        if(rs.reshade_temporal_stability!="Spatial Only (Off)" && rs.reshade_depth_mode!="Off")
            return 8;
    }
    if(stages.contains("fsr_frame_generation"))return 8;
    return 0;
}
inline bool rrNeedsGeneratedHistory(const QStringList& stages) {
    const auto fi=stages.contains("fsr_frame_generation")?stages.indexOf("fsr_frame_generation"):stages.indexOf("frame_generation"),rr=stages.indexOf("dlss_ray_reconstruction"),rs=stages.indexOf("reshade");
    return fi>=0&&((rr>fi)||(rs>fi));
}
enum class Workflow { NeuralRendering, Upscale, FrameInterpolation };
struct PipelineEstimate {
    int width = 0, height = 0;
    double fps = 0;
    bool hdr = false;
    QStringList stages;
};
struct Progress {
    double fraction = 0;
    QString message;
    qint64 frames = 0, totalFrames = 0;
    double fps = 0;
};
using ProgressCallback = std::function<void(const Progress&)>;
struct RenderRequest {
    QString input, output;
    Settings settings;
    Workflow workflow = Workflow::NeuralRendering;
    bool preview = false;
    double startSeconds = 0;
    double seconds = 0; // Zero exports the complete source.
    PreviewFrameCache* stillSources = nullptr; // Optional reuse of the original decoded preview image.
    bool isolatedVfxWorker = false; // Set only by the internal worker, independently of Qt's storage identity.
    bool isolatedReShadeWorker = false; // Set only by the internal ReShade worker.
    bool temporalContextIncluded = false; // Internal guard for bounded RR clip overlap.
    bool isolatedFsrWorker = false;
};
struct RenderResult {
    QString input, output, report;
    MediaInfo info;
    double elapsed = 0;
};
[[nodiscard]] Settings workflowSettings(Settings settings, Workflow workflow, bool video);
[[nodiscard]] Settings videoPreviewSettings(Settings settings);
[[nodiscard]] PipelineEstimate estimatePipeline(const MediaInfo& info, const Settings& settings,
                                                Workflow workflow);
[[nodiscard]] QSize dlssOutputSize(QSize input, const QString& mode, bool video);
[[nodiscard]] QSize rtxOutputSize(QSize input, const Settings& settings, bool video);
[[nodiscard]] QString renderOutputPath(const QString& input, const QString& directory,
                                       const Settings& settings, Workflow workflow, bool video);
class FramePipeline {
  public:
    FramePipeline(Settings settings, MediaInfo input, QStringList stages, bool finalOutput = true,
                  bool rtxInputNormalized=false);
    ~FramePipeline();
    void configurePreviewCache(PreviewFrameCache& cache, QByteArray identity);
    void reuseResetPreviewResources();
    [[nodiscard]] QImage process(const QImage& input, JobControl& control, qint64 frame, bool reset, float frameTimeMs = 0,
                                double presentationSeconds = -1);
    [[nodiscard]] std::vector<QImage> processBatch(std::span<const QImage> input, JobControl& control,
                                                   qint64 firstFrame, bool reset, float frameTimeMs = 0,
                                                   double presentationSeconds = -1);

  private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
class Pipeline {
  public:
    [[nodiscard]] RenderResult render(const RenderRequest& request, JobControl& control,
                                      const ProgressCallback& progress = {});
};
} // namespace ve
