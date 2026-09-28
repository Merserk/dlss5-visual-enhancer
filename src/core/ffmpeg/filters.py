"""GPU pixel operations shared by file and host-frame media pipelines."""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from ..jobs import Cancelled, JobController
from ..paths import FFMPEG, APP_TEMP
from .vulkan import device_args, filter_device, libplacebo, quote_filter_path


def shader_file(source: str) -> Path:
    directory = APP_TEMP / "vulkan-shaders"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(source.encode()).hexdigest() + ".hook")
    if not path.exists():
        # Identical contents make concurrent writers safe; each replacement is atomic.
        with tempfile.NamedTemporaryFile(dir=directory, suffix=".hook", delete=False) as stream:
            stream.write(source.encode())
            temporary = Path(stream.name)
        temporary.replace(path)
    return path


def shader_filter(source: str, *, options: str = "") -> str:
    return libplacebo("custom_shader_path=" + quote_filter_path(shader_file(source))
                      + (":" + options if options else ""))


def filter_array(pixels: np.ndarray, graph: str, *, width: int | None = None,
                 height: int | None = None, controller: JobController | None = None,
                 selection: str | None = None) -> np.ndarray:
    if pixels.ndim != 3 or pixels.shape[2] != 4 or pixels.dtype not in (np.uint8, np.uint16):
        raise ValueError("Vulkan filtering requires uint8/uint16 RGBA pixels.")
    width, height = width or pixels.shape[1], height or pixels.shape[0]
    fmt = "rgba64le" if pixels.dtype == np.uint16 else "rgba"
    device = filter_device(selection or getattr(controller, "ffmpeg_device", None))
    command = [str(FFMPEG), "-v", "error", *device_args(device), "-f", "rawvideo",
               "-pixel_format", fmt, "-video_size", f"{pixels.shape[1]}x{pixels.shape[0]}",
               "-framerate", "1", "-i", "pipe:0", "-vf",
               "setparams=alpha_mode=premultiplied,hwupload," + graph + f",{libplacebo('format=' + fmt)},hwdownload,format={fmt}",
               "-frames:v", "1", "-c:v", "rawvideo", "-pix_fmt", fmt, "-f", "rawvideo", "pipe:1"]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if controller:
        controller.register(process)
    try:
        data = np.ascontiguousarray(pixels).tobytes()
        while True:
            if controller and controller.cancel.is_set():
                raise Cancelled("Render stopped by user.")
            try:
                output, errors = process.communicate(input=data, timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                data = None
        if process.returncode:
            raise RuntimeError("Vulkan filtering failed: " + errors.decode("utf-8", "replace")[-2500:])
        expected = width * height * 4 * pixels.dtype.itemsize
        if len(output) != expected:
            raise RuntimeError(f"Vulkan filter returned {len(output)} bytes; expected {expected}.")
        return np.frombuffer(output, dtype=pixels.dtype).reshape(height, width, 4).copy()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        if controller:
            controller.unregister(process)


def scaling_filter(width: int, height: int, method: str, *, source_size: tuple[int, int] | None = None,
                   fit: bool = False) -> str:
    kernels = {"Spline36": "spline36", "Lanczos": "lanczos", "Bicubic": "bicubic",
               "Bilinear": "bilinear", "Nearest": "nearest"}
    if method in kernels:
        kernel = kernels[method]
        return libplacebo(f"w={width}:h={height}:upscaler={kernel}:downscaler={kernel}"
                          + (":fit_mode=contain:fillcolor=black@1" if fit else ""))
    if method not in {"Area", "Lanczos4"}:
        raise ValueError(f"Unknown Vulkan scaling filter: {method!r}.")
    fit_width, fit_height, left, top = width, height, 0, 0
    if fit and source_size:
        scale = min(width / source_size[0], height / source_size[1])
        fit_width = max(1, min(width, round(source_size[0] * scale)))
        fit_height = max(1, min(height, round(source_size[1] * scale)))
        left, top = (width - fit_width) // 2, (height - fit_height) // 2
    header = f"""//!HOOK MAIN
//!BIND HOOKED
//!WIDTH {width}
//!HEIGHT {height}
vec4 pixel(ivec2 p) {{
    return HOOKED_tex((vec2(clamp(p, ivec2(0), ivec2(HOOKED_size)-1)) + 0.5) / HOOKED_size);
}}
vec4 hook() {{
    vec2 outpos = HOOKED_pos * vec2({width}.0, {height}.0) - vec2({left}.0, {top}.0);
    if (outpos.x < 0.0 || outpos.y < 0.0 || outpos.x >= {fit_width}.0 || outpos.y >= {fit_height}.0)
        return vec4(0.0, 0.0, 0.0, 1.0);
    vec2 ratio = HOOKED_size / vec2({fit_width}.0, {fit_height}.0);
"""
    if method == "Area":
        body = """
    vec2 lo = (outpos - 0.5) * ratio, hi = (outpos + 0.5) * ratio;
    vec4 sum = vec4(0.0); float total = 0.0;
    for (int y = int(floor(lo.y)); y < int(ceil(hi.y)); y++) {
        float wy = max(0.0, min(hi.y, float(y+1)) - max(lo.y, float(y)));
        for (int x = int(floor(lo.x)); x < int(ceil(hi.x)); x++) {
            float wx = max(0.0, min(hi.x, float(x+1)) - max(lo.x, float(x)));
            sum += pixel(ivec2(x,y)) * wx * wy; total += wx * wy;
        }
    }
    return sum / max(total, 1e-20);
}
"""
    else:
        header = header.replace("vec4 hook()", "float lanczos(float x) { x=abs(x); if(x<1e-6) return 1.0; if(x>=4.0) return 0.0; return sin(3.14159265*x)*sin(3.14159265*x/4.0)/(3.14159265*3.14159265*x*x/4.0); }\nvec4 hook()")
        body = """
    vec2 p = outpos * ratio - 0.5, support = max(ratio, vec2(1.0));
    vec4 sum = vec4(0.0); float total = 0.0;
    for(int y=int(ceil(p.y-4.0*support.y)); y<=int(floor(p.y+4.0*support.y)); y++) {
        float wy=lanczos((float(y)-p.y)/support.y);
        for(int x=int(ceil(p.x-4.0*support.x)); x<=int(floor(p.x+4.0*support.x)); x++) {
            float w=wy*lanczos((float(x)-p.x)/support.x);
            sum+=pixel(ivec2(x,y))*w; total+=w;
        }
    }
    return clamp(sum/max(total,1e-20),0.0,1.0);
}
"""
    return shader_filter(header + body, options=f"w={width}:h={height}:upscaler=nearest:downscaler=nearest")


def resize_fit(pixels: np.ndarray, width: int, height: int, *, interpolation: str = "Lanczos4",
               controller: JobController | None = None) -> np.ndarray:
    if (pixels.shape[1], pixels.shape[0]) == (width, height):
        return np.ascontiguousarray(pixels)
    graph = scaling_filter(width, height, interpolation,
                           source_size=(pixels.shape[1], pixels.shape[0]), fit=True)
    return filter_array(pixels, graph, width=width, height=height, controller=controller)
