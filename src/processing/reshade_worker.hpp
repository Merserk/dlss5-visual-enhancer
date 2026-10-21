#pragma once
#include "pipeline.hpp"

namespace ve {
[[nodiscard]] RenderResult renderIsolatedReShade(const RenderRequest& request, JobControl& control,
                                                 const ProgressCallback& progress);
[[nodiscard]] QImage previewIsolatedReShade(const QString& path, const Settings& settings,
                                            const MediaInfo& info, double seconds, JobControl& control);
int runReShadeWorker(const QString& path);
[[nodiscard]] QString reshadeWorkerDiagnostics();
} // namespace ve
