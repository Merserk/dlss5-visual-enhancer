from . import processor as _processor
from .cuda_decode import install_cuda_decode

# Patch the feature-18 video processor once so every caller (single preview,
# full render and batch) gets the same optional FFmpeg CUDA/NVDEC input path.
install_cuda_decode(_processor)

from .batch import convert_videos
from .models import (
    ConversionOptions, ConversionResult, DLSS_MODEL_PRESETS, NR_PRESETS, NR_STYLES,
    UPSCALING_MODES, VideoBatchResult, VideoConversionFailure, VideoConversionSuccess,
)
from .sizing import (
    AUTO_BITRATE_DIVISORS, ENCODING_QUALITIES, UPSCALING_CHOICES,
    calculate_auto_bitrate_kbps, probe_video, resolve_encoding_quality,
    resolve_native_settings, resolve_output_size, resolve_upscaling_mode, validate_codec_container,
)

convert_video = _processor.convert_video

__all__ = [
    "AUTO_BITRATE_DIVISORS", "ConversionOptions", "ConversionResult", "DLSS_MODEL_PRESETS",
    "ENCODING_QUALITIES", "NR_PRESETS", "NR_STYLES", "UPSCALING_CHOICES", "UPSCALING_MODES",
    "VideoBatchResult", "VideoConversionFailure", "VideoConversionSuccess",
    "calculate_auto_bitrate_kbps", "convert_video", "convert_videos", "probe_video",
    "resolve_encoding_quality", "resolve_native_settings", "resolve_output_size",
    "resolve_upscaling_mode", "validate_codec_container",
]
