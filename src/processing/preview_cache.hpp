#pragma once
#include "fsr_native.hpp"
#include "configuration/settings.hpp"
#include "infrastructure/job.hpp"
#include "media/media.hpp"
#include "reshade_dependencies.hpp"
#include <QCache>
#include <QColorSpace>
#include <QCryptographicHash>
#include <QDateTime>
#include <QFile>
#include <QFileInfo>
#include <QDirIterator>
#include <QJsonDocument>
#include <QMutex>
#include <QMutexLocker>
#include <optional>

namespace ve {
// Only reset still evaluations use this cache. Temporal clip jobs own their
// histories independently and never substitute a still for a video frame.
class PreviewFrameCache {
  public:
    struct Entry { QImage pixels; bool hdr=false, rgb10=false; double mediaTimestamp=0; };
    explicit PreviewFrameCache(int kibibytes=128*1024) : entries_(kibibytes) {}
    [[nodiscard]] std::optional<Entry> find(const QByteArray& key) {
        QMutexLocker lock(&mutex_);
        if (const auto* entry=entries_.object(key)) { ++hits_; return *entry; }
        return {};
    }
    void store(const QByteArray& key, Entry entry) {
        const auto bytes=entry.pixels.sizeInBytes()+key.size()+qint64(sizeof(Entry))+128;
        QMutexLocker lock(&mutex_);
        if (bytes>qint64(entries_.maxCost())*1024) return;
        entries_.insert(key,new Entry(std::move(entry)),int((bytes+1023)/1024));
    }
    void clear() { QMutexLocker lock(&mutex_); entries_.clear(); }
    [[nodiscard]] qint64 allocatedBytes() const { QMutexLocker lock(&mutex_); return qint64(entries_.totalCost())*1024; }
    [[nodiscard]] quint64 hits() const { QMutexLocker lock(&mutex_); return hits_; }
  private:
    mutable QMutex mutex_;
    QCache<QByteArray,Entry> entries_;
    quint64 hits_=0;
};
inline QByteArray previewFileIdentity(const QString& path) {
    const QFileInfo file(path);
    return QJsonDocument(QJsonObject{{"path",file.absoluteFilePath()}, {"bytes",file.size()},
        {"modified",file.lastModified().toMSecsSinceEpoch()}, {"created",file.birthTime().toMSecsSinceEpoch()}}).toJson(QJsonDocument::Compact);
}
// The files remain in the existing preview store, whose cleanup accounts for
// their disk bytes and protects active viewers. This cache owns no decoder or
// native session; a hit must still validate the immutable file identity.
class PreviewVideoCache {
  public:
    struct Entry { QString path; MediaInfo info; QByteArray identity; };
    explicit PreviewVideoCache(int kibibytes=256*1024) : entries_(kibibytes) {}
    [[nodiscard]] std::optional<Entry> find(const QByteArray& key) {
        QMutexLocker lock(&mutex_);
        const auto* entry=entries_.object(key);
        if (!entry) return {};
        if (!QFileInfo::exists(entry->path) || previewFileIdentity(entry->path)!=entry->identity) {
            entries_.remove(key);
            return {};
        }
        return *entry;
    }
    void store(const QByteArray& key,const QString& path,const MediaInfo& info) {
        // This budget is for lookup metadata. Disk files have their own shared
        // budget; charging them here evicted large clips without removing files.
        const qint64 bytes=QJsonDocument(info.metadata).toJson(QJsonDocument::Compact).size()*4+
            qint64(path.capacity()+1)*qint64(sizeof(QChar))+key.capacity()+qint64(sizeof(Entry))+1024;
        QMutexLocker lock(&mutex_);
        if (bytes>qint64(entries_.maxCost())*1024) return;
        entries_.insert(key,new Entry{path,info,previewFileIdentity(path)},int((bytes+1023)/1024));
    }
    [[nodiscard]] qint64 chargedBytes() const {
        QMutexLocker lock(&mutex_); return qint64(entries_.totalCost())*1024;
    }
    void removePaths(const QStringList& paths) {
        QMutexLocker lock(&mutex_);
        for (const auto& key : entries_.keys())
            if (paths.contains(entries_.object(key)->path)) entries_.remove(key);
    }
  private:
    mutable QMutex mutex_;
    QCache<QByteArray,Entry> entries_;
};
inline QByteArray previewPixelIdentity(const QImage& image, JobControl& control) {
    QCryptographicHash hash(QCryptographicHash::Sha256);
    for (int row=0; row<image.height(); ++row) {
        control.checkpoint();
        hash.addData(QByteArrayView(reinterpret_cast<const char*>(image.constScanLine(row)),image.width()*image.depth()/8));
    }
    hash.addData(QByteArray::number(int(image.format())));
    hash.addData(QByteArray::number(image.width())+'x'+QByteArray::number(image.height()));
    hash.addData(image.colorSpace().iccProfile());
    return hash.result();
}
struct StillPreviewSource { QImage pixels; QByteArray identity; };
inline StillPreviewSource decodeStillPreviewSource(const QString& path, PreviewFrameCache& cache, JobControl& control) {
    control.checkpoint();
    const QFileInfo file(path);
    const auto suffix=file.suffix().toLower();
    // Snapshot common raster files so pixels and identity come from the same
    // bytes. Content changes invalidate reuse even when file dates are preserved.
    if (file.size()<=32ll*1024*1024 &&
        QStringList{"jpg","jpeg","png","webp","bmp","tif","tiff"}.contains(suffix)) {
        QFile input(path);
        if (!input.open(QIODevice::ReadOnly)) throw std::runtime_error(input.errorString().toStdString());
        const auto bytes=input.readAll();
        if (input.error()!=QFileDevice::NoError) throw std::runtime_error(input.errorString().toStdString());
        control.checkpoint();
        auto identity=QByteArray("decoded-still/v1")+previewFileIdentity(path)+
            QCryptographicHash::hash(bytes,QCryptographicHash::Sha256);
        const auto key=QCryptographicHash::hash(identity,QCryptographicHash::Sha256);
        if (auto found=cache.find(key)) return {std::move(found->pixels),std::move(identity)};
        auto pixels=decodeImageData(bytes,control);
        cache.store(key,{pixels,false,false});
        return {std::move(pixels),std::move(identity)};
    }
    auto pixels=decodeImage(path,control);
    auto identity=previewFileIdentity(path)+previewPixelIdentity(pixels,control);
    return {std::move(pixels),std::move(identity)};
}
inline QList<QByteArray> previewStageKeys(QByteArray identity, const Settings& settings,
    const MediaInfo& info, const QStringList& stages, bool finalOutput, const QString& root) {
    const QMap<QString,QStringList> fields{
        {"neural_model",{"nr_style","nr_intensity","nr_passes","nr_optical_flow_quality","local_tone_strength",
            "local_structure_strength","skin_structure_strength","automatic_mask","nr_mask"}},
        {"scale_method",{"upscaling_factor","image_scaling_filter","video_scaling_filter"}},
        {"dlss_super_resolution",{"upscale_dlss_mode","upscale_dlss_preset","upscale_image_dlss_mode","upscale_image_dlss_preset","upscale_optical_flow_quality"}},
        {"dlss_ray_reconstruction",{"rr_optical_flow_quality"}},
        {"super_resolution",{"upscale_vsr_quality","upscale_size_mode","upscale_scale_factor","upscale_width","upscale_height","upscale_aspect_lock",
            "upscale_image_vsr_quality","upscale_image_size_mode","upscale_image_scale_factor","upscale_image_width","upscale_image_height","upscale_image_aspect_lock"}},
        {"rtx_video_hdr",{"upscale_hdr_contrast","upscale_hdr_saturation","upscale_hdr_middle_gray","upscale_hdr_peak_luminance","upscale_hdr_precision"}},
        {"cas_sharpening",{"sharpening_method","cas_sharpness"}},
        {"grain",{"grain_amount","grain_size","grain_color","grain_response","grain_seed","grain_animated"}},
        {"frame_generation",{"frame_interpolation_frame_rate_mode","frame_interpolation_multiplier","frame_interpolation_target_fps",
            "frame_interpolation_optical_flow_quality"}},
        {"fsr_frame_generation",{"fsr_frame_generation_frame_rate_mode","fsr_frame_generation_multiplier","fsr_frame_generation_target_fps","fsr_adapter_id"}}
    };
    identity+=QJsonDocument(QJsonObject{{"video",info.video},{"hdr",info.hdr},{"width",info.width},{"height",info.height},
        {"format",info.pixelFormat},{"matrix",info.colorSpace},{"range",info.range},{"primaries",info.primaries},{"transfer",info.transfer},
        {"cache_bits",info.sourceBitDepth},{"ai_gpu",settings.ai_gpu_uuid},{"video_gpu",settings.video_gpu_uuid},{"filter_device",settings.ffmpeg_device}}).toJson(QJsonDocument::Compact);
    identity+=previewFileIdentity(root+"/bin/ffmpeg/bin/ffmpeg.exe");
    QList<QByteArray> keys;
    for (qsizetype index=0; index<stages.size(); ++index) {
        const auto& stage=stages[index];
        if(stage=="frame_generation")identity+="/temporal-grid-v2/monotonic-resets-v2/";
        QVariantMap relevant;
        if(stage=="dlss_ray_reconstruction")identity+="/rr-native-1x-abi2/rr-guides-v2/fixed-still-video/scene-v2/full-fingerprint/streamed-parked-32-v3/single-still/";
        if(stage=="fsr_frame_generation"){
            identity+="/fsr3-3.1.6/amd-of-video-guides-v1/precision16/scene-v1/cadence-adaptive-1over16-v2/history8-global-clock-v2/monotonic-resets-v2/gpu-encoding-required-v1/";
            identity+=fsrAdapterCacheIdentity();
            const auto engineOverride=qEnvironmentVariable("VE_FSR_RUNTIME_DIRECTORY"),mediaOverride=qEnvironmentVariable("VE_MEDIA_RUNTIME_DIRECTORY");
            const auto fsrRoot=engineOverride.isEmpty()?root+"/bin/runtime/fsrfg/":engineOverride+'/';
            for(const auto* file:{"neuroframe_engine_fsr_frame_generation.dll","amd_fidelityfx_framegeneration_dx12.dll","runtime-manifest.json"})identity+=previewFileIdentity(fsrRoot+file);
            QDir av(mediaOverride.isEmpty()?root+"/bin/runtime/av":mediaOverride);
            for(const auto& file:av.entryList({"avcodec-*.dll","avutil-*.dll","avformat-*.dll","swscale-*.dll"},QDir::Files,QDir::Name))identity+=previewFileIdentity(av.filePath(file));
        }
        for (const auto& field : fields.value(stage)) relevant.insert(field,settings.value(field));
        if(isVfxStage(stage)||(hasVfxStages(settings.stages(true))&&QStringList{"super_resolution","rtx_video_hdr"}.contains(stage))){
            identity+="/vfx-source-colors-v2/";
            identity+=info.metadata.value("sample_aspect_ratio").toString().toUtf8();
            identity+=QByteArray::number(info.clockwiseRotation());
            const auto values=settings.values();for(auto it=values.begin();it!=values.end();++it)if(it.key().startsWith("vfx_"))relevant.insert(it.key(),it.value());
            const auto vfxRoot=root+"/bin/runtime/vfx/";
            identity+=previewFileIdentity(vfxRoot+"neuroframe_engine_video_effects.dll");
            identity+=previewFileIdentity(vfxRoot+"runtime-manifest.json");
            QDirIterator components(vfxRoot,{"*.dll","*.trtpkg"},QDir::Files,QDirIterator::Subdirectories);
            QStringList componentPaths;while(components.hasNext())componentPaths.append(components.next());componentPaths.sort();
            for(const auto& component:componentPaths)identity+=previewFileIdentity(component);
        }
        if (stage=="coloring") {
            const auto values=settings.values();
            for (auto it=values.begin(); it!=values.end(); ++it)
                if (it.key().startsWith("lut_")) relevant.insert(it.key(),it.value());
        }
        if (stage=="reshade") {
            identity += reshadeDependencyIdentity(settings, info.video, root);
            const auto rs=reshadeSettingsForMode(settings,info.video);
            const auto values=rs.values();
            for (auto it=values.begin(); it!=values.end(); ++it)
                if (it.key().startsWith("reshade_")) relevant.insert(it.key(),it.value());
        }
        identity+=stage.toUtf8()+QJsonDocument(QJsonObject::fromVariantMap(relevant)).toJson(QJsonDocument::Compact);
        if (stage=="coloring") {
            for (const auto& path : {settings.lut_path,settings.lut_reference_image})
                if (!path.isEmpty()) identity+=previewFileIdentity(path);
        }
        if (stage=="neural_model") {
            const auto mask=settings.nr_mask.value("path").toString();
            if (!mask.isEmpty()) identity+=previewFileIdentity(mask);
            const auto pixels=qvariant_cast<QImage>(settings.nr_mask.value("pixels"));
            identity+=QByteArray::number(pixels.cacheKey());
        }
        const QString module=stage=="neural_model" ? "dlssnr" : stage=="dlss_super_resolution" ? "dlsssr"
            : stage=="dlss_ray_reconstruction" ? "dlssrr"
            : stage=="super_resolution" || stage=="rtx_video_hdr" ? "rtx_video"
            : stage=="frame_generation" ? "dlssg" : QString{};
        const QString binary=stage=="neural_model" ? "neuroframe_engine_neural_rendering.dll"
            : stage=="dlss_super_resolution" ? "neuroframe_engine_super_resolution.dll"
            : stage=="dlss_ray_reconstruction" ? "neuroframe_engine_ray_reconstruction.dll"
            : stage=="frame_generation" ? "neuroframe_engine_frame_interpolation.dll" : "neuroframe_engine_upscaling.dll";
        if (!module.isEmpty()) {
            identity+=previewFileIdentity(root+"/bin/runtime/"+module+'/'+binary);
            const QString model=stage=="neural_model" ? "nvngx_dlssnr.dll"
                : stage=="dlss_super_resolution" ? "nvngx_dlss.dll"
                : stage=="dlss_ray_reconstruction" ? "nvngx_dlssd.dll"
                : stage=="frame_generation" ? "nvngx_dlssg.dll"
                : stage=="super_resolution" ? "nvngx_vsr.dll"
                : stage=="rtx_video_hdr" ? "nvngx_truehdr.dll" : QString{};
            if (!model.isEmpty()) identity+=previewFileIdentity(root+"/bin/runtime/"+module+'/'+model);
        }
        // A final SR output retains more precision than an intermediate card.
        const auto boundary=(finalOutput && index+1==stages.size()) ? QByteArray("final") : QByteArray("intermediate");
        keys.push_back(QCryptographicHash::hash(identity+boundary,QCryptographicHash::Sha256));
    }
    return keys;
}
} // namespace ve
