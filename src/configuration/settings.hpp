#pragma once
#include <QJsonObject>
#include <QString>
#include <QStringList>
#include <QVariantMap>
#include <expected>

namespace ve {
struct Settings {
#define VE_SETTING(type, name, initial) type name = initial;
#include "settings_fields.inc"
#undef VE_SETTING
    [[nodiscard]] QVariant value(QStringView key) const;
    [[nodiscard]] QVariantMap values() const;
    [[nodiscard]] std::expected<void, QString> set(QStringView key, const QVariant& value);
    [[nodiscard]] std::expected<void, QString> validate() const;
    [[nodiscard]] QStringList stages(bool video) const;
    [[nodiscard]] QJsonObject preset(const QString& name) const;
    static Settings load(const QString& path);
    [[nodiscard]] std::expected<void, QString> save(const QString& path) const;
    [[nodiscard]] std::expected<QString, QString> importPreset(const QString& path);
    [[nodiscard]] std::expected<void, QString> exportPreset(const QString& path, const QString& name) const;
};
[[nodiscard]] const QStringList& vfxStageIds();
[[nodiscard]] bool isVfxStage(QStringView stage);
[[nodiscard]] bool hasVfxStages(const QStringList& stages);
[[nodiscard]] QStringList normalizeVfxStages(const QStringList& stages,const Settings& settings);
[[nodiscard]] Settings vfxSettingsForStages(const Settings& settings,const QStringList& stages);
[[nodiscard]] QStringList vfxStageGroup(const QStringList& stages,qsizetype begin,const Settings& settings);
[[nodiscard]] Settings reshadeSettingsForMode(const Settings& settings, bool video);
[[nodiscard]] QStringList codecChoices();
[[nodiscard]] QStringList containersForCodec(const QString& codec);
[[nodiscard]] QString defaultContainer(const QString& codec);
[[nodiscard]] bool hdrSupported(const QString& codec);
[[nodiscard]] QString normalizeCodec(QString codec);
} // namespace ve
