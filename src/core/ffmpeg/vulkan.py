"""Vendor-neutral Vulkan device selection and FFmpeg command planning.

Codec support is established by actual FFmpeg probes, including an independent
AV1 decode check. CPU mode affects codecs only;
pixel filters use Vulkan on the selected GPU (automatic in CPU mode).
"""
from __future__ import annotations

import ctypes as ct
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import app_log
from ..jobs import Cancelled, JobController
from ..paths import FFMPEG, FFPROBE

DEVICE_AUTO = "auto"
DEVICE_CPU = "cpu"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class _ApplicationInfo(ct.Structure):
    _fields_ = [("sType", ct.c_uint32), ("pNext", ct.c_void_p),
                ("name", ct.c_char_p), ("version", ct.c_uint32),
                ("engine", ct.c_char_p), ("engineVersion", ct.c_uint32),
                ("apiVersion", ct.c_uint32)]


class _InstanceInfo(ct.Structure):
    _fields_ = [("sType", ct.c_uint32), ("pNext", ct.c_void_p),
                ("flags", ct.c_uint32), ("application", ct.POINTER(_ApplicationInfo)),
                ("layerCount", ct.c_uint32), ("layers", ct.c_void_p),
                ("extensionCount", ct.c_uint32), ("extensions", ct.c_void_p)]


class _Properties(ct.Structure):
    # The public prefix is followed by an aligned, oversized output buffer for
    # VkPhysicalDeviceLimits and VkPhysicalDeviceSparseProperties. Those fields
    # are deliberately not interpreted; this does not guess their ABI layout.
    _fields_ = [("api", ct.c_uint32), ("driver", ct.c_uint32),
                ("vendor", ct.c_uint32), ("device", ct.c_uint32),
                ("kind", ct.c_uint32), ("name", ct.c_char * 256),
                ("pipelineCacheUUID", ct.c_ubyte * 16), ("unused", ct.c_uint64 * 128)]


class _DeviceID(ct.Structure):
    _fields_ = [("sType", ct.c_uint32), ("pNext", ct.c_void_p),
                ("deviceUUID", ct.c_ubyte * 16), ("driverUUID", ct.c_ubyte * 16),
                ("deviceLUID", ct.c_ubyte * 8), ("nodeMask", ct.c_uint32),
                ("luidValid", ct.c_uint32)]


class _Properties2(ct.Structure):
    _fields_ = [("sType", ct.c_uint32), ("pNext", ct.c_void_p), ("properties", _Properties)]


class _Extension(ct.Structure):
    _fields_ = [("name", ct.c_char * 256), ("version", ct.c_uint32)]


@dataclass(frozen=True)
class VulkanDevice:
    index: int
    uuid: str
    name: str
    device_type: int
    vendor_id: int
    extensions: frozenset[str]

    @property
    def selection(self) -> str:
        return "vulkan:" + self.uuid

    @property
    def discrete(self) -> bool:
        return self.device_type == 2

    @property
    def label(self) -> str:
        kind = "Discrete GPU" if self.discrete else "Integrated GPU" if self.device_type == 1 else "GPU"
        return f"{self.name} ({kind})"


def valid_selection(value: object) -> bool:
    if not isinstance(value, str):
        return False
    if value in {DEVICE_AUTO, DEVICE_CPU}:
        return True
    if not value.startswith("vulkan:"):
        return False
    try:
        uuid.UUID(value[7:])
        return True
    except (ValueError, AttributeError):
        return False


def current_selection() -> str:
    # Lazy import avoids the settings -> ffmpeg -> settings import cycle.
    from ...settings.storage import SETTINGS_STATE, load_settings
    from ..paths import CONFIG_PATH
    with SETTINGS_STATE.lock:
        settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    return settings.ffmpeg_device


@lru_cache(maxsize=1)
def detect_vulkan_devices() -> tuple[VulkanDevice, ...]:
    """Enumerate AMD, Intel and NVIDIA physical devices without CUDA tools."""
    instance = ct.c_void_p()
    try:
        library = ct.WinDLL("vulkan-1.dll") if os.name == "nt" else ct.CDLL("libvulkan.so.1")
        library.vkCreateInstance.argtypes = [ct.POINTER(_InstanceInfo), ct.c_void_p, ct.POINTER(ct.c_void_p)]
        library.vkCreateInstance.restype = ct.c_int32
        library.vkDestroyInstance.argtypes = [ct.c_void_p, ct.c_void_p]
        library.vkEnumeratePhysicalDevices.argtypes = [ct.c_void_p, ct.POINTER(ct.c_uint32), ct.c_void_p]
        library.vkEnumeratePhysicalDevices.restype = ct.c_int32
        library.vkGetPhysicalDeviceProperties2.argtypes = [ct.c_void_p, ct.POINTER(_Properties2)]
        library.vkEnumerateDeviceExtensionProperties.argtypes = [ct.c_void_p, ct.c_char_p, ct.POINTER(ct.c_uint32), ct.c_void_p]
        library.vkEnumerateDeviceExtensionProperties.restype = ct.c_int32
        application = _ApplicationInfo(0, None, b"Visual Enhancer", 1, b"FFmpeg", 1, (1 << 22) | (1 << 12))
        info = _InstanceInfo(1, None, 0, ct.pointer(application), 0, None, 0, None)
        if library.vkCreateInstance(ct.byref(info), None, ct.byref(instance)) != 0:
            return ()
        count = ct.c_uint32()
        if library.vkEnumeratePhysicalDevices(instance, ct.byref(count), None) != 0:
            return ()
        handles = (ct.c_void_p * count.value)()
        if library.vkEnumeratePhysicalDevices(instance, ct.byref(count), handles) not in (0, 5):
            return ()
        devices = []
        for index, handle in enumerate(handles[:count.value]):
            identity = _DeviceID(sType=1000071004)
            properties = _Properties2(sType=1000059001, pNext=ct.addressof(identity))
            library.vkGetPhysicalDeviceProperties2(handle, ct.byref(properties))
            prefix = properties.properties
            if prefix.kind == 4:  # Software Vulkan implementations are not GPU acceleration.
                continue
            extension_count = ct.c_uint32()
            library.vkEnumerateDeviceExtensionProperties(handle, None, ct.byref(extension_count), None)
            extensions = (_Extension * extension_count.value)()
            library.vkEnumerateDeviceExtensionProperties(handle, None, ct.byref(extension_count), extensions)
            devices.append(VulkanDevice(
                index, str(uuid.UUID(bytes=bytes(identity.deviceUUID))),
                prefix.name.decode("utf-8", "replace"), int(prefix.kind), int(prefix.vendor),
                frozenset(item.name.decode("ascii", "replace") for item in extensions[:extension_count.value])))
        return tuple(devices)
    except (OSError, AttributeError, ValueError):
        return ()
    finally:
        if instance.value:
            library.vkDestroyInstance(instance, None)


def device_choices() -> list[dict[str, str]]:
    return [{"label": "Automatic (Best Available)", "value": DEVICE_AUTO},
            {"label": "CPU (Software decoding / encoding)", "value": DEVICE_CPU},
            *[{"label": device.label, "value": device.selection} for device in candidates(DEVICE_AUTO)]]


def candidates(selection: str | None = None, *, filters: bool = False) -> tuple[VulkanDevice, ...]:
    selection = current_selection() if selection is None else selection
    devices = tuple(sorted(detect_vulkan_devices(), key=lambda device: (not device.discrete, device.index)))
    if selection == DEVICE_CPU:
        return devices if filters else ()
    if selection == DEVICE_AUTO:
        return devices
    selected = tuple(device for device in devices if device.selection == selection)
    if not selected:
        raise ValueError("The selected FFmpeg/Vulkan GPU is unavailable. Choose Automatic or another GPU in Settings.")
    return selected


def filter_device(selection: str | None = None) -> VulkanDevice:
    devices = candidates(selection, filters=True)
    for device in devices:
        if filter_supported(device.selection):
            return device
    raise RuntimeError("Vulkan GPU filtering is unavailable. Install a current AMD, Intel or NVIDIA Vulkan driver.")


def device_args(device: VulkanDevice) -> list[str]:
    return ["-init_hw_device", f"vulkan=ve_vk:{device.index}", "-filter_hw_device", "ve_vk"]


def _capture(command: list[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=timeout, creationflags=_NO_WINDOW)


@lru_cache(maxsize=32)
def filter_supported(selection: str) -> bool:
    device = next((item for item in detect_vulkan_devices() if item.selection == selection), None)
    if device is None:
        return False
    try:
        return _capture([str(FFMPEG), "-v", "error", *device_args(device),
                         "-f", "lavfi", "-i", "color=s=64x64:r=1",
                         "-vf", "format=rgba,hwupload,libplacebo=format=rgba,hwdownload,format=rgba",
                         "-frames:v", "1", "-f", "null", "-"]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=1)
def ffmpeg_encoders() -> frozenset[str]:
    try:
        result = _capture([str(FFMPEG), "-hide_banner", "-encoders"])
        return frozenset(re.findall(r"^\s*V\S{5}\s+(\S+)", result.stdout.decode("utf-8", "replace"), re.M))
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()


def _probe_av1_bitstream(command: list[str]) -> bool:
    """Reject encoders that initialize successfully but write invalid AV1.

    Some Vulkan driver/FFmpeg combinations produce packets which their own
    hardware decoder accepts, while independent software decoders reject them.
    Check three frames (including references) before selecting that backend.
    The enclosing encoder probe caches this result for each configuration.
    """
    with tempfile.TemporaryDirectory(prefix="visual-enhancer-av1-probe-") as folder:
        sample = Path(folder) / "sample.mkv"
        command = list(command)
        command[command.index("-frames:v") + 1] = "3"
        command[command.index("-f", command.index("-c:v")) + 1] = "matroska"
        command[-1] = str(sample)
        encoded = _capture(command)
        if encoded.returncode or not sample.is_file() or not sample.stat().st_size:
            return False
        # No hardware decoder or command planner here: verify the bitstream
        # independently, rather than repeating the producing backend's behavior.
        decoded = _capture([str(FFMPEG), "-v", "error", "-xerror", "-err_detect", "explode",
                            "-i", str(sample), "-map", "0:v:0", "-an", "-sn", "-dn",
                            "-fps_mode", "passthrough", "-f", "framemd5", "-"])
        frames = [line for line in decoded.stdout.splitlines() if line.strip() and not line.startswith(b"#")]
        return decoded.returncode == 0 and len(frames) == 3


@lru_cache(maxsize=128)
def encoder_supported(selection: str, encoder: str, width: int, height: int, pix_fmt: str,
                      options: tuple[str, ...] = (), rate: str = "30", time_base: str | None = None) -> bool:
    device = next((item for item in detect_vulkan_devices() if item.selection == selection), None)
    if device is None or encoder not in ffmpeg_encoders():
        return False
    extension = {"h264_vulkan": "VK_KHR_video_encode_h264", "hevc_vulkan": "VK_KHR_video_encode_h265",
                 "av1_vulkan": "VK_KHR_video_encode_av1"}.get(encoder)
    if extension and extension not in device.extensions:
        return False
    extras = list(options) or (["-level", "3"] if encoder == "ffv1_vulkan" else ["-profile:v", "3"] if encoder == "prores_ks_vulkan" else ["-qp", "24"])
    probe_matrix = _option(list(options), "-colorspace", "0" if pix_fmt.startswith("gbr") else "bt709")
    try:
        command = [str(FFMPEG), "-v", "error", *device_args(device),
                           "-f", "lavfi", "-i", f"{'testsrc2' if encoder == 'av1_vulkan' else 'color'}=s={width}x{height}:r={rate}",
                           "-vf", "format=rgba64le,setparams=colorspace=gbr:range=full,hwupload," +
                           libplacebo(f"format={pix_fmt}:colorspace={probe_matrix}:range={'pc' if pix_fmt.startswith('gbr') else 'tv'}"), "-frames:v", "1",
                           "-c:v", encoder, *extras,
                           *(["-enc_time_base:v", time_base] if time_base else []), "-f", "null", "-"]
        supported = _probe_av1_bitstream(command) if encoder == "av1_vulkan" else _capture(command).returncode == 0
        if not supported:
            app_log.info("vulkan", f"{device.name}: {encoder} unavailable for {width}x{height}/{pix_fmt}; trying next codec backend")
        return supported
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=256)
def _stream_info(source: str, mtime_ns: int, size: int) -> dict:
    result = _capture([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=codec_name,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base", "-of", "json", source])
    return json.loads(result.stdout).get("streams", [{}])[0]


def stream_info(source: str | Path) -> dict:
    path = Path(source)
    stat = path.stat()
    return _stream_info(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=256)
def _decoder_supported(selection: str, source: str, mtime_ns: int, size: int) -> bool:
    device = next(item for item in detect_vulkan_devices() if item.selection == selection)
    codec = _stream_info(source, mtime_ns, size).get("codec_name")
    extension = {"h264": "VK_KHR_video_decode_h264", "hevc": "VK_KHR_video_decode_h265",
                 "av1": "VK_KHR_video_decode_av1", "vp9": "VK_KHR_video_decode_vp9"}.get(codec)
    if extension and extension not in device.extensions:
        return False
    if codec not in {"h264", "hevc", "av1", "vp9", "ffv1", "prores"}:
        return False
    try:
        command = [str(FFMPEG), "-v", "error", *device_args(device),
                   "-hwaccel", "vulkan", "-hwaccel_device", "ve_vk", "-hwaccel_output_format", "vulkan",
                   "-i", source, "-vf", "format=vulkan,libplacebo=format=rgba,hwdownload,format=rgba",
                   "-frames:v", "1", "-f", "null", "-"]
        return _capture(command).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def decoder_args(device: VulkanDevice, source: str | Path, selection: str | None = None) -> list[str]:
    selection = current_selection() if selection is None else selection
    if selection == DEVICE_CPU:
        return []
    if str(source).startswith(("http://", "https://", "rtsp://", "rtmp://")):
        # FFmpeg negotiates the stream's profile with the driver and may fall
        # back to software. hwupload below accepts either resulting layout.
        return ["-hwaccel", "vulkan", "-hwaccel_device", "ve_vk", "-hwaccel_output_format", "vulkan"]
    if not Path(source).is_file():
        return []
    path = Path(source).resolve()
    stat = path.stat()
    if _decoder_supported(device.selection, str(path), stat.st_mtime_ns, stat.st_size):
        return ["-hwaccel", "vulkan", "-hwaccel_device", "ve_vk", "-hwaccel_output_format", "vulkan"]
    return []


def libplacebo(options: str = "") -> str:
    base = "libplacebo=deband=0:sigmoid=0:disable_linear=1:dithering=-1:apply_filmgrain=0:alpha_mode=premultiplied"
    return base + (":" + options if options else "")


def quote_filter_path(path: str | Path) -> str:
    # FFmpeg has two escaping levels: the filter graph and the option string.
    value = str(path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
    return "'" + value + "'"


_VK_ENCODERS = {"libx264": "h264_vulkan", "h264_nvenc": "h264_vulkan",
                "libx265": "hevc_vulkan", "hevc_nvenc": "hevc_vulkan",
                "libsvtav1": "av1_vulkan", "libaom-av1": "av1_vulkan", "av1_nvenc": "av1_vulkan",
                "ffv1": "ffv1_vulkan", "prores_ks": "prores_ks_vulkan"}
_SOFTWARE_ENCODERS = {"h264_nvenc": "libx264", "hevc_nvenc": "libx265", "av1_nvenc": "libsvtav1"}


def _option(command: list[str], name: str, default=None):
    return command[command.index(name) + 1] if name in command else default


def _remove_options(command: list[str], names: set[str]) -> list[str]:
    result = []
    index = 0
    while index < len(command):
        if command[index] in names:
            index += 2
        else:
            result.append(command[index])
            index += 1
    return result


def _split_filters(graph: str) -> list[str]:
    # Commas inside scale expressions or quoted paths are not graph separators.
    parts, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(graph):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "'":
            quoted = not quoted
        elif not quoted:
            depth += (char == "(") - (char == ")")
            if char == "," and depth == 0:
                parts.append(graph[start:index])
                start = index + 1
    return [part for part in [*parts, graph[start:]] if part]


def gpu_filter_graph(graph: str) -> str:
    """Replace supported FFmpeg pixel filters with vendor-neutral Vulkan ones."""
    from .filters import scaling_filter
    parts = _split_filters(graph.replace("ve_gpu,", "").replace("ve_gpu", ""))
    result = []
    tone = next((part for part in parts if part.startswith("tonemap=")), None)
    if tone:
        method = re.search(r"tonemap=([^:]+)", tone).group(1)
        result.append(libplacebo(f"color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv:tonemapping={method}:format=yuv420p"))
    for part in parts:
        name, _, options = part.partition("=")
        if tone and name in {"tonemap", "zscale", "format"}:
            continue
        if name == "format":
            result.append(libplacebo("format=" + options))
        elif name == "scale":
            if options.startswith("in_range="):
                result.append(libplacebo("range=" + ("pc" if "out_range=pc" in options else "tv")))
                continue
            positional = options.split(":")
            w = positional[0].removeprefix("w=").removeprefix("width=")
            h = positional[1].removeprefix("h=").removeprefix("height=") if len(positional) > 1 else "ih"
            flags = re.search(r"flags=([^:]+)", options)
            method = {"lanczos": "Lanczos", "area": "Area", "bicubic": "Bicubic", "neighbor": "Nearest"}.get(flags.group(1) if flags else "", "Bilinear")
            if w.isdigit() and h.isdigit():
                result.append(scaling_filter(int(w), int(h), method))
            else:
                kernel = {"Lanczos": "lanczos", "Area": "bilinear", "Bicubic": "bicubic", "Nearest": "nearest", "Bilinear": "bilinear"}[method]
                result.append(libplacebo(f"w={w}:h={h}:upscaler={kernel}:downscaler={kernel}"))
        elif name == "zscale":
            values = dict(item.split("=", 1) for item in options.split(":") if "=" in item)
            inputs = {"matrixin": "colorspace", "primariesin": "color_primaries", "transferin": "color_trc", "rangein": "range"}
            assumptions = [f"{inputs[key]}={value}" for key, value in values.items() if key in inputs]
            if assumptions:
                result.append("setparams=" + ":".join(assumptions))
            fields = {"matrix": "colorspace", "m": "colorspace", "primaries": "color_primaries", "p": "color_primaries", "transfer": "color_trc", "t": "color_trc", "range": "range", "r": "range", "w": "w", "h": "h"}
            converted = [f"{fields[key]}={value}" for key, value in values.items() if key in fields and value != "gbr"]
            result.append(libplacebo(":".join(converted)))
        elif name == "transpose":
            direction = "3" if options in {"cclock", "2"} else "1"
            result.append(libplacebo(f"rotate={direction}:w=ih:h=iw"))
        elif name in {"hflip", "vflip"}:
            from .filters import shader_filter
            point = "vec2(1.0-HOOKED_pos.x,HOOKED_pos.y)" if name == "hflip" else "vec2(HOOKED_pos.x,1.0-HOOKED_pos.y)"
            result.append(shader_filter("//!HOOK MAIN\n//!BIND HOOKED\nvec4 hook() { return HOOKED_tex(" + point + "); }\n"))
        elif name == "bwdif":
            result.append(name + "_vulkan" + ("=" + options if options else ""))
        elif name == "lut3d":
            path = options.removeprefix("file=").split(":interp=")[0]
            result.append(libplacebo("lut=" + path + ":lut_type=native"))
        else:
            result.append(part)
    return ",".join(result)


def prepare_command(command: list[str], *, selection: str | None = None,
                    dimensions: tuple[int, int] | None = None, input_format: str | None = None,
                    rate: str = "30", time_base: str | None = None) -> list[str]:
    """Choose Vulkan codecs and transfer boundaries for one FFmpeg video output.

    GPU graphs are marked with ve_gpu by their callers. This planner never
    retries a partly written output or silently replaces a requested filter.
    """
    command = list(command)
    if not command or Path(command[0]).name.lower() not in {"ffmpeg", "ffmpeg.exe"}:
        return command
    selection = current_selection() if selection is None else selection
    encoder = _option(command, "-c:v", _option(command, "-vcodec"))
    if encoder == "copy":
        return command
    if encoder is None and "-vf" not in command:
        return command
    graph = _option(command, "-vf", "")
    graph = gpu_filter_graph(graph)
    first_input = _option(command, "-i", "")
    width, height = dimensions or (0, 0)
    if not width and first_input and Path(first_input).is_file():
        metadata = stream_info(first_input)
        input_format = input_format or metadata.get("pix_fmt")
        rate = metadata.get("avg_frame_rate") or metadata.get("r_frame_rate") or rate
        if rate == "0/0":
            rate = "30"
        time_base = metadata.get("time_base") or time_base
        width, height = int(metadata.get("width", 0)), int(metadata.get("height", 0))
        scaled = re.search(r"\bw=(\d+):h=(\d+)", graph)
        if scaled:
            width, height = map(int, scaled.groups())
        capped = re.search(r"w=if\(gt\(iw\\?,(\d+)\)", graph)
        if capped and width > int(capped.group(1)):
            new_width = int(capped.group(1))
            height = max(2, round(height * new_width / width / 2) * 2)
            width = new_width
    pix_fmt = _option(command, "-pix_fmt", "yuv420p")
    target = _VK_ENCODERS.get(encoder)
    vk_format = ("p010le" if "10" in pix_fmt or pix_fmt == "p010le" else "nv12") if target in {"h264_vulkan", "hevc_vulkan", "av1_vulkan"} else pix_fmt
    rgb_output = vk_format.startswith(("gbr", "rgb", "bgr", "argb", "abgr"))
    matrix = _option(command, "-colorspace")
    if rgb_output:
        matrix = "0"
    elif matrix in (None, "2", "0", "gbr", "unknown", "unspecified"):
        matrix = "bt2020nc" if _option(command, "-color_primaries") in {"9", "bt2020"} else "smpte170m" if encoder == "mjpeg" or _option(command, "-f") == "mjpeg" else "bt709"
    if "-colorspace" in command:
        command[command.index("-colorspace")+1] = matrix
    else:
        command[-1:-1] = ["-colorspace", matrix]
    encode_device = None
    codec_options = []
    for name in ("-profile:v", "-profile", "-bits_per_mb", "-level", "-slicecrc", "-b:v"):
        if name in command:
            codec_options += [name, _option(command, name)]
    if target in {"h264_vulkan", "hevc_vulkan", "av1_vulkan"} and (not codec_options or _option(command, "-crf") == "0"):
        codec_options = (["-tune", "lossless"] if _option(command, "-crf") == "0" else []) + ["-rc_mode", "cqp", "-qp", "0" if _option(command, "-crf") == "0" else _option(command, "-crf", "18")]
    for name in ("-color_range", "-colorspace", "-color_primaries", "-color_trc"):
        if name in command:
            codec_options += [name, _option(command, name)]
    if _option(command, "-enc_time_base", _option(command, "-enc_time_base:v")) not in (None, "demux", "filter"):
        time_base = _option(command, "-enc_time_base", _option(command, "-enc_time_base:v"))
    if target and width > 0 and height > 0:
        encode_device = next((device for device in candidates(selection)
                              if encoder_supported(device.selection, target, width, height, vk_format, tuple(codec_options), rate, time_base)), None)
    device = encode_device or filter_device(selection)
    decode = decoder_args(device, first_input, selection) if first_input else []
    command = _remove_options(command, {"-init_hw_device", "-filter_hw_device", "-hwaccel", "-hwaccel_device", "-hwaccel_output_format"})
    input_index = command.index("-i")
    command[input_index:input_index] = decode
    command[1:1] = device_args(device)
    graph = "hwupload" + ("," + graph if graph else "")
    def output_format(fmt: str) -> str:
        rgb = fmt.startswith(("gbr", "rgb", "bgr", "argb", "abgr"))
        color_options = ["format=" + fmt]
        explicit_range = _option(command, "-color_range")
        color_options.append("range=" + (explicit_range if explicit_range not in (None, "0", "unknown", "unspecified") else "pc" if rgb else "tv"))
        for name, option in (("-color_primaries", "color_primaries"), ("-color_trc", "color_trc"), ("-colorspace", "colorspace")):
            value = _option(command, name)
            if value not in (None, "unknown", "unspecified", "2"):
                color_options.append(f"{option}={value}")
        result = libplacebo(":".join(color_options))
        narrow_rgb = re.fullmatch(r"(gbrap|gbrp)(?:9|10|12|14)(le|be)", fmt)
        if narrow_rgb:
            # Sub-16-bit RGB uses 16-bit storage with a smaller legal range.
            # YUV superwhites / conversion overshoot can survive direct output
            # as codes above that range; packed RGBA64 conversion then wraps
            # the bright channels to black. A full-range UNORM16 surface clamps
            # in the output transfer function before the lower-depth packing.
            normalized = ["format=" + narrow_rgb[1] + "16" + narrow_rgb[2], *color_options[1:]]
            return libplacebo(":".join(normalized)) + "," + result
        return result
    if encode_device:
        if input_format != vk_format or _option(command, "-vf"):
            graph = (graph + "," if graph else "") + output_format(vk_format)
        lossless = _option(command, "-crf") == "0" or _option(command, "-cq") == "0"
        quality = _option(command, "-crf", _option(command, "-cq", "18"))
        command = _remove_options(command, {"-preset", "-tune", "-crf", "-cq", "-rc", "-gpu",
                                            "-x264-params", "-x265-params", "-cpu-used", "-pix_fmt"})
        flag = "-c:v" if "-c:v" in command else "-vcodec"
        command[command.index(flag) + 1] = target
        if target in {"h264_vulkan", "hevc_vulkan", "av1_vulkan"}:
            if lossless:
                command = _remove_options(command, {"-b:v", "-q:v"})
                command[-1:-1] = ["-tune", "lossless", "-rc_mode", "cqp", "-qp", "0"]
            elif "-b:v" not in command or _option(command, "-b:v") == "0":
                command = _remove_options(command, {"-b:v"})
                command[-1:-1] = ["-rc_mode", "cqp", "-qp", quality]
    else:
        graph = (graph + "," if graph else "") + f"{output_format(pix_fmt)},hwdownload,format={pix_fmt}"
        if encoder in _SOFTWARE_ENCODERS:
            command[command.index("-c:v") + 1] = _SOFTWARE_ENCODERS[encoder]
            command = _remove_options(command, {"-gpu", "-tune", "-rc", "-cq", "-preset"})
    if "-vf" in command:
        command[command.index("-vf") + 1] = graph
    else:
        command[-1:-1] = ["-vf", graph]
    app_log.info("vulkan", f"filters={device.name} decode={'Vulkan' if decode else 'CPU'} encode={target if encode_device else encoder}")
    return command


def run(command: list[str], controller: JobController, label: str, *, cwd: Path | None = None,
        selection: str | None = None, dimensions: tuple[int, int] | None = None,
        progress=None, metadata: dict | None = None) -> None:
    if controller.cancel.is_set():
        raise Cancelled("Render stopped by user.")
    command = prepare_command(command, selection=selection or getattr(controller, "ffmpeg_device", None), dimensions=dimensions)
    if progress is not None:
        from .progress import run_with_progress
        return run_with_progress(command, controller, label, progress, metadata=metadata, cwd=cwd)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               cwd=cwd, creationflags=_NO_WINDOW)
    controller.register(process)
    try:
        while True:
            if controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            try:
                _, errors = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"{label} failed: {errors.decode('utf-8', 'replace')[-2500:]}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        controller.unregister(process)
        if process.stderr:
            process.stderr.close()


def clear_caches() -> None:
    for function in (detect_vulkan_devices, filter_supported, ffmpeg_encoders,
                     encoder_supported, _stream_info, _decoder_supported):
        function.cache_clear()
