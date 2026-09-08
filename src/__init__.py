"""Portable DLSS 5 Visual Enhancer; public processing APIs load on first use.

Importing a lightweight diagnostic or the early loading screen must not import
Gradio, PyAV, Pillow and every render pipeline before it can display anything.
"""
from importlib import import_module

_EXPORTS = {
    "probe_video": ".core.ffmpeg",
    **dict.fromkeys((
        "ImageBatchResult", "ImageConversionFailure", "ImageConversionOptions",
        "ImageConversionResult", "convert_image", "convert_images", "probe_image",
    ), ".neural_rendering.image"),
    **dict.fromkeys((
        "FrameInterpolationBatchResult", "FrameInterpolationCapabilities",
        "FrameInterpolationOptions", "FrameInterpolationResult",
        "interpolate_video", "interpolate_videos", "probe_frame_interpolation_capabilities",
    ), ".frame_interpolation"),
    **dict.fromkeys((
        "ConversionOptions", "ConversionResult", "VideoBatchResult",
        "VideoConversionFailure", "VideoConversionSuccess", "convert_video", "convert_videos",
    ), ".neural_rendering.video"),
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
