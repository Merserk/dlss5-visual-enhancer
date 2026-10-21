#pragma once
#include "configuration/settings.hpp"
#include "media/cuda_video.hpp"
#include "media/native_rgb.hpp"
#include "processing/reshade/reshade_bridge.h"
#include <QImage>
#include <QSize>
#include <QString>
#include <QVariantMap>
#include <memory>

namespace ve {
class JobControl;
struct MediaInfo;

[[nodiscard]] uint32_t reshadeColorTransfer(const MediaInfo& info, bool hdrOverride = false);
[[nodiscard]] QString reshadeReport(const Settings& settings, bool video);
[[nodiscard]] bool reshadeUsesTemporalDepth(const Settings& settings, bool video);

class ReShadeEngine {
  public:
    static void initialize(int ordinal = 0);
    [[nodiscard]] static VeReShadeCapabilities capabilities(int ordinal = 0);
    [[nodiscard]] static QString version();
    [[nodiscard]] static QString opticalFlowBackend();
    [[nodiscard]] static QString diagnostics();
    [[nodiscard]] static QVariantMap statusMap();
    [[nodiscard]] static QVariantMap inspectPreset(const Settings& settings, bool video,
                                                   QSize previewSize = QSize(128, 128));
    // Restore the authored technique selection, including explicit lens effects.
    // Other overrides are omitted so uniforms, sorting and macros come from the preset.
    [[nodiscard]] static QString presetResetOverrides(const QString& presetPath);
    static void exportPresetCopy(const Settings& settings, bool video, const QString& destinationPath);
};

class ReShadeSession {
  public:
    ReShadeSession(QSize size, const Settings& settings, const MediaInfo& colors,
                   bool still, bool wideOutput = false, bool preserveAlpha = true);
    ~ReShadeSession();
    ReShadeSession(const ReShadeSession&) = delete;
    ReShadeSession& operator=(const ReShadeSession&) = delete;

    [[nodiscard]] QImage process(const QImage& input, bool reset, qint64 frameOrdinal = 0,
                                 qint64 ptsNs = 0, qint64 durationNs = 33333333ll);
    [[nodiscard]] CudaVideoFrame process(const CudaVideoFrame& input, CudaFramePool& pool,
                                         bool p010, bool reset, qint64 frameOrdinal = 0);
    [[nodiscard]] GpuRgbFrame processRgb(const CudaVideoFrame& input, bool reset, qint64 frameOrdinal = 0);
    [[nodiscard]] GpuRgbFrame processRgb(const GpuRgbFrame& input, bool reset, qint64 frameOrdinal = 0,
                                         qint64 ptsNs = 0, qint64 durationNs = 33333333ll);
    [[nodiscard]] CudaVideoFrame process(const GpuRgbFrame& input, CudaFramePool& pool,
                                         bool p010, bool reset, qint64 frameOrdinal = 0,
                                         qint64 ptsNs = 0, qint64 durationNs = 33333333ll);

    [[nodiscard]] QString diagnostics() const;
    [[nodiscard]] QVariantMap statusMap() const;
    [[nodiscard]] VeReShadeFrameResult lastResult() const;

  private:
    struct State;
    std::unique_ptr<State> state_;
};
} // namespace ve
