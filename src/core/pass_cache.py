"""Lossless, byte-budgeted frame mappings shared with disposable stage workers.

Pixels never cross the controller's pipes. A mapping is reserved before it is
created, has a fixed capacity, and becomes readable only after its worker exits
and commits its header. RAM and disk backing have the same ownership contract.
"""
from __future__ import annotations

import ctypes
import mmap
import os
import struct
import uuid
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

HEADER_BYTES = 4096
RECORD = struct.Struct("<8q")
MAGIC = b"VECACHE2"
FORMATS = {"rgb24": (np.uint8, 3), "rgba": (np.uint8, 4), "rgba64le": (np.uint16, 4),
           "gbrp10le": (np.uint16, 3), "rgba16f": (np.float16, 4)}


def frame_bytes(width, height, pixel_format):
    dtype, channels = FORMATS[pixel_format]
    return int(width) * int(height) * channels * np.dtype(dtype).itemsize


def storage_bytes(width, height, pixel_format, capacity):
    return HEADER_BYTES + int(capacity) * (RECORD.size + frame_bytes(width, height, pixel_format))


def available_ram():
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_uint32), ("load", ctypes.c_uint32),
                        *( (name, ctypes.c_uint64) for name in
                           ("total", "available", "page_total", "page_available", "virtual_total",
                            "virtual_available", "extended_available") )]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.available)
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, AttributeError, OSError):
        return 0


@dataclass(frozen=True)
class FrameTiming:
    pts: int
    duration: int
    time_base: Fraction
    segment: int = 0
    source_index: int = -1
    generated: bool = False

    @property
    def stamp(self):
        return self.pts * self.time_base


class FrameMapping:
    """One generation of a part; the parent owns its budget and lifetime."""

    def __init__(self, descriptor, *, budget=None, owner=False):
        self.descriptor = dict(descriptor)
        self.budget, self.owner = budget, owner
        self.width, self.height = int(descriptor["width"]), int(descriptor["height"])
        self.format = descriptor["format"]
        self.capacity = int(descriptor["capacity"])
        self.size = storage_bytes(self.width, self.height, self.format, self.capacity)
        self.record_end = HEADER_BYTES + RECORD.size * self.capacity
        self.pixel_bytes = frame_bytes(self.width, self.height, self.format)
        self.mapping = self.file = None
        self.closed = False
        if owner:
            budget.reserve(self.size)
        try:
            if descriptor["backing"] == "ram":
                self.mapping = mmap.mmap(-1, self.size, tagname=descriptor["name"], access=mmap.ACCESS_WRITE)
            else:
                path = Path(descriptor["path"])
                self.file = path.open("w+b" if owner else "r+b", buffering=0)
                if owner:
                    self.file.truncate(self.size)
                self.mapping = mmap.mmap(self.file.fileno(), self.size, access=mmap.ACCESS_WRITE)
            if owner:
                self.mapping[:HEADER_BYTES] = bytes(HEADER_BYTES)
        except BaseException:
            self.close()
            raise

    @classmethod
    def create(cls, directory, budget, width, height, pixel_format, capacity, *, backing=None):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        size = storage_bytes(width, height, pixel_format, capacity)
        name = "ve-cache-" + uuid.uuid4().hex
        automatic = backing is None
        if automatic:
            backing = "ram" if os.name == "nt" and available_ram() > size + 2_000_000_000 else "disk"
        descriptor = dict(version=2, width=width, height=height, format=pixel_format,
                          capacity=int(capacity), backing=backing, name=name,
                          path=str((directory / (name + ".frames")).resolve()))
        try:
            return cls(descriptor, budget=budget, owner=True)
        except OSError:
            if not automatic or backing != "ram":
                raise
            descriptor["backing"] = "disk"
            return cls(descriptor, budget=budget, owner=True)

    @property
    def count(self):
        if self.mapping[:8] != MAGIC:
            raise RuntimeError("A stage cache has not been committed.")
        value = struct.unpack_from("<Q", self.mapping, 8)[0]
        first, history = struct.unpack_from("<QQ", self.mapping, 16)
        if not 0 < value <= self.capacity or history > min(32, value) or first >= 32:
            raise RuntimeError("Invalid stage cache frame count.")
        return int(value)

    def pixels(self, index):
        if not 0 <= index < self.capacity:
            raise RuntimeError("Part capacity exceeded before writing a frame.")
        dtype, channels = FORMATS[self.format]
        # Planar formats use three contiguous RGB planes; packed ones use HWC.
        shape = ((3, self.height, self.width) if self.format == "gbrp10le"
                 else (self.height, self.width, channels))
        return np.ndarray(shape, dtype=dtype, buffer=self.mapping,
                          offset=self.record_end + self._physical(index) * self.pixel_bytes)

    def _physical(self, index):
        if self.mapping[:8] == MAGIC:
            first, history = struct.unpack_from("<QQ", self.mapping, 16)
            if index < history:
                return (first + index) % 32
        return index

    def timing(self, index):
        pts, duration, numerator, denominator, segment, source_index, generated, _ = RECORD.unpack_from(
            self.mapping, HEADER_BYTES + self._physical(index) * RECORD.size)
        return FrameTiming(pts, duration, Fraction(numerator, denominator), segment, source_index, bool(generated))

    def write_timing(self, index, value):
        if not 0 <= index < self.capacity:
            raise RuntimeError("Part capacity exceeded before writing timing.")
        RECORD.pack_into(self.mapping, HEADER_BYTES + index * RECORD.size,
                         int(value.pts), int(value.duration), value.time_base.numerator,
                         value.time_base.denominator, int(value.segment),
                         int(value.source_index), int(value.generated), 0)

    def write_frame(self, index, frame, *, segment=0, source_index=-1, generated=False):
        target = self.pixels(index)
        if self.format == "gbrp10le":
            if frame.format.name == "gbrp10le":
                for channel, plane in zip((1, 2, 0), frame.planes):
                    rows = np.frombuffer(plane, dtype=np.uint16).reshape(plane.height, plane.line_size // 2)
                    np.copyto(target[channel], rows[:, :self.width])
            else:
                pixels = frame.to_ndarray(format="gbrp10le")
                np.copyto(target, pixels.transpose(2, 0, 1))
        else:
            np.copyto(target, frame.to_ndarray(format=self.format))
        self.write_timing(index, FrameTiming(frame.pts, frame.duration or 0, Fraction(frame.time_base),
                                            segment, source_index, generated))

    def frame(self, index):
        import av
        if self.format == "rgba16f":
            raise ValueError("FP16 FG grids must be consumed without AVFrame quantization.")
        pixels = self.pixels(index)
        if self.format == "gbrp10le":
            # PyAV's array API names RGB channels even though its planes are GBR.
            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(pixels.transpose(1, 2, 0)), format=self.format)
        else:
            frame = av.VideoFrame.from_numpy_buffer(pixels, format=self.format)
        timing = self.timing(index)
        frame.pts, frame.duration, frame.time_base = timing.pts, timing.duration, timing.time_base
        frame.opaque = timing
        return frame

    def frames(self):
        for index in range(self.count):
            yield self.frame(index)

    def timed_pixels(self):
        for index in range(self.count):
            yield self.pixels(index), self.timing(index)

    def commit(self, count, *, history_start=0, history_count=0):
        if not 0 < count <= self.capacity or not 0 <= history_count <= min(32, count) or not 0 <= history_start < 32:
            raise RuntimeError("A stage cannot commit an empty or oversized part.")
        # The parent reads this only after the worker and its entire tree exit.
        if self.file:
            self.mapping.flush()
        struct.pack_into("<Q", self.mapping, 8, count)
        struct.pack_into("<QQ", self.mapping, 16, history_start, history_count)
        self.mapping[:8] = MAGIC

    def close(self):
        if self.closed:
            return
        # Exported NumPy/AVFrame views must release their memory first. A failed
        # unmap keeps its reservation, so live memory is never undercounted.
        if self.mapping:
            self.mapping.close()
        if self.file:
            self.file.close()
        if self.owner:
            if self.descriptor["backing"] == "disk":
                Path(self.descriptor["path"]).unlink(missing_ok=True)
            self.budget.release(self.size)
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
