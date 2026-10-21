#pragma once
#include "pipeline.hpp"
#include <QJsonArray>
namespace ve {
[[nodiscard]] RenderResult renderIsolatedFsr(const RenderRequest& request,JobControl& control,const ProgressCallback& progress);
int runFsrWorker(const QString& path);
[[nodiscard]] QString fsrWorkerDiagnostics();
[[nodiscard]] QJsonArray fsrAuditLoadedModules(bool strict);
[[nodiscard]] QImage previewIsolatedFsr(const QString& path,const Settings& settings,const MediaInfo& info,double seconds,JobControl& control);
}
