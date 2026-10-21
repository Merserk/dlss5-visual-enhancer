#pragma once
#include "configuration/settings.hpp"
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSet>

namespace ve {
inline QJsonObject reshadeOverrideValues(const QString& raw) {
    if (raw.trimmed().startsWith('{')) return QJsonDocument::fromJson(raw.toUtf8()).object();
    QJsonObject values;
    for (const auto& line : raw.split('\n', Qt::SkipEmptyParts)) {
        const auto eq = line.indexOf('=');
        if (eq > 0) values.insert(line.left(eq).trimmed(), line.mid(eq + 1).trimmed());
    }
    return values;
}
inline QString reshadeGraphOverrides(const QString& raw) {
    const auto values = reshadeOverrideValues(raw);
    QJsonObject graph;
    for (auto it = values.begin(); it != values.end(); ++it)
        if (it.key() == "Techniques" || it.key() == "TechniqueSorting" || it.key().endsWith("PreprocessorDefinitions"))
            graph.insert(it.key(), it.value());
    return QString::fromUtf8(QJsonDocument(graph).toJson(QJsonDocument::Compact));
}
// A CPU-only identity shared by inspection and preview caching. Automatic
// package roots must be represented even when the path fields are empty.
inline QByteArray reshadeDependencyIdentity(const Settings& raw, bool video, const QString& root) {
    const auto s = reshadeSettingsForMode(raw, video);
    QSet<QString> files;
    const auto addFile = [&](const QString& path) {
        const QFileInfo fi(path);
        if (fi.isFile()) files.insert(fi.canonicalFilePath());
    };
    QSet<QString> scanned;
    const auto scan = [&](const QString& dir, bool recursive) {
        const auto key = QDir::cleanPath(dir) + (recursive ? "/**" : "/.");
        if (scanned.contains(key)) return;
        scanned.insert(key);
        QDirIterator it(dir, QDir::Files, recursive ? QDirIterator::Subdirectories : QDirIterator::NoIteratorFlags);
        while (it.hasNext()) {
            const auto path = it.next();
            const auto ext = QFileInfo(path).suffix().toLower();
            if (QStringList{"ini", "fx", "fxh", "cube", "png", "jpg", "jpeg", "bmp", "dds", "tga", "tif", "tiff", "webp", "json"}.contains(ext))
                addFile(path);
        }
    };
    addFile(s.reshade_preset_path);
    const auto parent = QFileInfo(s.reshade_preset_path).absolutePath();
    const auto runtime = root + "/bin/runtime/reshade";
    for (const auto& paths : {s.reshade_shader_search_paths, s.reshade_texture_search_paths}) {
        if (!paths.trimmed().isEmpty()) {
            for (const auto& path : paths.split(';', Qt::SkipEmptyParts)) scan(path.trimmed(), true);
        } else if (!s.reshade_preset_path.isEmpty()) {
            scan(parent, false);
            for (const auto& path : {parent + "/Shaders", parent + "/Textures", parent + "/reshade-shaders",
                                     runtime + "/reshade-shaders", runtime + "/shaders", runtime + "/textures"})
                scan(path, true);
        }
    }
    addFile(runtime + "/runtime-manifest.json");
    addFile(runtime + "/models/moge-2-vits/model.manifest.json");
    auto sorted = files.values();
    sorted.sort();
    QCryptographicHash hash(QCryptographicHash::Sha256);
    hash.addData("reshade-media-v5-moge2");
    for (const auto& path : sorted) {
        hash.addData(path.toUtf8());
        QFile f(path);
        if (f.open(QIODevice::ReadOnly)) hash.addData(&f);
    }
    for (const auto& name : {"neuroframe_engine_reshade.dll", "ve_reshade_runtime.dll", "onnxruntime.dll", "DirectML.dll"}) {
        const QFileInfo f(runtime + '/' + name);
        hash.addData(QJsonDocument(QJsonObject{{"path", f.absoluteFilePath()}, {"bytes", f.size()},
                                              {"modified", f.lastModified().toMSecsSinceEpoch()}}).toJson(QJsonDocument::Compact));
    }
    for (const auto& name : {"model.onnx"}) {
        const QFileInfo f(runtime + "/models/moge-2-vits/" + name);
        hash.addData(QJsonDocument(QJsonObject{{"bytes", f.size()}, {"modified", f.lastModified().toMSecsSinceEpoch()}}).toJson(QJsonDocument::Compact));
    }
    return hash.result();
}
} // namespace ve
