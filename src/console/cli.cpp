#include "cli.hpp"
#include "configuration/settings.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "media/media.hpp"
#include "processing/native_engines.hpp"
#include "processing/pipeline.hpp"
#include "processing/fsr_native.hpp"
#include "processing/fsr_worker.hpp"
#include "processing/reshade_native.hpp"
#include "processing/reshade_worker.hpp"
#include "processing/vfx_worker.hpp"
#include <QDir>
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QSet>
#include <QTextStream>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <windows.h>

namespace ve {
namespace {
class UsageError : public std::runtime_error {
  public:
    explicit UsageError(const QString& message) : std::runtime_error(message.toStdString()) {}
};
QTextStream output(stdout), errors(stderr);
void json(const QJsonObject& value) {
    output << QJsonDocument(value).toJson(QJsonDocument::Compact) << '\n';
    output.flush();
}
void help(const QString& command = {}) {
    output << "Visual Enhancer for images and videos, from your terminal.\n\n"
              "Usage: VE_CLI.exe COMMAND [INPUT ...] [OPTIONS]\n\n"
              "Commands: render, reconstruct, reshade, upscale, interpolate, devices\n";
    if (command == "devices")
        output << "  --json                     Write GPU information as JSON\n";
    else if (!command.isEmpty()) {
        output << "  -o, --output PATH          Output folder or filename for one input\n"
                  "  --preset JSON              Load an app preset; flags override its values\n"
                  "  --recursive                Scan subfolders\n"
                  "  --gpu ID                   auto, NVIDIA index, or GPU UUID\n"
                  "  --codec h264|hevc|av1|prores|ffv1\n"
                  "  --container mp4|mkv|mov\n"
                  "  --quality auto|good|best|max\n"
                  "                             Constant quality: balanced, high, very high, maximum\n"
                  "  --dry-run                  Plan without rendering or checking hardware\n"
                  "  --quiet                    Hide progress\n"
                  "  --json                     One JSON result; progress and errors on stderr\n";
        if (command != "interpolate")
            output << "  --format png|jpeg|webp|avif|tiff\n";
        if (command == "render")
            output << "  --style 0|1|2\n  --strength 0..2\n";
        if (command == "render" || command == "reshade")
            output << "  --reshade-preset PATH      ReShade .ini preset file\n"
                      "  --reshade-shaders PATH     Shader (.fx/.fxh) search folder(s), separated by ';'\n"
                      "  --reshade-textures PATH    Texture/LUT search folder(s), separated by ';'\n"
                      "  --reshade-mix 0..1         Blend factor between source and ReShade output\n"
                      "  --reshade-depth none|auto|moge-2-vits\n"
                      "  --reshade-temporal auto|quality|off\n";
        if (command == "reconstruct")
            output << "  Native-resolution (1x) Ray Reconstruction\n"
                      "  --flow-quality high|medium|low  Video NVOFA quality\n";
        if (command == "upscale")
            output << "  --engine rtx|dlss\n  --scale FACTOR             RTX scale factor, at least 1\n  "
                      "--size WIDTHxHEIGHT        Exact RTX dimensions\n  --mode "
                      "dlaa|quality|balanced|performance|ultra-performance\n";
        if (command == "interpolate")
            output << "  --fps FPS                  Target frame rate (default: 60)\n"
                      "  --multiplier 2..8          Multiply the source frame rate (instead of --fps)\n"
                      "  --interpolator dlss|fsr3   Frame generation provider (default: dlss)\n"
                      "  --fsr-gpu ID               auto, DXGI adapter index, or saved DXGI ID\n";
    } else
        output << "\nRun VE_CLI.exe COMMAND --help for options. Advanced settings: export a JSON preset from "
                  "the app.\n";
    output.flush();
}
struct Arguments {
    QString command;
    QStringList inputs;
    QMap<QString, QString> options;
    QSet<QString> flags;
};
Arguments parse(QStringList values) {
    Arguments result;
    result.command = values.takeFirst();
    if (!QStringList{"render", "reconstruct", "reshade", "upscale", "interpolate", "devices"}.contains(
            result.command))
        throw UsageError("Unknown command: " + result.command);
    const QSet<QString> switches{"--json", "--quiet", "--dry-run", "--recursive"};
    QSet<QString> options{"--output", "--preset", "--gpu", "--codec", "--container", "--quality"};
    if (result.command != "interpolate")
        options.insert("--format");
    if (result.command == "render")
        options.unite({"--style", "--strength"});
    if (result.command == "render" || result.command == "reshade")
        options.unite({"--reshade-preset", "--reshade-shaders", "--reshade-textures", "--reshade-mix",
                       "--reshade-depth", "--reshade-temporal"});
    if (result.command == "reconstruct")
        options.insert("--flow-quality");
    if (result.command == "upscale")
        options.unite({"--engine", "--scale", "--size", "--mode"});
    if (result.command == "interpolate")
        options.unite({"--fps","--multiplier","--interpolator","--fsr-gpu"});
    bool positional = false;
    for (qsizetype i = 0; i < values.size(); ++i) {
        auto value = values[i];
        if (value == "--" && !positional) {
            positional = true;
            continue;
        }
        if (value.startsWith('-') && !positional) {
            if (value == "-o")
                value = "--output";
            const auto equal = value.indexOf('=');
            const QString key = equal >= 0 ? value.left(equal) : value;
            if (switches.contains(key) && equal < 0) {
                if (result.command == "devices" && key != "--json")
                    throw UsageError("Unknown option: " + key);
                result.flags.insert(key);
            } else if (options.contains(key) && result.command != "devices") {
                if (equal < 0 && i + 1 == values.size())
                    throw UsageError("Missing value for " + key);
                result.options.insert(key, equal >= 0 ? value.mid(equal + 1) : values[++i]);
            } else
                throw UsageError("Unknown option: " + key);
        } else
            result.inputs.append(value);
    }
    if (result.command == "devices" && !result.inputs.isEmpty())
        throw UsageError("devices does not accept input files.");
    if (result.command != "devices" && result.inputs.isEmpty())
        throw UsageError("At least one input file or folder is required.");
    if (result.options.contains("--size") && result.options.contains("--scale"))
        throw UsageError("--scale and --size are mutually exclusive.");
    return result;
}
QString choice(const Arguments& args, const QString& key, const QMap<QString, QString>& choices) {
    const auto value = args.options.value(key);
    if (!choices.contains(value))
        throw UsageError("Invalid value for " + key + ": " + value);
    return choices.value(value);
}
double number(const QString& value, const QString& name) {
    bool valid = false;
    const auto result = value.toDouble(&valid);
    if (!valid || !std::isfinite(result))
        throw UsageError(name + " requires a finite number.");
    return result;
}
QStringList splitSearchPaths(const QString& raw) {
    QStringList out;
    for (const auto& part : raw.split(';', Qt::SkipEmptyParts)) {
        const auto cleaned = localPath(part.trimmed());
        if (!cleaned.isEmpty() && !out.contains(cleaned, Qt::CaseInsensitive))
            out.append(cleaned);
    }
    return out;
}
void applyReShadeOptions(const Arguments& args, Settings& settings) {
    if (args.options.contains("--reshade-preset"))
        settings.reshade_image_preset_path = settings.reshade_preset_path =
            localPath(args.options.value("--reshade-preset"));
    if (args.options.contains("--reshade-shaders"))
        settings.reshade_image_shader_search_paths = settings.reshade_shader_search_paths =
            splitSearchPaths(args.options.value("--reshade-shaders")).join(';');
    if (args.options.contains("--reshade-textures"))
        settings.reshade_image_texture_search_paths = settings.reshade_texture_search_paths =
            splitSearchPaths(args.options.value("--reshade-textures")).join(';');
    if (args.options.contains("--reshade-mix")) {
        const auto raw = args.options.value("--reshade-mix");
        const double v = number(raw, "--reshade-mix");
        const int pct =
            (v >= 0.0 && v <= 1.0 && raw.contains('.')) ? int(std::llround(v * 100.0)) : int(std::llround(v));
        settings.reshade_image_mix = settings.reshade_mix = pct;
    }
    if (args.options.contains("--reshade-depth"))
        settings.reshade_image_depth_mode = settings.reshade_depth_mode =
            choice(args, "--reshade-depth",
                   {{"none", "Off"},
                    {"off", "Off"},
                    {"auto", "Auto"},
                    {"moge-2-vits", "AI Depth"},
                    {"ai", "AI Depth"}});
    if (args.options.contains("--reshade-temporal"))
        settings.reshade_temporal_stability =
            choice(args, "--reshade-temporal",
                   {{"auto", "Auto"},
                    {"off", "Spatial Only (Off)"},
                    {"quality", "Quality"},
                    {"amd-optical-flow", "Quality"}});
}
Settings settingsFor(const Arguments& args, const QStringList& paths) {
    Settings settings;
    settings.image_rename_mode = settings.video_rename_mode = settings.upscale_image_rename_mode =
        settings.upscale_rename_mode = settings.frame_interpolation_rename_mode = "Copy";
    if (args.options.contains("--preset")) {
        if (auto result = settings.importPreset(localPath(args.options.value("--preset"))); !result)
            throw UsageError(result.error());
    }
    const bool images = std::ranges::any_of(paths, isImage), videos = std::ranges::any_of(paths, isVideo);
    const bool hasReShadeOption =
        args.options.contains("--reshade-preset") || args.options.contains("--reshade-shaders") ||
        args.options.contains("--reshade-textures") || args.options.contains("--reshade-mix") ||
        args.options.contains("--reshade-depth") || args.options.contains("--reshade-temporal");
    if (args.command == "render") {
        if (!args.options.contains("--preset")) {
            if (hasReShadeOption && !args.options.contains("--style") && !args.options.contains("--strength"))
                settings.image_enabled_stages = settings.video_enabled_stages = {"reshade"};
            else if (hasReShadeOption)
                settings.image_enabled_stages = settings.video_enabled_stages = {"neural_model", "reshade"};
            else
                settings.image_enabled_stages = settings.video_enabled_stages = {"neural_model"};
        }
        if (args.options.contains("--style") || args.options.contains("--strength")) {
            if ((images && !settings.image_enabled_stages.contains("neural_model")) ||
                (videos && !settings.video_enabled_stages.contains("neural_model")))
                throw UsageError(
                    "The preset disables Neural Rendering. Enable it before using --style or --strength.");
        }
        if (hasReShadeOption) {
            if ((images && !settings.image_enabled_stages.contains("reshade")) ||
                (videos && !settings.video_enabled_stages.contains("reshade")))
                throw UsageError("The preset disables ReShade. Enable it before using --reshade-* options.");
        }
        if (args.options.contains("--style"))
            settings.nr_style =
                choice(args, "--style", {{"0", "Style 0"}, {"1", "Style 1"}, {"2", "Style 2"}});
        if (args.options.contains("--strength"))
            settings.nr_intensity = number(args.options.value("--strength"), "--strength");
        applyReShadeOptions(args, settings);
    } else if (args.command == "reshade") {
        if (!args.options.contains("--preset"))
            settings.image_enabled_stages = settings.video_enabled_stages = {"reshade"};
        applyReShadeOptions(args, settings);
    } else if (args.command == "reconstruct") {
        settings.image_enabled_stages=settings.video_enabled_stages={"dlss_ray_reconstruction"};
        if(args.options.contains("--flow-quality"))
            settings.rr_optical_flow_quality=choice(args,"--flow-quality",{{"high","High"},{"medium","Medium"},{"low","Low"}});
    } else if (args.command == "upscale") {
        if (args.options.contains("--engine"))
            settings.upscale_image_engine = settings.upscale_engine =
                choice(args, "--engine", {{"rtx", "RTX Video Super Resolution"}, {"dlss", "DLSS"}});
        const bool dlss = (images && settings.upscale_image_engine == "DLSS") ||
                          (videos && settings.upscale_engine == "DLSS");
        const bool rtx = (images && settings.upscale_image_engine != "DLSS") ||
                         (videos && settings.upscale_engine != "DLSS");
        if (dlss && (args.options.contains("--scale") || args.options.contains("--size")))
            throw UsageError("DLSS output size is set by --mode. --scale and --size require --engine rtx.");
        if (rtx && args.options.contains("--mode"))
            throw UsageError("--mode requires --engine dlss.");
        settings.image_enabled_stages = {settings.upscale_image_engine == "DLSS" ? "dlss_super_resolution"
                                                                                 : "super_resolution"};
        settings.video_enabled_stages = {settings.upscale_engine == "DLSS" ? "dlss_super_resolution"
                                                                           : "super_resolution"};
        settings.image_format = settings.upscale_image_output_format;
        settings.image_quality = settings.upscale_image_quality;
        settings.codec = settings.upscale_codec;
        settings.container = settings.upscale_container;
        settings.quality = settings.upscale_quality;
        settings.hdr_mode = false;
        settings.image_rename_mode = settings.upscale_image_rename_mode;
        settings.image_custom_suffix = settings.upscale_image_custom_suffix;
        settings.video_rename_mode = settings.upscale_rename_mode;
        settings.video_custom_suffix = settings.upscale_custom_suffix;
        if (args.options.contains("--scale")) {
            settings.upscale_image_scale_factor = settings.upscale_scale_factor =
                number(args.options.value("--scale"), "--scale");
            settings.upscale_image_size_mode = settings.upscale_size_mode = "Scale factor";
        }
        if (args.options.contains("--size")) {
            const auto match = QRegularExpression("^(\\d+)[xX](\\d+)$").match(args.options.value("--size"));
            if (!match.hasMatch())
                throw UsageError("Use WIDTHxHEIGHT, with each dimension from 2 to 32768.");
            const int width = match.captured(1).toInt(), height = match.captured(2).toInt();
            if (std::min(width, height) < 2 || std::max(width, height) > 32768)
                throw UsageError("Use WIDTHxHEIGHT, with each dimension from 2 to 32768.");
            settings.upscale_image_size_mode = settings.upscale_size_mode = "Custom dimensions";
            settings.upscale_image_width = settings.upscale_width = width;
            settings.upscale_image_height = settings.upscale_height = height;
            settings.upscale_image_aspect_lock = settings.upscale_aspect_lock = false;
        }
        if (args.options.contains("--mode"))
            settings.upscale_image_dlss_mode = settings.upscale_dlss_mode =
                choice(args, "--mode",
                       {{"dlaa", "DLAA"},
                        {"quality", "Quality"},
                        {"balanced", "Balanced"},
                        {"performance", "Performance"},
                        {"ultra-performance", "Ultra Performance"}});
    } else {
        if (images)
            throw UsageError("interpolate accepts videos only. Use render or upscale for images.");
        if(args.options.contains("--interpolator"))settings.frame_interpolation_provider=choice(args,"--interpolator",{{"dlss","DLSS"},{"fsr3","FSR 3"}});
        const bool fsr=settings.frame_interpolation_provider=="FSR 3";
        settings.video_enabled_stages = {fsr?"fsr_frame_generation":"frame_generation"};
        if (args.options.contains("--fps") && args.options.contains("--multiplier"))
            throw UsageError("Choose either --fps or --multiplier.");
        if (args.options.contains("--fps")){
            (fsr ? settings.fsr_frame_generation_frame_rate_mode : settings.frame_interpolation_frame_rate_mode) = "Target FPS";
            if(fsr)settings.fsr_frame_generation_target_fps=args.options.value("--fps");
            else settings.frame_interpolation_target_fps = args.options.value("--fps");
        }
        if (args.options.contains("--multiplier")) {
            bool valid = false;
            const auto multiplier = args.options.value("--multiplier").toInt(&valid);
            if (!valid || multiplier < 2 || multiplier > 8) throw UsageError("--multiplier must be an integer from 2 to 8.");
            (fsr ? settings.fsr_frame_generation_frame_rate_mode : settings.frame_interpolation_frame_rate_mode) = "Multiplier";
            (fsr ? settings.fsr_frame_generation_multiplier : settings.frame_interpolation_multiplier) = multiplier;
        }
        if(args.options.contains("--fsr-gpu"))settings.fsr_adapter_id=args.options.value("--fsr-gpu");
        if(!fsr&&args.options.contains("--fsr-gpu"))throw UsageError("--fsr-* options require --interpolator fsr3");
        settings.codec = settings.frame_interpolation_codec;
        settings.container = settings.frame_interpolation_container;
        settings.quality = settings.frame_interpolation_quality;
        settings.hdr_mode = settings.frame_interpolation_hdr_mode;
        settings.video_rename_mode = settings.frame_interpolation_rename_mode;
        settings.video_custom_suffix = settings.frame_interpolation_custom_suffix;
    }
    if (args.options.contains("--format")) {
        if (!images)
            throw UsageError("--format is for images. Use --container for video output.");
        settings.image_format =
            choice(args, "--format",
                   {{"png", "PNG"}, {"jpeg", "JPEG"}, {"webp", "WebP"}, {"avif", "AVIF"}, {"tiff", "TIFF"}});
    }
    if (!videos && (args.options.contains("--codec") || args.options.contains("--container") ||
                    args.options.contains("--quality")))
        throw UsageError("--codec, --container and --quality are video options; use --format for images.");
    if (args.options.contains("--codec")) {
        settings.codec = choice(args, "--codec",
                                {{"h264", "H.264"},
                                 {"hevc", "H.265"},
                                 {"av1", "AV1"},
                                 {"prores", "ProRes HQ"},
                                 {"ffv1", "FFV1 Lossless RGB 10-bit"}});
        if (!args.options.contains("--container") &&
            !containersForCodec(settings.codec).contains(settings.container))
            settings.container = defaultContainer(settings.codec);
    }
    if (args.options.contains("--container"))
        settings.container = choice(args, "--container", {{"mp4", "MP4"}, {"mkv", "MKV"}, {"mov", "MOV"}});
    if (args.options.contains("--quality"))
        settings.quality =
            choice(args, "--quality",
                   {{"auto", "Auto (Default)"}, {"good", "Good"}, {"best", "Best"}, {"max", "Max"}});
    if (args.options.contains("--gpu")) {
        const auto selection = args.options.value("--gpu");
        QString uuid;
        if (selection.compare("auto", Qt::CaseInsensitive) == 0)
            uuid = "auto";
        else
            for (const auto& gpu : detectGpus())
                if (selection == QString::number(gpu.index) ||
                    selection.compare(gpu.uuid, Qt::CaseInsensitive) == 0) {
                    if (!gpu.compatible)
                        throw UsageError(gpu.name + " cannot run RTX processing.");
                    uuid = gpu.uuid;
                    break;
                }
        if (uuid.isEmpty())
            throw UsageError("Unknown GPU: " + selection + ". Run VE_CLI.exe devices for available IDs.");
        settings.ai_gpu_uuid = settings.video_gpu_uuid = uuid;
    }
    if (auto valid = settings.validate(); !valid)
        throw UsageError(valid.error());
    return settings;
}
std::atomic<JobControl*> activeJob = nullptr;
BOOL WINAPI consoleControl(DWORD type) {
    if (type == CTRL_C_EVENT || type == CTRL_BREAK_EVENT || type == CTRL_CLOSE_EVENT) {
        if (auto* job = activeJob.load())
            job->cancel();
        return TRUE;
    }
    return FALSE;
}
struct ConsoleCancellation {
    explicit ConsoleCancellation(JobControl& job) {
        activeJob = &job;
        SetConsoleCtrlHandler(consoleControl, TRUE);
    }
    ~ConsoleCancellation() {
        SetConsoleCtrlHandler(consoleControl, FALSE);
        activeJob = nullptr;
    }
};
} // namespace
int runCli(const QStringList& arguments) {
    if(arguments.size()==2&&arguments.front()=="--internal-fsr-worker")return runFsrWorker(arguments.back());
    if(arguments.size()==2&&arguments.front()=="--internal-vfx-worker")return runVfxWorker(arguments.back());
    if(arguments.size()==2&&arguments.front()=="--internal-reshade-worker")return runReShadeWorker(arguments.back());
    const bool jsonOutput = arguments.contains("--json");
    try {
        if (arguments.isEmpty() || arguments.contains("--help") || arguments.contains("-h")) {
            help(arguments.isEmpty()                 ? QString{}
                 : arguments.front().startsWith('-') ? QString{}
                                                     : arguments.front());
            return 0;
        }
        if (arguments == QStringList{"--version"}) {
            output << "Visual Enhancer CLI " << QCoreApplication::applicationVersion() << '\n';
            output.flush();
            return 0;
        }
        const auto args = parse(arguments);
        if (args.command == "devices") {
            QJsonArray devices;
            for (const auto& gpu : detectGpus()) {
                bool reshadeAvailable = false;
                QString reshadeMotion = ReShadeEngine::opticalFlowBackend();
                QString reshadeDepth = QStringLiteral("MoGe-2 ViT-S (ONNX Runtime DirectML)");
                QString reshadeError;
                try {
                    ReShadeEngine::initialize(gpu.index);
                    const auto caps = ReShadeEngine::capabilities(gpu.index);
                    reshadeAvailable = caps.available != 0;
                    if (!caps.depth_available)
                        reshadeDepth = QStringLiteral("unavailable");
                    if (!caps.amd_optical_flow_available)
                        reshadeMotion = QStringLiteral("unavailable");
                } catch (const std::exception& e) {
                    reshadeError = QString::fromUtf8(e.what());
                }
                devices.append(QJsonObject{
                    {"index", gpu.index},
                    {"name", gpu.name},
                    {"uuid", gpu.uuid},
                    {"driver", gpu.driver},
                    {"memory_mb", gpu.memoryMiB},
                    {"ai_compatible", gpu.compatible},
                    {"compatibility_error",
                     gpu.compatible ? ""
                                    : "Requires an NVIDIA RTX GPU with compute capability 7.5 or newer."},
                    {"reshade_available", reshadeAvailable},
                    {"reshade_motion_backend", reshadeMotion},
                    {"reshade_depth_backend", reshadeDepth},
                    {"reshade_error", reshadeError}});
                if (!jsonOutput)
                    output << gpu.index << "  " << gpu.name << "  ("
                           << QString::number(gpu.memoryMiB / 1024., 'f', 1) << " GB VRAM)\n   " << gpu.uuid
                           << " | Driver " << gpu.driver
                           << " | RTX processing: " << (gpu.compatible ? "yes" : "unavailable")
                           << " | ReShade: " << (reshadeAvailable ? "yes (" + reshadeMotion + ")" : "unavailable")
                           << '\n';
            }
            QJsonArray fsrDevices;
            for(const auto& gpu:detectFsrAdapters()){
                fsrDevices.append(QJsonObject{{"dxgi_index",int(gpu.index)},{"id",gpu.id},{"name",gpu.name},{"available",gpu.available},{"error",gpu.unavailableReason},{"dedicated_memory_bytes",qint64(gpu.memoryBytes)}});
                if(!jsonOutput)output<<"DXGI "<<gpu.index<<"  "<<gpu.name<<" | FSR 3: "<<(gpu.available?QString("available"):gpu.unavailableReason)<<"\n   "<<gpu.id<<'\n';
            }
            if (jsonOutput)
                json({{"schema_version", 1}, {"ok", true}, {"devices", devices},{"fsr_adapters",fsrDevices}});
            else
                output.flush();
            return 0;
        }
        const auto target =
            QFileInfo(localPath(args.options.value("--output", Paths::instance().at("outputs"))))
                .absoluteFilePath();
        QStringList paths;
        for (const auto& input : args.inputs) {
            const auto accepted = collectInputs({input}, args.flags.contains("--recursive"), target);
            if (accepted.isEmpty())
                throw UsageError("No supported images or videos found: " + input);
            for (const auto& path : accepted)
                if (!paths.contains(path, Qt::CaseInsensitive))
                    paths.append(path);
        }
        auto settings = settingsFor(args, paths);
        const QMap<QString, QString> formats{{"png", "PNG"},   {"jpg", "JPEG"},  {"jpeg", "JPEG"},
                                             {"webp", "WebP"}, {"avif", "AVIF"}, {"tif", "TIFF"},
                                             {"tiff", "TIFF"}};
        const auto extension = QFileInfo(target).suffix().toLower();
        const bool explicitFile =
            args.options.contains("--output") && !QFileInfo(target).isDir() &&
            (formats.contains(extension) || QStringList{"mp4", "mkv", "mov"}.contains(extension));
        if (QFileInfo::exists(target) && !QFileInfo(target).isDir())
            throw UsageError("Output already exists: " + target + ". Choose another name.");
        if (explicitFile) {
            if (paths.size() != 1)
                throw UsageError(
                    "An output filename requires exactly one input. Use an output folder for batches.");
            if (isImage(paths[0])) {
                if (!formats.contains(extension))
                    throw UsageError("Image input requires an image output filename.");
                if (args.options.contains("--format") && settings.image_format != formats.value(extension))
                    throw UsageError("--format does not match the output filename extension.");
                settings.image_format = formats.value(extension);
            } else {
                if (!QStringList{"mp4", "mkv", "mov"}.contains(extension))
                    throw UsageError("Video input requires an MP4, MKV or MOV output filename.");
                if (args.options.contains("--container") && settings.container.toLower() != extension)
                    throw UsageError("--container does not match the output filename extension.");
                settings.container = extension.toUpper();
            }
            if (auto valid = settings.validate(); !valid)
                throw UsageError(valid.error());
        }
        QStringList destinations;
        QSet<QString> reserved;
        for (const auto& path : paths) {
            auto destination = explicitFile ? target
                                            : renderOutputPath(path, target, settings,
                                                               Workflow::NeuralRendering, isVideo(path));
            const auto original = destination;
            int sequence = 2;
            while (reserved.contains(destination.toCaseFolded())) {
                const QFileInfo file(original);
                destination = file.dir().filePath(file.completeBaseName() + '_' +
                                                  QString::number(sequence++) + '.' + file.suffix());
            }
            destinations.append(destination);
            reserved.insert(destination.toCaseFolded());
        }
        if (args.flags.contains("--dry-run")) {
            QJsonArray items;
            JobControl control;
            for (qsizetype i = 0; i < paths.size(); ++i) {
                const auto info = probeMedia(paths[i], &control);
                const auto plan = estimatePipeline(info, settings, Workflow::NeuralRendering);
                auto stages = plan.stages;
                stages.append("export");
                items.append(QJsonObject{{"input_path", paths[i]},
                                         {"output_path", destinations[i]},
                                         {"media_type", info.video ? "video" : "image"},
                                         {"stages", QJsonArray::fromStringList(stages)},
                                         {"width", plan.width},
                                         {"height", plan.height},
                                         {"fps", plan.fps},
                                         {"hdr", plan.hdr}});
                if (!jsonOutput)
                    output << '[' << i + 1 << '/' << paths.size() << "] " << paths[i] << "\n  -> "
                           << destinations[i] << "\n  " << plan.width << 'x' << plan.height
                           << (plan.fps ? QString(" | %1 FPS").arg(plan.fps) : QString{}) << " | "
                           << stages.join(" -> ") << '\n';
            }
            if (jsonOutput)
                json({{"schema_version", 1},
                      {"ok", true},
                      {"command", args.command},
                      {"dry_run", true},
                      {"total", paths.size()},
                      {"items", items}});
            else {
                output << "Plan only. Hardware availability is checked when rendering.\n";
                output.flush();
            }
            return 0;
        }
        Paths::instance().prepare();
        JobControl control;
        ConsoleCancellation cancellation(control);
        QElapsedTimer elapsed;
        elapsed.start();
        QJsonArray items;
        int completed = 0, failed = 0, skipped = 0;
        for (qsizetype i = 0; i < paths.size(); ++i) {
            if (control.cancelled()) {
                ++skipped;
                items.append(QJsonObject{{"input_path", paths[i]},
                                         {"output_path", destinations[i]},
                                         {"state", "skipped"},
                                         {"error", ""},
                                         {"elapsed_seconds", 0}});
                continue;
            }
            QElapsedTimer itemTimer;
            itemTimer.start();
            QString error, state = "complete";
            try {
                const RenderRequest request{paths[i], destinations[i], settings};
                const auto rendered = Pipeline{}.render(request, control, [&](const Progress& progress) {
                    if (!args.flags.contains("--quiet")) {
                        errors << '\r' << '[' << i + 1 << '/' << paths.size() << "] "
                               << QFileInfo(paths[i]).fileName() << "  "
                               << QString::number(progress.fraction * 100, 'f', 1) << "%  "
                               << progress.message << "        ";
                        errors.flush();
                    }
                });
                (void)rendered;
                ++completed;
            } catch (const std::exception& exception) {
                error = QString::fromUtf8(exception.what());
                state = control.cancelled() ? "cancelled" : "failed";
                if (!control.cancelled())
                    ++failed;
                errors << "\nError: " << error << '\n';
                errors.flush();
                Log::instance().append("ERROR", "cli", error);
            }
            if (!args.flags.contains("--quiet")) {
                errors << '\n';
                errors.flush();
            }
            items.append(QJsonObject{{"input_path", paths[i]},
                                     {"output_path", destinations[i]},
                                     {"state", state},
                                     {"error", error},
                                     {"elapsed_seconds", itemTimer.elapsed() / 1000.}});
        }
        const int status = control.cancelled() ? 130 : failed ? 1 : 0;
        const QJsonObject result{{"schema_version", 1},
                                 {"ok", status == 0},
                                 {"exit_code", status},
                                 {"command", args.command},
                                 {"total", paths.size()},
                                 {"completed", completed},
                                 {"failed", failed},
                                 {"skipped", skipped},
                                 {"cancelled", control.cancelled()},
                                 {"elapsed_seconds", elapsed.elapsed() / 1000.},
                                 {"items", items},
                                 {"log_path", Log::instance().sessionPath()}};
        if (jsonOutput)
            json(result);
        else {
            output << (control.cancelled() ? "Cancelled: " : "Completed: ") << completed << '/'
                   << paths.size() << " files in " << QString::number(elapsed.elapsed() / 1000., 'f', 1)
                   << "s; " << failed << " failed.\n";
            if (completed)
                output << "Output: " << (paths.size() == 1 ? destinations[0] : target) << '\n';
            if (status)
                output << "Log: " << Log::instance().sessionPath() << '\n';
            output.flush();
        }
        return status;
    } catch (const std::exception& exception) {
        const auto message = QString::fromUtf8(exception.what());
        const int status = dynamic_cast<const UsageError*>(&exception) ? 2 : 1;
        if (jsonOutput)
            json({{"schema_version", 1}, {"ok", false}, {"exit_code", status}, {"error", message}});
        errors << "Error: " << message << '\n';
        if (status == 2 && !jsonOutput)
            errors << "Run VE_CLI.exe COMMAND --help for usage.\n";
        errors.flush();
        return status;
    }
}
} // namespace ve
