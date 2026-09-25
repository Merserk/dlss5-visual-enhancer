from __future__ import annotations

import atexit
import os
import struct
import tempfile
import threading
import time
import warnings as python_warnings
import zlib
from collections import OrderedDict
from pathlib import Path

import numpy as np
import cv2
import pillow_heif
import tifffile
from PIL import Image

from .models import ImageConversionOptions
from ...core.jobs import Cancelled
from ...core.paths import APP_TEMP
from ...core.render_metadata import (
    IMAGE_NOTE_FORMATS, MetadataNoteError, check_cancelled, embedding_warning,
    merge_render_note, record_embedding,
)

# Keep UI previews bounded in RAM. Older previews spill as already-downscaled PNGs
# so large batches never have to reopen and resize their full-resolution outputs.
_PREVIEW_CACHE_BYTES = 256 * 1024 * 1024
_preview_cache: OrderedDict[str, tuple[Image.Image, int]] = OrderedDict()
_preview_spill: dict[str, Path] = {}
_preview_cache_bytes = 0
_preview_cache_lock = threading.Lock()


def _rgba8_preview(rgba: np.ndarray) -> np.ndarray:
    if rgba.dtype == np.uint8:
        return rgba
    return np.rint(rgba.astype(np.float32) * (255.0 / 65535.0)).astype(np.uint8)


def _clear_preview_cache() -> None:
    global _preview_cache_bytes
    with _preview_cache_lock:
        while _preview_cache:
            _key, (image, _size) = _preview_cache.popitem()
            image.close()
        for path in _preview_spill.values():
            _delete_spill(path)
        _preview_spill.clear()
        _preview_cache_bytes = 0


def _delete_spill(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


atexit.register(_clear_preview_cache)


def take_image_preview(output_path: str | os.PathLike[str]) -> Image.Image | None:
    """Return and remove a pre-generated UI thumbnail for a rendered output."""
    global _preview_cache_bytes
    key = str(Path(output_path).resolve())
    with _preview_cache_lock:
        cached = _preview_cache.pop(key, None)
        spill = _preview_spill.pop(key, None)
        if cached is not None:
            image, size = cached
            _preview_cache_bytes -= size
            return image
    if spill is None:
        return None
    try:
        with Image.open(spill) as image:
            image.load()
            return image.copy()
    finally:
        _delete_spill(spill)


def _spill_preview(key: str, image: Image.Image) -> None:
    APP_TEMP.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(
        prefix="dlss5-preview-", suffix=".png", dir=APP_TEMP
    )
    os.close(handle)
    path = Path(raw_path)
    try:
        image.save(path, format="PNG", optimize=False, compress_level=1)
    except Exception:
        path.unlink(missing_ok=True)
        return
    previous = _preview_spill.pop(key, None)
    _delete_spill(previous)
    _preview_spill[key] = path


def make_image_preview(
    rgba: np.ndarray, output_format: str, has_transparency: bool | None = None,
) -> Image.Image:
    """Build the same gallery thumbnail used by a completed image render.

    The preview is derived directly from the processed RGBA pixels, before file
    encoding. JPEG is the one format-specific exception: production JPEG output
    composites transparency over white, so the gallery preview mirrors that.
    """
    image = Image.fromarray(_rgba8_preview(rgba), mode="RGBA")
    try:
        if output_format == "JPEG":
            if has_transparency is None:
                has_transparency = bool(np.any(rgba[..., 3] != 255))
            if has_transparency:
                preview = Image.new("RGBA", image.size, (255, 255, 255, 255))
                preview.alpha_composite(image)
            else:
                preview = image.copy()
        else:
            preview = image.copy()
        # Preview pixels are not production output; bilinear is dramatically
        # cheaper than Lanczos and is visually sufficient at gallery scale.
        preview.thumbnail((1200, 900), Image.Resampling.BILINEAR)
        return preview
    finally:
        image.close()


def save_full_size_image_preview(
    rgba: np.ndarray, output_format: str, has_transparency: bool | None = None,
) -> str:
    """Write processed preview pixels as a full-resolution PNG for the native viewer.

    JPEG preview semantics still composite transparent
    pixels over white, matching the production JPEG path.
    """
    APP_TEMP.mkdir(parents=True, exist_ok=True)
    handle, raw_path = tempfile.mkstemp(
        prefix="dlss5-fullsize-processed-", suffix=".png", dir=APP_TEMP
    )
    os.close(handle)
    path = Path(raw_path)
    source = Image.fromarray(_rgba8_preview(rgba), mode="RGBA")
    display = source
    try:
        if output_format == "JPEG":
            if has_transparency is None:
                has_transparency = bool(np.any(rgba[..., 3] != 255))
            if has_transparency:
                display = Image.new("RGBA", source.size, (255, 255, 255, 255))
                display.alpha_composite(source)
        display.save(path, format="PNG", optimize=False, compress_level=1)
        return str(path.resolve())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        if display is not source:
            display.close()
        source.close()


def _remember_image_preview(
    output_path: Path, rgba: np.ndarray, output_format: str,
    has_transparency: bool | None = None,
) -> None:
    global _preview_cache_bytes
    preview = make_image_preview(rgba, output_format, has_transparency)
    try:
        key = str(output_path.resolve())
        size = preview.width * preview.height * 4
        with _preview_cache_lock:
            previous = _preview_cache.pop(key, None)
            if previous is not None:
                old, old_size = previous
                _preview_cache_bytes -= old_size
                old.close()
            old_spill = _preview_spill.pop(key, None)
            _delete_spill(old_spill)
            _preview_cache[key] = (preview, size)
            _preview_cache_bytes += size
            preview = None
            while _preview_cache_bytes > _PREVIEW_CACHE_BYTES and _preview_cache:
                old_key, (old_image, old_size) = _preview_cache.popitem(last=False)
                _preview_cache_bytes -= old_size
                _spill_preview(old_key, old_image)
                old_image.close()
    finally:
        if preview is not None:
            preview.close()


def _encode_image(
    output: Path,
    rgba: np.ndarray,
    options: ImageConversionOptions,
    metadata: dict[str, object],
    *, generate_preview: bool = True, preview_path: Path | None = None,
    render_note: str | None = None, metadata_diagnostics: dict | None = None,
    controller=None, has_transparency: bool | None = None, timings: dict[str, float] | None = None,
) -> list[str]:
    started = time.monotonic()
    warnings = save_image(
        output, rgba, options, metadata, render_note=render_note,
        metadata_diagnostics=metadata_diagnostics, controller=controller,
        has_transparency=has_transparency, timings=timings,
    )
    if timings is not None:
        timings["encode"] = time.monotonic() - started
    try:
        if generate_preview:
            preview_started = time.monotonic()
            _remember_image_preview(
                preview_path or output, rgba, options.output_format, has_transparency
            )
            if timings is not None:
                timings["preview"] = time.monotonic() - preview_started
    except Exception:
        # A UI convenience must never invalidate an otherwise correct output.
        pass
    return warnings


def _metadata_save_args(metadata: dict[str, object], output_format: str) -> dict[str, object]:
    args: dict[str, object] = {}
    if metadata.get("icc_profile"):
        args["icc_profile"] = metadata["icc_profile"]
    if metadata.get("dpi"):
        args["dpi"] = metadata["dpi"]
    if metadata.get("exif") and output_format in {"JPEG", "WebP", "AVIF", "TIFF", "PNG"}:
        args["exif"] = metadata["exif"]
    if metadata.get("xmp") and output_format in {"WebP", "AVIF", "PNG", "TIFF"}:
        args["xmp"] = metadata["xmp"]
    return args


def _png_chunk(name: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))


def _save_high_image(path: Path, rgba: np.ndarray, output_format: str,
                     args: dict[str, object], quality: int, source_depth: int) -> None:
    if output_format == "PNG":
        bgra = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
        ok, encoded = cv2.imencode(".png", bgra, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError("16-bit PNG encoder failed")
        raw = encoded.tobytes()
        chunks: list[bytes] = []
        profile = args.get("icc_profile")
        if isinstance(profile, bytes) and profile:
            chunks.append(_png_chunk(b"iCCP", b"ICC Profile\0\0" + zlib.compress(profile)))
        exif = args.get("exif")
        if isinstance(exif, bytes) and exif:
            chunks.append(_png_chunk(b"eXIf", exif.removeprefix(b"Exif\0\0")))
        dpi = args.get("dpi")
        if isinstance(dpi, (tuple, list)) and len(dpi) >= 2:
            xppm = round(float(dpi[0]) / 0.0254)
            yppm = round(float(dpi[1]) / 0.0254)
            chunks.append(_png_chunk(b"pHYs", struct.pack(">IIB", xppm, yppm, 1)))
        xmp = args.get("xmp")
        if isinstance(xmp, bytes) and xmp:
            chunks.append(_png_chunk(b"iTXt", b"XML:com.adobe.xmp\0\0\0\0\0" + xmp))
        ihdr_end = 8 + 4 + 4 + 13 + 4
        path.write_bytes(raw[:ihdr_end] + b"".join(chunks) + raw[ihdr_end:])
    elif output_format == "TIFF":
        exif = Image.Exif()
        if isinstance(args.get("exif"), bytes):
            exif.load(args["exif"])
        extra = []
        for tag in (269, 271, 272, 305, 306, 315, 316, 33432):
            value = exif.get(tag)
            if isinstance(value, str) and value:
                extra.append((tag, "s", 0, value, False))
        xmp = args.get("xmp")
        if isinstance(xmp, bytes) and xmp:
            extra.append((700, "B", len(xmp), xmp, False))
        tifffile.imwrite(
            path, rgba, photometric="rgb", extrasamples=["unassalpha"],
            compression=None, metadata=None,
            description=exif.get(270),
            iccprofile=args.get("icc_profile") if isinstance(args.get("icc_profile"), bytes) else None,
            resolution=args.get("dpi") if isinstance(args.get("dpi"), tuple) else None,
            resolutionunit="INCH", extratags=extra,
        )
    elif output_format == "AVIF":
        image = pillow_heif.from_bytes("RGBA;16", (rgba.shape[1], rgba.shape[0]), rgba.tobytes())
        profile = args.get("icc_profile")
        if isinstance(profile, bytes) and profile:
            image[0].info["icc_profile"] = profile
        image.save(
            path, quality=int(quality), bit_depth=12 if source_depth > 10 else 10,
            **{key: args[key] for key in ("exif", "xmp") if key in args},
        )
    else:
        raise ValueError(f"Unsupported high-depth image output: {output_format}")


def _save_high_image_atomic(output: Path, rgba: np.ndarray, options: ImageConversionOptions,
                            metadata: dict[str, object], render_note: str | None,
                            metadata_diagnostics: dict | None, controller,
                            timings: dict[str, float] | None) -> list[str]:
    warnings: list[str] = []
    args = _metadata_save_args(metadata, options.output_format) if options.preserve_metadata else {}
    temporary = output.with_name(f".{output.stem}.{time.time_ns()}{output.suffix}")
    description = None
    note_requested = render_note is not None and options.output_format in IMAGE_NOTE_FORMATS
    try:
        check_cancelled(controller)
        if note_requested:
            try:
                args, description = _add_render_note(args, render_note)
            except (ValueError, TypeError, KeyError, SyntaxError, OSError) as exc:
                warnings.append(embedding_warning(metadata_diagnostics, exc))
                note_requested = False
        try:
            _save_high_image(temporary, rgba, options.output_format, args,
                             options.quality, int(metadata.get("source_bit_depth") or 16))
            _verify_image(temporary, (rgba.shape[1], rgba.shape[0]), description if note_requested else None)
        except (ValueError, TypeError, KeyError, SyntaxError, OSError, RuntimeError) as exc:
            if not note_requested:
                raise
            warnings.append(embedding_warning(metadata_diagnostics, exc))
            args.pop("exif", None)
            _save_high_image(temporary, rgba, options.output_format, args,
                             options.quality, int(metadata.get("source_bit_depth") or 16))
            _verify_image(temporary, (rgba.shape[1], rgba.shape[0]), None)
            note_requested = False
        check_cancelled(controller)
        os.replace(temporary, output)
        if note_requested:
            record_embedding(metadata_diagnostics, "embedded", field="EXIF.ImageDescription")
    finally:
        temporary.unlink(missing_ok=True)
    return warnings


def save_image(
    output: Path,
    rgba: np.ndarray,
    options: ImageConversionOptions,
    metadata: dict[str, object],
    *, render_note: str | None = None, metadata_diagnostics: dict | None = None,
    controller=None, has_transparency: bool | None = None, timings: dict[str, float] | None = None,
) -> list[str]:
    output_format = options.output_format
    if rgba.dtype == np.uint16 and output_format in {"PNG", "TIFF", "AVIF"}:
        return _save_high_image_atomic(output, rgba, options, metadata, render_note,
                                       metadata_diagnostics, controller, timings)
    if rgba.dtype == np.uint16:
        rgba = _rgba8_preview(rgba)
    source_image = Image.fromarray(rgba, mode="RGBA")
    image = source_image
    warnings: list[str] = []
    args = _metadata_save_args(metadata, output_format) if options.preserve_metadata else {}
    if output_format == "PNG":
        # PNG compression effort changes only file size/CPU time, never pixels.
        args.update(optimize=False, compress_level=1)
    elif output_format == "TIFF":
        # LZW is lossless and materially lighter on CPU than Deflate here.
        args.update(compression="tiff_lzw")
    elif output_format == "JPEG":
        if has_transparency is None:
            has_transparency = bool(np.any(rgba[..., 3] != 255))
        if has_transparency:
            background = Image.new("RGBA", source_image.size, (255, 255, 255, 255))
            background.alpha_composite(source_image)
            image = background.convert("RGB")
            background.close()
            warnings.append("Transparency was composited over white for JPEG output.")
        else:
            image = source_image.convert("RGB")
        args.update(
            quality=int(options.quality),
            subsampling=0,
            optimize=False,
            progressive=False,
        )
    elif output_format == "WebP":
        # Preserve the requested quality while avoiding maximum encoder effort.
        args.update(quality=int(options.quality), method=4)
    elif output_format == "AVIF":
        # Preserve the requested quality; speed controls encoder effort.
        args.update(quality=int(options.quality), speed=6)

    temporary = output.with_name(f".{output.stem}.{time.time_ns()}{output.suffix}")
    note_requested = render_note is not None and output_format in IMAGE_NOTE_FORMATS
    original_args = args.copy()
    description = None
    try:
        check_cancelled(controller)
        if note_requested:
            try:
                args, description = _add_render_note(args, render_note)
            except (ValueError, TypeError, KeyError, SyntaxError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno is not None:
                    raise
                warnings.append(embedding_warning(metadata_diagnostics, exc))
                note_requested = False
        elif render_note is not None:
            record_embedding(metadata_diagnostics, "skipped", reason="unsupported_format")
        elif metadata_diagnostics is not None and not metadata_diagnostics:
            record_embedding(metadata_diagnostics, "not_requested")

        try:
            check_cancelled(controller)
            image.save(temporary, format=output_format, **args)
            check_cancelled(controller)
            # Header/container validation deliberately avoids saved.load(), which
            # would decode every output pixel a second time immediately after save.
            if render_note is not None or metadata_diagnostics is not None:
                verify_started = time.monotonic()
                _verify_image(temporary, image.size, description if note_requested else None)
                if timings is not None:
                    timings["output_verification"] = timings.get("output_verification", 0.0) + time.monotonic() - verify_started
        except Cancelled:
            raise
        except (ValueError, TypeError, KeyError, SyntaxError, OSError, RuntimeError) as exc:
            # Disk/permission errors and cancellation are not metadata failures.
            if not note_requested or (isinstance(exc, OSError) and exc.errno is not None):
                raise
            check_cancelled(controller)
            warnings.append(embedding_warning(metadata_diagnostics, exc))
            image.save(temporary, format=output_format, **original_args)
            check_cancelled(controller)
            verify_started = time.monotonic()
            _verify_image(temporary, image.size, None)
            if timings is not None:
                timings["output_verification"] = timings.get("output_verification", 0.0) + time.monotonic() - verify_started
            note_requested = False
        check_cancelled(controller)
        os.replace(temporary, output)
        if note_requested:
            record_embedding(metadata_diagnostics, "embedded", field="EXIF.ImageDescription")
    finally:
        if image is not source_image:
            image.close()
        source_image.close()
        if temporary.exists():
            temporary.unlink()
    return warnings


def _add_render_note(args: dict, note: str) -> tuple[dict, str]:
    # EXIF may contain nested IFDs; Pillow preserves these through its Exif API.
    # Never alter the decoder's metadata object or its original byte payload.
    exif = Image.Exif()
    with python_warnings.catch_warnings(record=True) as caught:
        python_warnings.simplefilter("always")
        if args.get("exif"):
            exif.load(args["exif"])
        original_fields = _exif_fields(exif)
        description = merge_render_note(exif.get(270), note)
        exif[270] = description
        encoded = exif.tobytes()
        reread = Image.Exif()
        reread.load(encoded)
        if caught or reread.get(270) != description or _exif_fields(reread) != original_fields:
            raise MetadataNoteError("EXIF description could not be preserved exactly")
    return {**args, "exif": encoded}, description


def _exif_fields(exif: Image.Exif) -> dict:
    """Compare content, not relocated IFD offsets, before adding the note."""
    fields = {tag: value for tag, value in exif.items() if tag not in {270, 34665, 34853}}
    for tag in (34665, 34853):
        if tag in exif:
            nested = dict(exif.get_ifd(tag))
            if tag == 34665 and 40965 in nested:
                nested[40965] = dict(exif.get_ifd(40965))
            fields[tag] = nested
    return fields


def _verify_image(path: Path, size: tuple[int, int], description: str | None) -> None:
    # Image.open() parses the container header lazily. Accessing size and EXIF does
    # not decode the raster payload, which avoids a redundant full-image pass.
    with Image.open(path) as saved:
        if saved.size != size:
            raise MetadataNoteError("Saved image dimensions changed")
        if description is not None and saved.getexif().get(270) != description:
            raise MetadataNoteError("Saved image did not retain the settings note")
