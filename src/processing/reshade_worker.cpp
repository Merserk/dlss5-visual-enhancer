#include "reshade_worker.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "infrastructure/scratch.hpp"
#include "media/video_preview.hpp"
#include "native_engines.hpp"
#include "reshade_native.hpp"
#include "video_still_preview.hpp"
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QScopeGuard>
#include <QTemporaryFile>
#include <QTextStream>
#include <algorithm>
#include <mutex>
#include <thread>

namespace ve {
namespace {
std::mutex diagnosticsMutex;
QString diagnostics;

void save(const QString& path, const QJsonObject& data) {
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly) || file.write(QJsonDocument(data).toJson()) < 0) {
        throw std::runtime_error("Cannot write ReShade worker request.");
    }
}

void call(const QString& file, JobControl& control, const ProgressCallback& progress = {}) {
    const auto state = file + ".state";
    const auto writeState = [state](bool paused, bool cancelled) {
        QFile value(state);
        if (value.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            value.write(cancelled ? "C" : paused ? "P" : "R");
            value.flush();
        }
    };
    writeState(control.paused(), control.cancelled());
    control.setStateChanged(writeState);
    const auto cleanup = qScopeGuard([&] { control.setStateChanged({}); });
    QByteArray pending;
    const auto result = runProcess(
        Paths::instance().at("VE_CLI.exe"), {"--internal-reshade-worker", file}, &control, 0, {},
        [&](const QByteArray& data) {
            pending += data;
            for (auto end = pending.indexOf('\n'); end >= 0; end = pending.indexOf('\n')) {
                const auto message = QJsonDocument::fromJson(pending.left(end)).object();
                pending.remove(0, end + 1);
                if (progress && message.value("kind") == "progress") {
                    progress({message.value("fraction").toDouble(),
                              message.value("message").toString(),
                              message.value("frames").toInteger(),
                              message.value("total").toInteger(),
                              message.value("fps").toDouble()});
                }
                if (message.value("kind") == "diagnostics") {
                    {
                        std::lock_guard lock(diagnosticsMutex);
                        diagnostics = message.value("text").toString();
                    }
                    info("reshade-worker", message.value("text").toString());
                }
            }
        });
    if (result.exitCode) {
        throw std::runtime_error(result.error.isEmpty()
            ? "ReShade worker exited unexpectedly."
            : result.error.toStdString());
    }
}

QJsonObject base(const QString& kind, const QString& input, const QString& output,
                 const Settings& settings, const QString& preset) {
    if (auto result = settings.exportPreset(preset, "ReShade worker"); !result) {
        throw std::runtime_error(result.error().toStdString());
    }
    return {{"protocol", 1}, {"kind", kind}, {"input", input}, {"output", output}, {"preset", preset}};
}

void send(const QJsonObject& object) {
    QTextStream stream(stdout);
    stream << QJsonDocument(object).toJson(QJsonDocument::Compact) << '\n';
    stream.flush();
}
} // namespace

QString reshadeWorkerDiagnostics() {
    std::lock_guard lock(diagnosticsMutex);
    return diagnostics;
}

RenderResult renderIsolatedReShade(const RenderRequest& request, JobControl& control,
                                   const ProgressCallback& progress) {
    QElapsedTimer elapsed;
    elapsed.start();
    if (QFileInfo::exists(request.output)) {
        throw std::runtime_error("Output already exists. Choose a different filename.");
    }
    ProcessingTemporaryDirectory directory(ProcessingCacheKind::Scratch);
    if (!directory.isValid()) {
        throw std::runtime_error("Cannot create ReShade worker directory.");
    }
    const auto rendered = directory.filePath("processed." + QFileInfo(request.output).suffix());
    auto job = base("render", request.input, rendered, request.settings, directory.filePath("preset.json"));
    job.insert("workflow", int(request.workflow));
    job.insert("preview", request.preview);
    job.insert("start", request.startSeconds);
    job.insert("seconds", request.seconds);
    job.insert("temporal_context", request.temporalContextIncluded);
    const auto path = directory.filePath("job.json");
    save(path, job);
    directory.cacheRoot()->registerFile(path + ".state");
    call(path, control, [&](const Progress& p) {
        if (progress) {
            auto delivery = p;
            delivery.fraction = std::min(0.98, p.fraction);
            progress(delivery);
        }
    });
    QDir().mkpath(QFileInfo(request.output).absolutePath());
    QFile source(rendered);
    QTemporaryFile destination(QFileInfo(request.output).absolutePath() + "/.reshade-delivery-XXXXXX");
    if (!source.open(QIODevice::ReadOnly) || !destination.open()) {
        throw std::runtime_error("Cannot save ReShade worker delivery.");
    }
    while (!source.atEnd()) {
        control.checkpoint();
        const auto bytes = source.read(8 * 1024 * 1024);
        if (bytes.isEmpty() && source.error() != QFileDevice::NoError) {
            throw std::runtime_error("Cannot read ReShade worker delivery.");
        }
        if (destination.write(bytes) != bytes.size()) {
            throw std::runtime_error("Cannot write ReShade worker delivery.");
        }
    }
    control.checkpoint();
    destination.close();
    if (!destination.rename(request.output)) {
        throw std::runtime_error("Cannot commit ReShade worker delivery; the destination may already exist.");
    }
    destination.setAutoRemove(false);
    const auto outInfo = probeMedia(request.output, &control);
    if (progress) {
        progress({1.0, outInfo.video ? QT_TRANSLATE_NOOP("App", "Video complete")
                                     : QT_TRANSLATE_NOOP("App", "Image complete")});
    }
    return {request.input, request.output, reshadeReport(request.settings, outInfo.video),
            outInfo, elapsed.elapsed() / 1000.0};
}

QImage previewIsolatedReShade(const QString& path, const Settings& settings,
                              const MediaInfo&, double seconds, JobControl& control) {
    ProcessingTemporaryDirectory directory(ProcessingCacheKind::Scratch);
    if (!directory.isValid()) {
        throw std::runtime_error("Cannot create ReShade preview worker directory.");
    }
    const auto output = directory.filePath("preview.png");
    auto job = base("still", path, output, settings, directory.filePath("preset.json"));
    job.insert("seconds", seconds);
    const auto file = directory.filePath("job.json");
    save(file, job);
    directory.cacheRoot()->registerFile(file + ".state");
    call(file, control);
    QImage pixels(output);
    if (pixels.isNull()) {
        throw std::runtime_error("ReShade preview worker returned no image.");
    }
    return pixels;
}

int runReShadeWorker(const QString& path) {
    try {
        ProcessingCacheActivity cacheActivity;
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly)) {
            throw std::runtime_error("Cannot read ReShade worker request.");
        }
        const auto job = QJsonDocument::fromJson(file.readAll()).object();
        if (job.value("protocol").toInt() != 1) {
            throw std::runtime_error("Invalid ReShade worker protocol.");
        }
        WorkerScratchScope scratch(QFileInfo(path).absolutePath());
        Settings settings;
        if (auto preset = settings.importPreset(job.value("preset").toString()); !preset) {
            throw std::runtime_error(preset.error().toStdString());
        }
        JobControl control;
        const auto input = job.value("input").toString();
        const auto output = job.value("output").toString();
        QElapsedTimer poll;
        poll.start();
        control.setExternalCheckpoint([&] {
            if (poll.elapsed() < 20) return;
            const auto started = std::chrono::steady_clock::now();
            bool paused = false;
            for (;;) {
                QFile state(path + ".state");
                QByteArray value;
                if (state.open(QIODevice::ReadOnly)) value = state.readAll();
                state.close();
                if (value == "C") throw Cancelled{};
                if (value != "P") break;
                paused = true;
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
            }
            if (paused) {
                control.accountExternalPause(std::chrono::steady_clock::now() - started);
            }
            poll.restart();
        });

        if (job.value("kind") == "render") {
            RenderRequest request{input, output, settings, Workflow(job.value("workflow").toInt()),
                                  job.value("preview").toBool(), job.value("start").toDouble(),
                                  job.value("seconds").toDouble()};
            request.isolatedReShadeWorker = true;
            request.temporalContextIncluded = job.value("temporal_context").toBool();
            const auto rendered = Pipeline{}.render(request, control, [](const Progress& p) {
                send({{"kind", "progress"}, {"fraction", p.fraction}, {"message", p.message},
                      {"frames", p.frames}, {"total", p.totalFrames}, {"fps", p.fps}});
            });
            (void)rendered;
        } else if (job.value("kind") == "still") {
            auto info = probeMedia(input, &control);
            const double seconds = job.value("seconds").toDouble();
            const auto position = quantizePlayhead(seconds * 1000, info.fps, info.duration);
            const auto stages = settings.stages(true);
            VideoFrameReader reader;
            auto source = reader.frame(input, info, position, control, false,
                                       gpuOrdinal(settings.ai_gpu_uuid), settings.ffmpeg_device != "cpu");
            const bool history = stages.contains("dlss_super_resolution") ||
                                 stages.contains("dlss_ray_reconstruction") ||
                                 (stages.contains("reshade") && reshadeUsesTemporalDepth(settings, true));
            std::vector<TimedImage> temporal;
            if (!stages.contains("dlss_ray_reconstruction") && history) {
                temporal = reader.temporalContext(input, info, position, source.absoluteSeconds,
                                                  reconstructionHistoryFrames(settings, stages), control,
                                                  gpuOrdinal(settings.ai_gpu_uuid), settings.ffmpeg_device != "cpu",
                                                  rrNeedsGeneratedHistory(stages));
            }
            PreviewFrameCache cache;
            VideoStillPreview preview;
            auto pixels = preview.process(source.pixels, settings, info, stages, cache, source.identity,
                                          control, temporal,
                                          stages.contains("dlss_ray_reconstruction") ? source.absoluteSeconds : -1,
                                          input);
            if (info.hdr || stages.contains("rtx_video_hdr")) {
                auto colors = info;
                colors.hdr = true;
                if (stages.contains("rtx_video_hdr")) {
                    colors.primaries = "bt2020";
                    colors.transfer = "smpte2084";
                }
                pixels = toneMapVideoPreview(pixels, colors, control, settings.ffmpeg_device);
            }
            if (!pixels.save(output, "PNG")) {
                throw std::runtime_error("Cannot save ReShade worker preview.");
            }
        } else {
            throw std::runtime_error("Unknown ReShade worker operation.");
        }
        send({{"kind", "diagnostics"}, {"text", ReShadeEngine::diagnostics()}});
        return 0;
    } catch (const std::exception& error) {
        send({{"kind", "diagnostics"}, {"text", ReShadeEngine::diagnostics()}});
        QTextStream(stderr) << error.what() << '\n';
        return 1;
    }
}
} // namespace ve
