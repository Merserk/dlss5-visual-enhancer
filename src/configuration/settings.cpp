#include "settings.hpp"
#include "infrastructure/paths.hpp"
#include <QFile>
#include <QJsonArray>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QSaveFile>
#include <QSet>
#include <QTextStream>
#include <QUuid>
#include <algorithm>
#include <cmath>
#include <limits>

namespace ve {
const QStringList& vfxStageIds(){
    static const QStringList ids{"vfx_vsr","vfx_upscale","vfx_frame_generation"};return ids;
}
bool isVfxStage(QStringView id){return id==u"nvidia_vfx"||vfxStageIds().contains(id.toString());}
bool hasVfxStages(const QStringList& stages){return std::any_of(stages.begin(),stages.end(),[](const QString& id){return isVfxStage(id);});}
namespace {
bool legacyVfxEnabled(const Settings& s,const QString& id){
    return id=="vfx_vsr"?s.vfx_vsr_enabled:id=="vfx_frame_generation"?s.vfx_frame_generation_enabled:id=="vfx_upscale"?s.vfx_upscale_enabled:false;
}
}
QStringList normalizeVfxStages(const QStringList& stages,const Settings& s){
    QStringList result;for(const auto& id:stages){
        if(id!="nvidia_vfx")result.append(id);
        else{bool any=false;for(const auto& card:vfxStageIds())if(legacyVfxEnabled(s,card)){result.append(card);any=true;}if(!any)result.append(id);}
    }return result;
}
Settings vfxSettingsForStages(const Settings& settings,const QStringList& stages){
    const auto active=normalizeVfxStages(stages,settings);auto s=settings;
    s.vfx_vsr_enabled=active.contains("vfx_vsr");
    s.vfx_frame_generation_enabled=active.contains("vfx_frame_generation");
    s.vfx_upscale_enabled=active.contains("vfx_upscale");return s;
}
QStringList vfxStageGroup(const QStringList& stages,qsizetype begin,const Settings& settings){
    QStringList group;int rank=-1;
    for(auto i=begin;i<stages.size()&&isVfxStage(stages[i]);++i){
        const auto& id=stages[i];const int next=int(vfxStageIds().indexOf(id));
        if(!group.isEmpty()&&(next<=rank||(id=="vfx_upscale"&&group.contains("vfx_vsr")&&(settings.vfx_vsr_mode<8||settings.vfx_vsr_mode>15))))break;
        group.append(id);rank=next;
    }return group;
}
Settings reshadeSettingsForMode(const Settings& settings, bool video) {
    if (video)
        return settings;
    const Settings defaults;
    const bool hasImageSpecific =
        settings.reshade_image_configured ||
        !settings.reshade_image_preset_path.isEmpty() ||
        !settings.reshade_image_shader_search_paths.isEmpty() ||
        !settings.reshade_image_texture_search_paths.isEmpty() ||
        !settings.reshade_image_parameter_overrides.isEmpty() ||
        settings.reshade_image_mix != defaults.reshade_image_mix ||
        settings.reshade_image_depth_mode != defaults.reshade_image_depth_mode ||
        // The old default alone must not turn an untouched legacy image
        // configuration into an override of the shared preset.
        (settings.reshade_image_depth_resolution != defaults.reshade_image_depth_resolution &&
         settings.reshade_image_depth_resolution != 504) ||
        settings.reshade_image_precision_policy != defaults.reshade_image_precision_policy ||
        settings.reshade_image_hdr_policy != defaults.reshade_image_hdr_policy ||
        settings.reshade_image_preserve_alpha != defaults.reshade_image_preserve_alpha ||
        settings.reshade_image_deterministic_seed != defaults.reshade_image_deterministic_seed;
    Settings active = settings;
    active.reshade_temporal_stability = QStringLiteral("Spatial Only (Off)");
    active.reshade_temporal_strength = 0;
    if (!hasImageSpecific && !settings.reshade_preset_path.isEmpty())
        return active;
    active.reshade_preset_path = settings.reshade_image_preset_path;
    active.reshade_shader_search_paths = settings.reshade_image_shader_search_paths;
    active.reshade_texture_search_paths = settings.reshade_image_texture_search_paths;
    active.reshade_parameter_overrides = settings.reshade_image_parameter_overrides;
    active.reshade_mix = settings.reshade_image_mix;
    active.reshade_depth_mode = settings.reshade_image_depth_mode;
    active.reshade_depth_resolution = settings.reshade_image_depth_resolution;
    active.reshade_precision_policy = settings.reshade_image_precision_policy;
    active.reshade_hdr_policy = settings.reshade_image_hdr_policy;
    active.reshade_preserve_alpha = settings.reshade_image_preserve_alpha;
    active.reshade_deterministic_seed = settings.reshade_image_deterministic_seed;
    return active;
}
namespace {
template <class T> T convert(const QVariant& value) {
    return value.value<T>();
}
template <> QStringList convert(const QVariant& value) {
    if (value.metaType().id() == QMetaType::QStringList)
        return value.toStringList();
    QStringList values;
    for (const auto& entry : value.toList())
        values.append(entry.toString());
    return values;
}
QStringList canonicalStages(bool video) {
    const Settings defaults;
    return video ? defaults.video_stage_order : defaults.image_stage_order;
}
void migrateStages(Settings& settings, bool video) {
    auto& order = video ? settings.video_stage_order : settings.image_stage_order;
    auto& enabled = video ? settings.video_enabled_stages : settings.image_enabled_stages;
    if(video){
        const auto legacy=order.indexOf("nvidia_vfx");
        if(legacy>=0){order.removeAt(legacy);for(qsizetype n=0;n<vfxStageIds().size();++n)order.insert(legacy+n,vfxStageIds()[n]);}
        if(enabled.removeAll("nvidia_vfx"))for(const auto& id:vfxStageIds())if(legacyVfxEnabled(settings,id))enabled.append(id);
    }
    const auto allowed = canonicalStages(video);
    const bool legacyDlss = !order.contains("dlss_super_resolution"),
               legacyHdr = video && !order.contains("rtx_video_hdr");
    const auto engine = video ? settings.upscale_engine : settings.upscale_image_engine;
    const bool scaleDlss =
        legacyDlss && settings.nr_scale_method == "DLSS" && enabled.contains("scale_method");
    const bool upscaleDlss = legacyDlss && engine == "DLSS" && enabled.contains("super_resolution");
    for (auto it = order.begin(); it != order.end();) {
        if (!allowed.contains(*it))
            it = order.erase(it);
        else
            ++it;
    }
    order.removeDuplicates();
    if(video&&!order.contains("fsr_frame_generation"))order.insert(std::max<qsizetype>(0,order.indexOf("frame_generation")+1),"fsr_frame_generation");
    if (legacyDlss) {
        const auto origin =
            scaleDlss && (!upscaleDlss || order.indexOf("scale_method") < order.indexOf("super_resolution"))
                ? QString("scale_method")
                : QString("super_resolution");
        order.insert(std::max<qsizetype>(0, order.indexOf(origin)), "dlss_super_resolution");
        if (scaleDlss) {
            enabled.removeAll("scale_method");
            enabled.append("dlss_super_resolution");
        }
        if (upscaleDlss) {
            enabled.removeAll("super_resolution");
            enabled.append("dlss_super_resolution");
        }
    }
    if (legacyHdr && enabled.contains("super_resolution") && settings.upscale_hdr_enabled) {
        enabled.append("rtx_video_hdr");
        if (!settings.upscale_vsr_enabled)
            enabled.removeAll("super_resolution");
    }
    for (const auto& stage : allowed)
        if (!order.contains(stage)) {
            if (stage == "dlss_super_resolution")
                order.insert(std::max<qsizetype>(0, order.indexOf("super_resolution")), stage);
            else if (stage == "dlss_ray_reconstruction")
                order.insert(order.indexOf("dlss_super_resolution") + 1, stage);
            else if (stage == "rtx_video_hdr")
                order.insert(order.indexOf("super_resolution") + 1, stage);
            else if (stage == "reshade") {
                const auto coloring = order.indexOf("coloring");
                if (coloring >= 0)
                    order.insert(coloring, stage);
                else
                    order.append(stage);
            } else
                order.append(stage);
        }
    enabled.removeDuplicates();
    enabled.removeIf([&allowed](const QString& stage) { return !allowed.contains(stage); });
}
void migrateRetiredReShadeDepth(Settings& settings) {
    for (auto* mode : {&settings.reshade_depth_mode, &settings.reshade_image_depth_mode})
        if (mode->startsWith("AI Depth (")) *mode = "AI Depth";
}
void migrateRetiredReShadeMotion(Settings& settings) {
    migrateRetiredReShadeDepth(settings);
    if (settings.reshade_temporal_stability == "Balanced")
        settings.reshade_temporal_stability = "Auto";
}
void applyValues(Settings& settings, const QVariantMap& changes) {
#define VE_SETTING(type, name, initial)                                                                      \
    if (changes.contains(QStringLiteral(#name)))                                                             \
        settings.name = convert<type>(changes.value(QStringLiteral(#name)));
#include "settings_fields.inc"
#undef VE_SETTING
    migrateRetiredReShadeMotion(settings);
}
std::expected<Settings, QString> migratePreset(const Settings& current, QVariantMap changes, int version,
                                               const QVariantMap& imported) {
    const Settings defaults;
    const auto defaultValues = defaults.values();
    auto reset = [&](std::initializer_list<const char*> keys) {
        for (auto key : keys)
            changes[QString::fromLatin1(key)] = defaultValues.value(QString::fromLatin1(key));
    };
    auto layout = [&](bool video, const Settings& fallback) {
        const auto orderKey = video ? QString("video_stage_order") : QString("image_stage_order"),
                   enabledKey = video ? QString("video_enabled_stages") : QString("image_enabled_stages");
        Settings temporary = fallback;
        applyValues(temporary, changes);
        const auto& order = video ? temporary.video_stage_order : temporary.image_stage_order;
        const auto& enabled = video ? temporary.video_enabled_stages : temporary.image_enabled_stages;
        const QSet<QString> seen(order.begin(), order.end()), active(enabled.begin(), enabled.end());
        if (seen.size() != order.size() || active.size() != enabled.size())
            return false;
        for (const auto& stage : order)
            if (!canonicalStages(video).contains(stage) && stage != "denoising")
                return false;
        for (const auto& stage : enabled)
            if (!seen.contains(stage))
                return false;
        if (!seen.contains("neural_model") || !seen.contains("scale_method") ||
            !seen.contains("super_resolution") || (video && !seen.contains("frame_generation")))
            return false;
        migrateStages(temporary, video);
        changes[orderKey] = video ? temporary.video_stage_order : temporary.image_stage_order;
        changes[enabledKey] = video ? temporary.video_enabled_stages : temporary.image_enabled_stages;
        return true;
    };
    if (version < 5)
        reset({"nr_passes"});
    if (version < 32)
        reset({"nr_optical_flow_quality"});
    if (version < 33)
        reset({"upscale_optical_flow_quality"});
    if (version < 34)
        reset({"frame_interpolation_optical_flow_quality"});
    if (version < 40)
        reset({"frame_interpolation_provider","fsr_frame_generation_target_fps","fsr_adapter_id"});
    if (version < 41)
        reset({"frame_interpolation_frame_rate_mode", "frame_interpolation_multiplier",
               "fsr_frame_generation_frame_rate_mode", "fsr_frame_generation_multiplier"});
    if (version < 8)
        reset({"nr_scale_method", "nr_dlss_mode", "nr_dlss_preset", "upscale_image_engine",
               "upscale_image_dlss_mode", "upscale_image_dlss_preset", "upscale_engine", "upscale_dlss_mode",
               "upscale_dlss_preset"});
    if (version < 9)
        reset({"image_stage_order", "video_stage_order", "image_enabled_stages", "video_enabled_stages"});
    if (version < 10) {
        const auto scale = changes.value("nr_scale_method", defaults.nr_scale_method).toString();
        changes["image_scaling_filter"] = "Lanczos4";
        changes["video_scaling_filter"] = scale == "Standard" ? "Lanczos" : defaults.video_scaling_filter;
        if (version == 9)
            for (bool video : {false, true}) {
                Settings temporary = defaults;
                applyValues(temporary, changes);
                const auto& order = video ? temporary.video_stage_order : temporary.image_stage_order;
                const auto& active = video ? temporary.video_enabled_stages : temporary.image_enabled_stages;
                const auto engine = video ? temporary.upscale_engine : temporary.upscale_image_engine;
                if (scale == "DLSS" && active.contains("scale_method") &&
                    (!active.contains("super_resolution") || engine != "DLSS" ||
                     order.indexOf("scale_method") < order.indexOf("super_resolution"))) {
                    changes[video ? "upscale_dlss_mode" : "upscale_image_dlss_mode"] = temporary.nr_dlss_mode;
                    changes[video ? "upscale_dlss_preset" : "upscale_image_dlss_preset"] =
                        temporary.nr_dlss_preset;
                }
                if (!layout(video, defaults))
                    return std::unexpected("Pipeline order contains unknown or duplicate processing cards.");
            }
        changes["nr_scale_method"] = "Standard";
        changes["upscale_image_engine"] = "RTX Video Super Resolution";
        changes["upscale_engine"] = "RTX Video Super Resolution";
        if (!changes.value("upscale_vsr_enabled", true).toBool() &&
            !changes.value("upscale_hdr_enabled", false).toBool())
            changes["upscale_vsr_enabled"] = true;
    }
    if (version < 11) {
        if (!layout(false, defaults))
            return std::unexpected("Invalid image pipeline layout.");
    }
    if (version < 12) {
        if (!layout(true, defaults))
            return std::unexpected("Invalid video pipeline layout.");
        changes["lut_path"] = "";
    }
    if (version < 14)
        reset({"image_bit_depth"});
    if (version < 15) {
        reset({"lut_resolution"});
        for (auto it = defaultValues.begin(); it != defaultValues.end(); ++it)
            if (it.key().startsWith("lut_") && it.key() != "lut_path" && it.key() != "lut_reference_image")
                changes[it.key()] = it.value();
    }
    if (version < 19)
        for (bool video : {false, true})
            if (!layout(video, version < 17 ? defaults : current))
                return std::unexpected("Invalid pipeline layout.");
    if (version < 17)
        reset({"cas_sharpness"});
    if (version < 18)
        reset({"sharpening_method"});
    if (version < 20)
        reset({"ffmpeg_device"});
    if (version < 21 && !layout(true, current))
        return std::unexpected("Invalid video pipeline layout.");
    if (version < 22)
        reset({"lut_reference_image"});
    if (version < 28) {
        reset(
            {"grain_amount", "grain_size", "grain_color", "grain_response", "grain_seed", "grain_animated"});
        for (bool video : {false, true}) {
            if (!layout(video, current))
                return std::unexpected("Invalid pipeline layout.");
            const auto key = video ? "video_enabled_stages" : "image_enabled_stages";
            auto active = convert<QStringList>(changes.value(key));
            active.removeAll("grain");
            changes[key] = active;
        }
    }
    for (auto it = defaultValues.begin(); it != defaultValues.end(); ++it)
        if (it.key().startsWith("upscale_") && !changes.contains(it.key()))
            changes[it.key()] = it.value();
    if (changes.value("upscale_vsr_quality").toInt() == 0)
        changes["upscale_vsr_quality"] = defaults.upscale_vsr_quality;
    Settings result = current;
    applyValues(result, changes);
    if (version < 24) {
        const auto automatic = version < 23 ? QVariant(true) : imported.value("automatic_container", true);
        if (automatic.metaType().id() != QMetaType::Bool)
            return std::unexpected("Preset setting automatic_container must be a boolean.");
        if (automatic.toBool()) {
            result.container = defaultContainer(result.codec);
            result.upscale_container = defaultContainer(result.upscale_codec);
            result.frame_interpolation_container = defaultContainer(result.frame_interpolation_codec);
        }
    }
    return result;
}
} // namespace
QVariant Settings::value(QStringView key) const {
#define VE_SETTING(type, name, initial)                                                                      \
    if (key == u## #name)                                                                                    \
        return QVariant::fromValue(name);
#include "settings_fields.inc"
#undef VE_SETTING
    return {};
}
QVariantMap Settings::values() const {
    QVariantMap result;
#define VE_SETTING(type, name, initial) result.insert(QStringLiteral(#name), QVariant::fromValue(name));
#include "settings_fields.inc"
#undef VE_SETTING
    return result;
}
std::expected<void, QString> Settings::set(QStringView key, const QVariant& input) {
    Settings candidate = *this;
    bool found = false;
#define VE_SETTING(type, name, initial)                                                                      \
    if (key == u## #name) {                                                                                  \
        candidate.name = convert<type>(input);                                                               \
        found = true;                                                                                        \
    }
#include "settings_fields.inc"
#undef VE_SETTING
    if (!found)
        return std::unexpected("Unknown setting: " + key.toString());
    migrateRetiredReShadeDepth(candidate);
    if (key.endsWith(u"codec")) {
        if (key == u"codec")
            candidate.container = containersForCodec(candidate.codec).contains(candidate.container)
                                      ? candidate.container
                                      : defaultContainer(candidate.codec);
        if (key == u"upscale_codec")
            candidate.upscale_container =
                containersForCodec(candidate.upscale_codec).contains(candidate.upscale_container)
                    ? candidate.upscale_container
                    : defaultContainer(candidate.upscale_codec);
        if (key == u"frame_interpolation_codec")
            candidate.frame_interpolation_container =
                containersForCodec(candidate.frame_interpolation_codec)
                        .contains(candidate.frame_interpolation_container)
                    ? candidate.frame_interpolation_container
                    : defaultContainer(candidate.frame_interpolation_codec);
    }
    if (!hdrSupported(candidate.codec))
        candidate.hdr_mode = false;
    if (!hdrSupported(candidate.frame_interpolation_codec))
        candidate.frame_interpolation_hdr_mode = false;
    if (key == u"image_format" && candidate.image_format != "PNG" && candidate.image_format != "TIFF")
        candidate.image_bit_depth = 8;
    if (auto valid = candidate.validate(); !valid)
        return valid;
    *this = std::move(candidate);
    return {};
}
std::expected<void, QString> Settings::validate() const {
    if(video_enabled_stages.contains("nvidia_vfx")||video_stage_order.contains("nvidia_vfx")){
        if(video_enabled_stages.contains("nvidia_vfx")&&normalizeVfxStages({"nvidia_vfx"},*this)==QStringList{"nvidia_vfx"})
            return std::unexpected("Enable at least one effect in the legacy NVIDIA VFX preset.");
        auto migrated=*this;migrateStages(migrated,true);return migrated.validate();
    }
    if(!(vfx_vsr_mode>=0&&vfx_vsr_mode<=4)&&!(vfx_vsr_mode>=8&&vfx_vsr_mode<=19))
        return std::unexpected("Unknown or reserved VFX VSR mode.");
    if(vfx_vsr_strength<0||vfx_vsr_strength>1||vfx_upscale_strength<0||vfx_upscale_strength>1)
        return std::unexpected("VFX effect strength must be between zero and one.");
    if(vfx_frame_generation_model<0||vfx_frame_generation_model>2)
        return std::unexpected("Unknown VFX frame-generation mode.");
    if(vfx_frame_multiplier<2||vfx_frame_multiplier>8)
        return std::unexpected("VFX frame multiplier must be between 2 and 8.");
    if(!QStringList{"Multiplier","Target FPS"}.contains(vfx_frame_rate_mode))
        return std::unexpected("Unknown VFX frame-rate mode.");
    bool vfxFpsOkay=false;const auto vfxFps=vfx_target_fps.toDouble(&vfxFpsOkay);
    if(!vfxFpsOkay||!std::isfinite(vfxFps)||vfxFps<=0||vfxFps>1000)
        return std::unexpected("VFX target FPS must be greater than zero and at most 1000.");
    const bool vfxScaleAllowed=std::abs(vfx_upscale_scale-4.0/3)<1e-9||vfx_upscale_scale==1.5||vfx_upscale_scale==2||vfx_upscale_scale==3||vfx_upscale_scale==4;
    if(!vfxScaleAllowed)return std::unexpected("Unknown Lightweight Upscale factor.");
    for (bool video : {false, true}) {
        const auto& order = video ? video_stage_order : image_stage_order;
        const auto& enabled = video ? video_enabled_stages : image_enabled_stages;
        const auto allowed = canonicalStages(video);
        const QSet<QString> seen(order.begin(), order.end());
        if (order.size() != allowed.size() || seen.size() != allowed.size())
            return std::unexpected("Pipeline order must contain each processing card exactly once.");
        for (const auto& stage : allowed)
            if (!seen.contains(stage))
                return std::unexpected("Pipeline contains an unavailable processing card.");
        const QSet<QString> active(enabled.begin(), enabled.end());
        if (active.size() != enabled.size())
            return std::unexpected("Pipeline enabled stages contain a duplicate card.");
        for (const auto& stage : enabled)
            if (!seen.contains(stage))
                return std::unexpected("Pipeline enabled stages contain an unavailable card.");
        if (enabled.contains("dlss_ray_reconstruction") && enabled.contains("dlss_super_resolution"))
            return std::unexpected("Select either DLSS Ray Reconstruction or DLSS Super Resolution in one workflow.");
    }
    const auto map = values();
    for (auto it = map.begin(); it != map.end(); ++it) {
        const auto& key = it.key();
        const auto& v = it.value();
        const auto number = v.toDouble();
        if (v.metaType().id() == QMetaType::Double && !std::isfinite(number))
            return std::unexpected(key + " must be finite.");
        double low = 0, high = 0;
        bool ranged = true;
        if (key.endsWith("nr_intensity"))
            high = 2;
        else if (key.endsWith("tone_strength") || key.endsWith("structure_strength"))
            high = 1;
        else if (key.endsWith("nr_passes")) {
            low = 1;
            high = 4;
        } else if (key == "cas_sharpness" || key == "grain_amount" || key == "grain_color" ||
                   key == "grain_response")
            high = 100;
        else if (key == "grain_size") {
            low = 0.5;
            high = 4;
        } else if (key == "grain_seed")
            high = 65535;
        else if (key.endsWith("image_quality") || key == "image_quality") {
            low = 1;
            high = 100;
        } else if (key.endsWith("vsr_quality")) {
            low = key.startsWith("upscale_image_") ? 1 : 0;
            high = 4;
        } else if (key.endsWith("_width") || key.endsWith("_height")) {
            low = key.startsWith("upscale_image_") ? 1 : 2;
            high = 32768;
        } else if (key.endsWith("hdr_contrast") || key.endsWith("hdr_saturation") || key == "lut_mix")
            high = 200;
        else if (key == "upscale_hdr_middle_gray") {
            low = 10;
            high = 100;
        } else if (key == "upscale_hdr_peak_luminance") {
            low = 400;
            high = 2000;
        } else if (key == "lut_exposure") {
            low = -3;
            high = 3;
        } else if (key == "lut_hue") {
            low = -180;
            high = 180;
        } else if (key.startsWith("lut_") && v.metaType().id() == QMetaType::Int && key != "lut_resolution") {
            low = -100;
            high = 100;
        } else if (key.endsWith("scale_factor")) {
            low = 1;
            high = std::numeric_limits<double>::max();
        } else
            ranged = false;
        if (ranged && (number < low || number > high))
            return std::unexpected(key + " must be between " + QString::number(low) + " and " +
                                   QString::number(high) + '.');
        if (key.endsWith("nr_style") && !QStringList{"Style 0", "Style 1", "Style 2"}.contains(v.toString()))
            return std::unexpected("Unknown Neural Rendering style.");
        if (key.endsWith("optical_flow_quality") &&
            !QStringList{"High", "Medium", "Low"}.contains(v.toString()))
            return std::unexpected("Optical Flow Quality must be High, Medium, or Low.");
        if (key.endsWith("codec") &&
            !codecChoices().contains(normalizeCodec(v.toString())))
            return std::unexpected("Unknown video codec: " + v.toString());
        if (key.endsWith("rename_mode") && !QStringList{"Auto", "Copy", "Custom"}.contains(v.toString()))
            return std::unexpected("Rename must be Auto, Copy, or Custom.");
        if (key.endsWith("dlss_mode") &&
            !QStringList{"DLAA", "Quality", "Balanced", "Performance", "Ultra Performance"}.contains(
                v.toString()))
            return std::unexpected("Unknown DLSS mode.");
        if (key.endsWith("dlss_preset") && !QStringList{"Default", "J", "K", "L", "M"}.contains(v.toString()))
            return std::unexpected("Unknown DLSS preset.");
        if (key.endsWith("preview_length") &&
            !QStringList{"1", "3", "5", "10", "20", "30"}.contains(v.toString()))
            return std::unexpected("Unknown preview length.");
        if ((key == "quality" || key.endsWith("_quality")) && !key.endsWith("image_quality") &&
            !key.endsWith("vsr_quality") && !key.endsWith("optical_flow_quality") &&
            !QStringList{"Auto (Default)", "Max", "Best", "Good"}.contains(v.toString()))
            return std::unexpected("Unknown encoding quality.");
        if (key.endsWith("size_mode") &&
            !QStringList{"Scale factor", "Custom dimensions"}.contains(v.toString()))
            return std::unexpected("Unknown output size mode.");
        if ((key == "upscale_engine" || key == "upscale_image_engine") &&
            !QStringList{"DLSS", "RTX Video Super Resolution"}.contains(v.toString()))
            return std::unexpected("Unknown upscaling engine.");
        if (key.endsWith("image_format") || key.endsWith("output_format")) {
            if (!QStringList{"PNG", "JPEG", "WebP", "AVIF", "TIFF"}.contains(v.toString()))
                return std::unexpected("Unknown image format.");
        }
        if (key == "lut_path" || key == "lut_reference_image")
            if (v.toString().size() > 4096)
                return std::unexpected("Reference path is too long.");
        if (key.endsWith("rename_mode") && v.toString() == "Custom") {
            const auto suffix = map.value(key.left(key.size() - 11) + "custom_suffix").toString();
            if (suffix.isEmpty() || suffix.contains(QRegularExpression("[<>:\"/\\\\|?*\\x00-\\x1f]")) ||
                suffix.endsWith(' ') || suffix.endsWith('.'))
                return std::unexpected("Custom suffix is empty or contains invalid filename characters.");
        }
    }
    if (!QStringList{"PNG", "JPEG", "WebP", "AVIF", "TIFF"}.contains(image_format))
        return std::unexpected("Unknown image format.");
    // Standard spatial scaling has no model-defined list of ratios. Actual
    // dimensions are checked against the input at pipeline estimation time.
    if (!std::isfinite(upscaling_factor) || upscaling_factor <= 0 || upscaling_factor > 32768)
        return std::unexpected("Standard scale factor must be positive and at most 32768.");
    if (nr_scale_method != "Standard" && nr_scale_method != "DLSS")
        return std::unexpected("Unknown Neural Rendering scale method.");
    if (upscale_mode != "Image" && upscale_mode != "Video")
        return std::unexpected("Unknown Upscale mode.");
    if (upscale_hdr_precision != "Packed 10-bit" && upscale_hdr_precision != "FP16")
        return std::unexpected("Unknown HDR precision.");
    for (const auto& mode : {frame_interpolation_frame_rate_mode, fsr_frame_generation_frame_rate_mode})
        if (mode != "Multiplier" && mode != "Target FPS") return std::unexpected("Unknown frame-rate mode.");
    for (const auto multiplier : {frame_interpolation_multiplier, fsr_frame_generation_multiplier})
        if (multiplier < 2 || multiplier > 8) return std::unexpected("Frame generation multiplier must be from 2 to 8.");
    if(!QStringList{"DLSS","FSR 3"}.contains(frame_interpolation_provider))return std::unexpected("Unknown frame interpolation provider.");
    if(fsr_adapter_id!="auto"&&!QRegularExpression("^(?:[0-9]+|dxgi:[0-9a-fA-F]{4}:[0-9a-fA-F]{4}:[0-9a-fA-F]{8}:[0-9a-fA-F]{2}:[0-9]+)$").match(fsr_adapter_id).hasMatch())
        return std::unexpected("Invalid FSR DirectX adapter selection.");
    if (!QStringList{"Lanczos4", "Area", "Bicubic", "Bilinear", "Nearest"}.contains(image_scaling_filter) ||
        !QStringList{"Spline36", "Lanczos", "Bicubic", "Area", "Bilinear"}.contains(video_scaling_filter))
        return std::unexpected("Unknown scaling filter.");
    for (const auto& pair : {std::pair{codec, container}, std::pair{upscale_codec, upscale_container},
                             std::pair{frame_interpolation_codec, frame_interpolation_container}})
        if (!containersForCodec(pair.first).contains(pair.second))
            return std::unexpected("The selected codec does not support this container.");
    if ((hdr_mode && !hdrSupported(codec)) ||
        (frame_interpolation_hdr_mode && !hdrSupported(frame_interpolation_codec)))
        return std::unexpected("HDR requires a codec supporting 10-bit video.");
    if (!QStringList{"AMD CAS", "NVIDIA NIS"}.contains(sharpening_method))
        return std::unexpected("Unknown sharpening method.");
    if (lut_resolution != 33 && lut_resolution != 65 && lut_resolution != 129)
        return std::unexpected("LUT resolution must be 33, 65, or 129.");
    if (image_bit_depth != 8 && image_bit_depth != 16)
        return std::unexpected("Image bit depth must be 8 or 16.");
    if (image_bit_depth == 16 && image_format != "PNG" && image_format != "TIFF")
        return std::unexpected("16-bit image output is available for PNG and TIFF only.");
    if (!QStringList{"23.976", "25", "29.97", "30", "50", "59.94", "60", "90", "119.88", "120", "144", "165",
                     "180", "240", "360", "480"}
             .contains(frame_interpolation_target_fps))
        return std::unexpected("Unsupported output FPS.");
    if(!QStringList{"23.976","25","29.97","30","50","59.94","60","90","119.88","120","144","165","180","240","360","480"}.contains(fsr_frame_generation_target_fps))
        return std::unexpected("Unsupported FSR output FPS.");
    if (ffmpeg_device != "auto" && ffmpeg_device != "cpu" &&
        (!ffmpeg_device.startsWith("vulkan:") || QUuid(ffmpeg_device.mid(7)).isNull()))
        return std::unexpected("Invalid FFmpeg/Vulkan device selection.");
    for (const auto& uuid : {ai_gpu_uuid, video_gpu_uuid})
        if (uuid != "auto" && (!uuid.startsWith("GPU-") || QUuid(uuid.mid(4)).isNull()))
            return std::unexpected("Invalid NVIDIA GPU selection.");
    const auto validateReShadePaths = [](const QString& value, const char* label) -> std::expected<void, QString> {
        if (value.size() > 4096)
            return std::unexpected(QString::fromLatin1(label) + " is too long.");
        const QRegularExpression binaryPattern(R"(\.(dll|addon|addon32|addon64)\s*([;,]|$))",
                                               QRegularExpression::CaseInsensitiveOption);
        if (binaryPattern.match(value).hasMatch())
            return std::unexpected("ReShade DLLs and binary add-ons cannot be loaded as presets or shader packages.");
        return {};
    };
    for (const auto& [path, label] : {
             std::pair{reshade_preset_path, "ReShade preset path"},
             std::pair{reshade_shader_search_paths, "ReShade shader search paths"},
             std::pair{reshade_texture_search_paths, "ReShade texture search paths"},
             std::pair{reshade_image_preset_path, "ReShade image preset path"},
             std::pair{reshade_image_shader_search_paths, "ReShade image shader search paths"},
             std::pair{reshade_image_texture_search_paths, "ReShade image texture search paths"}}) {
        if (auto ok = validateReShadePaths(path, label); !ok)
            return ok;
    }
    for (const auto& overrides : {reshade_parameter_overrides, reshade_image_parameter_overrides}) {
        if (overrides.size() > 65536)
            return std::unexpected("ReShade parameter overrides are too large.");
        const auto trimmed = overrides.trimmed();
        if (!trimmed.isEmpty()) {
            if (trimmed.startsWith('{')) {
                QJsonParseError err;
                const auto doc = QJsonDocument::fromJson(trimmed.toUtf8(), &err);
                if (err.error != QJsonParseError::NoError || !doc.isObject())
                    return std::unexpected("ReShade parameter overrides must be a valid JSON object.");
            } else {
                for (const auto& rawLine : trimmed.split('\n', Qt::SkipEmptyParts)) {
                    const auto line = rawLine.trimmed();
                    if (!line.isEmpty() && !line.startsWith('#') && !line.contains('='))
                        return std::unexpected("ReShade parameter override entries must have the form KEY=VALUE.");
                }
            }
        }
    }
    for (int mix : {reshade_mix, reshade_image_mix})
        if (mix < 0 || mix > 100)
            return std::unexpected("ReShade mix must be between 0 and 100.");
    if (reshade_temporal_strength < 0 || reshade_temporal_strength > 100)
        return std::unexpected("ReShade temporal strength must be between 0 and 100.");
    for (const auto& depthMode : {reshade_depth_mode, reshade_image_depth_mode})
        if (!QStringList{"Auto", "Off", "AI Depth"}.contains(depthMode))
            return std::unexpected("Unknown ReShade depth mode.");
    for (int depthRes : {reshade_depth_resolution, reshade_image_depth_resolution})
        if (depthRes < 196 || depthRes > 1008 || (depthRes % 14) != 0)
            return std::unexpected("ReShade depth resolution must be a multiple of 14 between 196 and 1008.");
    if (!QStringList{"Auto", "Quality", "Spatial Only (Off)"}.contains(reshade_temporal_stability))
        return std::unexpected("Unknown ReShade temporal stability policy.");
    for (const auto& precision : {reshade_precision_policy, reshade_image_precision_policy})
        if (!QStringList{"Auto", "FP16", "8-bit sRGB Compatibility"}.contains(precision))
            return std::unexpected("Unknown ReShade precision policy.");
    for (const auto& hdrPolicy : {reshade_hdr_policy, reshade_image_hdr_policy})
        if (!QStringList{"Auto", "HDR Aware", "SDR Adapter", "Reject Unsupported"}.contains(hdrPolicy))
            return std::unexpected("Unknown ReShade HDR policy.");
    for (int seed : {reshade_deterministic_seed, reshade_image_deterministic_seed})
        if (seed < 0 || seed > 65535)
            return std::unexpected("ReShade deterministic seed must be between 0 and 65535.");
    return {};
}
QStringList Settings::stages(bool video) const {
    QStringList result;
    const auto& order = video ? video_stage_order : image_stage_order;
    const auto& active = video ? video_enabled_stages : image_enabled_stages;
    for (const auto& stage : order)
        if (active.contains(stage)||(video&&active.contains("nvidia_vfx")&&legacyVfxEnabled(*this,stage)))
            result.append(stage);
    if(video&&active.contains("nvidia_vfx")&&!hasVfxStages(result))result.append("nvidia_vfx");
    return video?normalizeVfxStages(result,*this):result;
}
Settings Settings::load(const QString& path) {
    Settings result;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text))
        return result;
    bool section = false;
    QTextStream stream(&file);
    QVariantMap loaded;
    const auto defaults = result.values();
    while (!stream.atEnd()) {
        const auto line = stream.readLine().trimmed();
        if (line.startsWith('[')) {
            section = line == "[Settings]";
            continue;
        }
        if (!section || line.startsWith('#') || line.startsWith(';'))
            continue;
        const auto separator = line.indexOf('=');
        if (separator <= 0)
            continue;
        const auto key = line.left(separator).trimmed();
        auto value = line.mid(separator + 1).trimmed();
        if (!defaults.contains(key) || key == "nr_mask")
            continue;
        const auto original = defaults.value(key);
        QVariant parsed = value;
        if (original.metaType().id() == QMetaType::Bool)
            parsed = QStringList{"true", "yes", "on", "1"}.contains(value.toLower());
        else if (original.metaType().id() == QMetaType::Int) {
            bool ok = false;
            auto v = value.toInt(&ok);
            if (!ok)
                continue;
            parsed = v;
        } else if (original.metaType().id() == QMetaType::Double) {
            bool ok = false;
            auto v = value.toDouble(&ok);
            if (!ok || !std::isfinite(v))
                continue;
            parsed = v;
        } else if (original.metaType().id() == QMetaType::QStringList) {
            const auto json = QJsonDocument::fromJson(value.toUtf8());
            if (!json.isArray())
                continue;
            parsed = json.array().toVariantList();
        } else if (original.metaType().id() == QMetaType::QVariantMap) {
            parsed = QJsonDocument::fromJson(value.toUtf8()).object().toVariantMap();
        } else if (key.endsWith("codec"))
            parsed = normalizeCodec(value);
        else if (key.endsWith("nr_style"))
            parsed = QMap<QString, QString>{{"Default", "Style 0"},
                                            {"Natural", "Style 1"},
                                            {"Cinematic", "Style 2"}}
                         .value(value, value);
        loaded.insert(key, parsed);
    }
#define VE_SETTING(type, name, initial)                                                                      \
    if (loaded.contains(QStringLiteral(#name)))                                                              \
        result.name = convert<type>(loaded.value(QStringLiteral(#name)));
#include "settings_fields.inc"
#undef VE_SETTING
    migrateRetiredReShadeMotion(result);
    migrateStages(result, false);
    migrateStages(result, true);
    // Repair individual invalid saved values without discarding the rest of the user's settings.
    if (!result.validate()) {
        Settings repaired;
        repaired.image_stage_order = result.image_stage_order;
        repaired.video_stage_order = result.video_stage_order;
        repaired.image_enabled_stages = result.image_enabled_stages;
        repaired.video_enabled_stages = result.video_enabled_stages;
        for (auto it = loaded.begin(); it != loaded.end(); ++it) {
            auto applied = repaired.set(it.key(), it.value());
            (void)applied;
        }
        result = std::move(repaired);
    }
    return result;
}
std::expected<void, QString> Settings::save(const QString& path) const {
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text))
        return std::unexpected(file.errorString());
    QByteArray bytes("[Settings]\n");
    const auto map = values();
    for (auto it = map.begin(); it != map.end(); ++it) {
        if (it.key() == "nr_mask")
            continue;
        QString text;
        const auto& value = it.value();
        if (value.metaType().id() == QMetaType::QStringList)
            text = QString::fromUtf8(QJsonDocument(QJsonArray::fromStringList(value.toStringList()))
                                         .toJson(QJsonDocument::Compact));
        else if (value.metaType().id() == QMetaType::QVariantMap)
            text = QString::fromUtf8(
                QJsonDocument(QJsonObject::fromVariantMap(value.toMap())).toJson(QJsonDocument::Compact));
        else if (value.metaType().id() == QMetaType::Bool)
            text = value.toBool() ? "true" : "false";
        else
            text = value.toString();
        bytes += (it.key() + " = " + text + '\n').toUtf8();
    }
    if (file.write(bytes) != bytes.size() || !file.commit())
        return std::unexpected(file.errorString());
    return {};
}
QJsonObject Settings::preset(const QString& name) const {
    auto migrated=*this;migrateStages(migrated,false);migrateStages(migrated,true);auto map = migrated.values();
    map.remove("nr_mask");
    return {{"format", "dlss5-visual-enhancer-settings-preset"},
            {"schema_version", 41},
            {"name", name.trimmed()},
            {"settings", QJsonObject::fromVariantMap(map)}};
}
std::expected<void, QString> Settings::exportPreset(const QString& path, const QString& name) const {
    if (name.trimmed().isEmpty() || name.size() > 120)
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Preset name must contain 1 to 120 characters."));
    if (auto valid = validate(); !valid)
        return valid;
    QSaveFile file(localPath(path));
    if (!file.open(QIODevice::WriteOnly))
        return std::unexpected(file.errorString());
    const auto json = QJsonDocument(preset(name)).toJson(QJsonDocument::Indented);
    if (file.write(json) != json.size() || !file.commit())
        return std::unexpected(file.errorString());
    return {};
}
std::expected<QString, QString> Settings::importPreset(const QString& path) {
    QFile file(localPath(path));
    if (!file.open(QIODevice::ReadOnly))
        return std::unexpected(file.errorString());
    if (file.size() > 1024 * 1024)
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Preset file is too large; the maximum size is 1 MiB."));
    QJsonParseError error;
    const auto json = QJsonDocument::fromJson(file.readAll(), &error);
    if (!json.isObject())
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Preset file is not valid UTF-8 JSON."));
    const auto object = json.object();
    if (object.value("format").toString() != "dlss5-visual-enhancer-settings-preset")
        return std::unexpected(QT_TRANSLATE_NOOP("App", "This JSON file is not a Visual Enhancer settings preset."));
    if (!object.value("schema_version").isDouble() ||
        std::floor(object.value("schema_version").toDouble()) != object.value("schema_version").toDouble())
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Preset schema_version must be an integer."));
    const int version = object.value("schema_version").toInt();
    if (version < 1 || version > 41)
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Unsupported preset schema version."));
    const auto name = object.value("name").toString().trimmed();
    if (!object.value("settings").isObject() || name.isEmpty() || name.size() > 120 ||
        name.contains(QRegularExpression("[\\x00-\\x1f]")))
        return std::unexpected(QT_TRANSLATE_NOOP("App", "Preset must contain a valid name and settings object."));
    auto changes = object.value("settings").toObject().toVariantMap();
    const auto originals = values();
    changes.remove("nr_mask");
    for (auto it = changes.begin(); it != changes.end(); ++it) {
        if (!originals.contains(it.key()))
            continue;
        const auto type = originals.value(it.key()).metaType().id();
        const auto actual = it.value().metaType().id();
        const bool numeric =
            actual == QMetaType::Double || actual == QMetaType::Int || actual == QMetaType::LongLong;
        if ((type == QMetaType::Bool && actual != QMetaType::Bool) ||
            (type == QMetaType::QString && actual != QMetaType::QString) ||
            (type == QMetaType::Int &&
             (!numeric || std::floor(it.value().toDouble()) != it.value().toDouble() ||
              it.value().toDouble() < std::numeric_limits<int>::min() ||
              it.value().toDouble() > std::numeric_limits<int>::max())) ||
            (type == QMetaType::Double && (!numeric || !std::isfinite(it.value().toDouble()))) ||
            (type == QMetaType::QStringList && actual != QMetaType::QVariantList))
            return std::unexpected("Preset setting has an invalid type: " + it.key());
        if (type == QMetaType::QStringList)
            for (const auto& stage : it.value().toList())
                if (stage.metaType().id() != QMetaType::QString)
                    return std::unexpected(QT_TRANSLATE_NOOP("App", "Pipeline stages must be text."));
        if (it.key().endsWith("codec"))
            it.value() = normalizeCodec(it.value().toString());
        if (version < 31 && it.key().endsWith("nr_style"))
            it.value() = QMap<QString, QString>{{"Default", "Style 0"},
                                                {"Natural", "Style 1"},
                                                {"Cinematic", "Style 2"}}
                             .value(it.value().toString(), it.value().toString());
        if (version < 31 && (it.key().endsWith("tone_strength") || it.key().endsWith("structure_strength")))
            it.value() = it.key().endsWith("skin_structure_strength") && it.value().toDouble() == -1
                             ? 1.
                             : std::clamp(it.value().toDouble(), 0., 1.);
    }
    auto migrated =
        migratePreset(*this, changes, version, object.value("settings").toObject().toVariantMap());
    if (!migrated)
        return std::unexpected(migrated.error());
    auto candidate = std::move(*migrated);
    if (version <= 35) {
        const auto retired=[](const QString& stage) {
            return stage=="vfx_hdr" || stage=="vfx_green_screen" || stage=="vfx_relighting";
        };
        candidate.video_stage_order.removeIf(retired);
        candidate.video_enabled_stages.removeIf(retired);
    }
    if (version >= 28 && version < 34) {
        for (bool video : {false, true}) {
            const auto& order = video ? candidate.video_stage_order : candidate.image_stage_order;
            const auto& enabled = video ? candidate.video_enabled_stages : candidate.image_enabled_stages;
            const QSet<QString> seen(order.begin(), order.end()), active(enabled.begin(), enabled.end());
            if (seen.size() != order.size() || active.size() != enabled.size())
                return std::unexpected("Pipeline contains duplicate processing cards.");
            for (const auto& stage : order)
                if (!canonicalStages(video).contains(stage))
                    return std::unexpected("Pipeline contains an unavailable processing card.");
            for (const auto& stage : enabled)
                if (!seen.contains(stage))
                    return std::unexpected("Pipeline enables an unavailable processing card.");
            migrateStages(candidate, video);
        }
    }
    if(version<=35){
        const QSet<QString> order(candidate.video_stage_order.begin(),candidate.video_stage_order.end()),active(candidate.video_enabled_stages.begin(),candidate.video_enabled_stages.end());
        if(order.size()!=candidate.video_stage_order.size()||active.size()!=candidate.video_enabled_stages.size())return std::unexpected("Pipeline contains duplicate processing cards.");
        for(const auto& stage:candidate.video_enabled_stages)if(!order.contains(stage)&&stage!="nvidia_vfx")return std::unexpected("Pipeline enables an unavailable processing card.");
        if(candidate.video_enabled_stages.contains("nvidia_vfx")&&normalizeVfxStages({"nvidia_vfx"},candidate)==QStringList{"nvidia_vfx"})
            return std::unexpected("Enable at least one effect in the legacy NVIDIA VFX preset.");
        for(const auto& stage:candidate.video_stage_order)if(!canonicalStages(true).contains(stage)&&stage!="nvidia_vfx")return std::unexpected("Pipeline contains an unavailable processing card.");
        migrateStages(candidate,true);
    }
    if (version < 37) {
        // Old presets never enabled RR. Preserve all previous relative ordering.
        const Settings defaults;
        candidate.rr_optical_flow_quality=defaults.rr_optical_flow_quality;
        for(bool video:{false,true}) {
            auto& order=video?candidate.video_stage_order:candidate.image_stage_order;
            auto& enabled=video?candidate.video_enabled_stages:candidate.image_enabled_stages;
            const QSet<QString> seen(order.begin(),order.end()), active(enabled.begin(),enabled.end());
            if(seen.size()!=order.size()||active.size()!=enabled.size())
                return std::unexpected("Pipeline contains duplicate processing cards.");
            for(const auto& stage:order)if(!canonicalStages(video).contains(stage))
                return std::unexpected("Pipeline contains an unavailable processing card.");
            for(const auto& stage:enabled)if(!order.contains(stage))
                return std::unexpected("Pipeline enables an unavailable processing card.");
            enabled.removeAll("dlss_ray_reconstruction");
            migrateStages(candidate,video);
        }
    }
    if (version < 39) {
        // Pre-39 presets never enabled ReShade. Preserve all previous relative ordering.
        for (bool video : {false, true}) {
            auto& order = video ? candidate.video_stage_order : candidate.image_stage_order;
            auto& enabled = video ? candidate.video_enabled_stages : candidate.image_enabled_stages;
            const QSet<QString> seen(order.begin(), order.end()), active(enabled.begin(), enabled.end());
            if (seen.size() != order.size() || active.size() != enabled.size())
                return std::unexpected("Pipeline contains duplicate processing cards.");
            for (const auto& stage : order)
                if (!canonicalStages(video).contains(stage))
                    return std::unexpected("Pipeline contains an unavailable processing card.");
            for (const auto& stage : enabled)
                if (!order.contains(stage))
                    return std::unexpected("Pipeline enables an unavailable processing card.");
            enabled.removeAll("reshade");
            migrateStages(candidate, video);
        }
    }
    if(version<40){
        const auto& order=candidate.video_stage_order;const auto& enabled=candidate.video_enabled_stages;
        const QSet<QString> seen(order.begin(),order.end()),active(enabled.begin(),enabled.end());
        if(seen.size()!=order.size()||active.size()!=enabled.size())return std::unexpected("Pipeline contains duplicate processing cards.");
        for(const auto& stage:order)if(!canonicalStages(true).contains(stage))return std::unexpected("Pipeline contains an unavailable processing card.");
        for(const auto& stage:enabled)if(!order.contains(stage))return std::unexpected("Pipeline enables an unavailable processing card.");
        candidate.video_enabled_stages.removeAll("fsr_frame_generation");migrateStages(candidate,true);
    }
    if (auto valid = candidate.validate(); !valid)
        return std::unexpected(valid.error());
    *this = std::move(candidate);
    return name;
}
QString normalizeCodec(QString codec) {
    if (codec == "HEVC" || codec == "H265" || codec == "H.265 (HEVC)")
        return "H.265";
    if (codec == "HEVC (NVIDIA NVENC)")
        return "H.265 (NVIDIA NVENC)";
    return codec;
}
QStringList codecChoices() {
    return {"H.264",
            "H.264 (NVIDIA NVENC)",
            "H.265",
            "H.265 (NVIDIA NVENC)",
            "AV1",
            "AV1 (NVIDIA NVENC)",
            "ProRes Proxy",
            "ProRes HQ",
            "FFV1 Lossless RGB 10-bit"};
}
QStringList containersForCodec(const QString& codec) {
    if (codec.startsWith("ProRes"))
        return {"MOV", "MKV"};
    if (codec.startsWith("AV1"))
        return {"MP4", "MKV"};
    return {"MP4", "MKV", "MOV"};
}
QString defaultContainer(const QString& codec) {
    if (codec.startsWith("ProRes"))
        return "MOV";
    if (codec.startsWith("H.264"))
        return "MP4";
    return "MKV";
}
bool hdrSupported(const QString& codec) {
    return !codec.startsWith("H.264") && codecChoices().contains(codec);
}
} // namespace ve
