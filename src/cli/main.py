"""Small CLI surface over the application's existing processing workflow.

Only the standard library is imported at startup. Help and version work without
initializing Qt, GPU engines, logs, or application settings.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
import glob
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
import traceback

from . import VERSION
from .progress import ConsoleProgress

IMAGE_FORMATS = {"png": "PNG", "jpg": "JPEG", "jpeg": "JPEG", "webp": "WebP",
                 "avif": "AVIF", "tif": "TIFF", "tiff": "TIFF"}
VIDEO_CODECS = {"h264": "H.264", "hevc": "H.265", "av1": "AV1",
                "prores": "ProRes HQ", "ffv1": "FFV1 Lossless RGB 10-bit"}
QUALITIES = {"auto": "Auto (Default)", "good": "Good", "best": "Best", "max": "Max"}
DLSS_MODES = {"dlaa": "DLAA", "quality": "Quality", "balanced": "Balanced",
              "performance": "Performance", "ultra-performance": "Ultra Performance"}

class UsageError(ValueError):
    """Invalid arguments or configuration; exit status 2."""

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)

def finite_float(value):
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number") from exc
    if not math.isfinite(result):
        raise argparse.ArgumentTypeError("expected a finite number")
    return result

def dimensions(value):
    match = re.fullmatch(r"(\d+)[xX](\d+)", value)
    if not match or not all(2 <= int(v) <= 16384 for v in match.groups()):
        raise argparse.ArgumentTypeError("use WIDTHxHEIGHT, with each dimension from 2 to 16384")
    return tuple(int(v) for v in match.groups())

def build_parser():
    parser = Parser(prog="VE_CLI.exe", description="Visual Enhancer for images and videos, from your terminal.",
                    epilog="Run VE_CLI.exe COMMAND --help for options. Advanced settings: export a JSON preset from the app.")
    parser.add_argument("--version", action="version", version=f"Visual Enhancer CLI {VERSION}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    descriptions = {
        "render": "Enhance images or videos with Neural Rendering or a GUI pipeline preset.",
        "upscale": "Upscale images or videos with RTX Super Resolution or DLSS.",
        "interpolate": "Increase video frame rate with DLSS Frame Generation.",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument("inputs", nargs="+", metavar="INPUT", help="files, folders, or quoted wildcards")
        command.add_argument("-o", "--output", metavar="PATH", help="output folder, or output filename for one input (default: app's outputs folder)")
        command.add_argument("--preset", metavar="JSON", help="settings preset exported from the app; flags override its values")
        command.add_argument("--recursive", action="store_true", help="include subfolders when scanning input folders")
        command.add_argument("--gpu", metavar="ID", help="AI/video GPU: auto, NVIDIA index, or GPU UUID (see devices)")
        if name != "interpolate":
            command.add_argument("--format", choices=("png", "jpeg", "webp", "avif", "tiff"), help="image output format (default: PNG or preset value)")
        command.add_argument("--codec", choices=tuple(VIDEO_CODECS), help="video codec (default: command/preset value)")
        command.add_argument("--container", choices=("mp4", "mkv", "mov"), help="supported video container (default: command/preset value)")
        command.add_argument("--quality", choices=tuple(QUALITIES), help="video encoding quality (default: auto or preset value)")
        command.add_argument("--dry-run", action="store_true", help="show planned inputs, stages and output sizes without rendering")
        command.add_argument("--quiet", action="store_true", help="hide progress; keep errors and the final result")
        command.add_argument("--json", action="store_true", help="write one JSON result to stdout; progress/errors go to stderr")
        if name == "render":
            command.add_argument("--style", choices=("0", "1", "2"), help="Neural Rendering style (default: 0 or preset value)")
            command.add_argument("--strength", type=finite_float, metavar="0..2", help="Neural Rendering intensity (default: 1 or preset value)")
        elif name == "upscale":
            command.add_argument("--engine", choices=("rtx", "dlss"), help="upscaling engine (default: rtx or preset value)")
            size = command.add_mutually_exclusive_group()
            size.add_argument("--scale", type=finite_float, metavar="FACTOR", help="RTX scale factor, at least 1 (default: 2 or preset value)")
            size.add_argument("--size", type=dimensions, metavar="WIDTHxHEIGHT", help="RTX output dimensions; uses the exact size")
            command.add_argument("--mode", choices=tuple(DLSS_MODES), help="DLSS mode (default: quality or preset value); controls output size")
        else:
            command.add_argument("--fps", metavar="FPS", help="target frame rate (default: 60 or preset value; e.g. 59.94, 120, 240)")
    devices = commands.add_parser("devices", help="List NVIDIA GPUs and IDs for --gpu.", description="List NVIDIA GPUs and IDs for --gpu.")
    devices.add_argument("--json", action="store_true", help="write GPU information as JSON")
    return parser

def media_mode(path):
    from ..core.disk_paths import supported_file

    if supported_file(path, "image"):
        return "Image"
    if supported_file(path, "video"):
        return "Video"
    return None

def collect_inputs(values, *, recursive=False, exclude=None):
    paths, seen = [], set()
    excluded = Path(exclude).resolve() if exclude else None
    for value in values:
        if not value.strip():
            raise UsageError("Input path cannot be empty.")
        path = Path(value).expanduser()
        literal = path.exists()
        matches = [path] if literal else [Path(p) for p in sorted(glob.glob(str(path), recursive=recursive))]
        if not matches:
            raise UsageError(f"Input does not exist or wildcard matched no files: {value}")
        accepted = 0
        for match in matches:
            if match.is_dir():
                root = match.resolve()
                if not literal and excluded and root.is_relative_to(excluded):
                    continue
                files = match.rglob("*") if recursive else match.iterdir()
                candidates = sorted((p for p in files if p.is_file()), key=lambda p: (str(p).casefold(), str(p)))
                candidates = [p for p in candidates if not (excluded and excluded != root and p.resolve().is_relative_to(excluded))]
            elif match.is_file():
                candidates = [match]
                if not media_mode(match):
                    if literal:
                        raise UsageError(f"Unsupported image/video input: {match}")
                    continue
                if not literal and excluded and match.resolve().is_relative_to(excluded):
                    continue
            else:
                raise UsageError(f"Input is not a file or folder: {match}")
            for candidate in candidates:
                if not media_mode(candidate):
                    continue
                accepted += 1
                resolved = candidate.resolve()
                key = os.path.normcase(str(resolved))
                if key not in seen:
                    seen.add(key)
                    paths.append(resolved)
        if not accepted:
            raise UsageError(f"No supported images or videos found: {value}")
    return paths

def selected_gpu(value):
    from ..core.gpu_detection import detect_gpus

    if value.casefold() == "auto":
        return "auto"
    for gpu in detect_gpus():
        if value.casefold() in {str(gpu["index"]), gpu["uuid"].casefold()}:
            if not gpu["ai_compatible"]:
                raise UsageError(f"{gpu['name']} cannot run RTX processing. {gpu['compatibility_error']}")
            return gpu["uuid"]
    raise UsageError(f"Unknown GPU: {value}. Run VE_CLI.exe devices for available IDs.")

def settings_for(args, modes):
    from ..core.ffmpeg import container_for_codec, containers_for_codec
    from ..settings.models import UISettings, _validate
    from ..settings.presets import import_settings_preset

    base = replace(UISettings(), image_rename_mode="Copy", video_rename_mode="Copy",
                   upscale_image_rename_mode="Copy", upscale_rename_mode="Copy",
                   frame_interpolation_rename_mode="Copy")
    try:
        settings = import_settings_preset(Path(args.preset).expanduser().resolve(), base)[1] if args.preset else base
        if args.command == "render":
            if not args.preset:
                settings = replace(settings, image_enabled_stages=("neural_model",), video_enabled_stages=("neural_model",))
            changes = {}
            if args.style:
                changes["nr_style"] = "Style " + args.style
            if args.strength is not None:
                changes["nr_intensity"] = args.strength
            if changes and any("neural_model" not in (settings.image_enabled_stages if mode == "Image" else settings.video_enabled_stages) for mode in modes):
                raise UsageError("The preset disables Neural Rendering. Enable it in the preset before using --style or --strength.")
            settings = replace(settings, **changes)
        elif args.command == "upscale":
            image_engine = settings.upscale_image_engine
            video_engine = settings.upscale_engine
            if args.engine:
                image_engine = video_engine = "DLSS" if args.engine == "dlss" else "RTX Video Super Resolution"
            engines = [image_engine if mode == "Image" else video_engine for mode in modes]
            if any(engine == "DLSS" for engine in engines) and (args.scale is not None or args.size):
                raise UsageError("DLSS output size is set by --mode. --scale and --size require --engine rtx.")
            if args.mode and any(engine != "DLSS" for engine in engines):
                raise UsageError("--mode requires --engine dlss.")
            settings = replace(settings,
                image_enabled_stages=("dlss_super_resolution" if image_engine == "DLSS" else "super_resolution",),
                video_enabled_stages=("dlss_super_resolution" if video_engine == "DLSS" else "super_resolution",),
                upscale_image_engine=image_engine, upscale_engine=video_engine,
                image_format=settings.upscale_image_output_format, image_quality=settings.upscale_image_quality,
                codec=settings.upscale_codec, container=settings.upscale_container,
                quality=settings.upscale_quality, hdr_mode=False,
                image_rename_mode=settings.upscale_image_rename_mode, image_custom_suffix=settings.upscale_image_custom_suffix,
                video_rename_mode=settings.upscale_rename_mode, video_custom_suffix=settings.upscale_custom_suffix)
            if args.scale is not None:
                settings = replace(settings, upscale_image_size_mode="Scale factor", upscale_size_mode="Scale factor",
                                   upscale_image_scale_factor=args.scale, upscale_scale_factor=args.scale)
            if args.size:
                width, height = args.size
                settings = replace(settings, upscale_image_size_mode="Custom dimensions", upscale_size_mode="Custom dimensions",
                                   upscale_image_width=width, upscale_image_height=height, upscale_width=width, upscale_height=height,
                                   upscale_image_aspect_lock=False, upscale_aspect_lock=False)
            if args.mode:
                settings = replace(settings, upscale_image_dlss_mode=DLSS_MODES[args.mode], upscale_dlss_mode=DLSS_MODES[args.mode])
        else:
            if "Image" in modes:
                raise UsageError("interpolate accepts videos only. Use render or upscale for images.")
            settings = replace(settings, video_enabled_stages=("frame_generation",),
                               frame_interpolation_target_fps=args.fps or settings.frame_interpolation_target_fps,
                               codec=settings.frame_interpolation_codec, container=settings.frame_interpolation_container,
                               quality=settings.frame_interpolation_quality, hdr_mode=settings.frame_interpolation_hdr_mode,
                               video_rename_mode=settings.frame_interpolation_rename_mode,
                               video_custom_suffix=settings.frame_interpolation_custom_suffix)
        changes = {}
        if getattr(args, "format", None):
            if "Image" not in modes:
                raise UsageError("--format is for image output. Use --container for video output.")
            changes["image_format"] = IMAGE_FORMATS[args.format]
        if args.codec or args.container or args.quality:
            if "Video" not in modes:
                raise UsageError("--codec, --container and --quality are video options; use --format for images.")
        if args.codec:
            changes["codec"] = VIDEO_CODECS[args.codec]
            if not args.container and settings.container not in containers_for_codec(changes["codec"]):
                changes["container"] = container_for_codec(changes["codec"])
        if args.container:
            changes["container"] = args.container.upper()
        if args.quality:
            changes["quality"] = QUALITIES[args.quality]
        if args.gpu:
            gpu_uuid = selected_gpu(args.gpu)
            changes.update(ai_gpu_uuid=gpu_uuid, video_gpu_uuid=gpu_uuid)
        return _validate(replace(settings, **changes))
    except ValueError as exc:
        raise UsageError(str(exc)) from exc

def plan_outputs(paths, settings, output, args):
    from ..core.ffmpeg import resolve_container
    from ..core.naming import output_filename, unique_output_path
    from ..core.paths import OUTPUTS
    from ..neural_rendering.image.models import IMAGE_EXTENSIONS
    from ..settings.models import _validate

    target = Path(output).expanduser().resolve() if output else OUTPUTS
    explicit = bool(output and not target.is_dir() and target.suffix.lower()[1:] in {*IMAGE_FORMATS, "mp4", "mkv", "mov"})
    if target.exists() and not target.is_dir():
        raise UsageError(f"Output already exists: {target}. Choose another name.")
    if explicit:
        if len(paths) != 1:
            raise UsageError("An output filename requires exactly one input. Use an output folder for batches.")
        extension = target.suffix.lower()[1:]
        if media_mode(paths[0]) == "Image":
            if extension not in IMAGE_FORMATS:
                raise UsageError("Image input requires an image output filename.")
            if args.format and IMAGE_FORMATS[args.format] != IMAGE_FORMATS[extension]:
                raise UsageError("--format does not match the output filename extension.")
            settings = replace(settings, image_format=IMAGE_FORMATS[extension])
        else:
            if extension not in {"mp4", "mkv", "mov"}:
                raise UsageError("Video input requires an MP4, MKV or MOV output filename.")
            if args.container and args.container != extension:
                raise UsageError("--container does not match the output filename extension.")
            settings = replace(settings, container=extension.upper())
        try:
            settings = _validate(settings)
        except ValueError as exc:
            raise UsageError(str(exc)) from exc
        return settings, [target], target.parent
    reserved, destinations = set(), []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for source in paths:
        image = media_mode(source) == "Image"
        extension = IMAGE_EXTENSIONS[settings.image_format] if image else "." + resolve_container(
            settings.codec, settings.container).lower()
        filename = output_filename(source, extension,
                                   settings.image_rename_mode if image else settings.video_rename_mode,
                                   settings.image_custom_suffix if image else settings.video_custom_suffix,
                                   f"{source.stem}_Pipeline_{stamp}")
        destination = unique_output_path(target / filename, reserved)
        reserved.add(destination)
        destinations.append(destination)
    return settings, destinations, target

@contextmanager
def cancellation_signals(controller):
    old = {}

    def cancel(_signum, _frame):
        # The processors poll this event and terminate their registered children.
        # Do not take the controller's process-list lock inside a signal handler.
        controller.cancel.set()

    try:
        for name in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, name):
                sig = getattr(signal, name)
                old[sig] = signal.signal(sig, cancel)
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)

def dry_run(paths, destinations, settings):
    from ..core.ffmpeg import probe_video
    from ..desktop.workflow import enabled_stages, estimate_pipeline
    from ..neural_rendering.image.decoder import _open_pillow_source

    jobs = []
    for path, output in zip(paths, destinations):
        mode = media_mode(path)
        if mode == "Image":
            image, _ = _open_pillow_source(path)
            try:
                width, height = image.size
                if image.getexif().get(274) in {5, 6, 7, 8}:
                    width, height = height, width
            finally:
                image.close()
            fps, hdr = 0.0, False
        else:
            metadata = probe_video(path, count_mode="metadata")
            width, height, fps, hdr = (int(metadata["width"]), int(metadata["height"]),
                                       float(metadata["fps"]), bool(metadata.get("hdr")))
        estimate = estimate_pipeline(width, height, fps, hdr, settings, mode)
        jobs.append({"input_path": str(path), "output_path": str(output), "media_type": mode.lower(),
                     "stages": [*enabled_stages(settings, mode), "export"],
                     "width": estimate.width, "height": estimate.height, "fps": estimate.fps, "hdr": estimate.hdr})
    return jobs

def run_jobs(paths, destinations, settings, console):
    from ..core import app_log
    from ..core.batch_progress import BatchProgress
    from ..core.disk_paths import prepare_output_dir
    from ..core.jobs import Cancelled, JobController, use_job_controller
    from ..desktop.workflow import preflight_capabilities, render_item

    controller = JobController()
    reporter = BatchProgress(paths, console)
    capability_errors, checked = {}, set()
    started = time.monotonic()
    log_path = str(app_log.init_session())
    app_log.info("cli", f"start: {len(paths)} file(s)")
    try:
        with cancellation_signals(controller), use_job_controller(controller):
            for index, (source, destination) in enumerate(zip(paths, destinations)):
                if controller.cancel.is_set():
                    break
                reporter.advance(index)
                mode = media_mode(source)
                try:
                    if mode not in checked:
                        try:
                            preflight_capabilities(settings, mode, controller)
                        except Exception as exc:
                            capability_errors[mode] = exc
                        checked.add(mode)
                    if mode in capability_errors:
                        raise capability_errors[mode]
                    if controller.cancel.is_set():
                        raise Cancelled("Stopped by user.")
                    prepare_output_dir(destination.parent)
                    rendered = render_item(source, settings, mode, destination, controller,
                        lambda value, detail, i=index: reporter.advance(i, value, detail),
                        capabilities_checked=True)
                    reporter.complete(index, rendered)
                except Exception as exc:
                    cancelled = isinstance(exc, Cancelled) or controller.cancel.is_set()
                    reporter.fail(index, exc, cancelled=cancelled)
                    app_log.fail("cli", f"cli-{time.time_ns()}", exc, {"traceback": traceback.format_exc()})
                    if cancelled:
                        controller.cancel.set()
                        break
        if controller.cancel.is_set():
            reporter.skip_from(0)
        reporter.finish(cancelled=controller.cancel.is_set(), manifest_path=log_path)
    finally:
        controller.terminate_processes()
        console.finish()
    items = [{"input_path": item.input_path, "output_path": item.output_path,
              "state": item.state.lower(), "error": item.detail if item.state in {"Failed", "Cancelled"} else "",
              "elapsed_seconds": round(item.elapsed_seconds, 3)} for item in reporter.items]
    failed = sum(item.state == "Failed" for item in reporter.items)
    completed = sum(item.state == "Complete" for item in reporter.items)
    cancelled = controller.cancel.is_set()
    status = 130 if cancelled else 1 if failed else 0
    app_log.info("cli", f"done: {completed} completed, {failed} failed, cancelled={cancelled}")
    return status, {"total": len(paths), "completed": completed, "failed": failed,
                    "skipped": sum(item.state == "Skipped" for item in reporter.items),
                    "cancelled": cancelled, "elapsed_seconds": round(time.monotonic() - started, 3),
                    "items": items, "log_path": log_path}

def emit_error(message, *, status, json_output):
    if json_output:
        print(json.dumps({"schema_version": 1, "ok": False, "exit_code": status, "error": str(message)}, ensure_ascii=False))
    print(f"Error: {message}", file=sys.stderr)
    if status == 2 and not json_output:
        print("Run VE_CLI.exe COMMAND --help for usage.", file=sys.stderr)
    return status

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    json_output = "--json" in argv
    try:
        if not argv:
            parser.print_help()
            return 0
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            return int(exc.code or 0)
        if not args.command:
            raise UsageError("Choose a command: render, upscale, interpolate, or devices.")
        if args.command == "devices":
            from ..core.gpu_detection import detect_gpus

            gpus = list(detect_gpus())
            if json_output:
                print(json.dumps({"schema_version": 1, "ok": True, "devices": gpus}, ensure_ascii=False))
            else:
                for gpu in gpus:
                    print(f"{gpu['index']}  {gpu['name']}  ({gpu['memory_mb'] / 1024:.1f} GB VRAM)")
                    print(f"   {gpu['uuid']} | Driver {gpu['driver']} | RTX processing: {'yes' if gpu['ai_compatible'] else 'unavailable'}")
            return 0
        from ..core.paths import OUTPUTS

        output = Path(args.output).expanduser().resolve() if args.output else OUTPUTS
        paths = collect_inputs(args.inputs, recursive=args.recursive, exclude=output)
        settings = settings_for(args, {media_mode(p) for p in paths})
        settings, destinations, folder = plan_outputs(paths, settings, args.output, args)
        if args.dry_run:
            try:
                jobs = dry_run(paths, destinations, settings)
            except (ValueError, OSError) as exc:
                raise UsageError(str(exc)) from exc
            result = {"schema_version": 1, "ok": True, "command": args.command, "dry_run": True, "total": len(paths), "items": jobs}
            if json_output:
                print(json.dumps(result, ensure_ascii=False))
            else:
                for index, job in enumerate(jobs):
                    fps = f" | {job['fps']:g} FPS" if job["fps"] else ""
                    print(f"[{index + 1}/{len(jobs)}] {job['input_path']}\n  -> {job['output_path']}\n  {job['width']}x{job['height']}{fps} | {' -> '.join(job['stages'])}")
                print("Plan only. Hardware availability is checked when rendering.")
            return 0
        from ..core import app_log

        console = ConsoleProgress(paths, sys.stderr, quiet=args.quiet)
        # Engine chatter belongs in the application log. stdout stays usable by
        # scripts, including when the shared engines emit diagnostic messages.
        with redirect_stdout(app_log.SessionStream()):
            status, result = run_jobs(paths, destinations, settings, console)
        result.update(schema_version=1, ok=status == 0, exit_code=status, command=args.command)
        if json_output:
            print(json.dumps(result, ensure_ascii=False))
        else:
            label = "Cancelled" if result["cancelled"] else "Completed"
            print(f"{label}: {result['completed']}/{result['total']} files in {result['elapsed_seconds']:.1f}s; {result['failed']} failed.")
            if result["completed"]:
                print(f"Output: {destinations[0] if len(paths) == 1 else folder}")
            if status:
                print(f"Log: {result['log_path']}")
        return status
    except UsageError as exc:
        return emit_error(exc, status=2, json_output=json_output)
    except KeyboardInterrupt:
        return emit_error("Stopped by user.", status=130, json_output=json_output)
    except Exception as exc:
        return emit_error(exc, status=1, json_output=json_output)
