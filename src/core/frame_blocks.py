"""Incremental cache blocks; consumed generations retire during the next pass."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

from .pass_cache import FrameMapping, HEADER_BYTES, RECORD, frame_bytes

BLOCK_FRAMES = 32
BLOCK_TARGET_BYTES = 64 * 1024 * 1024
RETAINED_BLOCKS = 3
_CONTROL_LOCK = threading.Lock()


def block_frame_limit(width, height, pixel_format):
    return max(1, min(BLOCK_FRAMES, BLOCK_TARGET_BYTES // frame_bytes(width, height, pixel_format)))


def blocks_storage_bytes(width, height, pixel_format, capacity, *, block_frames=None):
    block_frames = block_frames or block_frame_limit(width, height, pixel_format)
    return int(capacity) * (frame_bytes(width, height, pixel_format) + RECORD.size) + \
        ((int(capacity) + block_frames - 1) // block_frames) * HEADER_BYTES


class FrameBlocks:
    """Controller owner. Allocate on demand under this part's shared budget."""

    def __init__(self, directory, budget, width, height, pixel_format, capacity, *, backing=None, block_frames=None):
        self.directory, self.budget = Path(directory), budget
        self.width, self.height, self.format = int(width), int(height), pixel_format
        self.capacity, self.backing = int(capacity), backing
        self.block_frames = int(block_frames or block_frame_limit(width, height, pixel_format))
        if not 1 <= self.block_frames <= BLOCK_FRAMES or self.capacity < 1:
            raise ValueError("Invalid part cache block capacity.")
        self.history_start = self.history_count = 0
        self.committed_count = None
        self.blocks = {}
        self.closed = False

    def allocate(self, index):
        index = int(index)
        if not 0 <= index * self.block_frames < self.capacity:
            raise RuntimeError("The worker requested a cache block outside its planned capacity.")
        if index not in self.blocks:
            if index != len(self.blocks):
                raise RuntimeError("Cache blocks must be allocated in order.")
            capacity = min(self.block_frames, self.capacity - index * self.block_frames)
            self.blocks[index] = FrameMapping.create(self.directory, self.budget, self.width, self.height,
                                                   self.format, capacity, backing=self.backing)
        return self.blocks[index].descriptor

    def retire(self, index):
        block = self.blocks.get(int(index))
        if block:
            block.close()
            del self.blocks[int(index)]

    @property
    def descriptor(self):
        return dict(version=4, kind="blocks", width=self.width, height=self.height, format=self.format,
                    capacity=self.capacity, block_frames=self.block_frames, global_history=True,
                    retire_ack=False,
                    history_start=self.history_start, history_count=self.history_count,
                    count=self.committed_count, blocks=[dict(index=index, mapping=block.descriptor)
                        for index, block in sorted(self.blocks.items())])

    @property
    def count(self):
        return (self.committed_count if self.committed_count is not None else
                sum(block.count for block in self.blocks.values()))

    def _physical(self, index):
        return (self.history_start + index) % BLOCK_FRAMES if index < self.history_count else index

    def timing(self, index):
        physical = self._physical(index)
        return self.blocks[physical // self.block_frames].timing(physical % self.block_frames)

    def commit(self, count, *, history_start=0, history_count=0):
        if (not 0 < count <= self.capacity or not 0 <= history_count <= min(BLOCK_FRAMES, count)
                or not 0 <= history_start < BLOCK_FRAMES
                or sum(block.count for block in self.blocks.values()) != count):
            raise RuntimeError("The part cache committed invalid timing or frame ownership.")
        self.committed_count = int(count)
        self.history_start, self.history_count = int(history_start), int(history_count)

    def close(self):
        if not self.closed:
            for block in self.blocks.values():
                block.close()
            self.blocks.clear()
            self.closed = True


class WorkerBlocks:
    """Borrowed views; the owner serializes allocations and ordered retirements."""

    def __init__(self, descriptor, emit, *, output=False):
        self.width, self.height = descriptor["width"], descriptor["height"]
        self.format, self.capacity = descriptor["format"], int(descriptor["capacity"])
        self.block_frames = int(descriptor.get("block_frames", BLOCK_FRAMES))
        self.global_history = bool(descriptor.get("global_history"))
        self.retire_ack = bool(descriptor.get("retire_ack", True))
        self.history_start = int(descriptor.get("history_start", 0))
        self.history_count = int(descriptor.get("history_count", 0))
        self.committed_count = descriptor.get("count")
        self.descriptors = {item["index"]: item["mapping"] for item in descriptor["blocks"]}
        self.blocks = {}
        self.emit, self.output = emit, output
        self.recent = []
        self.last_use = None

    def _request(self, event, **values):
        # A filter can consume input on its bounded feeder thread while its
        # main thread allocates output. Keep control replies with their caller.
        with _CONTROL_LOCK:
            self.emit(dict(event=event, **values))
            line = sys.stdin.buffer.readline()
        if not line:
            raise RuntimeError("The cache owner closed its control channel.")
        response = json.loads(line)
        if response.get("error"):
            raise RuntimeError(response["error"])
        return response

    def _block(self, index):
        if not 0 <= index * self.block_frames < self.capacity:
            raise RuntimeError("Part capacity exceeded before allocating a block.")
        if index not in self.blocks:
            if index not in self.descriptors:
                if not self.output:
                    raise RuntimeError("The input cache block was already retired.")
                self.descriptors[index] = self._request("cache-allocate", index=index)["mapping"]
            self.blocks[index] = FrameMapping(self.descriptors[index])
        return self.blocks[index]

    @property
    def count(self):
        return (int(self.committed_count) if self.committed_count is not None else
                sum(self._block(index).count for index in sorted(self.descriptors)))

    def _physical(self, index):
        if self.global_history and index < self.history_count:
            return (self.history_start + index) % BLOCK_FRAMES
        return index

    def _location(self, index):
        physical = self._physical(index)
        return physical // self.block_frames, physical % self.block_frames

    def pixels(self, index):
        block, local = self._location(index)
        return self._block(block).pixels(local)

    def timing(self, index):
        block, local = self._location(index)
        return self._block(block).timing(local)

    def write_timing(self, index, timing):
        block, local = self._location(index)
        self._block(block).write_timing(local, timing)

    def write_frame(self, index, frame, **values):
        block, local = self._location(index)
        self._block(block).write_frame(local, frame, **values)

    def frame(self, index):
        block, local = self._location(index)
        return self._block(block).frame(local)

    def _retire_before(self, index):
        if self.output:
            return
        current, _ = self._location(index)
        if not self.recent or self.recent[-1] != current:
            self.recent.append(current)
            # Two recent blocks cover borrowed inputs and the bounded async
            # calls. A wrapped history block can also have a future use.
            self.recent = self.recent[-(RETAINED_BLOCKS - 1):]
        if self.last_use is None:
            self.last_use = {}
            for logical in range(self.count):
                block, _ = self._location(logical)
                self.last_use[block] = logical
        for old in tuple(self.blocks):
            if old not in self.recent and self.last_use[old] < index:
                self.blocks[old].close()
                del self.blocks[old]
                del self.descriptors[old]
                if self.retire_ack:
                    self._request("cache-retire", index=old)
                else:
                    # The borrowed mapping is already closed. FIFO control
                    # events release its owner before later output allocation,
                    # so no GPU pass needs to wait for a retirement reply.
                    with _CONTROL_LOCK:
                        self.emit(dict(event="cache-retire", index=old, acknowledge=False))

    def frames(self):
        count = self.count
        # Committed descriptors open input blocks on demand. Legacy headers
        # require borrowed mappings but still do not allocate pixel copies.
        for index in range(count):
            self._retire_before(index)
            yield self.frame(index)

    def timed_pixels(self):
        count = self.count
        for index in range(count):
            self._retire_before(index)
            yield self.pixels(index), self.timing(index)

    def commit(self, count, *, history_start=0, history_count=0):
        for index, block in sorted(self.blocks.items()):
            local = min(block.capacity, count - index * self.block_frames)
            if local <= 0:
                raise RuntimeError("An unused output block cannot be committed.")
            block.commit(local, history_start=history_start if index == 0 and not self.global_history else 0,
                         history_count=history_count if index == 0 and not self.global_history else 0)
        if self.global_history:
            self._request("cache-commit", count=count, history_start=history_start, history_count=history_count)
            self.committed_count = int(count)
            self.history_start, self.history_count = int(history_start), int(history_count)
