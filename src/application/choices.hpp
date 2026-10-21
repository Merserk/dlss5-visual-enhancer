#pragma once
#include "qml_facade.hpp"
#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <optional>
namespace ve {
inline std::optional<QVariant> staticChoices(Property property) {
    switch (property) {
    case Property::sharpeningMethodChoices:
        return QJsonDocument::fromJson(R"json(["NVIDIA NIS","AMD CAS"])json").array().toVariantList();
    case Property::lutResolutionChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"33³","value":33},{"label":"65³","value":65},{"label":"129³","value":129}])json")
            .array()
            .toVariantList();
    case Property::languageChoices:
        return QJsonDocument::fromJson(
                   R"json([{"value":"en_US","label":"English (US)","fontFamily":"Segoe UI"},{"value":"ar","label":"العربية","fontFamily":"Segoe UI"},{"value":"de","label":"Deutsch","fontFamily":"Segoe UI"},{"value":"es","label":"Español","fontFamily":"Segoe UI"},{"value":"fr","label":"Français","fontFamily":"Segoe UI"},{"value":"hi","label":"हिन्दी","fontFamily":"Nirmala UI"},{"value":"ja","label":"日本語","fontFamily":"Yu Gothic UI"},{"value":"ko","label":"한국어","fontFamily":"Malgun Gothic"},{"value":"pl","label":"Polski","fontFamily":"Segoe UI"},{"value":"uk","label":"Українська","fontFamily":"Segoe UI"},{"value":"zh_CN","label":"中文 (简体)","fontFamily":"Microsoft YaHei UI"},{"value":"zh_TW","label":"中文 (繁體)","fontFamily":"Microsoft JhengHei UI"}])json")
            .array()
            .toVariantList();
    case Property::nrStyleChoices: {
        QVariantList choices;
        for (int style = 0; style <= 2; ++style)
            choices.append(QVariantMap{{"label", QCoreApplication::translate("App", "Style %1").arg(style)},
                                      {"value", QString("Style %1").arg(style)}});
        return choices;
    }
    case Property::nrScaleChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"Source (Original)","value":1.0},{"label":"1600%","value":16.0},{"label":"800%","value":8.0},{"label":"400%","value":4.0},{"label":"300%","value":3.0},{"label":"200%","value":2.0},{"label":"175%","value":1.75},{"label":"150%","value":1.5},{"label":"125%","value":1.25},{"label":"75%","value":0.75},{"label":"50%","value":0.5},{"label":"25%","value":0.25}])json")
            .array()
            .toVariantList();
    case Property::imageScalingFilterChoices:
        return QJsonDocument::fromJson(R"json(["Lanczos4","Area","Bicubic","Bilinear","Nearest"])json")
            .array()
            .toVariantList();
    case Property::videoScalingFilterChoices:
        return QJsonDocument::fromJson(R"json(["Spline36","Lanczos","Bicubic","Area","Bilinear"])json")
            .array()
            .toVariantList();
    case Property::dlssPresetChoices:
        return QJsonDocument::fromJson(R"json(["Default","J","K","L","M"])json").array().toVariantList();
    case Property::upscaleEngineChoices:
        return QVariantList{QVariantMap{{"label", QCoreApplication::translate("App", "RTX Video Super Resolution")},
                                       {"value", "RTX Video Super Resolution"}},
                            QVariantMap{{"label", "DLSS"}, {"value", "DLSS"}}};
    case Property::imageFormatChoices:
        return QJsonDocument::fromJson(R"json(["PNG","JPEG","WebP","AVIF","TIFF"])json")
            .array()
            .toVariantList();
    case Property::codecChoices:
        return QJsonDocument::fromJson(
                   R"json(["H.264","H.264 (NVIDIA NVENC)","H.265","H.265 (NVIDIA NVENC)","AV1","AV1 (NVIDIA NVENC)","ProRes Proxy","ProRes HQ","FFV1 Lossless RGB 10-bit"])json")
            .array()
            .toVariantList();
    case Property::fixedQualityCodecs:
        return QJsonDocument::fromJson(R"json(["FFV1 Lossless RGB 10-bit","ProRes HQ"])json")
            .array()
            .toVariantList();
    case Property::encodingQualityChoices:
        return QJsonDocument::fromJson(R"json(["Auto (Default)","Max","Best","Good"])json")
            .array()
            .toVariantList();
    case Property::renameModeChoices:
        return QJsonDocument::fromJson(R"json(["Auto","Copy","Custom"])json").array().toVariantList();
    case Property::fiFpsChoices: {
        QVariantList choices;
        for (const auto* fps : {"23.976", "25", "29.97", "30", "50", "59.94", "60", "90",
                               "119.88", "120", "144", "165", "180", "240", "360", "480"}) {
            const auto value = QString::fromLatin1(fps);
            choices.append(QVariantMap{{"label", QCoreApplication::translate("App", "%1 FPS").arg(value)},
                                      {"value", value}});
        }
        return choices;
    }
    case Property::fiProviderChoices:
        return QVariantList{QVariantMap{{"label","DLSS"},{"value","DLSS"}},QVariantMap{{"label","AMD FSR 3"},{"value","FSR 3"}}};
    case Property::fiPreviewLengthChoices:
    case Property::upscalePreviewLengthChoices:
    case Property::nrPreviewLengthChoices: {
        QVariantList choices;
        for (int seconds : {1, 3, 5, 10, 20, 30})
            choices.append(QVariantMap{{"label", QCoreApplication::translate("App", "%1 s").arg(seconds)},
                                      {"value", QString::number(seconds)}});
        return choices;
    }
    case Property::imageSizeModeChoices:
        return QJsonDocument::fromJson(R"json(["Scale factor","Custom dimensions"])json")
            .array()
            .toVariantList();
    case Property::videoSizeModeChoices:
        return QJsonDocument::fromJson(R"json(["Scale factor","Custom dimensions"])json")
            .array()
            .toVariantList();
    case Property::imageScaleFactorChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"1×","value":1.0},{"label":"1.5×","value":1.5},{"label":"2×","value":2.0},{"label":"3×","value":3.0},{"label":"4×","value":4.0}])json")
            .array()
            .toVariantList();
    case Property::videoScaleFactorChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"1×","value":1.0},{"label":"1.5×","value":1.5},{"label":"2×","value":2.0},{"label":"3×","value":3.0},{"label":"4×","value":4.0}])json")
            .array()
            .toVariantList();
    case Property::vsrQualityChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"1 - Low","value":1},{"label":"2 - Medium","value":2},{"label":"3 - High","value":3},{"label":"4 - Ultra","value":4}])json")
            .array()
            .toVariantList();
    case Property::hdrPrecisionChoices:
        return QJsonDocument::fromJson(
                   R"json([{"label":"Packed 10-bit","value":"Packed 10-bit"},{"label":"Packed 10-bit (FP16)","value":"FP16"}])json")
            .array()
            .toVariantList();
    case Property::opticalFlowQualityChoices:
        return QJsonDocument::fromJson(R"json(["High","Medium","Low"])json").array().toVariantList();
    default:
        return std::nullopt;
    }
}
} // namespace ve
