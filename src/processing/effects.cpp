#include "effects.hpp"
#include "media/resolution.hpp"
#include "color_analysis.hpp"
#include "infrastructure/paths.hpp"
#include "infrastructure/process.hpp"
#include "infrastructure/vulkan_device.hpp"
#include "media/media.hpp"
#include "media/native_frame.hpp"
#include "media/cuda_video.hpp"
#include "media/video_backend.hpp"
#include <QCryptographicHash>
#include <QFile>
#include <QDir>
#include <QFileInfo>
#include <QRegularExpression>
#include <QRgba64>
#include <QSaveFile>
#include <QTextStream>
#include "infrastructure/scratch.hpp"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <numbers>
#include <numeric>
#include <deque>

namespace ve {
namespace {
float clamp(float v) {
    return std::clamp(v, 0.f, 1.f);
}
float smooth(float a, float b, float v) {
    const float t = clamp((v - a) / (b - a));
    return t * t * (3 - 2 * t);
}
float linear(float c) {
    return c <= .04045f ? c / 12.92f : std::pow((c + .055f) / 1.055f, 2.4f);
}
float encoded(float c) {
    return c <= .0031308f ? c * 12.92f : 1.055f * std::pow(c, 1.f / 2.4f) - .055f;
}
float displayLuma(Rgb rgb) {
    // Match the reference's per-plane float32 dot product without rounding
    // the green and blue products separately from their accumulated sum.
    return std::fma(rgb[2],.0722f,std::fma(rgb[1],.7152f,rgb[0]*.2126f));
}
Rgb mixRgb(Rgb a, Rgb b, float weight) {
    for (int c = 0; c < 3; ++c)
        a[c] = a[c] * (1 - weight) + b[c] * weight;
    return a;
}
Rgb channels(const QRgba64& p) {
    return {p.red() / 65535.f, p.green() / 65535.f, p.blue() / 65535.f};
}
QRgba64 pixel(Rgb rgb, quint16 alpha) {
    return QRgba64::fromRgba64(quint16(std::lround(clamp(rgb[0]) * 65535)),
                               quint16(std::lround(clamp(rgb[1]) * 65535)),
                               quint16(std::lround(clamp(rgb[2]) * 65535)), alpha);
}
QImage transform(const QImage& input, JobControl& control, const auto& operation) {
    const auto source = input.convertToFormat(QImage::Format_RGBA64);
    requireImageAllocation(source, "Effect input conversion");
    QImage output(source.size(), QImage::Format_RGBA64);
    requireImageAllocation(output, "Effect output allocation");
    for (int y = 0; y < source.height(); ++y) {
        control.checkpoint();
        const auto* from = reinterpret_cast<const QRgba64*>(source.constScanLine(y));
        auto* to = reinterpret_cast<QRgba64*>(output.scanLine(y));
        for (int x = 0; x < source.width(); ++x)
            to[x] =
                from[x].alpha() == 0 ? from[x] : pixel(operation(channels(from[x]), x, y), from[x].alpha());
    }
    return output;
}
QString shaderTemplate(const QString& name) {
    QFile file(Paths::instance().at("resources/shaders/" + name + ".glsl.in"));
    if (!file.open(QIODevice::ReadOnly))
        throw std::runtime_error("Processing shader is missing.");
    return QString::fromUtf8(file.readAll());
}
QString shaderGraph(QString shader, const QString& options = {}) {
    ProcessingCacheActivity cacheActivity;
    const auto digest =
        QString::fromLatin1(QCryptographicHash::hash(shader.toUtf8(), QCryptographicHash::Sha256).toHex());
    const auto directory = QDir(Paths::instance().cacheDirectory()).filePath("shaders");
    if (!QDir().mkpath(directory))
        throw std::runtime_error("Cannot create the generated shader cache.");
    const auto path = QDir(directory).filePath("shader-" + digest + ".hook");
    if (!QFileInfo::exists(path)) {
        QSaveFile file(path);
        if (!file.open(QIODevice::WriteOnly))
            throw std::runtime_error(file.errorString().toStdString());
        file.write(shader.toUtf8());
        if (!file.commit())
            throw std::runtime_error(file.errorString().toStdString());
    }
    return libplacebo("custom_shader_path=" + filterPath(path) +
                      (options.isEmpty() ? QString{} : ':' + options));
}
std::vector<QImage> filterFrames(std::span<const QImage> frames, const QString& graph, QSize output,
                                 JobControl& control, const QString& device) {
    if (frames.empty())
        return {};
    const auto size = frames.front().size();
    requireResolution(size, "Filter input");
    requireResolution(output, "Filter output");
    const bool wide=frames.front().depth()>32;
    const QString format=wide ? "rgba64le" : "rgba";
    QByteArray data;
    data.reserve(qsizetype(frames.size()) * size.width() * size.height() * (wide ? 8 : 4));
    for (const auto& frame : frames) {
        control.checkpoint();
        if (frame.size() != size)
            throw std::runtime_error("A filter batch must have consistent frame dimensions.");
        data += rgbaBytes(frame,wide);
    }
    const QString filter = "setparams=alpha_mode=premultiplied,hwupload," + graph + ',' +
                           libplacebo("format="+format) + ",hwdownload,format="+format;
    const auto result =
        runProcess(Paths::instance().ffmpeg(),
                   QStringList{"-v", "error"} + vulkanFilterArguments(device, size.expandedTo(output)) +
                       QStringList{"-f",
                                   "rawvideo",
                                   "-pixel_format",
                                   format,
                                   "-video_size",
                                   QString::number(size.width()) + 'x' + QString::number(size.height()),
                                   "-framerate",
                                   "30",
                                   "-i",
                                   "pipe:0",
                                   "-vf",
                                   filter,
                                   "-fps_mode",
                                   "passthrough",
                                   "-frames:v",
                                   QString::number(frames.size()),
                                   "-c:v",
                                   "rawvideo",
                                   "-threads:v",
                                   "1",
                                   "-pix_fmt",
                                   format,
                                   "-f",
                                   "rawvideo",
                                   "pipe:1"},
                   &control, 0, data);
    if (result.exitCode != 0)
        throw std::runtime_error(QString::fromUtf8(result.error).right(5000).toStdString());
    const qsizetype frameBytes = qsizetype(output.width()) * output.height() * (wide ? 8 : 4);
    if (result.output.size() != frameBytes * qsizetype(frames.size()))
        throw std::runtime_error("The video filter returned an incomplete batch.");
    std::vector<QImage> images;
    images.reserve(frames.size());
    for (size_t index = 0; index < frames.size(); ++index)
        images.push_back(fromRgbaBytes(
            QByteArray::fromRawData(result.output.constData() + qsizetype(index) * frameBytes, frameBytes),
            output.width(), output.height(),wide));
    return images;
}
} // namespace
QString libplacebo(const QString& options) {
    return "libplacebo=deband=0:sigmoid=0:disable_linear=1:dithering=-1:apply_filmgrain=0:alpha_mode="
           "premultiplied" +
           (options.isEmpty() ? QString{} : ':' + options);
}
QString filterPath(const QString& path) {
    QString clean = path;
    clean.replace('\\', '/');
    clean.replace(":", "\\:");
    clean.replace("'", "'\\''");
    return '\'' + clean + '\'';
}
CubeLut CubeLut::identity() {
    CubeLut result;
    for (int b = 0; b < 2; ++b)
        for (int g = 0; g < 2; ++g)
            for (int r = 0; r < 2; ++r)
                result.entries.push_back({float(r), float(g), float(b)});
    return result;
}
CubeLut CubeLut::load(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text) || file.size() > 128 * 1024 * 1024)
        throw std::runtime_error("LUT must be an existing .cube file smaller than 128 MiB.");
    CubeLut result;
    result.size = 0;
    QTextStream stream(&file);
    const QRegularExpression whitespace("\\s+");
    while (!stream.atEnd()) {
        auto line = stream.readLine().section('#', 0, 0).trimmed();
        if (line.isEmpty())
            continue;
        const auto words = line.split(whitespace, Qt::SkipEmptyParts);
        const auto key = words[0].toUpper();
        if (key == "TITLE")
            continue;
        if (key == "LUT_3D_SIZE") {
            if (words.size() != 2 || result.size)
                throw std::runtime_error("Invalid LUT_3D_SIZE declaration.");
            result.size = words[1].toInt();
            if (result.size < 2 || result.size > 129)
                throw std::runtime_error("3D LUT size must be between 2 and 129.");
            result.entries.reserve(size_t(result.size) * result.size * result.size);
            continue;
        }
        if (key == "LUT_1D_SIZE")
            throw std::runtime_error("Select a 3D .cube LUT; 1D LUTs are not supported.");
        if (key == "DOMAIN_MIN" || key == "DOMAIN_MAX") {
            if (words.size() != 4)
                throw std::runtime_error("Invalid LUT domain.");
            auto& domain = key == "DOMAIN_MIN" ? result.minimum : result.maximum;
            for (int c = 0; c < 3; ++c)
                domain[c] = words[c + 1].toFloat();
            continue;
        }
        if (key == "LUT_3D_INPUT_RANGE") {
            if (words.size() != 3)
                throw std::runtime_error("Invalid LUT input range.");
            result.minimum.fill(words[1].toFloat());
            result.maximum.fill(words[2].toFloat());
            continue;
        }
        if (words.size() != 3 || result.size == 0)
            throw std::runtime_error("Invalid 3D LUT entry.");
        Rgb rgb{};
        for (int c = 0; c < 3; ++c) {
            bool okay = false;
            rgb[c] = words[c].toFloat(&okay);
            if (!okay || !std::isfinite(rgb[c]))
                throw std::runtime_error("Nonfinite 3D LUT entry.");
        }
        result.entries.push_back(rgb);
    }
    if (result.entries.size() != size_t(result.size) * result.size * result.size)
        throw std::runtime_error("The 3D LUT has an incorrect number of color entries.");
    for (int c = 0; c < 3; ++c)
        if (!std::isfinite(result.minimum[c]) || !std::isfinite(result.maximum[c]) ||
            result.minimum[c] >= result.maximum[c])
            throw std::runtime_error("LUT DOMAIN_MAX must exceed DOMAIN_MIN.");
    return result;
}
Rgb CubeLut::sample(Rgb rgb) const {
    std::array<int, 3> low{}, high{};
    Rgb weight{};
    for (int c = 0; c < 3; ++c) {
        const float coordinate = clamp((rgb[c] - minimum[c]) / (maximum[c] - minimum[c])) * (size - 1);
        low[c] = int(coordinate);
        high[c] = std::min(low[c] + 1, size - 1);
        weight[c] = coordinate - low[c];
    }
    auto at = [&](int r, int g, int b) { return entries[size_t((b * size + g) * size + r)]; };
    auto plane = [&](int b) {
        return mixRgb(mixRgb(at(low[0], low[1], b), at(high[0], low[1], b), weight[0]),
                      mixRgb(at(low[0], high[1], b), at(high[0], high[1], b), weight[0]), weight[1]);
    };
    return mixRgb(plane(low[2]), plane(high[2]), weight[2]);
}
Rgb gradeRgb(Rgb input, Rgb sampled, const Settings& s) {
    return gradeRgb(input, sampled, ColorGrade(s));
}
Rgb gradeRgb(Rgb input, Rgb sampled, const ColorGrade& s) {
    const float mix = float(s.lut_mix) / 100;
    if (mix == 0) {
        for (auto& c : input) c=clamp(c);
        return input;
    }
    // A selected cube can intentionally contain values outside display range.
    // Export it unchanged until a grading control or blend is applied.
    if (!s.hasAdjustments()) return sampled;
    Rgb rgb = sampled;
    for (auto& c : rgb)
        c = clamp(c);
    if (s.lut_exposure || s.lut_temperature || s.lut_tint) {
        const double warmth = s.lut_temperature / 100., tint = s.lut_tint / 100.;
        Rgb gains{float(1 + .2 * warmth + .075 * tint), float(1 - .15 * tint),
                  float(1 - .2 * warmth + .075 * tint)};
        for (int c = 0; c < 3; ++c)
            rgb[c] =
                clamp(encoded(std::max(0.f, linear(rgb[c]) * gains[c] * float(std::exp2(s.lut_exposure)))));
    }
    if (s.lut_contrast)
        for (auto& c : rgb)
            c = clamp((c - .5f) * float(1 + s.lut_contrast / 100.) + .5f);
    if (s.lut_shadows || s.lut_highlights || s.lut_whites || s.lut_blacks) {
        const float luma = displayLuma(rgb);
        const float shift = s.lut_shadows / 100.f * .32f * (1 - luma) * (1 - luma) +
                            s.lut_highlights / 100.f * .32f * luma * luma +
                            s.lut_whites / 100.f * .18f * smooth(.62f, 1, luma) +
                            s.lut_blacks / 100.f * .18f * (1 - smooth(0, .38f, luma));
        for (auto& c : rgb)
            c = clamp(c + shift);
    }
    if (s.lut_midtones)
        for (auto& c : rgb)
            c = std::pow(c, std::exp2(-s.lut_midtones / 100.f));
    if (s.lut_hue) {
        const float angle = float(s.lut_hue) * std::numbers::pi_v<float> / 180;
        const float y = rgb[0] * .299f + rgb[1] * .587f + rgb[2] * .114f;
        const float i = rgb[0] * .596f - rgb[1] * .274f - rgb[2] * .322f,
                    q = rgb[0] * .211f - rgb[1] * .523f + rgb[2] * .312f;
        const float ri = i * std::cos(angle) - q * std::sin(angle),
                    rq = i * std::sin(angle) + q * std::cos(angle);
        rgb = {clamp(y + .956f * ri + .621f * rq), clamp(y - .272f * ri - .647f * rq),
               clamp(y - 1.106f * ri + 1.703f * rq)};
    }
    if (s.lut_saturation || s.lut_vibrance) {
        const float luma = displayLuma(rgb);
        const float chroma =
            *std::max_element(rgb.begin(), rgb.end()) - *std::min_element(rgb.begin(), rgb.end());
        const float factor = std::max(0.f,float(1 + s.lut_saturation / 100.) +
            float(s.lut_vibrance / 100.) * (1 - chroma) * (1 - chroma));
        for (auto& c : rgb)
            c = clamp(luma + (c - luma) * factor);
    }
    rgb = mixRgb(input, rgb, mix);
    for (auto& c : rgb)
        c = clamp(c);
    return rgb;
}
void saveGradedLut(const QString& path, const Settings& settings, JobControl& control,int decimalPlaces) {
    const auto lut = settings.lut_path.isEmpty() ? CubeLut::identity() : CubeLut::load(settings.lut_path);
    QSaveFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text))
        throw std::runtime_error(file.errorString().toStdString());
    QTextStream stream(&file);
    stream << "TITLE \"Visual Enhancer Graded LUT\"\nLUT_3D_SIZE " << settings.lut_resolution
           << "\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n";
    const auto writeComponent=[&](float value) {
        // Decimal ties round to even, as in the reference's cube serialization.
        std::array<char,64> buffer{};
        const auto result=std::to_chars(buffer.data(),buffer.data()+buffer.size(),
                                        double(value),std::chars_format::fixed,decimalPlaces);
        if (result.ec!=std::errc{}) throw std::runtime_error("Cannot serialize LUT component.");
        stream << QString::fromLatin1(buffer.data(),result.ptr-buffer.data());
    };
    const int size = settings.lut_resolution;
    for (int b = 0; b < size; ++b) {
        control.checkpoint();
        for (int g = 0; g < size; ++g)
            for (int r = 0; r < size; ++r) {
                const Rgb input{r / float(size - 1), g / float(size - 1), b / float(size - 1)};
                const auto rgb = gradeRgb(input, lut.sample(input), settings);
                writeComponent(rgb[0]); stream << ' ';
                writeComponent(rgb[1]); stream << ' ';
                writeComponent(rgb[2]); stream << '\n';
            }
    }
    stream.flush();
    if (!file.commit())
        throw std::runtime_error(file.errorString().toStdString());
}
namespace {
QImage colorImageImpl(const QImage& pixels, const Settings& settings, JobControl& control, bool compose) {
    ScratchDirectory directory(QStringLiteral("image-lut-XXXXXX"));
    if (!directory.isValid()) throw std::runtime_error("Cannot create the image LUT directory.");
    const auto path=directory.filePath("reference.cube");
    const bool adjusted=ColorGrade(settings).hasAdjustments();
    if (!compose && !adjusted && !settings.lut_path.isEmpty()) {
        if (!QFile::copy(settings.lut_path,path)) throw std::runtime_error("Cannot prepare the selected image LUT.");
    } else {
        auto lutSettings=settings;
        if (!compose && !adjusted) lutSettings.lut_resolution=2;
        saveGradedLut(path,lutSettings,control,9);
    }
    const auto original=pixels.convertToFormat(QImage::Format_RGBA64);
    auto opaque=original;
    for (int y=0;y<opaque.height();++y) {
        control.checkpoint();
        auto* row=reinterpret_cast<QRgba64*>(opaque.scanLine(y));
        for (int x=0;x<opaque.width();++x) row[x].setAlpha(65535);
    }
    if (pixels.depth()<=32) opaque=opaque.convertToFormat(QImage::Format_RGBA8888);
    auto output=filterFrames({&opaque,1},libplacebo("lut="+filterPath(path)+":lut_type=native"),
        opaque.size(),control,settings.ffmpeg_device).front();
    output=output.convertToFormat(QImage::Format_RGBA64);
    for (int y=0;y<output.height();++y) {
        control.checkpoint();
        auto* row=reinterpret_cast<QRgba64*>(output.scanLine(y));
        const auto* source=reinterpret_cast<const QRgba64*>(original.constScanLine(y));
        for (int x=0;x<output.width();++x) row[x].setAlpha(source[x].alpha());
    }
    return output.convertToFormat(pixels.depth()<=32 ? QImage::Format_RGBA8888 : QImage::Format_RGBA64);
}
} // namespace
QImage colorImage(const QImage& pixels, const Settings& settings, JobControl& control) {
    return colorImageImpl(pixels, settings, control, false);
}
QImage colorStillPreview(const QImage& pixels, const Settings& settings, JobControl& control) {
    return colorImageImpl(pixels, settings, control, true);
}
namespace {
QString sharpeningGraph(const Settings& settings, const QString& transfer, bool eightBit=false) {
    QString shader = shaderTemplate("sharpening_0");
    if (eightBit) shader.replace("return HOOKED_tex((p + 0.5) / HOOKED_size);",
        "return round(HOOKED_tex((p + 0.5) / HOOKED_size) * 255.0) / 255.0;");
    if (settings.sharpening_method == "AMD CAS") {
        if (transfer == "linear")
            shader += "vec3 lin(vec3 c){return c;} vec3 enc(vec3 c){return c;}\n";
        else if (transfer == "bt709")
            shader +=
                "vec3 lin(vec3 c){return "
                "mix(pow((c+0.099)/1.099,vec3(1.0/0.45)),c/4.5,lessThan(c,vec3(0.081)));} vec3 enc(vec3 "
                "c){return mix(1.099*pow(c,vec3(0.45))-0.099,c*4.5,lessThan(c,vec3(0.018)));}\n";
        else
            shader += "vec3 lin(vec3 c){return "
                      "mix(pow((c+0.055)/1.055,vec3(2.4)),c/12.92,lessThanEqual(c,vec3(0.04045)));} vec3 "
                      "enc(vec3 c){return "
                      "mix(1.055*pow(c,vec3(1.0/2.4))-0.055,c*12.92,lessThanEqual(c,vec3(0.0031308)));}\n";
        auto body = shaderTemplate("sharpening_1");
        body.replace("@-1.0 / (8.0 - 3.0 * sharpness / 100.0)@",
                     QString::number(-1. / (8. - 3. * settings.cas_sharpness / 100.), 'f', 10));
        body.replace("@sharpness / 100.0@", QString::number(settings.cas_sharpness / 100., 'f', 8));
        shader += body;
    } else {
        shader += shaderTemplate("sharpening_2");
        auto body = shaderTemplate("sharpening_3");
        const double slider = settings.cas_sharpness / 100. - .5;
        const bool positive = slider >= 0;
        const double low = std::max(0., .4 + slider * (positive ? 1.25 : 1.) * 1.2),
                     high = 1.6 + slider * (positive ? 1.25 : 1.75) * 1.8;
        const double limitLow = std::max(.1, .14 + slider * (positive ? 1.25 : 1.) * .32),
                     limitHigh = .5 + slider * (positive ? 1.25 : 1.) * .6;
        body.replace("@strength_max - strength_min@", QString::number(high - low, 'f', 10));
        body.replace("@strength_min@", QString::number(low, 'f', 10));
        body.replace("@limit_max - limit_min@", QString::number(limitHigh - limitLow, 'f', 10));
        body.replace("@limit_min@", QString::number(limitLow, 'f', 10));
        shader += body;
    }
    return shaderGraph(shader);
}
QString grainGraph(const Settings& settings, quint64 frame, bool hdr, bool video) {
    auto shader = shaderTemplate("grain_0");
    const bool color = settings.grain_color > 0;
    shader.replace("@uint_kind@", color ? "uvec4" : "uint");
    shader.replace("@kind@", color ? "vec4" : "float");
    shader.replace("@options.seed@", QString::number(settings.grain_seed));
    shader.replace("@salts@", color ? "uvec4(0u, 0x68bc21ebu, 0x02e5be93u, 0x967a889bu)" : "0u");
    shader.replace("@options.size@", QString::number(settings.grain_size, 'f', 8));
    shader.replace("@temporal@",
                   video && settings.grain_animated ? QString::number(frame) + "u + uint(frame)" : "0u");
    if (color) {
        const double chroma = settings.grain_color / 100.;
        shader += "    vec3 noise = " + QString::number(std::sqrt(1 - chroma * chroma), 'f', 8) +
                  " * vec3(n.x) + " + QString::number(chroma, 'f', 8) + " * n.yzw;\n";
    } else
        shader += "    vec3 noise = vec3(n);\n";
    auto body = shaderTemplate("grain_1");
    body.replace("@'0.2627, 0.6780, 0.0593' if hdr else '0.2126, 0.7152, 0.0722'@",
                 hdr ? "0.2627, 0.6780, 0.0593" : "0.2126, 0.7152, 0.0722");
    body.replace("@options.response / 100.0@", QString::number(settings.grain_response / 100., 'f', 8));
    body.replace("@options.amount / 100.0 * (0.025 if hdr else 0.06)@",
                 QString::number(settings.grain_amount / 100. * (hdr ? .025 : .06), 'f', 8));
    shader += body;
    if (!color)
        shader += "    headroom = vec3(min(headroom.r, min(headroom.g, headroom.b)));\n";
    shader += "    return vec4(rgb + clamp(delta, -headroom, headroom), c.a);\n}\n";
    return shaderGraph(shader);
}
QString resizingGraph(QSize input, QSize output, const QString& filter, bool fit) {
    const QMap<QString, QString> kernels{{"Spline36", "spline36"},
                                         {"Lanczos", "lanczos"},
                                         {"Bicubic", "bicubic"},
                                         {"Bilinear", "bilinear"},
                                         {"Nearest", "nearest"}};
    if (kernels.contains(filter)) {
        const auto kernel = kernels.value(filter);
        return libplacebo("w=" + QString::number(output.width()) + ":h=" + QString::number(output.height()) +
                          ":upscaler=" + kernel + ":downscaler=" + kernel +
                          (fit ? ":fit_mode=contain:fillcolor=black@1" : ""));
    }
    // Eight-tap Lanczos and exact area sampling use the original custom shaders.
    int fitWidth = output.width(), fitHeight = output.height(), left = 0, top = 0;
    if (fit) {
        const double scale =
            std::min(output.width() / double(input.width()), output.height() / double(input.height()));
        fitWidth = std::clamp(int(std::lround(input.width() * scale)), 1, output.width());
        fitHeight = std::clamp(int(std::lround(input.height() * scale)), 1, output.height());
        left = (output.width() - fitWidth) / 2;
        top = (output.height() - fitHeight) / 2;
    }
    auto header = shaderTemplate("filters_0");
    const QMap<QString, int> tokens{
        {"width", output.width()}, {"height", output.height()}, {"left", left}, {"top", top},
        {"fit_width", fitWidth},   {"fit_height", fitHeight}};
    for (auto it = tokens.begin(); it != tokens.end(); ++it)
        header.replace('@' + it.key() + '@', QString::number(it.value()));
    if (filter == "Lanczos4")
        header.replace(
            "vec4 hook()",
            "float lanczos(float x) { x=abs(x); if(x<1e-6) return 1.0; if(x>=4.0) return 0.0; return "
            "sin(3.14159265*x)*sin(3.14159265*x/4.0)/(3.14159265*3.14159265*x*x/4.0); }\nvec4 hook()");
    header += shaderTemplate(filter == "Area" ? "filters_1" : "filters_2");
    return shaderGraph(header, "w=" + QString::number(output.width()) + ":h=" +
                                   QString::number(output.height()) + ":upscaler=nearest:downscaler=nearest");
}
} // namespace

struct VideoFilterStage::Impl {
    ProcessingCacheActivity cacheActivity;
    ScratchDirectory directory{QStringLiteral("video-filter-XXXXXX")};
    std::unique_ptr<PipeProcess> process;
    std::unique_ptr<FramePacketReader> reader;
    QStringList arguments;
    QString filter;
    QString grain;
    Settings settings;
    quint64 firstFrame = 0, processed = 0;
    bool hdr = false;
    QString outputFormat = "gbrp10le";
    QSize input, output;
    TimedFrameSource source;
    JobControl* control=nullptr;
    struct Timing { qint64 pts, duration; TimeBase base; };
    std::deque<Timing> pending;
    QByteArray received;
    bool sourceFinished=false, planarInput=false, wideInput=false;
    size_t lookahead=0;
    [[nodiscard]] std::optional<TimedImage> readStream();
    std::vector<QByteArray> run(std::span<const QImage> frames, JobControl& control, bool planarRgb10);
    Impl(const QStringList& stages, QSize size, const MediaInfo& colors, const Settings& settings, quint64 firstFrame)
        : settings(settings), firstFrame(firstFrame), hdr(colors.hdr), input(size), output(size) {
        if (!directory.isValid()) throw std::runtime_error("Cannot create the video filter directory.");
        auto known = [](const QString& value, const QString& fallback) {
            return value.isEmpty() || QStringList{"unknown", "unspecified", "reserved"}.contains(value) ? fallback : value;
        };
        const auto primary = known(colors.primaries, colors.hdr ? "bt2020" : "bt709");
        const auto transfer = known(colors.transfer, colors.hdr ? "smpte2084" : "bt709");
        QStringList graph;
        for (const auto& stage : stages) {
            if (stage == "scale_method") {
                const QSize scaled(checkedDimension(output.width()*settings.upscaling_factor,DimensionRounding::EvenCeil),
                                   checkedDimension(output.height()*settings.upscaling_factor,DimensionRounding::EvenCeil));
                graph.append(resizingGraph(output, scaled, settings.video_scaling_filter, false)); output = scaled;
            } else if (stage == "cas_sharpening" && settings.cas_sharpness) {
                graph.append(sharpeningGraph(settings, transfer == "iec61966-2-1" ? "srgb" : transfer == "linear" ? "linear" : "bt709"));
            } else if (stage == "grain" && settings.grain_amount) {
                grain = grainGraph(settings, firstFrame, colors.hdr, true);
                graph.append(grain);
            } else if (stage == "coloring") {
                JobControl control;
                const auto lut = directory.filePath("reference.cube");
                saveGradedLut(lut, settings, control);
                graph.append("libplacebo=lut=" + filterPath(lut) + ":lut_type=native:deband=0:dithering=-1:format=gbrp10le");
            }
            graph.append(libplacebo("format=gbrp10le"));
        }
        const auto tags = ":range=pc:colorspace=0:color_primaries=" + primary + ":color_trc=" + transfer;
        // Normalize before packing sub-16-bit RGB, matching the reference's
        // clamping boundary for superwhites and GPU conversion overshoot.
        graph.append(libplacebo("format=gbrp16le" + tags));
        graph.append(libplacebo("format=gbrp10le" + tags));
        filter = "hwupload,setparams=colorspace=gbr:range=full:color_primaries=" + primary + ":color_trc=" + transfer +
            ',' + graph.join(',') + ",hwdownload,format=gbrp10le";
        if (stages==QStringList{"rtx_normalization"}) {
            const bool wide=colors.pixelFormat=="gbrp10le" || colors.pixelFormat.contains("16") || colors.pixelFormat.contains("12") || colors.pixelFormat.contains("10");
            outputFormat=wide ? "gbrp10le" : "rgba";
            QString curve;
            if (QStringList{"bt470m","gamma22"}.contains(transfer)) curve="return v;";
            else if (QStringList{"bt470bg","gamma28"}.contains(transfer)) curve="return pow(v,vec3(2.8/2.2));";
            else if (transfer=="linear") curve="return pow(v,vec3(1.0/2.2));";
            else if (QStringList{"iec61966-2-1","srgb"}.contains(transfer))
                curve="vec3 linear=mix(pow((v+0.055)/1.055,vec3(2.4)),v/12.92,lessThanEqual(v,vec3(0.04045))); return pow(linear,vec3(1.0/2.2));";
            else if (QStringList{"bt709","smpte170m","smpte240m","bt2020-10","bt2020-12","iec61966-2-4","bt1361e"}.contains(transfer))
                curve="return pow(v,vec3(2.4/2.2));";
            QString normalization;
            if (curve.isEmpty()) {
                normalization="hwdownload,format="+QString(wide ? "gbrp10le" : "rgba")+
                    ",zscale=matrixin=gbr:primariesin="+primary+":transferin="+transfer+":rangein=full:matrix=gbr:primaries=bt709:transfer=bt470m:range=full,hwupload";
            } else {
                const QString shader="//!HOOK MAIN\n//!BIND HOOKED\nvec3 rtx_gamma22(vec3 v) {v=max(v,vec3(0.0)); "+curve+
                    "}\nvec4 hook(){vec4 pixel=HOOKED_tex(HOOKED_pos);return vec4(rtx_gamma22(pixel.rgb),pixel.a);}\n";
                normalization=shaderGraph(shader,"format=gbrp16le:colorspace=0:color_primaries=bt709:color_trc=bt470m:range=pc:gamut_mode=clip");
            }
            // RTX Video consumes pure gamma 2.2 RGB. Keeping equal transfer
            // tags around the shader avoids libplacebo's display black scaling.
            filter="hwupload,setparams=colorspace=gbr:range=full:color_primaries="+primary+":color_trc=bt470m,"+
                normalization+','+libplacebo("format="+QString(wide ? "gbrp10le" : "gbrp"))+','+
                resizingGraph(input,input,"Lanczos",false)+",setsar=1,"+libplacebo("format="+outputFormat)+','+
                (wide ? libplacebo("format=gbrp16le:range=pc")+',' : QString{})+
                libplacebo("format="+outputFormat+":range=pc")+",hwdownload,format="+outputFormat;
        }
        if (stages==QStringList{"fi_hdr_input"}) {
            outputFormat="p010le";
            // A cached HDR frame is nonlinear PQ RGB. DLSSG consumes limited
            // P010 here; its native adapter performs the matching PQ decode.
            filter="hwupload,setparams=colorspace=gbr:range=full:color_primaries="+primary+":color_trc="+transfer+','+
                libplacebo("format=p010le")+','+
                libplacebo("format=p010le:range=tv:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc")+
                ",hwdownload,format=p010le";
        }
        requireResolution(input, "Video filter input");
        requireResolution(output, "Video filter output");
        const auto args = QStringList{"-v", "error"} + vulkanFilterArguments(settings.ffmpeg_device,input.expandedTo(output)) +
            QStringList{"-nofind_stream_info", "-f", "rawvideo", "-pixel_format", "rgba64le",
                "-video_size", QString::number(input.width())+'x'+QString::number(input.height()), "-framerate",
                QString::number(colors.fps > 0 ? colors.fps : 30., 'g', 15), "-threads:v", "1", "-i", "pipe:0", "-vf", filter,
                "-an", "-fps_mode", "passthrough", "-c:v", "rawvideo", "-threads:v", "1", "-pix_fmt", outputFormat,
                "-f", "rawvideo", "-flush_packets", "1", "pipe:1"};
        arguments = args;
    }
};
VideoFilterStage::VideoFilterStage(QStringList stages, QSize input, const MediaInfo& colors, const Settings& settings, quint64 firstFrame)
    : impl_(std::make_unique<Impl>(stages, input, colors, settings, firstFrame)) {}
VideoFilterStage::VideoFilterStage(QStringList stages, const MediaInfo& colors, const Settings& settings,
    const QString& source, double startSeconds, JobControl& control)
    : VideoFilterStage(std::move(stages), {colors.width, colors.height}, colors, settings) {
    auto& state = *impl_;
    const bool wide = colors.pixelFormat.contains("10") || colors.pixelFormat.contains("12") || colors.pixelFormat.contains("16");
    const QString decodedFormat = wide ? "rgba64le" : "rgba";
    // The reference decodes a source part into packed RGBA before filtering it.
    // Keep that real UNORM boundary: filtering YUV directly, or promoting an
    // 8-bit source before conversion, changes chroma reconstruction and pixels.
    const auto decoding = planVideoDecoder(settings.ffmpeg_device, source, control, decodedFormat, colors.clockwiseRotation());
    const auto filter = decoding.filter + ',' + state.filter;
    auto args = QStringList{"-v", "error", "-copyts"} + decoding.inputOptions;
    if (startSeconds > 0) args << "-ss" << QString::number(startSeconds, 'f', 9);
    args << "-i" << source << "-map" << "0:v:0" << "-an" << "-vf" << filter << "-fps_mode" << "passthrough"
        << "-enc_time_base:v" << "demux" << "-c:v" << "rawvideo" << "-threads:v" << "1" << "-pix_fmt" << "gbrp10le"
        << "-f" << "nut" << "-write_index" << "0" << "-flush_packets" << "1" << "pipe:1";
    state.process = std::make_unique<PipeProcess>(Paths::instance().ffmpeg(), args,PipeProcess::OutputMode::Backpressured);
    state.reader = std::make_unique<FramePacketReader>(*state.process, control, RawPixelFormat::Gbrp10);
}
VideoFilterStage::~VideoFilterStage() = default;
VideoFilterStage::VideoFilterStage(QStringList stages, const MediaInfo& colors, const Settings& settings,
    TimedFrameSource source, JobControl& control)
    : VideoFilterStage(std::move(stages),{colors.width,colors.height},colors,settings) {
    auto& state=*impl_;
    state.source=std::move(source); state.control=&control;
    state.planarInput=colors.pixelFormat=="gbrp10le";
    state.wideInput=state.planarInput || colors.pixelFormat.contains("10") || colors.pixelFormat.contains("12") ||
        colors.pixelFormat.contains("16") || colors.pixelFormat.contains("64");
    // Each libplacebo queue may need its next input to release the current
    // frame. Account for the actual graph plus the decoder/encoder boundary.
    state.lookahead=size_t(state.filter.count("libplacebo="))+2;
    auto arguments=state.arguments;
    arguments[arguments.indexOf("-pixel_format")+1]=state.planarInput ? "gbrp10le" : state.wideInput ? "rgba64le" : "rgba";
    state.process=std::make_unique<PipeProcess>(Paths::instance().ffmpeg(),arguments);
}
std::optional<TimedImage> VideoFilterStage::Impl::readStream() {
    if (outputFormat!="gbrp10le" && outputFormat!="rgba") throw std::logic_error("A timed RGB stream requires RGB10 or RGBA8 output.");
    const auto bytes=qsizetype(output.width())*output.height()*(outputFormat=="rgba" ? 4 : 6);
    while (received.size()<bytes) {
        control->checkpoint();
        received+=process->readAvailable(bytes-received.size(),*control);
        if (received.size()==bytes) break;
        if (!sourceFinished && pending.size()<lookahead) {
            auto next=source();
            if (!next) { sourceFinished=true; process->closeInput(); }
            else {
                if (next->image.size()!=input) throw std::runtime_error("Video filter input geometry changed.");
                const auto data=planarInput ? gbrp10Bytes(next->image) : rgbaBytes(next->image,wideInput);
                pending.push_back({next->pts,next->duration,next->timeBase});
                process->write(data.constData(),data.size(),*control);
            }
        } else {
            if (pending.empty()) {
                if (!received.isEmpty()) throw std::runtime_error("The video filter returned an incomplete frame.");
                process->finish(*control);
                return {};
            }
            auto data=process->readSome(bytes-received.size(),*control);
            if (data.isEmpty()) throw std::runtime_error("The video filter did not return every submitted frame.");
            received+=data;
        }
    }
    if (pending.empty()) throw std::runtime_error("The video filter returned an unexpected frame.");
    const auto timing=pending.front(); pending.pop_front();
    auto image=outputFormat=="rgba" ? fromRgbaBytes(received,output.width(),output.height(),false)
                                     : rgbaFromGbrp10(received,output);
    received.clear();
    return TimedImage{std::move(image),timing.pts,timing.duration,timing.base};
}
std::optional<TimedImage> VideoFilterStage::read() { return impl_->source ? impl_->readStream() : impl_->reader->read(); }
TimeBase VideoFilterStage::timeBase() const { return impl_->reader->timeBase(); }
std::vector<QImage> VideoFilterStage::process(std::span<const QImage> frames, JobControl& control, bool planarRgb10) {
    if (impl_->outputFormat=="p010le") throw std::logic_error("P010 requires an owned planar frame.");
    const auto raw=impl_->run(frames,control,planarRgb10);
    std::vector<QImage> output;
    output.reserve(raw.size());
    for (const auto& pixels : raw)
        output.push_back(impl_->outputFormat=="rgba" ? fromRgbaBytes(pixels,impl_->output.width(),impl_->output.height(),false)
            : rgbaFromGbrp10(pixels,impl_->output));
    return output;
}
std::vector<CudaVideoFrame> VideoFilterStage::processP010(std::span<const QImage> frames, JobControl& control, bool planarRgb10) {
    if (impl_->outputFormat!="p010le") throw std::logic_error("The filter does not produce P010.");
    auto raw=impl_->run(frames,control,planarRgb10);
    std::vector<CudaVideoFrame> output;
    output.reserve(raw.size());
    for (auto& pixels : raw) {
        auto owner=std::make_shared<QByteArray>(std::move(pixels));
        CudaVideoFrame frame;
        frame.owner=owner; frame.memory=FrameMemory::Host; frame.format=VideoPixelFormat::P010;
        frame.size=impl_->output; frame.strides[0]=uint32_t(frame.size.width())*2;
        frame.strides[1]=uint32_t((frame.size.width()+1)/2)*4;
        frame.planes[0]=reinterpret_cast<uint64_t>(owner->constData());
        frame.planes[1]=frame.planes[0]+uint64_t(frame.strides[0])*frame.size.height();
        frame.colorMatrix=2; frame.colorRange=0; frame.chromaLocation=1;
        output.push_back(std::move(frame));
    }
    return output;
}
std::vector<QByteArray> VideoFilterStage::Impl::run(std::span<const QImage> frames, JobControl& control, bool planarRgb10) {
    if (frames.empty()) return {};
    auto& state = *this;
    const bool wide = frames.front().format() != QImage::Format_RGBA8888;
    auto arguments = state.arguments;
    arguments[arguments.indexOf("-pixel_format")+1] = planarRgb10 ? "gbrp10le" : wide ? "rgba64le" : "rgba";
    if (!state.grain.isEmpty() && state.processed)
        arguments[arguments.indexOf("-vf")+1].replace(state.grain,
            grainGraph(state.settings, state.firstFrame+state.processed, state.hdr, true));
    state.process = std::make_unique<PipeProcess>(Paths::instance().ffmpeg(), arguments);
    for (const auto& image : frames) {
        control.checkpoint();
        if (image.size() != state.input) throw std::runtime_error("Video filter input geometry changed.");
        if ((image.format() != QImage::Format_RGBA8888) != wide)
            throw std::runtime_error("Video filter input precision changed within a batch.");
        const auto data = planarRgb10 ? gbrp10Bytes(image) : rgbaBytes(image, wide);
        state.process->write(data.constData(), data.size(), control);
    }
    // CLI filters can retain their last input until EOF. Flush the graph before
    // demanding a complete batch; a persistent producer must own the upstream
    // iterator instead of waiting for a synchronous batch to provide lookahead.
    state.process->closeInput();
    std::vector<QByteArray> output;
    output.reserve(frames.size());
    const auto planeBytes = qsizetype(state.output.width())*state.output.height()*2;
    for (size_t index=0; index<frames.size(); ++index) {
        const auto bytes=state.outputFormat=="p010le" ? qsizetype(state.output.width())*state.output.height()*2+
            qsizetype((state.output.width()+1)/2)*4*((state.output.height()+1)/2)
            : state.outputFormat=="rgba" ? planeBytes*2 : planeBytes*3;
        auto raw = state.process->read(bytes, control);
        if (raw.isEmpty()) throw std::runtime_error("The video filter returned no output frame.");
        output.push_back(std::move(raw));
    }
    state.process->finish(control);
    state.process.reset();
    state.processed += frames.size();
    return output;
}

std::vector<QImage> sharpenFrames(std::span<const QImage> frames, const Settings& settings,
                                  JobControl& control, const QString& transfer) {
    if (frames.empty() || settings.cas_sharpness == 0)
        return {frames.begin(), frames.end()};
    auto output=filterFrames(frames, sharpeningGraph(settings, transfer,frames.front().depth()<=32), frames.front().size(), control,
                        settings.ffmpeg_device);
    for (size_t index=0;index<frames.size();++index) {
        const auto original=frames[index].convertToFormat(QImage::Format_RGBA64);
        output[index]=output[index].convertToFormat(QImage::Format_RGBA64);
        for (int y=0;y<original.height();++y) {
            control.checkpoint();
            const auto* source=reinterpret_cast<const QRgba64*>(original.constScanLine(y));
            auto* row=reinterpret_cast<QRgba64*>(output[index].scanLine(y));
            for (int x=0;x<original.width();++x) {
                row[x].setAlpha(source[x].alpha());
                if (!source[x].alpha()) row[x]=source[x];
            }
        }
        if (frames[index].depth()<=32) output[index]=output[index].convertToFormat(QImage::Format_RGBA8888);
    }
    return output;
}
std::vector<QImage> resizeFrames(std::span<const QImage> frames, QSize output, const QString& filter,
                                 JobControl& control, bool fit, const QString& device) {
    if (frames.empty() || frames.front().size() == output)
        return {frames.begin(), frames.end()};
    return filterFrames(frames, resizingGraph(frames.front().size(), output, filter, fit), output, control,
                        device);
}
std::vector<QImage> grainFrames(std::span<const QImage> frames, const Settings& settings, JobControl& control,
                                quint64 firstFrame, bool hdr, bool video) {
    if (frames.empty() || !settings.grain_amount)
        return {frames.begin(), frames.end()};
    std::vector<QImage> opaque;
    opaque.reserve(frames.size());
    for (const auto& pixels : frames) {
        auto image = pixels.convertToFormat(QImage::Format_RGBA64);
        for (int y = 0; y < image.height(); ++y) {
            control.checkpoint();
            auto* row = reinterpret_cast<QRgba64*>(image.scanLine(y));
            for (int x = 0; x < image.width(); ++x)
                row[x].setAlpha(65535);
        }
        opaque.push_back(pixels.depth()<=32 ? image.convertToFormat(QImage::Format_RGBA8888) : std::move(image));
    }
    auto result = filterFrames(opaque, grainGraph(settings, firstFrame, hdr, video), frames.front().size(),
                               control, settings.ffmpeg_device);
    for (size_t index = 0; index < frames.size(); ++index) {
        const auto source = frames[index].convertToFormat(QImage::Format_RGBA64);
        auto& image = result[index];
        image=image.convertToFormat(QImage::Format_RGBA64);
        for (int y = 0; y < image.height(); ++y) {
            control.checkpoint();
            auto* row = reinterpret_cast<QRgba64*>(image.scanLine(y));
            const auto* original = reinterpret_cast<const QRgba64*>(source.constScanLine(y));
            for (int x = 0; x < image.width(); ++x) {
                row[x].setAlpha(original[x].alpha());
                if (!original[x].alpha())
                    row[x] = original[x];
            }
        }
        if (frames[index].depth()<=32) image=image.convertToFormat(QImage::Format_RGBA8888);
    }
    return result;
}
QImage sharpenImage(const QImage& pixels, const Settings& settings, JobControl& control,
                    const QString& transfer) {
    return sharpenFrames({&pixels, 1}, settings, control, transfer).front();
}
QImage grainImage(const QImage& pixels, const Settings& settings, JobControl& control, quint64 frame,
                  bool hdr, bool video) {
    return grainFrames({&pixels, 1}, settings, control, frame, hdr, video).front();
}
QImage resizeImage(const QImage& pixels, QSize output, const QString& filter, JobControl& control, bool fit,
                   const QString& device) {
    return resizeFrames({&pixels, 1}, output, filter, control, fit, device).front();
}
} // namespace ve
