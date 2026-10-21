#pragma once
#include "configuration/settings.hpp"
#include "packet_stream.hpp"

namespace ve {
[[nodiscard]] TimeBase frameGenerationTimeBase(double sourceFps, const Settings& settings, bool fsr = false);
[[nodiscard]] double frameGenerationFrameRate(double sourceFps, const Settings& settings, bool fsr = false);
} // namespace ve
