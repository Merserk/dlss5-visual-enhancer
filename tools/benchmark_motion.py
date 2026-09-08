"""CPU microbenchmark, NOT a DLSS/GPU/end-to-end throughput benchmark.

Compares the previous resize-then-scale algorithm with the new scratch-buffer
implementation, including FP16 output conversion. Requires NumPy and OpenCV.
Run with packaged Python; optional --iterations 20 --output result.json.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import platform
import statistics
import time

import cv2
import numpy as np


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=12)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 3:
        parser.error("--iterations must be at least 3")
    path = Path(__file__).resolve().parents[1] / "src/core/motion_resize.py"
    spec = importlib.util.spec_from_file_location("_motion_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cv2.setNumThreads(1)
    flow = np.random.default_rng(4090).normal(0, 3, (360, 640, 2)).astype(np.float32)
    results = []
    for width, height in ((1920, 1080), (3840, 2160), (7680, 4320)):
        resizer = module.MotionFieldResizer(width, height)

        def previous():
            resized = cv2.resize(flow, (width, height), interpolation=cv2.INTER_LINEAR)
            resized[..., 0] *= width / 640
            resized[..., 1] *= height / 360
            return resized.astype(np.float16)

        def updated():
            return module.motion_to_fp16(resizer.resize(flow))

        # Warm both paths, compare float32 math separately from FP16 rounding.
        previous(); updated()
        expected = cv2.resize(flow, (width, height), interpolation=cv2.INTER_LINEAR)
        expected[..., 0] *= width / 640
        expected[..., 1] *= height / 360
        actual = resizer.resize(flow)
        max_error = float(np.max(np.abs(expected - actual)))
        np.testing.assert_allclose(actual, expected, rtol=3e-5, atol=3e-5)
        del expected, actual
        samples = {"previous": [], "updated": []}
        functions = {"previous": previous, "updated": updated}
        # Alternate order to reduce temperature/cache/order bias.
        for index in range(args.iterations):
            for name in (("previous", "updated") if index % 2 == 0 else ("updated", "previous")):
                start = time.perf_counter()
                output = functions[name]()
                samples[name].append((time.perf_counter() - start) * 1000)
                del output
        before = statistics.median(samples["previous"])
        after = statistics.median(samples["updated"])
        results.append({"width": width, "height": height, "previous_median_ms": before,
                        "updated_median_ms": after, "speedup": before / after,
                        "max_float32_vector_error_pixels": max_error,
                        "scratch_bytes_cpu_ram": width * height * 2 * 4})
    report = {"scope": "CPU motion-field resize + vector scaling + FP16 conversion only",
              "not_measured": ["DLSS inference", "NVENC throughput", "GPU affinity", "VRAM usage"],
              "platform": platform.platform(), "python": platform.python_version(),
              "opencv": cv2.__version__, "numpy": np.__version__, "opencv_threads": 1,
              "iterations_per_path": args.iterations, "results": results}
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
