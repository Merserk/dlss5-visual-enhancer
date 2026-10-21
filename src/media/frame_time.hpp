#pragma once
#include <QtCore/qnumeric.h>
#include <compare>
#include <numeric>
#include <optional>
#include <utility>
#include <stdexcept>
#include <cstdint>

namespace ve {
// Exact nonnegative presentation time, independent of the delivery clock.
class FrameTime {
  public:
    FrameTime(std::int64_t numerator = 0, std::int64_t denominator = 1) {
        if (numerator < 0 || denominator <= 0) throw std::runtime_error("Invalid presentation time.");
        const auto common = std::gcd(numerator, denominator);
        numerator_ = numerator / common; denominator_ = denominator / common;
    }
    [[nodiscard]] double seconds() const { return double(numerator_) / double(denominator_); }
    [[nodiscard]] FrameTime roundedTo(const FrameTime& interval) const {
        return interval.fraction(ticks(interval.numerator_,interval.denominator_),1);
    }
    [[nodiscard]] std::int64_t ceilTicks(std::int64_t clockNumerator,std::int64_t clockDenominator) const {
        const auto value=fraction(clockDenominator,clockNumerator);auto result=value.numerator_/value.denominator_;
        if(value.numerator_%value.denominator_&&qAddOverflow(result,std::int64_t(1),&result))fail();return result;
    }
    [[nodiscard]] std::int64_t ticks(std::int64_t clockNumerator,std::int64_t clockDenominator) const {
        const auto value=fraction(clockDenominator,clockNumerator);
        const auto whole=value.numerator_/value.denominator_,remainder=value.numerator_%value.denominator_;
        std::int64_t result=whole;
        if(remainder>=value.denominator_/2+value.denominator_%2&&qAddOverflow(whole,std::int64_t(1),&result))fail();
        return result;
    }
    [[nodiscard]] std::strong_ordering operator<=>(const FrameTime& other) const {
        auto a = numerator_, b = other.numerator_, ad = denominator_, bd = other.denominator_;
        bool reverse = false;
        for (;;) {
            const auto order = (a/ad) <=> (b/bd);
            if (order != 0) return reverse ? 0 <=> order : order;
            a %= ad; b %= bd;
            if (!a || !b) {
                const auto ending = a <=> b;
                return reverse ? 0 <=> ending : ending;
            }
            std::swap(a, ad); std::swap(b, bd); reverse = !reverse;
        }
    }
    [[nodiscard]] bool operator==(const FrameTime&) const = default;
    [[nodiscard]] FrameTime operator+(const FrameTime& other) const {
        const auto common = std::gcd(denominator_, other.denominator_);
        std::int64_t sum;
        if (qAddOverflow(multiply(numerator_, other.denominator_/common),
                         multiply(other.numerator_, denominator_/common), &sum)) fail();
        return {sum, multiply(denominator_, other.denominator_/common)};
    }
    [[nodiscard]] FrameTime operator-(const FrameTime& other) const {
        if (*this < other) throw std::runtime_error("Presentation timestamps moved backwards.");
        const auto common = std::gcd(denominator_, other.denominator_);
        return {multiply(numerator_, other.denominator_/common) - multiply(other.numerator_, denominator_/common),
                multiply(denominator_, other.denominator_/common)};
    }
    [[nodiscard]] FrameTime fraction(std::int64_t numerator, std::int64_t denominator) const {
        if (numerator < 0 || denominator <= 0) throw std::runtime_error("Invalid time fraction.");
        const auto first = std::gcd(numerator_, denominator), second = std::gcd(numerator, denominator_);
        return {multiply(numerator_/first, numerator/second), multiply(denominator_/second, denominator/first)};
    }
  private:
    [[noreturn]] static void fail() { throw std::runtime_error("Presentation time exceeds the supported clock range."); }
    static std::int64_t multiply(std::int64_t left, std::int64_t right) {
        std::int64_t result; if (qMulOverflow(left, right, &result)) fail(); return result;
    }
    std::int64_t numerator_ = 0, denominator_ = 1;
};

template<class Frame, class Emit> class NearestFrameWriter {
  public:
    NearestFrameWriter(std::int64_t count, FrameTime interval, Emit write,FrameTime offset={})
        : count_(count), interval_(interval), offset_(offset), emit_(std::move(write)) {}
    void push(Frame frame) {
        if (previous_) {
            bool discontinuity=false;
            if constexpr(requires {frame.segment;})discontinuity=previous_->segment!=frame.segment;
            // A cut belongs at its source presentation time. Nearest-neighbour
            // selection across segments would otherwise move it half a source
            // interval early, despite correctly suppressing synthesized pixels.
            const auto midpoint = discontinuity?frame.time:(previous_->time + frame.time).fraction(1, 2);
            while (index_ < count_) {
                const auto target = offset_+interval_.fraction(index_, 1);
                if (target > midpoint) break;
                emit_(target == midpoint && (discontinuity||tieLate_) ? frame : *previous_, index_++);
                if (target == midpoint&&!discontinuity) tieLate_ = !tieLate_;
            }
        }
        previous_ = std::move(frame);
    }
    void finish() {
        if (!previous_) throw std::runtime_error("No frames are available for frame-rate conversion.");
        while (index_ < count_) emit_(*previous_, index_++);
    }
    void finish(std::int64_t count) {
        if (count < index_) throw std::runtime_error("Frame-rate conversion exceeded the source duration.");
        count_ = count; finish();
    }
    [[nodiscard]] std::int64_t written() const { return index_; }
  private:
    std::int64_t count_, index_ = 0;
    FrameTime interval_;
    FrameTime offset_;
    Emit emit_;
    std::optional<Frame> previous_;
    bool tieLate_ = false;
};
} // namespace ve
