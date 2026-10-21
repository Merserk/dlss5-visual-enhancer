#pragma once
#include "infrastructure/paths.hpp"
#include <QDir>
#include <stdexcept>
#include <vector>
#include <windows.h>

namespace ve {
// Resolve the supplied libav ABI rather than Qt Multimedia's private FFmpeg copy.
// Libraries remain loaded; frame, codec, I/O and hardware contexts have job owners.
class AvLibrary {
  public:
    template <class Function> [[nodiscard]] Function resolve(const char* name) const {
        for (const auto module : modules_)
            if (const auto symbol = GetProcAddress(module, name))
                return reinterpret_cast<Function>(symbol);
        throw std::runtime_error("Bundled media library is missing export " + std::string(name));
    }
    static AvLibrary& instance() {
        static AvLibrary library;
        return library;
    }

  private:
    AvLibrary() {
        const auto override=qEnvironmentVariable("VE_MEDIA_RUNTIME_DIRECTORY");
        QDir directory(override.isEmpty()?Paths::instance().at("bin/runtime/av"):override);
        for (const auto* name : {"avutil", "avcodec", "avformat", "swscale"}) {
            const auto files = directory.entryList({QString::fromLatin1(name) + "-*.dll"}, QDir::Files);
            if (files.size() != 1)
                throw std::runtime_error("Missing or ambiguous media library " + std::string(name));
            const auto path = QDir::toNativeSeparators(directory.filePath(files.front())).toStdWString();
            const auto module = LoadLibraryExW(path.c_str(), nullptr,
                LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
            if (!module)
                throw std::runtime_error("Cannot load media library " + std::string(name));
            modules_.push_back(module);
        }
    }
    std::vector<HMODULE> modules_;
};
} // namespace ve
