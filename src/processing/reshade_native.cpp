#include "reshade_native.hpp"
#include "reshade_cuda_adapter.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "media/media.hpp"
#include "media/resolution.hpp"
#include "native_engines.hpp"
#include <QColorSpace>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextStream>
#include <algorithm>
#include <array>
#include <cmath>
#include <mutex>
#include <stdexcept>
#include <windows.h>

namespace ve {
namespace {
using ErrorBuf = std::array<char, 4096>;

int reshadeDxgiOrdinal(int cudaOrdinal) {
    // CUDA/NVIDIA ordinals are not DXGI enumeration indices. Match by LUID
    // when CUDA exists; a host-only D3D12 session does not require CUDA.
    static HMODULE cuda = LoadLibraryW(L"nvcuda.dll");
    if (!cuda) return cudaOrdinal;
    const auto init = reinterpret_cast<int(__cdecl*)(unsigned)>(GetProcAddress(cuda, "cuInit"));
    const auto device = reinterpret_cast<int(__cdecl*)(int*, int)>(GetProcAddress(cuda, "cuDeviceGet"));
    const auto luid = reinterpret_cast<int(__cdecl*)(char*, unsigned*, int)>(GetProcAddress(cuda, "cuDeviceGetLuid"));
    int dev = 0;
    LUID id{};
    unsigned mask = 0;
    if (!init || !device || !luid || init(0) || device(&dev, cudaOrdinal) ||
        luid(reinterpret_cast<char*>(&id), &mask, dev)) return cudaOrdinal;
    Microsoft::WRL::ComPtr<IDXGIFactory4> factory;
    if (FAILED(CreateDXGIFactory1(IID_PPV_ARGS(&factory)))) return cudaOrdinal;
    for (UINT index = 0;; ++index) {
        Microsoft::WRL::ComPtr<IDXGIAdapter1> adapter;
        if (factory->EnumAdapters1(index, &adapter) == DXGI_ERROR_NOT_FOUND) break;
        DXGI_ADAPTER_DESC1 desc{};
        if (adapter && SUCCEEDED(adapter->GetDesc1(&desc)) &&
            desc.AdapterLuid.HighPart == id.HighPart && desc.AdapterLuid.LowPart == id.LowPart) return 100 + int(index);
    }
    throw std::runtime_error("The selected NVIDIA GPU has no matching D3D12 adapter.");
}

void check(int ok, const ErrorBuf& err, const char* fallback = "ReShade native engine call failed.") {
    if (!ok) {
        throw std::runtime_error(err[0] ? err.data() : fallback);
    }
}

struct Api {
    HMODULE library = nullptr;
    std::recursive_mutex mutex;
    std::mutex sessionMutex;
    bool initialized = false;
    int ordinal = -1;

#define VE_RS_FN(name) decltype(&name) name##Fn = nullptr
    VE_RS_FN(reshade_abi_version);
    VE_RS_FN(reshade_version);
    VE_RS_FN(reshade_optical_flow_backend);
    VE_RS_FN(reshade_init_v2);
    VE_RS_FN(reshade_capabilities);
    VE_RS_FN(reshade_status);
    VE_RS_FN(reshade_inspect_preset);
    VE_RS_FN(reshade_export_preset_copy);
    VE_RS_FN(reshade_session_create);
    VE_RS_FN(reshade_session_release);
    VE_RS_FN(reshade_session_status);
    VE_RS_FN(reshade_process_host);
    VE_RS_FN(reshade_process_d3d12);
    VE_RS_FN(reshade_session_d3d12_device);
    VE_RS_FN(reshade_session_d3d12_queue);

    int (*reshade_process_cuda_yuvFn)(void*, const VeReShadeCudaYuvDesc*, void**, VeReShadeFrameResult*, char*, int) =
        [](void* handle, const VeReShadeCudaYuvDesc* frame, void** out_surface, VeReShadeFrameResult* result, char* error, int capacity) -> int {
            return ReShadeCudaAdapter::instance().processCudaYuv(handle, frame, out_surface, result, error, capacity);
        };
    int (*reshade_process_cuda_rgba16fFn)(void*, const VeReShadeCudaRgbDesc*, void**, VeReShadeFrameResult*, char*, int) =
        [](void* handle, const VeReShadeCudaRgbDesc* frame, void** out_surface, VeReShadeFrameResult* result, char* error, int capacity) -> int {
            return ReShadeCudaAdapter::instance().processCudaRgb(handle, frame, out_surface, result, error, capacity);
        };
    int (*reshade_cuda_surface_descFn)(void*, VeReShadeCudaSurfaceDesc*) =
        [](void* surface, VeReShadeCudaSurfaceDesc* desc) -> int {
            return ReShadeCudaAdapter::instance().surfaceDesc(surface, desc);
        };
    void (*reshade_cuda_surface_retainFn)(void*) =
        [](void* surface) {
            ReShadeCudaAdapter::instance().surfaceRetain(surface);
        };
    void (*reshade_cuda_surface_releaseFn)(void*) =
        [](void* surface) {
            ReShadeCudaAdapter::instance().surfaceRelease(surface);
        };
#undef VE_RS_FN

    Api() {
        const auto dllPath = QDir::toNativeSeparators(
            Paths::instance().at("bin/runtime/reshade/neuroframe_engine_reshade.dll")).toStdWString();
        library = LoadLibraryExW(dllPath.c_str(), nullptr,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_APPLICATION_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32);
        if (!library) {
            throw std::runtime_error("Cannot load ReShade native engine (neuroframe_engine_reshade.dll): Windows error " +
                                     std::to_string(GetLastError()));
        }
#define VE_RS_LOAD(name) \
        name##Fn = reinterpret_cast<decltype(name##Fn)>(GetProcAddress(library, #name)); \
        if (!name##Fn) throw std::runtime_error("Missing ReShade engine export: " #name)
        VE_RS_LOAD(reshade_abi_version);
        VE_RS_LOAD(reshade_version);
        VE_RS_LOAD(reshade_optical_flow_backend);
        VE_RS_LOAD(reshade_init_v2);
        VE_RS_LOAD(reshade_capabilities);
        VE_RS_LOAD(reshade_status);
        VE_RS_LOAD(reshade_inspect_preset);
        VE_RS_LOAD(reshade_export_preset_copy);
        VE_RS_LOAD(reshade_session_create);
        VE_RS_LOAD(reshade_session_release);
        VE_RS_LOAD(reshade_session_status);
        VE_RS_LOAD(reshade_process_host);
        VE_RS_LOAD(reshade_process_d3d12);
        VE_RS_LOAD(reshade_session_d3d12_device);
        VE_RS_LOAD(reshade_session_d3d12_queue);
#undef VE_RS_LOAD

        if (reshade_abi_versionFn() != VE_RESHADE_ABI_VERSION) {
            throw std::runtime_error("Incompatible ReShade native engine ABI version.");
        }
        const QString backend = QString::fromUtf8(reshade_optical_flow_backendFn());
        if (!backend.startsWith("AMD FidelityFX Optical Flow 1.1.2")) {
            throw std::runtime_error("ReShade optical flow backend contract violation: expected AMD FidelityFX Optical Flow 1.1.2.");
        }
    }

    void ensureInitialized(int gpuOrdinal) {
        std::lock_guard lock(mutex);
        if (initialized && ordinal == gpuOrdinal) {
            return;
        }
        const auto runtimeRoot = QDir::toNativeSeparators(Paths::instance().at("bin/runtime/reshade")).toStdWString();
        const auto dataRoot = QDir::toNativeSeparators(Paths::instance().nativeDataDirectory()).toStdWString();
        ErrorBuf err{};
        check(reshade_init_v2Fn(reshadeDxgiOrdinal(gpuOrdinal), runtimeRoot.c_str(), dataRoot.c_str(), err.data(), int(err.size())),
              err, "ReShade engine initialization failed.");
        ReShadeCudaAdapter::instance().initialize(gpuOrdinal);
        initialized = true;
        ordinal = gpuOrdinal;
    }

    static Api& get() {
        static Api instance;
        return instance;
    }
};

uint32_t mapPrecisionPolicy(const QString& policy) {
    if (policy == "FP16") return VE_RESHADE_PRECISION_FP16;
    if (policy == "8-bit sRGB Compatibility") return VE_RESHADE_PRECISION_UNORM8_SRGB;
    return VE_RESHADE_PRECISION_AUTO;
}

uint32_t mapHdrPolicy(const QString& policy) {
    if (policy == "HDR Aware") return VE_RESHADE_HDR_AWARE;
    if (policy == "SDR Adapter") return VE_RESHADE_HDR_SDR_ADAPTER;
    if (policy == "Reject Unsupported") return VE_RESHADE_HDR_REJECT_UNSUPPORTED;
    return VE_RESHADE_HDR_AUTO;
}

uint32_t mapDepthMode(const QString& mode) {
    if (mode == "Off") return VE_RESHADE_DEPTH_OFF;
    if (mode == "AI Depth") return VE_RESHADE_DEPTH_AI;
    return VE_RESHADE_DEPTH_AUTO;
}

uint32_t mapTemporalPolicy(const QString& policy) {
    if (policy == "Spatial Only (Off)") return VE_RESHADE_TEMPORAL_SPATIAL_OFF;
    return VE_RESHADE_TEMPORAL_QUALITY;
}

qint64 secondsToNs(double seconds) {
    if (!std::isfinite(seconds) || seconds < 0.0) return 0;
    return qint64(std::llround(seconds * 1000000000.0));
}
} // namespace

uint32_t reshadeColorTransfer(const MediaInfo& info, bool hdrOverride) {
    if (info.transfer == "smpte2084") return VE_RESHADE_TRANSFER_PQ;
    if (info.transfer == "arib-std-b67") return VE_RESHADE_TRANSFER_HLG;
    if (info.transfer == "linear") return VE_RESHADE_TRANSFER_LINEAR;
    if (hdrOverride || info.hdr) return VE_RESHADE_TRANSFER_PQ;
    if (!info.video || QStringList{"iec61966-2-1", "srgb"}.contains(info.transfer)) return VE_RESHADE_TRANSFER_SRGB;
    if (QStringList{"gamma22", "bt470m"}.contains(info.transfer)) return VE_RESHADE_TRANSFER_GAMMA22;
    if (QStringList{"gamma28", "bt470bg"}.contains(info.transfer)) return VE_RESHADE_TRANSFER_GAMMA28;
    return VE_RESHADE_TRANSFER_BT709;
}

bool reshadeUsesTemporalDepth(const Settings& raw, bool video) {
    if (!video) return false;
    const auto s = reshadeSettingsForMode(raw, true);
    if (s.reshade_temporal_stability == "Spatial Only (Off)") return false;
    return s.reshade_depth_mode != "Off";
}

QString reshadeReport(const Settings& raw, bool video) {
    const auto s = reshadeSettingsForMode(raw, video);
    const QString presetName = s.reshade_preset_path.isEmpty()
        ? QStringLiteral("No preset (passthrough)")
        : QFileInfo(s.reshade_preset_path).fileName();
    return QString("ReShade (%1, mix %2%, depth %3, motion AMD FidelityFX Optical Flow 1.1.2)")
        .arg(presetName)
        .arg(s.reshade_mix)
        .arg(s.reshade_depth_mode);
}

void ReShadeEngine::initialize(int ordinal) {
    Api::get().ensureInitialized(ordinal);
}

VeReShadeCapabilities ReShadeEngine::capabilities(int ordinal) {
    auto& api = Api::get();
    api.ensureInitialized(ordinal);
    std::lock_guard lock(api.mutex);
    VeReShadeCapabilities caps{};
    caps.size = sizeof(caps);
    caps.abi = VE_RESHADE_ABI_VERSION;
    ErrorBuf err{};
    check(api.reshade_capabilitiesFn(&caps, err.data(), int(err.size())), err, "Cannot query ReShade capabilities.");
    caps.cuda_interop_available = ReShadeCudaAdapter::instance().isAvailable() ? 1u : 0u;
    return caps;
}

QString ReShadeEngine::version() {
    return QString::fromUtf8(Api::get().reshade_versionFn());
}

QString ReShadeEngine::opticalFlowBackend() {
    return QString::fromUtf8(Api::get().reshade_optical_flow_backendFn());
}

QString ReShadeEngine::diagnostics() {
    try {
        auto& api = Api::get();
        if (!api.initialized) {
            api.ensureInitialized(0);
        }
        std::array<char, 8192> buf{};
        if (api.reshade_statusFn(buf.data(), int(buf.size()))) {
            return QString::fromUtf8(buf.data());
        }
        return QStringLiteral("{}");
    } catch (const std::exception& e) {
        return QString::fromUtf8(e.what());
    }
}

QVariantMap ReShadeEngine::statusMap() {
    const auto json = diagnostics().toUtf8();
    return QJsonDocument::fromJson(json).object().toVariantMap();
}

QString ReShadeEngine::presetResetOverrides(const QString& presetPath) {
    if (presetPath.trimmed().isEmpty()) return {};
    struct PresetDefaults {
        bool hasTechniques = false;
        QString techniques, redirect;
    };
    const auto read = [](const QString& path) {
        QFile file(path);
        if (!file.open(QIODevice::ReadOnly | QIODevice::Text))
            throw std::runtime_error(file.errorString().toStdString());
        PresetDefaults defaults;
        QTextStream stream(&file);
        QString section;
        while (!stream.atEnd()) {
            const auto line = stream.readLine().trimmed();
            if (line.isEmpty() || line.startsWith(';') || line.startsWith('#')) continue;
            if (line.startsWith('[') && line.endsWith(']')) {
                section = line.mid(1, line.size() - 2);
                continue;
            }
            const auto separator = line.indexOf('=');
            if (separator < 0) continue;
            const auto key = line.left(separator).trimmed();
            const auto value = line.mid(separator + 1).trimmed();
            if (section.isEmpty() && key == "Techniques") {
                defaults.hasTechniques = true;
                defaults.techniques = value;
            } else if (section == "GENERAL") {
                if (key == "PresetFiles") defaults.redirect = value.section(',', 0, 0).trimmed();
                else if (defaults.redirect.isEmpty() && (key == "PresetPath" || key == "CurrentPresetPath"))
                    defaults.redirect = value;
            }
        }
        if (stream.status() != QTextStream::Ok)
            throw std::runtime_error(file.errorString().toStdString());
        return defaults;
    };
    const QFileInfo preset(localPath(presetPath));
    auto defaults = read(preset.absoluteFilePath());
    // Match the runtime's one-level ReShade.ini preset indirection.
    if (!defaults.hasTechniques && !defaults.redirect.isEmpty()) {
        const auto target = QDir(preset.absolutePath()).filePath(defaults.redirect);
        if (QFileInfo(target).isFile()) defaults = read(target);
    }
    QJsonObject overrides;
    if (defaults.hasTechniques) overrides.insert("Techniques", defaults.techniques);
    return QString::fromUtf8(QJsonDocument(overrides).toJson(QJsonDocument::Compact));
}

QByteArray normalizeOverridesForBridge(const QString& raw) {
    const auto trimmed = raw.trimmed();
    if (trimmed.isEmpty())
        return {};
    if (trimmed.startsWith('{')) {
        QJsonParseError err{};
        const auto doc = QJsonDocument::fromJson(trimmed.toUtf8(), &err);
        if (err.error == QJsonParseError::NoError && doc.isObject()) {
            QStringList lines;
            const auto obj = doc.object();
            for (auto it = obj.begin(); it != obj.end(); ++it) {
                if (it.value().isBool())
                    lines.append(it.key() + '=' + (it.value().toBool() ? "1" : "0"));
                else if (it.value().isArray()) {
                    QStringList parts;
                    for (const auto& elem : it.value().toArray())
                        parts.append(elem.toVariant().toString());
                    lines.append(it.key() + '=' + parts.join(','));
                } else
                    lines.append(it.key() + '=' + it.value().toVariant().toString());
            }
            return lines.join('\n').toUtf8();
        }
    }
    return trimmed.toUtf8();
}

QVariantMap ReShadeEngine::inspectPreset(const Settings& raw, bool video, QSize previewSize) {
    const auto s = reshadeSettingsForMode(raw, video);
    if (s.reshade_preset_path.trimmed().isEmpty()) {
        return {
            {"valid", true},
            {"empty", true},
            {"error", QString()},
            {"compatibility_class", QStringLiteral("none")},
            {"requires_depth", false},
            {"requires_unorm8", false},
            {"requires_custom_semantic", false},
            {"uses_interactive_input", false},
            {"optical_flow_backend", QStringLiteral("AMD FidelityFX Optical Flow 1.1.2")},
            {"techniques", QVariantList{}},
            {"parameters", QVariantList{}},
            {"dependencies", QVariantList{}}
        };
    }

    auto& api = Api::get();
    api.ensureInitialized(gpuOrdinal(s.ai_gpu_uuid));
    std::lock_guard sessionLock(api.sessionMutex);
    std::lock_guard apiLock(api.mutex);

    const QByteArray presetUtf8 = s.reshade_preset_path.toUtf8();
    const QByteArray shadersUtf8 = s.reshade_shader_search_paths.toUtf8();
    const QByteArray texturesUtf8 = s.reshade_texture_search_paths.toUtf8();
    const QByteArray overridesUtf8 = normalizeOverridesForBridge(s.reshade_parameter_overrides);

    VeReShadePresetDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    desc.preset_path_utf8 = presetUtf8.constData();
    desc.shader_roots_utf8 = shadersUtf8.constData();
    desc.texture_roots_utf8 = texturesUtf8.constData();
    desc.overrides_json_utf8 = overridesUtf8.constData();
    desc.preview_width = uint32_t(std::clamp(previewSize.width(), 16, 4096));
    desc.preview_height = uint32_t(std::clamp(previewSize.height(), 16, 4096));

    std::vector<char> jsonBuf(1024 * 1024, '\0');
    ErrorBuf err{};
    const int rc = api.reshade_inspect_presetFn(&desc, jsonBuf.data(), int(jsonBuf.size()), err.data(), int(err.size()));
    auto map = QJsonDocument::fromJson(QByteArray(jsonBuf.data())).object().toVariantMap();
    if (!rc && map.value("error").toString().isEmpty()) {
        map["valid"] = false;
        map["error"] = QString::fromUtf8(err[0] ? err.data() : "ReShade preset validation failed.");
    }
    return map;
}

void ReShadeEngine::exportPresetCopy(const Settings& raw, bool video, const QString& destinationPath) {
    const auto s = reshadeSettingsForMode(raw, video);
    if (s.reshade_preset_path.trimmed().isEmpty()) {
        throw std::runtime_error("Select a ReShade .ini preset before exporting a copy.");
    }
    auto& api = Api::get();
    api.ensureInitialized(gpuOrdinal(s.ai_gpu_uuid));
    std::lock_guard apiLock(api.mutex);

    const QByteArray presetUtf8 = s.reshade_preset_path.toUtf8();
    const QByteArray shadersUtf8 = s.reshade_shader_search_paths.toUtf8();
    const QByteArray texturesUtf8 = s.reshade_texture_search_paths.toUtf8();
    const QByteArray overridesUtf8 = normalizeOverridesForBridge(s.reshade_parameter_overrides);
    const QByteArray dstUtf8 = destinationPath.toUtf8();

    VeReShadePresetDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    desc.preset_path_utf8 = presetUtf8.constData();
    desc.shader_roots_utf8 = shadersUtf8.constData();
    desc.texture_roots_utf8 = texturesUtf8.constData();
    desc.overrides_json_utf8 = overridesUtf8.constData();
    desc.preview_width = 64;
    desc.preview_height = 64;

    ErrorBuf err{};
    check(api.reshade_export_preset_copyFn(&desc, dstUtf8.constData(), err.data(), int(err.size())),
          err, "Cannot export ReShade preset copy.");
}

struct ReShadeSession::State {
    std::unique_lock<std::mutex> ownership;
    void* handle = nullptr;
    QSize size;
    MediaInfo colors;
    bool still = false;
    bool wideOutput = false;
    uint32_t transfer = VE_RESHADE_TRANSFER_BT709;
    VeReShadeFrameResult lastResult{};

    ~State() {
        if (handle) {
            ReShadeCudaAdapter::instance().releaseSessionBuffers(handle);
            auto& api = Api::get();
            std::lock_guard lock(api.mutex);
            api.reshade_session_releaseFn(handle);
            handle = nullptr;
        }
    }
};

ReShadeSession::ReShadeSession(QSize size, const Settings& raw, const MediaInfo& colors,
                               bool still, bool wideOutput, bool preserveAlpha)
    : state_(std::make_unique<State>()) {
    requireResolution(size, "ReShade session");
    const auto s = reshadeSettingsForMode(raw, !still);
    const int ordinal = gpuOrdinal(s.ai_gpu_uuid);
    auto& api = Api::get();
    api.ensureInitialized(ordinal);

    state_->ownership = std::unique_lock<std::mutex>(api.sessionMutex);
    std::lock_guard apiLock(api.mutex);

    state_->size = size;
    state_->colors = colors;
    state_->still = still;
    state_->wideOutput = wideOutput;
    state_->transfer = reshadeColorTransfer(colors);

    const QByteArray presetUtf8 = s.reshade_preset_path.trimmed().toUtf8();
    const QByteArray shadersUtf8 = s.reshade_shader_search_paths.toUtf8();
    const QByteArray texturesUtf8 = s.reshade_texture_search_paths.toUtf8();
    const QByteArray overridesUtf8 = normalizeOverridesForBridge(s.reshade_parameter_overrides);

    VeReShadeSessionDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    desc.width = uint32_t(size.width());
    desc.height = uint32_t(size.height());
    desc.still_image = still ? 1u : 0u;
    desc.precision_policy = mapPrecisionPolicy(s.reshade_precision_policy);
    desc.color_transfer = state_->transfer;
    desc.hdr_policy = mapHdrPolicy(s.reshade_hdr_policy);
    desc.depth_mode = mapDepthMode(s.reshade_depth_mode);
    desc.depth_resolution_bound = uint32_t(std::clamp(s.reshade_depth_resolution, 196, 1008));
    desc.temporal_policy = still ? VE_RESHADE_TEMPORAL_SPATIAL_OFF : mapTemporalPolicy(s.reshade_temporal_stability);
    desc.temporal_strength = float(std::clamp(s.reshade_temporal_strength / 100.0, 0.0, 1.0));
    desc.mix = float(std::clamp(s.reshade_mix / 100.0, 0.0, 1.0));
    desc.preserve_alpha = (preserveAlpha && s.reshade_preserve_alpha) ? 1u : 0u;
    desc.deterministic_seed = uint32_t(s.reshade_deterministic_seed);
    desc.preset_path_utf8 = presetUtf8.constData();
    desc.shader_roots_utf8 = shadersUtf8.constData();
    desc.texture_roots_utf8 = texturesUtf8.constData();
    desc.overrides_json_utf8 = overridesUtf8.constData();

    ErrorBuf err{};
    state_->handle = api.reshade_session_createFn(&desc, err.data(), int(err.size()));
    check(state_->handle != nullptr, err, "Cannot create ReShade session.");
}

ReShadeSession::~ReShadeSession() = default;

QImage ReShadeSession::process(const QImage& input, bool reset, qint64 frameOrdinal,
                               qint64 ptsNs, qint64 durationNs) {
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);

    const bool fp16 = input.format() == QImage::Format_RGBA16FPx4;
    const auto source = fp16 ? input
        : input.convertToFormat(input.format() == QImage::Format_RGBA8888
                                    ? QImage::Format_RGBA8888
                                    : QImage::Format_RGBA64);
    const bool eightOut = !fp16 && state_->still && !state_->wideOutput && source.depth() <= 32;
    QImage output(state_->size, fp16 ? QImage::Format_RGBA16FPx4
                                     : eightOut ? QImage::Format_RGBA8888
                                                : QImage::Format_RGBA64);
    requireImageAllocation(output, "ReShade output allocation");

    VeReShadeHostFrameDesc frame{};
    frame.size = sizeof(frame);
    frame.abi = VE_RESHADE_ABI_VERSION;
    frame.input_pixels = source.constBits();
    frame.output_pixels = output.bits();
    frame.input_stride = uint32_t(source.bytesPerLine());
    frame.output_stride = uint32_t(output.bytesPerLine());
    frame.input_format = fp16 ? VE_RESHADE_FORMAT_RGBA16F
        : source.format() == QImage::Format_RGBA8888 ? VE_RESHADE_FORMAT_RGBA8
                                                     : VE_RESHADE_FORMAT_RGBA16_UNORM;
    frame.output_format = fp16 ? VE_RESHADE_FORMAT_RGBA16F
        : eightOut ? VE_RESHADE_FORMAT_RGBA8
                   : VE_RESHADE_FORMAT_RGBA16_UNORM;
    frame.frame_ordinal = uint64_t(std::max<qint64>(0, frameOrdinal));
    frame.pts_ns = ptsNs;
    frame.duration_ns = std::max<qint64>(1, durationNs);
    frame.reset = reset ? 1u : 0u;

    state_->lastResult = {};
    state_->lastResult.size = sizeof(VeReShadeFrameResult);
    state_->lastResult.abi = VE_RESHADE_ABI_VERSION;
    ErrorBuf err{};
    check(api.reshade_process_hostFn(state_->handle, &frame, &state_->lastResult, err.data(), int(err.size())),
          err, "ReShade host frame processing failed.");
    output.setColorSpace(input.colorSpace());
    return output;
}

CudaVideoFrame ReShadeSession::process(const CudaVideoFrame& input, CudaFramePool& pool,
                                       bool p010, bool reset, qint64 frameOrdinal) {
    if (!input.owner || input.memory != FrameMemory::Cuda) {
        throw std::runtime_error("ReShade CUDA YUV processing requires owned CUDA decoder planes.");
    }
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);

    const double ptsSec = (input.timeBase.denominator > 0)
        ? double(input.pts) * input.timeBase.numerator / input.timeBase.denominator
        : 0.0;
    const double durSec = (input.duration > 0 && input.timeBase.denominator > 0)
        ? double(input.duration) * input.timeBase.numerator / input.timeBase.denominator
        : (state_->colors.fps > 0 ? 1.0 / state_->colors.fps : 1.0 / 30.0);

    VeReShadeCudaYuvDesc frame{};
    frame.size = sizeof(frame);
    frame.abi = VE_RESHADE_ABI_VERSION;
    frame.y_pointer = input.planes[0];
    frame.uv_pointer = input.planes[1];
    frame.y_stride = input.strides[0];
    frame.uv_stride = input.strides[1];
    frame.width = uint32_t(input.size.width());
    frame.height = uint32_t(input.size.height());
    frame.pixel_format = input.format == VideoPixelFormat::P010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12;
    frame.output_format = p010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12;
    frame.color_matrix = input.colorMatrix;
    frame.color_range = input.colorRange;
    frame.chroma_location = input.chromaLocation;
    frame.rotation = input.rotation;
    frame.frame_ordinal = uint64_t(std::max<qint64>(0, frameOrdinal));
    frame.pts_ns = secondsToNs(ptsSec);
    frame.duration_ns = std::max<int64_t>(1, secondsToNs(durSec));
    frame.reset = reset ? 1u : 0u;

    state_->lastResult = {};
    state_->lastResult.size = sizeof(VeReShadeFrameResult);
    state_->lastResult.abi = VE_RESHADE_ABI_VERSION;
    void* surface = nullptr;
    ErrorBuf err{};
    check(api.reshade_process_cuda_yuvFn(state_->handle, &frame, &surface, &state_->lastResult, err.data(), int(err.size())),
          err, "ReShade CUDA YUV frame processing failed.");

    const auto release = api.reshade_cuda_surface_releaseFn;
    std::shared_ptr<void> owner(surface, [release](void* s) { release(s); });
    VeReShadeCudaSurfaceDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    if (!api.reshade_cuda_surface_descFn(surface, &desc) ||
        desc.width != uint32_t(state_->size.width()) ||
        desc.height != uint32_t(state_->size.height()) ||
        desc.pixel_format != uint32_t(p010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12)) {
        throw std::runtime_error("ReShade returned an incompatible CUDA YUV surface.");
    }
    auto output = pool.wrap(std::move(owner), {desc.y_pointer, desc.uv_pointer, 0},
                            {desc.stride, desc.stride, 0}, input.colorMatrix, input.colorRange);
    output.pts = input.pts;
    output.duration = input.duration;
    output.timeBase = input.timeBase;
    return output;
}

GpuRgbFrame ReShadeSession::processRgb(const CudaVideoFrame& input, bool reset, qint64 frameOrdinal) {
    if (!input.owner || input.memory != FrameMemory::Cuda) {
        throw std::runtime_error("ReShade RGB delivery requires owned CUDA decoder planes.");
    }
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);

    const double ptsSec = (input.timeBase.denominator > 0)
        ? double(input.pts) * input.timeBase.numerator / input.timeBase.denominator
        : 0.0;
    const double durSec = (input.duration > 0 && input.timeBase.denominator > 0)
        ? double(input.duration) * input.timeBase.numerator / input.timeBase.denominator
        : (state_->colors.fps > 0 ? 1.0 / state_->colors.fps : 1.0 / 30.0);

    VeReShadeCudaYuvDesc frame{};
    frame.size = sizeof(frame);
    frame.abi = VE_RESHADE_ABI_VERSION;
    frame.y_pointer = input.planes[0];
    frame.uv_pointer = input.planes[1];
    frame.y_stride = input.strides[0];
    frame.uv_stride = input.strides[1];
    frame.width = uint32_t(input.size.width());
    frame.height = uint32_t(input.size.height());
    frame.pixel_format = input.format == VideoPixelFormat::P010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12;
    frame.output_format = VE_RESHADE_FORMAT_RGBA16F;
    frame.color_matrix = input.colorMatrix;
    frame.color_range = input.colorRange;
    frame.chroma_location = input.chromaLocation;
    frame.rotation = input.rotation;
    frame.frame_ordinal = uint64_t(std::max<qint64>(0, frameOrdinal));
    frame.pts_ns = secondsToNs(ptsSec);
    frame.duration_ns = std::max<int64_t>(1, secondsToNs(durSec));
    frame.reset = reset ? 1u : 0u;

    state_->lastResult = {};
    state_->lastResult.size = sizeof(VeReShadeFrameResult);
    state_->lastResult.abi = VE_RESHADE_ABI_VERSION;
    void* surface = nullptr;
    ErrorBuf err{};
    check(api.reshade_process_cuda_yuvFn(state_->handle, &frame, &surface, &state_->lastResult, err.data(), int(err.size())),
          err, "ReShade CUDA YUV to RGBA16F processing failed.");

    const auto release = api.reshade_cuda_surface_releaseFn;
    std::shared_ptr<void> owner(surface, [release](void* s) { release(s); });
    VeReShadeCudaSurfaceDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    if (!api.reshade_cuda_surface_descFn(surface, &desc) ||
        desc.pixel_format != VE_RESHADE_FORMAT_RGBA16F || desc.uv_pointer != 0 ||
        desc.width != uint32_t(state_->size.width()) || desc.height != uint32_t(state_->size.height())) {
        throw std::runtime_error("ReShade returned an incompatible CUDA RGBA16F surface.");
    }
    return {std::move(owner), state_->size, desc.y_pointer, desc.stride,
            state_->transfer, state_->transfer >= 5 ? 2u : 1u, bool(state_->lastResult.history_reset)};
}

GpuRgbFrame ReShadeSession::processRgb(const GpuRgbFrame& input, bool reset, qint64 frameOrdinal,
                                       qint64 ptsNs, qint64 durationNs) {
    if (!input.owner || !input.pointer || input.transfer != state_->transfer) {
        throw std::runtime_error("ReShade RGB input requires an owned surface matching the session transfer function.");
    }
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);

    VeReShadeCudaRgbDesc frame{};
    frame.size = sizeof(frame);
    frame.abi = VE_RESHADE_ABI_VERSION;
    frame.pointer = input.pointer;
    frame.stride = input.stride;
    frame.width = uint32_t(input.size.width());
    frame.height = uint32_t(input.size.height());
    frame.output_format = VE_RESHADE_FORMAT_RGBA16F;
    frame.color_matrix = input.primaries == 2 ? 2u : 1u;
    frame.color_range = 0;
    frame.frame_ordinal = uint64_t(std::max<qint64>(0, frameOrdinal));
    frame.pts_ns = ptsNs;
    frame.duration_ns = std::max<int64_t>(1, durationNs);
    frame.reset = (reset || input.historyReset) ? 1u : 0u;

    state_->lastResult = {};
    state_->lastResult.size = sizeof(VeReShadeFrameResult);
    state_->lastResult.abi = VE_RESHADE_ABI_VERSION;
    void* surface = nullptr;
    ErrorBuf err{};
    check(api.reshade_process_cuda_rgba16fFn(state_->handle, &frame, &surface, &state_->lastResult, err.data(), int(err.size())),
          err, "ReShade CUDA RGBA16F to RGBA16F processing failed.");

    const auto release = api.reshade_cuda_surface_releaseFn;
    std::shared_ptr<void> owner(surface, [release](void* s) { release(s); });
    VeReShadeCudaSurfaceDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    if (!api.reshade_cuda_surface_descFn(surface, &desc) ||
        desc.pixel_format != VE_RESHADE_FORMAT_RGBA16F || desc.uv_pointer != 0 ||
        desc.width != uint32_t(state_->size.width()) || desc.height != uint32_t(state_->size.height())) {
        throw std::runtime_error("ReShade returned an incompatible CUDA RGBA16F surface.");
    }
    return {std::move(owner), state_->size, desc.y_pointer, desc.stride,
            state_->transfer, input.primaries, bool(state_->lastResult.history_reset) || input.historyReset};
}

CudaVideoFrame ReShadeSession::process(const GpuRgbFrame& input, CudaFramePool& pool,
                                       bool p010, bool reset, qint64 frameOrdinal,
                                       qint64 ptsNs, qint64 durationNs) {
    if (!input.owner || !input.pointer || input.transfer != state_->transfer) {
        throw std::runtime_error("ReShade RGB input requires an owned surface matching the session transfer function.");
    }
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);

    VeReShadeCudaRgbDesc frame{};
    frame.size = sizeof(frame);
    frame.abi = VE_RESHADE_ABI_VERSION;
    frame.pointer = input.pointer;
    frame.stride = input.stride;
    frame.width = uint32_t(input.size.width());
    frame.height = uint32_t(input.size.height());
    frame.output_format = p010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12;
    frame.color_matrix = input.primaries == 2 ? 2u : 1u;
    frame.color_range = 0;
    frame.frame_ordinal = uint64_t(std::max<qint64>(0, frameOrdinal));
    frame.pts_ns = ptsNs;
    frame.duration_ns = std::max<int64_t>(1, durationNs);
    frame.reset = (reset || input.historyReset) ? 1u : 0u;

    state_->lastResult = {};
    state_->lastResult.size = sizeof(VeReShadeFrameResult);
    state_->lastResult.abi = VE_RESHADE_ABI_VERSION;
    void* surface = nullptr;
    ErrorBuf err{};
    check(api.reshade_process_cuda_rgba16fFn(state_->handle, &frame, &surface, &state_->lastResult, err.data(), int(err.size())),
          err, "ReShade CUDA RGBA16F to YUV processing failed.");

    const auto release = api.reshade_cuda_surface_releaseFn;
    std::shared_ptr<void> owner(surface, [release](void* s) { release(s); });
    VeReShadeCudaSurfaceDesc desc{};
    desc.size = sizeof(desc);
    desc.abi = VE_RESHADE_ABI_VERSION;
    if (!api.reshade_cuda_surface_descFn(surface, &desc) ||
        desc.pixel_format != uint32_t(p010 ? VE_RESHADE_FORMAT_P010 : VE_RESHADE_FORMAT_NV12) ||
        desc.width != uint32_t(state_->size.width()) || desc.height != uint32_t(state_->size.height())) {
        throw std::runtime_error("ReShade returned an incompatible CUDA YUV delivery surface.");
    }
    return pool.wrap(std::move(owner), {desc.y_pointer, desc.uv_pointer, 0},
                     {desc.stride, desc.stride, 0}, frame.color_matrix, frame.color_range);
}

QString ReShadeSession::diagnostics() const {
    auto& api = Api::get();
    std::lock_guard lock(api.mutex);
    std::array<char, 8192> buf{};
    if (!api.reshade_session_statusFn(state_->handle, buf.data(), int(buf.size()))) {
        throw std::runtime_error("Cannot read ReShade session telemetry.");
    }
    return QString::fromUtf8(buf.data());
}

QVariantMap ReShadeSession::statusMap() const {
    return QJsonDocument::fromJson(diagnostics().toUtf8()).object().toVariantMap();
}

VeReShadeFrameResult ReShadeSession::lastResult() const {
    return state_->lastResult;
}
} // namespace ve
