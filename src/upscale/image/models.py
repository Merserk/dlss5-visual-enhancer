from __future__ import annotations

import math
from dataclasses import dataclass, field, fields

from ..video.models import SCALE_FACTORS, SIZE_MODES, TEXTURE_LIMIT, VSR_QUALITIES, UpscaleOptions
from ...core.naming import validate_rename
from ...core.dlss_modes import UPSCALE_ENGINES, dlss_output_size, validate_dlss
from ...neural_rendering.image.models import IMAGE_FORMATS, IMAGE_EXTENSIONS


@dataclass(slots=True)
class ImageUpscaleOptions:
    engine: str = "RTX Video Super Resolution"
    dlss_mode: str = "Quality"
    dlss_preset: str = "Default"
    vsr_quality: int = 4
    size_mode: str = "Scale factor"
    scale_factor: float = 2.0
    width: int = 3840
    height: int = 2160
    aspect_lock: bool = True
    output_format: str = "PNG"
    quality: int = 95
    rename_mode: str = "Auto"
    custom_suffix: str = "_Upscale"
    ai_gpu_uuid: str = "auto"

    def validate(self, *, for_render: bool = True):
        if self.engine not in UPSCALE_ENGINES:
            raise ValueError(f"Unknown image upscale engine: {self.engine!r}.")
        validate_dlss(self.dlss_mode, self.dlss_preset)
        for name, low, high in (("vsr_quality", 1, 4), ("width", 1, TEXTURE_LIMIT),
                                ("height", 1, TEXTURE_LIMIT), ("quality", 1, 100)):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) or int(value) != value or not low <= value <= high):
                raise ValueError(f"{name} must be an integer from {low} to {high}.")
        if (isinstance(self.scale_factor, bool) or not isinstance(self.scale_factor, (int, float)) or
                not math.isfinite(self.scale_factor) or self.scale_factor < 1):
            raise ValueError("Scale factor must be a finite number of at least 1.")
        if self.size_mode not in SIZE_MODES or self.output_format not in IMAGE_FORMATS:
            raise ValueError("Unknown image sizing mode or output format.")
        if not isinstance(self.aspect_lock, bool):
            raise ValueError("Aspect lock must be on or off.")
        validate_rename(self.rename_mode, self.custom_suffix)

    def native_options(self):
        # Only frame processing settings cross into the existing worker protocol.
        return UpscaleOptions(vsr_quality=int(self.vsr_quality), ai_gpu_uuid=self.ai_gpu_uuid)


SETTING_FIELDS = tuple(f.name for f in fields(ImageUpscaleOptions) if f.name != "ai_gpu_uuid")


def options_from_settings(settings):
    return ImageUpscaleOptions(**{n: getattr(settings, "upscale_image_" + n) for n in SETTING_FIELDS},
                               ai_gpu_uuid=settings.ai_gpu_uuid)


def output_size(width, height, options):
    options.validate()
    if options.engine == "DLSS":
        return dlss_output_size(width, height, options.dlss_mode)
    if min(width, height) < 1 or max(width, height) > TEXTURE_LIMIT:
        raise ValueError("Input dimensions must be between 1 and 16384 pixels.")
    if options.size_mode == "Scale factor":
        w, h = width * options.scale_factor, height * options.scale_factor
    else:
        w = options.width
        h = w * height / width if options.aspect_lock else options.height
    if not math.isfinite(w) or not math.isfinite(h) or max(w, h) > TEXTURE_LIMIT:
        raise ValueError("Requested output exceeds D3D11's 16384-pixel texture dimension.")
    if w < width or h < height:
        raise ValueError("Upscale output must not be smaller than the source.")
    return math.ceil(w), math.ceil(h)


@dataclass(slots=True)
class ImageUpscaleResult:
    input_path: str
    output_path: str
    report_path: str
    output_width: int
    output_height: int
    elapsed_seconds: float
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImageUpscaleFailure:
    index: int
    input_path: str
    error: str
    cancelled: bool = False


@dataclass(slots=True)
class ImageUpscaleBatchResult:
    successes: list[ImageUpscaleResult]
    failures: list[ImageUpscaleFailure]
    cancelled: bool
    manifest_path: str
