"""Deterministic, spatially correlated film grain rendered entirely on Vulkan.

The noise field is a variance-normalized Gaussian reconstruction of independent
Gaussian samples on a rotated lattice. It has no repeating texture, keeps its
strength when grain size changes, and uses integer hashing rather than the
precision-sensitive sin(dot(...)) random functions often used in grain effects.
MAIN is transfer-encoded RGB, so grain is applied in perceptual signal space
without changing the source transfer function (including PQ / HLG).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .filters import filter_array, shader_filter


GRAIN_RANGES = {
    "grain_amount": (0, 100),
    "grain_size": (0.5, 4.0),
    "grain_color": (0, 100),
    "grain_response": (0, 100),
    "grain_seed": (0, 65535),
}
GRAIN_FIELDS = (*GRAIN_RANGES, "grain_animated")


@dataclass(frozen=True, slots=True)
class GrainOptions:
    amount: int = 20
    size: float = 1.0
    color: int = 0
    response: int = 70
    seed: int = 0
    animated: bool = True

    @classmethod
    def from_settings(cls, settings, *, video: bool = True) -> GrainOptions:
        return cls(**{name.removeprefix("grain_"): getattr(settings, name)
                      for name in GRAIN_FIELDS if name != "grain_animated"},
                   animated=settings.grain_animated if video else False)

    def validate(self) -> GrainOptions:
        for name, (minimum, maximum) in GRAIN_RANGES.items():
            value = getattr(self, name.removeprefix("grain_"))
            expected = (int, float) if name == "grain_size" else (int,)
            if (isinstance(value, bool) or not isinstance(value, expected)
                    or not math.isfinite(value) or not minimum <= value <= maximum):
                raise ValueError(f"{name} must be {'a number' if name == 'grain_size' else 'an integer'} "
                                 f"from {minimum} to {maximum}.")
        if not isinstance(self.animated, bool):
            raise ValueError("grain_animated must be a boolean.")
        return self


def grain_filter(options: GrainOptions, *, hdr: bool = False, frame_offset: int = 0) -> str:
    """Build one persistent shader pass; frame is libplacebo's execution counter.

    Static grain never consumes frame/random state. Video hashes the counter
    into the seed so every delivered frame receives a fresh, reproducible field.
    HDR uses a smaller signal-space amplitude to accommodate PQ/HLG encoding.
    """
    options.validate()
    if not options.amount:
        return ""
    temporal = "uint(frame)" if options.animated else "0u"
    if options.animated and frame_offset:
        temporal = f"(uint(frame) + {int(frame_offset) & 0xffffffff}u)"
    # Generate only the components actually needed by the selected color mix.
    kind = "vec4" if options.color else "float"
    uint_kind = "uvec4" if options.color else "uint"
    salts = "uvec4(0u, 0x68bc21ebu, 0x02e5be93u, 0x967a889bu)" if options.color else "0u"
    source = f"""//!HOOK MAIN
//!BIND HOOKED
//!DESC Film grain (Gaussian, variance normalized)
uint grain_hash(uint x) {{
    x ^= x >> 16; x *= 0x7feb352du;
    x ^= x >> 15; x *= 0x846ca68bu;
    return x ^ (x >> 16);
}}
{uint_kind} grain_hashv({uint_kind} x) {{
    x ^= x >> 16; x *= 0x7feb352du;
    x ^= x >> 15; x *= 0x846ca68bu;
    return x ^ (x >> 16);
}}
{kind} grain_normal(ivec2 p, uint tick) {{
    uint base = grain_hash(uint(p.x)) ^ grain_hash(uint(p.y) + 0x9e3779b9u)
              ^ grain_hash({options.seed}u ^ grain_hash(tick));
    {uint_kind} h = {uint_kind}(base) + {salts};
    {kind} u = ({kind}(grain_hashv(h) >> 8) + 0.5) / 16777216.0;
    {kind} v = ({kind}(grain_hashv(h ^ 0xa511e9b3u) >> 8) + 0.5) / 16777216.0;
    return sqrt(-2.0 * log(u)) * cos(6.28318530718 * v);
}}
{kind} grain_field(vec2 p, uint tick) {{
    // Rotation avoids a visible horizontal / vertical grid at larger sizes.
    p = mat2(0.8, 0.6, -0.6, 0.8) * p / {options.size:.8f};
    ivec2 base = ivec2(floor(p));
    {kind} sum = {kind}(0.0); float variance = 0.0;
    for (int y = -1; y <= 2; y++) for (int x = -1; x <= 2; x++) {{
        ivec2 q = base + ivec2(x, y);
        vec2 d = p - vec2(q);
        float w = exp(-dot(d, d) / 0.605);
        sum += w * grain_normal(q, tick);
        variance += w * w;
    }}
    return sum * inversesqrt(max(variance, 1e-12));
}}
vec4 hook() {{
    vec4 c = HOOKED_tex(HOOKED_pos);
    if (c.a <= 0.0) return c;
    {kind} n = grain_field(floor(HOOKED_pos * HOOKED_size) + 0.5, {temporal});
"""
    if options.color:
        chroma = options.color / 100.0
        source += f"    vec3 noise = {math.sqrt(1.0 - chroma * chroma):.8f} * vec3(n.x) + {chroma:.8f} * n.yzw;\n"
    else:
        source += "    vec3 noise = vec3(n);\n"
    source += f"""    vec3 rgb = clamp(c.rgb, 0.0, 1.0);
    float luma = dot(rgb, vec3({"0.2627, 0.6780, 0.0593" if hdr else "0.2126, 0.7152, 0.0722"}));
    float envelope = mix(1.0, sqrt(max(4.0 * luma * (1.0 - luma), 0.0)), {options.response / 100.0:.8f});
    vec3 delta = noise * {options.amount / 100.0 * (0.025 if hdr else 0.06):.8f} * envelope;
    // Symmetric limiting prevents clipping from lifting blacks or lowering
    // whites. Monochrome grain keeps one common delta across RGB channels.
    vec3 headroom = min(rgb, 1.0 - rgb);
"""
    if not options.color:
        source += "    headroom = vec3(min(headroom.r, min(headroom.g, headroom.b)));\n"
    source += "    return vec4(rgb + clamp(delta, -headroom, headroom), c.a);\n}\n"
    return shader_filter(source)


def grain_rgba(pixels: np.ndarray, options: GrainOptions, *, controller=None) -> np.ndarray:
    """Preserve 8/16-bit image precision, exact alpha and hidden RGB samples."""
    options.validate()
    if pixels.ndim != 3 or pixels.shape[2] != 4 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("Grain requires uint8/uint16 RGBA pixels.")
    if not options.amount:
        return pixels.copy()
    # Process straight RGB as opaque: alpha never influences color conversion
    # or noise synthesis, including along partially transparent image edges.
    opaque = pixels.copy()
    opaque[..., 3] = np.iinfo(pixels.dtype).max
    result = filter_array(opaque, grain_filter(replace(options, animated=False)), controller=controller)
    result[..., 3] = pixels[..., 3]
    result[pixels[..., 3] == 0] = pixels[pixels[..., 3] == 0]
    return result
