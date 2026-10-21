#include "media.hpp"
#include "frame_generation_rate.hpp"
#include "av_api.hpp"
#include "image_codecs.hpp"
#include "image_metadata.hpp"
#include "png_encoder.hpp"
#include "resolution.hpp"
#include "infrastructure/log.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include <QColorSpace>
#include <QBuffer>
#include <QFileInfo>
#include <QImageReader>
#include <QImageWriter>
#include <QJsonArray>
#include <QJsonDocument>
#include <QSaveFile>
#include "infrastructure/scratch.hpp"
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <numeric>

namespace ve {
QSize MediaInfo::outputSampleAspectRatio() const {
    const auto ratio=metadata.value("output_sample_aspect_ratio").toString().split(':');
    const int numerator=ratio.value(0).toInt(),denominator=ratio.value(1).toInt();
    return numerator>0&&denominator>0?QSize(numerator,denominator):QSize(1,1);
}
namespace {
double rate(const QString& expression) {
    const auto parts = expression.split('/');
    if (parts.size() == 2) {
        const auto denominator = parts[1].toDouble();
        return denominator > 0 ? parts[0].toDouble() / denominator : 0;
    }
    return expression.toDouble();
}
void requireSuccess(const ProcessResult& result) {
    if (result.exitCode != 0)
        throw std::runtime_error(QString::fromUtf8(result.error).right(6000).toStdString());
}
QImage readRaster(QImageReader& reader) {
    configureImageDecoding();
    if (reader.size().isValid()) requireResolution(reader.size(), "Input image");
    reader.setAutoTransform(true);
    auto image=reader.read();
    if (image.isNull()) return {};
    requireResolution(image.size(), "Decoded image");
    if (image.depth()<=32) {
        if (image.colorSpace().isValid()) image.convertToColorSpace(QColorSpace::SRgb);
        image.setColorSpace(QColorSpace::SRgb);
    }
    auto result = image.convertToFormat(image.depth()<=32 ? QImage::Format_RGBA8888 : QImage::Format_RGBA64);
    requireImageAllocation(result, "Image conversion");
    return result;
}
} // namespace
int MediaInfo::precisionBits() const {
    if (sourceBitDepth > 0) return sourceBitDepth;
    auto& api = av::Api::get();
    if (const auto* format = api.av_pix_fmt_desc_get(api.av_get_pix_fmt(pixelFormat.toUtf8().constData()))) {
        int bits = 0;
        for (int component = 0; component < format->nb_components; ++component)
            bits = std::max(bits, format->comp[component].depth);
        if (bits > 0) return bits;
    }
    const auto declared = metadata.value("bits_per_raw_sample").toVariant().toInt();
    return declared > 0 ? declared : hdr ? 10 : 8;
}
double MediaInfo::presentationStartSeconds() const {
    bool valid=false;
    const auto stream=metadata.value("start_time").toVariant().toDouble(&valid);
    if(valid&&std::isfinite(stream))return stream;
    const auto format=metadata.value("source_format").toObject().value("start_time").toVariant().toDouble(&valid);
    return valid&&std::isfinite(format)?format:0.;
}
int MediaInfo::clockwiseRotation() const {
    int display = metadata.value("tags").toObject().value("rotate").toVariant().toInt();
    for (const auto& side : metadata.value("side_data_list").toArray())
        if (side.toObject().contains("rotation")) display = side.toObject().value("rotation").toInt();
    // FFmpeg's display matrix reports counter-clockwise degrees. Pixel
    // transforms and the NR/FI descriptors use clockwise degrees.
    return (360 - (display % 360 + 360) % 360) % 360;
}
QString MediaInfo::description() const {
    auto text = QString::number(width) + QChar(0x00D7) + QString::number(height);
    if (video)
        text += " | " + QString::number(fps, 'g', 5) + " FPS | " + QString::number(duration, 'f', 2) + " s";
    if (hdr)
        text += " | HDR";
    return text;
}
MediaInfo probeMedia(const QString& path, JobControl* control, const QStringList& inputOptions) {
    MediaInfo result;
    result.video = isVideo(path);
    result.fileBytes = QFileInfo(path).size();
    if (!result.video) {
        if (isRawImage(path)) {
            const auto size = rawImageSize(path);
            result.width = size.width();
            result.height = size.height();
            result.codec = "LibRaw";
            return result;
        }
        if (isHeifImage(path)) {
            JobControl local;
            const auto image = decodeHeifImage(path, control ? *control : local);
            result.width = image.width();
            result.height = image.height();
            result.codec = "HEIF";
            return result;
        }
        QImageReader reader(path);
        reader.setAutoTransform(true);
        auto size = reader.size();
        if (reader.transformation() & QImageIOHandler::TransformationRotate90)
            size.transpose();
        if (size.isValid()) {
            result.width = size.width();
            result.height = size.height();
            result.codec = QString::fromLatin1(reader.format());
            return result;
        }
    }
    const auto process =
        runProcess(Paths::instance().ffprobe(),
                   QStringList{"-v", "error", "-show_streams", "-show_format", "-of", "json"} + inputOptions +
                       QStringList{path},
                   control);
    requireSuccess(process);
    const auto json = QJsonDocument::fromJson(process.output).object();
    const auto streams = json.value("streams").toArray();
    for (const auto& streamValue : streams) {
        const auto stream = streamValue.toObject();
        if (stream.value("codec_type").toString() == "audio") {
            result.audioStreams.append(stream);
            continue;
        }
        if (stream.value("codec_type").toString() != "video" || result.width > 0)
            continue;
        result.width = stream.value("width").toInt();
        result.height = stream.value("height").toInt();
        result.fps = rate(stream.value("avg_frame_rate").toString());
        if (result.fps <= 0)
            result.fps = rate(stream.value("r_frame_rate").toString());
        const auto nominalRate = rate(stream.value("r_frame_rate").toString());
        result.variableFrameRate = nominalRate > 0 && std::abs(result.fps - nominalRate) > 1e-5;
        result.duration = stream.value("duration").toVariant().toDouble();
        if (result.duration <= 0)
            result.duration = json.value("format").toObject().value("duration").toVariant().toDouble();
        result.frames = stream.value("nb_frames").toVariant().toLongLong();
        if (result.frames <= 0 && result.fps > 0)
            result.frames = static_cast<qint64>(std::ceil(result.fps * result.duration - 1e-8));
        result.codec = stream.value("codec_name").toString();
        result.pixelFormat = stream.value("pix_fmt").toString();
        result.colorSpace = stream.value("color_space").toString("unknown");
        result.primaries = stream.value("color_primaries").toString("unknown");
        result.transfer = stream.value("color_transfer").toString("unknown");
        result.range = stream.value("color_range").toString("unknown");
        result.hdr = result.transfer == "smpte2084" || result.transfer == "arib-std-b67";
        int rotation = stream.value("tags").toObject().value("rotate").toString().toInt();
        for (const auto& side : stream.value("side_data_list").toArray())
            if (side.toObject().contains("rotation"))
                rotation = side.toObject().value("rotation").toInt();
        if (std::abs(rotation) % 180 == 90)
            std::swap(result.width, result.height);
        result.metadata = stream;
        result.metadata.insert("source_format", json.value("format"));
        result.sourceBitDepth = result.precisionBits();
    }
    if (result.width < 1 || result.height < 1)
        throw std::runtime_error("The source has no readable video or image stream.");
    QJsonArray subtitles;
    for(const auto& value:streams)if(value.toObject().value("codec_type")=="subtitle")subtitles.append(value);
    if(!subtitles.isEmpty())result.metadata.insert("source_subtitles",subtitles);
    if (result.hdr) {
        // HEVC/AV1 often report static HDR metadata on decoded frames instead
        // of the stream. Retain it before QImage working frames shed side data.
        const auto hdrProbe = runProcess(Paths::instance().ffprobe(),
            QStringList{"-v", "error", "-select_streams", "v:0", "-read_intervals", "%+#32",
                        "-show_frames", "-show_entries", "frame=side_data_list", "-of", "json"} +
                inputOptions + QStringList{path}, control);
        requireSuccess(hdrProbe);
        auto retained = result.metadata.value("side_data_list").toArray();
        const auto frames = QJsonDocument::fromJson(hdrProbe.output).object().value("frames").toArray();
        for (const auto& frame : frames)
            for (const auto& side : frame.toObject().value("side_data_list").toArray()) {
                const auto type = side.toObject().value("side_data_type").toString();
                if (type != "Mastering display metadata" && type != "Content light level metadata") continue;
                bool present = false;
                for (const auto& existing : retained)
                    present |= existing.toObject().value("side_data_type").toString() == type;
                if (!present) retained.append(side);
            }
        result.metadata.insert("side_data_list", retained);
    }
    return result;
}
QByteArray rgbaBytes(const QImage& image, bool sixteenBit) {
    const auto converted =
        image.convertToFormat(sixteenBit ? QImage::Format_RGBA64 : QImage::Format_RGBA8888);
    requireImageAllocation(converted, "Packed image conversion");
    const qsizetype stride = qsizetype(converted.width()) * (sixteenBit ? 8 : 4);
    QByteArray result(imageByteCount(converted.size(),sixteenBit ? 8 : 4), Qt::Uninitialized);
    for (int y = 0; y < converted.height(); ++y)
        std::memcpy(result.data() + stride * y, converted.constScanLine(y), static_cast<size_t>(stride));
    return result;
}
QImage fromRgbaBytes(const QByteArray& bytes, int width, int height, bool sixteenBit) {
    const auto expected = imageByteCount({width, height}, sixteenBit ? 8 : 4);
    if (bytes.size() != expected)
        throw std::runtime_error("Decoder returned an incomplete image.");
    auto image = QImage(reinterpret_cast<const uchar*>(bytes.constData()), width, height,
                  width * (sixteenBit ? 8 : 4), sixteenBit ? QImage::Format_RGBA64 : QImage::Format_RGBA8888)
        .copy();
    requireImageAllocation(image, "Decoded image allocation");
    return image;
}
QImage decodeImage(const QString& path, JobControl& control) {
    configureImageDecoding();
    control.checkpoint();
    if (isRawImage(path))
        return decodeRawImage(path, control);
    if (isHeifImage(path))
        return decodeHeifImage(path, control);
    if (QFileInfo(path).suffix().compare("avif", Qt::CaseInsensitive) == 0) {
        const auto declared=probeMedia(path,&control).pixelFormat;
        const bool wide=declared.contains("10") || declared.contains("12") || declared.contains("16");
        ScratchDirectory directory(QStringLiteral("avif-XXXXXX"));
        if (!directory.isValid()) throw std::runtime_error("Cannot create the AVIF decoder directory.");
        const auto decoded = directory.filePath("decoded.png");
        const auto result = runProcess(Paths::instance().at("bin/avif/avifdec.exe"),
                                       // libavif 1.4.2's CLI rejects pixel limits above its
                                       // compiled 268435456 maximum. Retain that feature limit.
                                       {"--depth", wide ? "16" : "8", "--dimension-limit", QString::number(maximumImageDimension),
                                        "--png-compress", "1", path, decoded}, &control, 0);
        requireSuccess(result);
        QImageReader reader(decoded);
        if (reader.size().isValid()) requireResolution(reader.size(), "AVIF image");
        reader.setAutoTransform(true);
        auto image = reader.read();
        if (image.isNull())
            throw std::runtime_error(reader.errorString().toStdString());
        auto converted = image.convertToFormat(wide ? QImage::Format_RGBA64 : QImage::Format_RGBA8888);
        requireImageAllocation(converted, "AVIF image conversion");
        return converted;
    }
    QImageReader reader(path);
    auto image = readRaster(reader);
    if (!image.isNull()) {
        return image;
    }
    const auto info = probeMedia(path, &control);
    requireResolution({info.width, info.height}, "Input image");
    const auto process = runProcess(
        Paths::instance().ffmpeg(),
        {"-v", "error", "-i", path, "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba64le", "pipe:1"},
        &control, 0);
    requireSuccess(process);
    return fromRgbaBytes(process.output, info.width, info.height);
}
QImage decodeImageData(const QByteArray& bytes, JobControl& control) {
    control.checkpoint();
    QBuffer input;
    input.setData(bytes);
    input.open(QIODevice::ReadOnly);
    QImageReader reader(&input);
    auto image=readRaster(reader);
    if (image.isNull()) throw std::runtime_error(reader.errorString().toStdString());
    control.checkpoint();
    return image;
}
QImage decodeVideoFrame(const QString& path, double seconds, JobControl& control) {
    const auto info = probeMedia(path, &control);
    const auto process =
        runProcess(Paths::instance().ffmpeg(),
                   {"-v", "error", "-ss", QString::number(std::max(0., seconds), 'f', 6), "-i", path,
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba64le", "pipe:1"},
                   &control, 0);
    requireSuccess(process);
    return fromRgbaBytes(process.output, info.width, info.height);
}
void encodeImage(const QString& path, const QImage& pixels, const QString& format, int quality, int bitDepth,
                 JobControl& control, const QString& metadataSource, const QString& note) {
    control.checkpoint();
    requireResolution(pixels.size(), "Output image");
    if (format == "WebP") requireResolution(pixels.size(), "WebP format", 16383);
    ImageMetadata metadata;
    try {
        metadata = readImageMetadata(metadataSource, control);
        normalizeImageOrientation(metadata);
        if (!note.isEmpty())
            addImageDescription(metadata, note);
    } catch (const Cancelled&) {
        throw;
    } catch (const std::exception& e) {
        info("metadata", QString::fromUtf8(e.what()));
    }
    auto image = pixels.convertToFormat(bitDepth == 16 && (format == "PNG" || format == "TIFF" || format=="AVIF")
                                            ? QImage::Format_RGBA64
                                            : QImage::Format_RGBA8888);
    requireImageAllocation(image, "Output image conversion");
    if (!image.colorSpace().isValid())
        image.setColorSpace(QColorSpace::SRgb);
    metadata.icc = image.colorSpace().iccProfile();
    if (format == "PNG" && bitDepth == 8) {
        encodePng8(path, image, metadata, control);
        return;
    }
    if (format == "PNG" || format == "TIFF" || format == "WebP") {
        {
            QImageWriter writer(path, format.toLatin1().toLower());
            writer.setQuality(quality);
            if (format == "PNG")
                writer.setCompression(1);
            if (format == "TIFF")
                writer.setCompression(bitDepth == 16 ? 0 : 1);
            if (!writer.write(image))
                throw std::runtime_error(writer.errorString().toStdString());
        }
        try {
            writeImageMetadata(path, metadata, control);
        } catch (const Cancelled&) {
            throw;
        } catch (const std::exception& e) {
            info("metadata", QString::fromUtf8(e.what()));
        }
        return;
    }
    if (format == "AVIF") {
        ScratchDirectory directory(QStringLiteral("avif-XXXXXX"));
        if (!directory.isValid()) throw std::runtime_error("Cannot create the AVIF encoder directory.");
        const auto input = directory.filePath("image.png");
        {
            QImageWriter writer(input, "png");
            writer.setCompression(1);
            if (!writer.write(image))
                throw std::runtime_error(writer.errorString().toStdString());
        }
        writeImageMetadata(input, metadata, control);
        const auto result = runProcess(Paths::instance().at("bin/avif/avifenc.exe"),
                                       {"--qcolor", QString::number(quality), "--qalpha", "100", "--speed",
                                        "6", "--depth", bitDepth==16 ? "12" : "8", "--yuv", "420",
                                        "--cicp", "1/13/6", input, path},
                                       &control, 0);
        requireSuccess(result);
        return;
    }
    if (format == "JPEG") {
        image = jpegOverWhite(image);
        encodeJpeg(path, image, quality);
        try {
            writeImageMetadata(path, metadata, control);
        } catch (const Cancelled&) {
            throw;
        } catch (const std::exception& e) {
            info("metadata", QString::fromUtf8(e.what()));
        }
        return;
    }
    throw std::runtime_error("Unknown image output format.");
}
QStringList videoEncoderArguments(const QString& codec, const QString& quality, int width, int height,
                                  double fps, bool hdr) {
    Q_UNUSED(width);
    Q_UNUSED(height);
    Q_UNUSED(fps);
    QString encoder;
    if (codec.contains("NVIDIA NVENC"))
        encoder = codec.startsWith("H.264")   ? "h264_nvenc"
                  : codec.startsWith("H.265") ? "hevc_nvenc"
                                              : "av1_nvenc";
    else
        encoder = codec.startsWith("H.264")    ? "libx264"
                  : codec.startsWith("H.265")  ? "libx265"
                  : codec.startsWith("AV1")    ? "libsvtav1"
                  : codec.startsWith("ProRes") ? "prores_ks"
                                               : "ffv1";
    QStringList result{"-c:v", encoder};
    const bool tenBit = hdr && !codec.startsWith("H.264");
    result << "-pix_fmt"
           << (codec.startsWith("FFV1")     ? "gbrp10le"
               : codec.startsWith("ProRes") ? "yuv422p10le"
               : tenBit                     ? (encoder.endsWith("nvenc") ? "p010le" : "yuv420p10le")
                                            : "yuv420p");
    if (codec.startsWith("ProRes")) {
        result << "-profile:v" << (codec.contains("Proxy") ? "0" : "3");
        if (codec.contains("Proxy") && quality != "Auto (Default)")
            result << "-bits_per_mb"
                   << QString::number(quality == "Max"    ? 800
                                      : quality == "Best" ? 500
                                                          : 300);
    } else if (codec.startsWith("FFV1"))
        result << "-level" << "3" << "-slicecrc" << "1" << "-color_range" << "pc";
    else if (quality != "Max") {
        // These are encoder-specific quality targets, independent of source
        // or output dimensions. VBR/CRF chooses bitrate from the actual frames.
        const int tier = quality == "Best" ? 2 : quality == "Good" ? 1 : 0;
        const std::array<int,3> values = encoder == "h264_nvenc" ? std::array{26,22,18}
            : encoder == "hevc_nvenc" ? std::array{28,24,20}
            : encoder == "av1_nvenc" ? std::array{34,29,24}
            : encoder == "libx264" ? std::array{23,20,17}
            : encoder == "libx265" ? std::array{26,23,20} : std::array{30,26,22};
        if (encoder.endsWith("nvenc"))
            result << "-cq" << QString::number(values[tier]) << "-qmin" << "-1" << "-qmax" << "-1";
        else
            result << "-crf" << QString::number(values[tier]);
        result << "-b:v" << "0";
    } else {
        if (encoder.endsWith("nvenc"))
            result << "-cq" << "1" << "-qmin" << "0" << "-qmax" << "1" << "-b:v" << "0";
        else {
            result << "-crf" << "0";
            if (encoder == "libsvtav1")
                result << "-svtav1-params" << "crf=0";
        }
    }
    if (encoder == "libx264" || encoder == "libx265")
        result << "-preset" << "slow";
    if (encoder == "libx265")
        result << "-x265-params" << "open-gop=0";
    if (encoder.endsWith("nvenc")) {
        result << "-preset" << "p6" << "-rc" << "vbr" << "-forced-idr" << "1";
        if (encoder == "h264_nvenc") result << "-profile:v" << "high";
        if (encoder != "av1_nvenc")
            result << "-tune" << "hq";
    }
    if (encoder == "libsvtav1")
        result << "-preset" << "6";
    return result;
}
// One rational delivery clock for estimates, previews and every export route.
// Multipliers preserve fractional source rates instead of rounding to integer FPS.
TimeBase frameGenerationTimeBase(double sourceFps, const Settings& settings, bool fsr) {
    const auto& mode = fsr ? settings.fsr_frame_generation_frame_rate_mode : settings.frame_interpolation_frame_rate_mode;
    if (mode == "Multiplier") {
        const int multiplier = fsr ? settings.fsr_frame_generation_multiplier : settings.frame_interpolation_multiplier;
        if (!std::isfinite(sourceFps) || sourceFps <= 0 || multiplier < 2 || multiplier > 8)
            throw std::runtime_error("Frame generation requires a positive source frame rate and a multiplier from 2 to 8.");
        const auto source = av::Api::get().av_d2q(sourceFps, 1000000);
        const auto common = std::gcd(source.den, multiplier);
        const auto numerator = std::int64_t(source.num) * (multiplier / common);
        if (source.num <= 0 || source.den <= 0 || numerator > std::numeric_limits<int>::max())
            throw std::runtime_error("The multiplied frame rate exceeds the supported rational clock range.");
        return {source.den / common, int(numerator)};
    }
    if (mode != "Target FPS") throw std::runtime_error("Unknown frame-rate mode.");
    const auto& target = fsr ? settings.fsr_frame_generation_target_fps : settings.frame_interpolation_target_fps;
    if (target == "23.976") return {1001, 24000};
    if (target == "29.97") return {1001, 30000};
    if (target == "59.94") return {1001, 60000};
    if (target == "119.88") return {1001, 120000};
    bool valid = false;
    const auto integer = target.toInt(&valid);
    if (!valid || integer <= 0) throw std::runtime_error("Invalid target frame rate.");
    return {1, integer};
}
double frameGenerationFrameRate(double sourceFps, const Settings& settings, bool fsr) {
    const auto interval = frameGenerationTimeBase(sourceFps, settings, fsr);
    return double(interval.denominator) / interval.numerator;
}

double targetFrameRate(const QString& choice) {
    static const QMap<QString, double> fractional{{"23.976", 24000. / 1001},
                                                  {"29.97", 30000. / 1001},
                                                  {"59.94", 60000. / 1001},
                                                  {"119.88", 120000. / 1001}};
    return fractional.value(choice, choice.toDouble());
}
QStringList audioEncoderArguments(const QString& path, const QString& container, const MediaInfo& source,
                                  JobControl& control) {
    QStringList result;
    ScratchDirectory directory(QStringLiteral("audio-XXXXXX"));
    if (!directory.isValid())
        throw std::runtime_error("Cannot create the audio planning directory.");
    const auto mux = container == "MKV" ? QString("matroska") : container.toLower();
    int outputIndex = 0;
    for (const auto& value : source.audioStreams) {
        control.checkpoint();
        const auto stream = value.toObject();
        const auto test = runProcess(
            Paths::instance().ffmpeg(),
            {"-v", "error", "-y", "-i", path, "-map", "0:" + QString::number(stream.value("index").toInt()),
             "-frames:a", "1", "-c:a", "copy", "-map_metadata", "-1", "-map_chapters", "-1", "-f", mux,
             directory.filePath("audio-" + QString::number(outputIndex) + '.' + container.toLower())},
            &control, 30000);
        result << "-c:a:" + QString::number(outputIndex) << (test.exitCode == 0 ? "copy" : "aac");
        if (test.exitCode != 0)
            result << "-b:a:" + QString::number(outputIndex) << "192k";
        ++outputIndex;
    }
    return result;
}
} // namespace ve
