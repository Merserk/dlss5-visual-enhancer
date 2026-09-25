"""File-backed sharpening stage with FidelityFX CAS and NVIDIA NVSharpen.

The CAS core below is a CPU implementation of the default no-scaling path in
``native (dev)/FidelityFX-SDK-v1.1.4/sdk/include/FidelityFX/gpu/cas/ffx_cas.h``.
It uses the SDK's sharpness constant, cross-neighbour soft min/max and green
channel weight. The SDK's D3D12/Vulkan dispatcher is designed for a GPU render
graph; this file-backed stage applies that pixel math to decoded RGB frames
and blends the result by the user-facing percentage. NVIDIA NVSharpen is
implemented separately in ``nis_sharpening.py`` and selected here at runtime.
"""

# CAS algorithm derived from FidelityFX SDK 1.1.4.
# Copyright (C) 2024 Advanced Micro Devices, Inc.
# See AMD-FidelityFX-CAS-LICENSE.txt in this directory.

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
import numpy as np

from ..core import ffmpeg
from ..core.jobs import Cancelled, JobController
from ..neural_rendering.image.decoder import decode_image
from ..neural_rendering.image.encoder import _encode_image
from ..neural_rendering.image.models import ImageConversionOptions
from ..settings.models import UISettings
from .nis_sharpening import sharpen_rgb as nis_sharpen_rgb


def _linearize(value: np.ndarray, transfer: str) -> np.ndarray:
    if transfer == "srgb":
        return np.where(value <= 0.04045, value / 12.92,
                        ((value + 0.055) / 1.055) ** 2.4)
    if transfer == "bt709":
        return np.where(value < 0.081, value / 4.5,
                        ((value + 0.099) / 1.099) ** (1.0 / 0.45))
    return value


def _encode_transfer(value: np.ndarray, transfer: str) -> np.ndarray:
    if transfer == "srgb":
        return np.where(value <= 0.0031308, value * 12.92,
                        1.055 * np.power(value, 1.0 / 2.4) - 0.055)
    if transfer == "bt709":
        return np.where(value < 0.018, value * 4.5,
                        1.099 * np.power(value, 0.45) - 0.099)
    return value


def sharpen_rgb(pixels: np.ndarray, sharpness: int, *, transfer: str = "srgb",
                alpha: np.ndarray | None = None,
                controller: JobController | None = None) -> np.ndarray:
    """Apply the SDK's sharpen-only CAS filter to 8/16-bit RGB pixels.

    0% is an application-level bypass. In the SDK, sharpness=0 still applies
    its baseline filter, so the filtered result is blended by the UI strength
    for a smooth transition from an untouched input to SDK maximum sharpness.
    Alpha is not filtered. Semi-transparent pixels and their opaque neighbours
    are left alone to keep hidden RGB from causing edge halos.
    """
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("CAS requires an 8-bit or 16-bit RGB image.")
    if isinstance(sharpness, bool) or not isinstance(sharpness, int) or not 0 <= sharpness <= 100:
        raise ValueError("CAS sharpness must be an integer from 0 to 100.")
    if alpha is not None and (alpha.shape != pixels.shape[:2] or alpha.dtype != pixels.dtype):
        raise ValueError("CAS alpha must match the RGB pixels.")
    if transfer not in {"srgb", "bt709", "linear"}:
        raise ValueError("Unsupported CAS transfer function.")
    if sharpness == 0 or min(pixels.shape[:2]) < 2:
        return pixels.copy()

    height, width = pixels.shape[:2]
    maximum = float(np.iinfo(pixels.dtype).max)
    mix = sharpness / 100.0
    peak = -1.0 / (8.0 - 3.0 * sharpness / 100.0)  # ffxCasSetup()
    result = np.empty_like(pixels)
    tile = 256  # Keep 4K/16-bit intermediates bounded while retaining a 1px halo.

    for y in range(0, height, tile):
        for x in range(0, width, tile):
            if controller is not None and controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            bottom, right = min(height, y + tile), min(width, x + tile)
            y0, y1 = max(0, y - 1), min(height, bottom + 1)
            x0, x1 = max(0, x - 1), min(width, right + 1)
            border = ((int(y == 0), int(bottom == height)),
                      (int(x == 0), int(right == width)))
            block = np.pad(pixels[y0:y1, x0:x1], (*border, (0, 0)), mode="edge")
            block = block.astype(np.float32) / maximum
            block = _linearize(block, transfer)
            b = block[:-2, 1:-1]
            d = block[1:-1, :-2]
            e = block[1:-1, 1:-1]
            f = block[1:-1, 2:]
            h = block[2:, 1:-1]

            minimum = np.minimum(np.minimum(np.minimum(d, e), f), np.minimum(b, h))
            maximum_rgb = np.maximum(np.maximum(np.maximum(d, e), f), np.maximum(b, h))
            # The SDK computes each channel's amplification, then uses only
            # green's weight for all output channels.
            green_min = minimum[..., 1]
            green_max = maximum_rgb[..., 1]
            amplification = np.sqrt(np.clip(
                np.minimum(green_min, 1.0 - green_max) /
                np.maximum(green_max, np.finfo(np.float32).tiny), 0.0, 1.0))
            weight = amplification * peak
            if alpha is not None:
                a = np.pad(alpha[y0:y1, x0:x1], border, mode="edge")
                fully_opaque = ((a[1:-1, 1:-1] == maximum) &
                                (a[:-2, 1:-1] == maximum) &
                                (a[1:-1, :-2] == maximum) &
                                (a[1:-1, 2:] == maximum) &
                                (a[2:, 1:-1] == maximum))
                weight = np.where(fully_opaque, weight, 0.0)
            filtered = np.clip((e + weight[..., None] * (b + d + f + h)) /
                               (1.0 + 4.0 * weight[..., None]), 0.0, 1.0)
            filtered = e + mix * (filtered - e)
            filtered = np.clip(_encode_transfer(filtered, transfer), 0.0, 1.0)
            result[y:bottom, x:right] = np.rint(filtered * maximum).astype(pixels.dtype)
    return result


def _selected_sharpen(pixels: np.ndarray, settings: UISettings, *, transfer: str,
                      alpha: np.ndarray | None = None,
                      controller: JobController | None = None) -> np.ndarray:
    if settings.sharpening_method == "NVIDIA NIS":
        return nis_sharpen_rgb(pixels, settings.cas_sharpness,
                               alpha=alpha, controller=controller)
    if settings.sharpening_method == "AMD CAS":
        return sharpen_rgb(pixels, settings.cas_sharpness, transfer=transfer,
                           alpha=alpha, controller=controller)
    raise ValueError(f"Unknown sharpening method: {settings.sharpening_method!r}.")


def sharpen_image(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable) -> Path:
    if settings.cas_sharpness == 0:
        progress(1.0, "Sharpening complete")
        return source
    decoded = decode_image(source)
    progress(0.1, "Sharpening image")
    rgba = decoded.rgba
    rgba[..., :3] = _selected_sharpen(rgba[..., :3], settings, transfer="srgb",
                                      alpha=decoded.alpha, controller=controller)
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    output = directory / "sharpened.tiff"
    progress(0.85, "Saving sharpened image")
    _encode_image(output, rgba,
                  ImageConversionOptions(output_format="TIFF", preserve_metadata=False),
                  {}, generate_preview=False, controller=controller,
                  has_transparency=decoded.alpha is not None)
    progress(1.0, "Sharpening complete")
    return output


def sharpen_video(source: Path, settings: UISettings, directory: Path,
                  controller: JobController, progress: Callable) -> Path:
    if settings.cas_sharpness == 0:
        progress(1.0, "Sharpening complete")
        return source
    metadata = ffmpeg.probe_video(source, count_mode="metadata", controller=controller)
    if metadata.get("hdr"):
        raise ValueError("Sharpening requires SDR video at this position. Move it before HDR conversion.")
    transfer = {"iec61966-2-1": "srgb", "linear": "linear"}.get(
        metadata.get("color_transfer"), "bt709")
    output = directory / "sharpened.mkv"
    input_container = av.open(str(source))
    output_container = None
    try:
        input_stream = input_container.streams.video[0]
        input_stream.thread_type = "AUTO"
        rate = input_stream.average_rate or Fraction(30, 1)
        time_base = input_stream.time_base or Fraction(1, max(1, round(float(rate))))
        output_container = av.open(str(output), mode="w")
        output_stream = output_container.add_stream("ffv1", rate=rate)
        output_stream.width = input_stream.width
        output_stream.height = input_stream.height
        output_stream.pix_fmt = "gbrp10le"
        if input_stream.sample_aspect_ratio is not None:
            output_stream.sample_aspect_ratio = input_stream.sample_aspect_ratio
        output_stream.time_base = time_base
        output_stream.codec_context.time_base = time_base
        output_stream.codec_context.colorspace = 0  # GBR
        output_stream.codec_context.color_range = 2  # full range
        output_stream.codec_context.color_primaries = input_stream.codec_context.color_primaries or 1
        output_stream.codec_context.color_trc = input_stream.codec_context.color_trc or 1
        output_stream.metadata.update(input_stream.metadata)
        output_stream.codec_context.open()
        estimated = int(metadata.get("frames") or 0)
        if estimated <= 0:
            estimated = max(1, round(float(metadata.get("duration") or 0) * float(rate)))
        count = 0
        last_pts: int | None = None
        frame_step = max(1, round(Fraction(1, 1) / rate / time_base))
        for frame in input_container.decode(input_stream):
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            if (frame.width, frame.height) != (output_stream.width, output_stream.height):
                raise ValueError("Video dimensions changed during sharpening.")
            rgb = frame.to_ndarray(format="rgb48le")
            filtered = _selected_sharpen(rgb, settings, transfer=transfer,
                                         controller=controller)
            processed = av.VideoFrame.from_ndarray(filtered, format="rgb48le")
            processed = processed.reformat(format="gbrp10le")
            pts = (round(Fraction(frame.pts) * (frame.time_base or time_base) / time_base)
                   if frame.pts is not None else (last_pts + frame_step if last_pts is not None else 0))
            if last_pts is not None and pts <= last_pts:
                raise ValueError("Source timestamps are not strictly increasing; Sharpening cannot preserve this video timing.")
            processed.pts = pts
            processed.time_base = time_base
            processed.color_primaries = output_stream.codec_context.color_primaries
            processed.color_trc = output_stream.codec_context.color_trc
            processed.colorspace = 0
            processed.color_range = 2
            for packet in output_stream.encode(processed):
                output_container.mux(packet)
            last_pts = pts
            count += 1
            if count == 1 or count % 12 == 0:
                progress(min(0.95, count / estimated), "Sharpening video")
        if not count:
            raise ValueError("The input contains no decodable video frames.")
        for packet in output_stream.encode():
            output_container.mux(packet)
    finally:
        if output_container is not None:
            output_container.close()
        input_container.close()
    progress(1.0, "Sharpening complete")
    return output
