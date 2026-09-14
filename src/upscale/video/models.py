from __future__ import annotations

import math
from dataclasses import dataclass, field, fields
from fractions import Fraction

from ...core.ffmpeg import CODEC_CHOICES, ENCODING_QUALITIES, container_for_codec, hdr_mode_supported, validate_codec_container
from ...core.naming import validate_rename

VSR_QUALITIES = [("1 - Low", 1), ("2 - Medium", 2), ("3 - High", 3), ("4 - Ultra", 4)]
SCALE_FACTORS = [("1×", 1.0), ("1.5×", 1.5), ("2×", 2.0), ("3×", 3.0), ("4×", 4.0)]
SIZE_MODES = ("Scale factor", "Custom dimensions")
HDR_PRECISIONS = ("Packed 10-bit", "FP16")
HDR_PRECISION_CHOICES = [("Packed 10-bit", "Packed 10-bit"), ("Packed 10-bit (FP16)", "FP16")]
TEXTURE_LIMIT = 16384


@dataclass(slots=True)
class UpscaleOptions:
    vsr_enabled: bool = True
    vsr_quality: int = 4
    size_mode: str = "Scale factor"
    scale_factor: float = 2.0
    width: int = 3840
    height: int = 2160
    aspect_lock: bool = True
    hdr_enabled: bool = False
    hdr_contrast: int = 100
    hdr_saturation: int = 100
    hdr_middle_gray: int = 50
    hdr_peak_luminance: int = 1000
    hdr_precision: str = "Packed 10-bit"
    codec: str = "H.265 (NVIDIA NVENC)"
    container: str = "MKV"
    quality: str = "Auto (Default)"
    rename_mode: str = "Auto"
    custom_suffix: str = "_Upscale"
    ai_gpu_uuid: str = "auto"
    video_gpu_uuid: str = "auto"
    preview_seconds: float | None = None
    preview_frames: int | None = None

    def validate(self, *, for_render: bool = True) -> None:
        for name in ("vsr_enabled", "hdr_enabled", "aspect_lock"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be on or off.")
        for name, low, high in (
            ("vsr_quality", 0, 4), ("width", 2, TEXTURE_LIMIT), ("height", 2, TEXTURE_LIMIT),
            ("hdr_contrast", 0, 200), ("hdr_saturation", 0, 200),
            ("hdr_middle_gray", 10, 100), ("hdr_peak_luminance", 400, 2000),
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or int(v) != v or not low <= v <= high:
                raise ValueError(f"{name.replace('_', ' ')} must be an integer from {low} to {high}.")
        if isinstance(self.scale_factor, bool) or not isinstance(self.scale_factor, (int, float)) or not math.isfinite(self.scale_factor) or self.scale_factor < 1:
            raise ValueError("Scale factor must be a finite number of at least 1.")
        for value, choices, name in ((self.size_mode, SIZE_MODES, "size mode"), (self.hdr_precision, HDR_PRECISIONS, "HDR precision"),
                                      (self.codec, CODEC_CHOICES, "codec"), (self.quality, ENCODING_QUALITIES, "encoding quality")):
            if value not in choices:
                raise ValueError(f"Unknown {name}: {value!r}.")
        if self.container not in ("MP4", "MKV", "MOV"):
            raise ValueError("Unknown output container.")
        if for_render:
            validate_codec_container(self.codec, self.container)
        validate_rename(self.rename_mode, self.custom_suffix)
        if for_render and not (self.vsr_enabled or self.hdr_enabled):
            raise ValueError("Enable RTX Video Super Resolution or RTX Video HDR.")
        if self.hdr_enabled and not hdr_mode_supported(self.codec):
            raise ValueError("RTX Video HDR requires H.265, AV1, or ProRes. H.264 cannot store this HDR output.")
        if self.preview_frames is not None and (isinstance(self.preview_frames, bool) or
                                               not isinstance(self.preview_frames, (int, float)) or
                                               not math.isfinite(self.preview_frames) or
                                               int(self.preview_frames) != self.preview_frames or self.preview_frames < 1):
            raise ValueError("Preview frame count must be a positive integer.")
        if self.preview_seconds is not None and (isinstance(self.preview_seconds, bool) or
                                                not isinstance(self.preview_seconds, (int, float)) or
                                                not math.isfinite(self.preview_seconds) or self.preview_seconds <= 0):
            raise ValueError("Preview duration must be positive and finite.")
        if self.preview_seconds is not None and self.preview_frames is not None:
            raise ValueError("Select one preview limit.")


SETTING_FIELDS = tuple(f.name for f in fields(UpscaleOptions) if f.name not in {
    "ai_gpu_uuid", "video_gpu_uuid", "preview_seconds", "preview_frames",
})


def options_from_settings(settings) -> UpscaleOptions:
    values = {name: getattr(settings, "upscale_" + name) for name in SETTING_FIELDS}
    values["container"] = container_for_codec(values["codec"])
    return UpscaleOptions(**values,
                          ai_gpu_uuid=settings.ai_gpu_uuid, video_gpu_uuid=settings.video_gpu_uuid)


def output_size(width: int, height: int, options: UpscaleOptions, sar: Fraction = Fraction(1)) -> tuple[int, int, str]:
    """Return square-pixel output dimensions; width drives a locked custom size."""
    options.validate()
    display_width = width * float(sar)
    if not options.vsr_enabled:
        w, h = display_width, float(height)
    elif options.size_mode == "Scale factor":
        w, h = display_width * options.scale_factor, height * options.scale_factor
    else:
        w = float(options.width)
        h = w * height / display_width if options.aspect_lock else float(options.height)
    if not math.isfinite(w) or not math.isfinite(h) or max(w, h) > TEXTURE_LIMIT:
        raise ValueError("Requested output exceeds D3D11's 16384-pixel texture dimension. Choose a smaller size.")
    # All offered delivery codecs accept even dimensions. Never round below source.
    ow, oh = max(2, math.ceil(w / 2) * 2), max(2, math.ceil(h / 2) * 2)
    if options.vsr_enabled and (ow < math.ceil(display_width) or oh < height):
        raise ValueError("Upscale output must not be smaller than the source. Use 1× for native-resolution enhancement.")
    rounding = " (rounded up to even dimensions for encoding)" if (ow != w or oh != h) else ""
    return ow, oh, rounding


@dataclass(slots=True)
class UpscaleCapabilities:
    gpu: dict
    luid: str
    vsr: dict
    hdr: dict
    sdk_version: str = "1.1.0"
    worker_version: str = "1.0.0"
    bridge_version: str = ""
    bridge_status: dict = field(default_factory=dict)


@dataclass(slots=True)
class UpscaleResult:
    output_path: str
    report_path: str
    frames: int
    output_width: int
    output_height: int
    hdr: bool
    elapsed_seconds: float
    bridge_version: str = ""
    memory_path: str = "host_staging"
    decode_backend: str = "software"
    encode_backend: str = ""
    timings: dict = field(default_factory=dict)
    bridge_status: dict = field(default_factory=dict)


@dataclass(slots=True)
class UpscaleSuccess:
    index: int
    input_path: str
    result: UpscaleResult


@dataclass(slots=True)
class UpscaleFailure:
    index: int
    input_path: str
    error: str
    cancelled: bool = False


@dataclass(slots=True)
class UpscaleBatchResult:
    successes: list[UpscaleSuccess]
    failures: list[UpscaleFailure]
    cancelled: bool
    manifest_path: str
