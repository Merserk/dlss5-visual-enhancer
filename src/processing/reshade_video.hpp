#pragma once
#include "pipeline.hpp"

namespace ve {
[[nodiscard]] bool renderReShadeVideo(const RenderRequest& request, const MediaInfo& source,
                                      const Settings& settings, const PipelineEstimate& estimate,
                                      const QString& destination, JobControl& control,
                                      const ProgressCallback& progress);
} // namespace ve
