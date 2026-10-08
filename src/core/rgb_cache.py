"""Bounded, independently compressed 8-bit RGB frames for disposable passes.

Zstandard's fast mode preserves RGB bytes exactly. No alpha or YUV conversion
is stored. Incompressible frames stay raw RGB. Blocks reserve their raw bound
before writing, settle to actual size on commit, and retire in FIFO order.
"""
from __future__ import annotations

import shutil
import uuid
from collections import deque
from compression import zstd
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import numpy as np

from .frame_blocks import FrameBlocks, WorkerBlocks
from .pass_cache import HEADER_BYTES, RECORD, FrameTiming, frame_bytes, storage_bytes

MAGIC = b"VERGBZ01"
COMPRESSION_LEVEL = -32


def _record(path):
    with path.open("rb") as source:
        if source.read(len(MAGIC)) != MAGIC:
            raise RuntimeError("Invalid compressed RGB cache header.")
        header = source.read(RECORD.size)
    if len(header) != RECORD.size:
        raise RuntimeError("Truncated compressed RGB timing record.")
    values = RECORD.unpack(header)
    if values[7] not in (0, 1):
        raise RuntimeError("Invalid compressed RGB storage method.")
    return FrameTiming(values[0], values[1], Fraction(values[2], values[3]),
                       values[4], values[5], bool(values[6])), values[7]


class RGBBlock:
    def __init__(self, descriptor, *, budget=None):
        self.descriptor = dict(descriptor)
        self.directory = Path(descriptor["directory"])
        self.width, self.height, self.capacity = descriptor["width"], descriptor["height"], descriptor["capacity"]
        self.pixel_bytes = frame_bytes(self.width, self.height, "rgb24")
        self.size = storage_bytes(self.width, self.height, "rgb24", self.capacity) + self.capacity * len(MAGIC)
        self.budget, self.closed = budget, False
        if budget:
            self._owned_path()
            budget.reserve(self.size)
            try:
                self.directory.mkdir(parents=True, exist_ok=False)
            except BaseException:
                budget.release(self.size)
                raise

    def _owned_path(self):
        root = Path(self.descriptor["owner_root"]).resolve()
        target = self.directory.resolve()
        if target.parent != root or not target.name.startswith("rgb-") or len(target.name) != 36:
            raise RuntimeError("Invalid owned compressed RGB cleanup path.")
        return target

    def path(self, index):
        if not 0 <= index < self.capacity:
            raise RuntimeError("Compressed RGB frame exceeds its block capacity.")
        return self.directory / f"{index}.rgbz"

    @property
    def count(self):
        count = int((self.directory / "count.json").read_text(encoding="utf-8"))
        if not 0 < count <= self.capacity:
            raise RuntimeError("Invalid compressed RGB block count.")
        return count

    def timing(self, index):
        return _record(self.path(index))[0]

    def write_rgb(self, index, frame, timing):
        import av
        if isinstance(frame, np.ndarray):
            if frame.dtype not in (np.uint8, np.uint16) or frame.shape[-1] not in (3, 4):
                raise ValueError("Fast compressed accepts integer RGB/RGBA SDR frames.")
            fmt = ("rgb48le" if frame.dtype == np.uint16 else "rgb24") if frame.shape[-1] == 3 else \
                  ("rgba64le" if frame.dtype == np.uint16 else "rgba")
            frame = av.VideoFrame.from_numpy_buffer(frame, format=fmt)
        if frame.format.name != "rgb24":
            frame = frame.reformat(format="rgb24")
        # AVFrame rows can contain alignment bytes. Store only RGB samples.
        plane = frame.planes[0]
        row_bytes = self.width * 3
        if frame.width != self.width or frame.height != self.height:
            raise RuntimeError("Compressed RGB cache dimensions changed within a pass.")
        pixels = (memoryview(plane)[:self.pixel_bytes] if plane.line_size == row_bytes else
                  np.ascontiguousarray(np.frombuffer(plane, np.uint8).reshape(self.height, plane.line_size)[:, :row_bytes]))
        pixels = memoryview(pixels).cast("B")
        packed = zstd.compress(pixels, level=COMPRESSION_LEVEL)
        compressed = len(packed) < self.pixel_bytes
        data = packed if compressed else pixels
        header = RECORD.pack(timing.pts, timing.duration, timing.time_base.numerator,
            timing.time_base.denominator, timing.segment, timing.source_index, int(timing.generated), int(compressed))
        # Replacing a temporal ring slot truncates the previous payload first;
        # no second cache file can overlap its reserved raw maximum.
        with self.path(index).open("wb") as target:
            target.write(MAGIC)
            target.write(header)
            target.write(data)

    def write_frame(self, index, frame, **values):
        self.write_rgb(index, frame, FrameTiming(frame.pts, frame.duration or 0, Fraction(frame.time_base),
            values.get("segment", 0), values.get("source_index", -1), values.get("generated", False)))

    def frame(self, index):
        import av
        path = self.path(index)
        timing, compressed = _record(path)
        with path.open("rb") as source:
            source.seek(len(MAGIC) + RECORD.size)
            data = source.read(self.pixel_bytes + 1)
        if len(data) > self.pixel_bytes:
            raise RuntimeError("Compressed RGB payload exceeds its raw reservation.")
        if compressed:
            decoder = zstd.ZstdDecompressor()
            data = decoder.decompress(data, max_length=self.pixel_bytes + 1)
            if not decoder.eof or decoder.unused_data:
                raise RuntimeError("Invalid or oversized compressed RGB frame.")
        if len(data) != self.pixel_bytes:
            raise RuntimeError("Compressed RGB cache returned an incomplete frame.")
        pixels = np.frombuffer(data, np.uint8).reshape(self.height, self.width, 3)
        frame = av.VideoFrame.from_numpy_buffer(pixels, format="rgb24")
        frame.pts, frame.duration, frame.time_base, frame.opaque = timing.pts, timing.duration, timing.time_base, timing
        frame.color_range, frame.colorspace = 2, 0
        return frame

    def commit(self, count, **_):
        if not 0 < count <= self.capacity:
            raise RuntimeError("Cannot commit an empty compressed RGB block.")
        (self.directory / "count.json").write_text(str(count), encoding="utf-8")

    def settle(self):
        count = self.count
        actual = HEADER_BYTES
        for index in range(count):
            path = self.path(index)
            size = path.stat().st_size
            if not len(MAGIC) + RECORD.size < size <= len(MAGIC) + RECORD.size + self.pixel_bytes:
                raise RuntimeError("Compressed RGB frame exceeded its byte reservation.")
            _record(path)
            actual += size
        if actual > self.size:
            raise RuntimeError("Compressed RGB block exceeded its byte reservation.")
        self.budget.release(self.size - actual)
        self.size = actual

    def close(self):
        if not self.closed:
            if self.budget:
                shutil.rmtree(self._owned_path())
                self.budget.release(self.size)
            self.closed = True


class RGBFrameBlocks(FrameBlocks):
    """Controller owner; pixel payloads stay in the worker and owned files."""

    def __init__(self, directory, budget, width, height, pixel_format, capacity, **options):
        super().__init__(directory, budget, width, height, "rgb24", capacity, **options)

    def allocate(self, index):
        index = int(index)
        if not 0 <= index * self.block_frames < self.capacity:
            raise RuntimeError("Compressed RGB block is outside its planned part.")
        if index not in self.blocks:
            if index != len(self.blocks):
                raise RuntimeError("Compressed RGB blocks must be allocated in order.")
            capacity = min(self.block_frames, self.capacity - index * self.block_frames)
            descriptor = dict(width=self.width, height=self.height, capacity=capacity,
                owner_root=str(self.directory.resolve()),
                directory=str((self.directory / ("rgb-" + uuid.uuid4().hex)).resolve()))
            self.blocks[index] = RGBBlock(descriptor, budget=self.budget)
        return self.blocks[index].descriptor

    @property
    def descriptor(self):
        return {**super().descriptor, "kind": "rgb-blocks", "compression": "zstd", "depth": 8}

    def commit(self, count, **values):
        super().commit(count, **values)
        for block in self.blocks.values():
            block.settle()


class RGBWorkerBlocks(WorkerBlocks):
    def __init__(self, descriptor, emit, *, output=False):
        super().__init__(descriptor, emit, output=output)
        self.timings = {}

    def _block(self, index):
        if not 0 <= index * self.block_frames < self.capacity:
            raise RuntimeError("Compressed RGB part capacity exceeded.")
        if index not in self.blocks:
            if index not in self.descriptors:
                if not self.output:
                    raise RuntimeError("The compressed RGB input block was already retired.")
                self.descriptors[index] = self._request("cache-allocate", index=index)["mapping"]
            self.blocks[index] = RGBBlock(self.descriptors[index])
        return self.blocks[index]

    def write_timing(self, index, timing):
        # NR allocates its slot before an asynchronous readback completes.
        block, _ = self._location(index)
        self._block(block)
        self.timings[index] = timing

    def write_rgb(self, index, pixels, timing):
        block, local = self._location(index)
        self._block(block).write_rgb(local, pixels, timing)
        self.timings.pop(index, None)

    def frames(self):
        # Decode ahead while the GPU consumes the current frame. Returned
        # AVFrames own detached RGB bytes, so file retirement never invalidates
        # a native input. Two queued frames share the existing working reserve.
        count = self.count
        def decode(index):
            self._retire_before(index)
            return self.frame(index)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rgb-cache-read") as executor:
            pending = deque(executor.submit(decode, i) for i in range(min(2, count)))
            try:
                for index in range(count):
                    frame = pending.popleft().result()
                    if index + 2 < count:
                        pending.append(executor.submit(decode, index + 2))
                    yield frame
            finally:
                for future in pending:
                    future.cancel()
