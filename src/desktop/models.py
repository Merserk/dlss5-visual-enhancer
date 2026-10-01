from __future__ import annotations

from dataclasses import dataclass
import os
import math
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractListModel, QByteArray, QModelIndex, Property, Qt, Signal, Slot


@dataclass
class BatchEntry:
    index: int
    input_path: str
    output_path: str = ""
    state: str = "Queued"
    progress: float = 0.0
    detail: str = ""
    elapsed_seconds: float = 0.0
    input_dimensions: str = ""
    output_dimensions: str = ""
    thumbnail_url: str = ""
    selected: bool = False
    file_size_bytes: int = 0
    duration_seconds: float = 0.0
    frame_count: int = 0
    processed_frames: int = 0
    processing_total_frames: int = 0
    processing_fps: float = 0.0
    _frame_sample: tuple[str, int, float] | None = None

    @property
    def file_name(self) -> str:
        return Path(self.input_path).name

    @property
    def remaining_seconds(self) -> float:
        """Estimate the entire file job from job progress, never stage frame counters."""
        if self.state != "Running" or self.elapsed_seconds < 1 or self.progress <= 0.01:
            return -1.0
        return self.elapsed_seconds * (1 - self.progress) / self.progress


class BatchListModel(QAbstractListModel):
    """QML list model used by all desktop batch queues."""

    countChanged = Signal()
    selectedIndexChanged = Signal()

    FileNameRole = Qt.ItemDataRole.UserRole + 1
    InputPathRole = Qt.ItemDataRole.UserRole + 2
    OutputPathRole = Qt.ItemDataRole.UserRole + 3
    StateRole = Qt.ItemDataRole.UserRole + 4
    ProgressRole = Qt.ItemDataRole.UserRole + 5
    DetailRole = Qt.ItemDataRole.UserRole + 6
    ElapsedSecondsRole = Qt.ItemDataRole.UserRole + 7
    InputDimensionsRole = Qt.ItemDataRole.UserRole + 8
    OutputDimensionsRole = Qt.ItemDataRole.UserRole + 9
    ThumbnailUrlRole = Qt.ItemDataRole.UserRole + 10
    IndexRole = Qt.ItemDataRole.UserRole + 11
    SelectedRole = Qt.ItemDataRole.UserRole + 12
    FileSizeBytesRole = Qt.ItemDataRole.UserRole + 13
    DurationSecondsRole = Qt.ItemDataRole.UserRole + 14
    FrameCountRole = Qt.ItemDataRole.UserRole + 15
    ProcessedFramesRole = Qt.ItemDataRole.UserRole + 16
    ProcessingTotalFramesRole = Qt.ItemDataRole.UserRole + 17
    ProcessingFpsRole = Qt.ItemDataRole.UserRole + 18
    RemainingSecondsRole = Qt.ItemDataRole.UserRole + 19

    # Native stages include a total, while rolling pipelines and FFmpeg with
    # incomplete container metadata may only report the processed frame count.
    # Throughput needs the count and elapsed time, never a known total.
    _frame_counter = re.compile(r"(\d[\d,]*)(?:\s*/\s*(\d[\d,]*))?\s+frames\b", re.IGNORECASE)

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._entries: list[BatchEntry] = []
        self._selected_index = -1

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._entries)

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._entries)

    @Property(int, notify=selectedIndexChanged)
    def selectedIndex(self) -> int:
        return self._selected_index

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]
        mapping = {
            self.FileNameRole: entry.file_name,
            self.InputPathRole: entry.input_path,
            self.OutputPathRole: entry.output_path,
            self.StateRole: entry.state,
            self.ProgressRole: entry.progress,
            self.DetailRole: entry.detail,
            self.ElapsedSecondsRole: entry.elapsed_seconds,
            self.InputDimensionsRole: entry.input_dimensions,
            self.OutputDimensionsRole: entry.output_dimensions,
            self.ThumbnailUrlRole: entry.thumbnail_url,
            self.IndexRole: entry.index,
            self.SelectedRole: entry.selected,
            self.FileSizeBytesRole: float(entry.file_size_bytes),
            self.DurationSecondsRole: entry.duration_seconds,
            self.FrameCountRole: entry.frame_count,
            self.ProcessedFramesRole: entry.processed_frames,
            self.ProcessingTotalFramesRole: entry.processing_total_frames,
            self.ProcessingFpsRole: entry.processing_fps,
            self.RemainingSecondsRole: entry.remaining_seconds,
        }
        return mapping.get(role)

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            self.FileNameRole: QByteArray(b"fileName"),
            self.InputPathRole: QByteArray(b"inputPath"),
            self.OutputPathRole: QByteArray(b"outputPath"),
            self.StateRole: QByteArray(b"itemState"),
            self.ProgressRole: QByteArray(b"progress"),
            self.DetailRole: QByteArray(b"detail"),
            self.ElapsedSecondsRole: QByteArray(b"elapsedSeconds"),
            self.InputDimensionsRole: QByteArray(b"inputDimensions"),
            self.OutputDimensionsRole: QByteArray(b"outputDimensions"),
            self.ThumbnailUrlRole: QByteArray(b"thumbnailUrl"),
            self.IndexRole: QByteArray(b"itemIndex"),
            self.SelectedRole: QByteArray(b"selected"),
            self.FileSizeBytesRole: QByteArray(b"fileSizeBytes"),
            self.DurationSecondsRole: QByteArray(b"durationSeconds"),
            self.FrameCountRole: QByteArray(b"frameCount"),
            self.ProcessedFramesRole: QByteArray(b"processedFrames"),
            self.ProcessingTotalFramesRole: QByteArray(b"processingTotalFrames"),
            self.ProcessingFpsRole: QByteArray(b"processingFps"),
            self.RemainingSecondsRole: QByteArray(b"remainingSeconds"),
        }

    @staticmethod
    def _new_entry(index: int, path: str) -> BatchEntry:
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        return BatchEntry(index=index, input_path=path, file_size_bytes=size)

    @Slot(list)
    def set_items(self, paths: list[str]) -> None:
        self.beginResetModel()
        valid_paths = [p for p in paths if p and Path(p).is_file()]
        self._entries = [self._new_entry(i, p) for i, p in enumerate(valid_paths)]
        self._selected_index = 0 if self._entries else -1
        if self._selected_index >= 0:
            self._entries[0].selected = True
        self.endResetModel()
        self.countChanged.emit()
        self.selectedIndexChanged.emit()

    @Slot(list)
    def add_items(self, paths: list[str]) -> None:
        existing_paths = {str(Path(e.input_path).resolve()).lower() for e in self._entries}
        new_paths: list[str] = []
        for p in paths:
            if not p or not Path(p).is_file():
                continue
            key = str(Path(p).resolve()).lower()
            if key not in existing_paths:
                existing_paths.add(key)
                new_paths.append(p)
        if not new_paths:
            return
        start = len(self._entries)
        self.beginInsertRows(QModelIndex(), start, start + len(new_paths) - 1)
        for i, path in enumerate(new_paths):
            self._entries.append(self._new_entry(start + i, path))
        self.endInsertRows()
        self.countChanged.emit()
        if self._selected_index < 0:
            self.select(0)

    def add_scanned_items(self, paths: list[str], file_sizes: dict[str, int]) -> None:
        """Accept canonical paths already checked by the input scan worker.

        Do not stat or resolve files on the GUI thread again: offline network
        drives, cloud hydration and file scanners can block those operations.
        """
        existing = {os.path.normcase(os.path.abspath(e.input_path)) for e in self._entries}
        new_paths = []
        for path in paths:
            key = os.path.normcase(os.path.abspath(path))
            if path and key not in existing:
                existing.add(key)
                new_paths.append(path)
        if not new_paths:
            return
        start = len(self._entries)
        self.beginInsertRows(QModelIndex(), start, start + len(new_paths) - 1)
        for offset, path in enumerate(new_paths):
            self._entries.append(BatchEntry(index=start + offset, input_path=path,
                                           file_size_bytes=max(0, int(file_sizes.get(path, 0)))))
        self.endInsertRows()
        self.countChanged.emit()
        if self._selected_index < 0:
            self.select(0)

    @Slot(int)
    def select(self, row: int) -> None:
        if not (0 <= row < len(self._entries)):
            return
        old = self._selected_index
        if old == row:
            return
        self._selected_index = row
        if 0 <= old < len(self._entries):
            self._entries[old].selected = False
            idx = self.index(old)
            self.dataChanged.emit(idx, idx, [self.SelectedRole])
        self._entries[row].selected = True
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [self.SelectedRole])
        self.selectedIndexChanged.emit()

    @Slot(int)
    def remove_item(self, row: int) -> None:
        if not (0 <= row < len(self._entries)):
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self._entries.pop(row)
        for i, e in enumerate(self._entries):
            e.index = i
        self.endRemoveRows()
        if row < len(self._entries):
            self.dataChanged.emit(self.index(row), self.index(len(self._entries) - 1), [self.IndexRole])
        if not self._entries:
            new_selected = -1
        elif self._selected_index == row:
            new_selected = min(row, len(self._entries) - 1)
        elif self._selected_index > row:
            new_selected = self._selected_index - 1
        else:
            new_selected = self._selected_index
        for e in self._entries:
            e.selected = False
        self._selected_index = new_selected
        if new_selected >= 0:
            self._entries[new_selected].selected = True
            idx = self.index(new_selected)
            self.dataChanged.emit(idx, idx, [self.SelectedRole])
        self.countChanged.emit()
        self.selectedIndexChanged.emit()

    @Slot()
    def clear(self) -> None:
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self._selected_index = -1
        self.endResetModel()
        self.countChanged.emit()
        self.selectedIndexChanged.emit()

    def get_paths(self) -> list[str]:
        return [e.input_path for e in self._entries]

    def selected_path(self) -> str | None:
        if 0 <= self._selected_index < len(self._entries):
            return self._entries[self._selected_index].input_path
        return None

    def update_metadata(self, row: int, *, input_dimensions: str | None = None,
                        output_dimensions: str | None = None, thumbnail_url: str | None = None,
                        duration_seconds: float | None = None, frame_count: int | None = None) -> None:
        if not (0 <= row < len(self._entries)):
            return
        entry = self._entries[row]
        roles: list[int] = []
        if input_dimensions is not None:
            entry.input_dimensions = input_dimensions
            roles.append(self.InputDimensionsRole)
        if output_dimensions is not None:
            entry.output_dimensions = output_dimensions
            roles.append(self.OutputDimensionsRole)
        if thumbnail_url is not None:
            entry.thumbnail_url = thumbnail_url
            roles.append(self.ThumbnailUrlRole)
        if duration_seconds is not None:
            entry.duration_seconds = max(0.0, float(duration_seconds))
            roles.append(self.DurationSecondsRole)
        if frame_count is not None:
            entry.frame_count = max(0, int(frame_count))
            roles.append(self.FrameCountRole)
        if roles:
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, roles)

    def update_item_progress(self, index: int, state: str, progress: float, detail: str = "",
                             output_path: str = "", elapsed_seconds: float = 0.0) -> None:
        if not (0 <= index < len(self._entries)):
            return
        state_aliases = {"Processing": "Running", "Complete": "Completed", "Canceled": "Cancelled"}
        state = state_aliases.get(state, state)
        entry = self._entries[index]
        entry.state = state
        # Backend fractions cover the whole file, including export. Stage changes
        # must never rewind the bar, and only a published result can reach 100%.
        if state in {"Completed", "CompletedWithWarnings"}:
            entry.progress = 1.0
        elif state == "Queued":
            entry.progress = 0.0
        elif math.isfinite(progress):
            limit = 0.99 if state == "Running" else 1.0
            entry.progress = max(entry.progress, max(0.0, min(limit, float(progress))))
        entry.detail = detail
        if output_path:
            entry.output_path = output_path
        if math.isfinite(elapsed_seconds) and elapsed_seconds >= 0:
            entry.elapsed_seconds = max(entry.elapsed_seconds, elapsed_seconds)
        counter = self._frame_counter.search(detail) if state == "Running" else None
        if counter:
            current = int(counter.group(1).replace(",", ""))
            total = int((counter.group(2) or "0").replace(",", ""))
            stage = detail[:counter.start()].rstrip(" :·–-")
            sample = entry._frame_sample
            sample_time = entry.elapsed_seconds
            if not sample or sample[0] != stage or current < sample[1]:
                entry.processing_fps = 0.0
                entry._frame_sample = (stage, current, sample_time)
            elif sample_time > sample[2]:
                measured = (current - sample[1]) / (sample_time - sample[2])
                entry.processing_fps = measured if entry.processing_fps <= 0 else (
                    0.35 * measured + 0.65 * entry.processing_fps)
                entry._frame_sample = (stage, current, sample_time)
            # Repeated/invalid timestamps cannot measure a rate. Retain the
            # baseline so the next timed update includes all intervening frames.
            entry.processed_frames, entry.processing_total_frames = current, total
        else:
            entry._frame_sample = None
            entry.processing_fps = 0.0
            entry.processed_frames = 0
            entry.processing_total_frames = 0
        idx = self.index(index)
        self.dataChanged.emit(idx, idx, [self.StateRole, self.ProgressRole, self.DetailRole,
                                         self.OutputPathRole, self.ElapsedSecondsRole, self.ProcessedFramesRole,
                                         self.ProcessingTotalFramesRole, self.ProcessingFpsRole, self.RemainingSecondsRole])


    @Slot()
    def clear_completed(self) -> None:
        remove = [i for i, e in enumerate(self._entries) if e.state in {"Completed", "CompletedWithWarnings", "Cancelled"}]
        if not remove:
            return
        # Remove rows in place so retained files keep their metadata and progress.
        for row in reversed(remove):
            self.remove_item(row)

    @Slot()
    def retry_failed(self) -> None:
        changed = False
        for row, entry in enumerate(self._entries):
            if entry.state in {"Failed", "Cancelled"}:
                entry.state = "Queued"
                entry.progress = 0.0
                entry.detail = ""
                entry.output_path = ""
                entry.elapsed_seconds = 0.0
                entry.processed_frames = entry.processing_total_frames = 0
                entry.processing_fps = 0.0
                entry._frame_sample = None
                idx = self.index(row)
                self.dataChanged.emit(idx, idx, [self.StateRole, self.ProgressRole, self.DetailRole,
                                                self.OutputPathRole, self.ElapsedSecondsRole, self.ProcessedFramesRole,
                                                self.ProcessingTotalFramesRole, self.ProcessingFpsRole, self.RemainingSecondsRole])
                changed = True
        if changed and self._selected_index < 0 and self._entries:
            self.select(0)

    def reset_all_states(self) -> None:
        if not self._entries:
            return
        for e in self._entries:
            e.state = "Queued"
            e.progress = 0.0
            e.detail = ""
            e.output_path = ""
            e.elapsed_seconds = 0.0
            e.processed_frames = e.processing_total_frames = 0
            e.processing_fps = 0.0
            e._frame_sample = None
        self.dataChanged.emit(self.index(0), self.index(len(self._entries) - 1),
                              [self.StateRole, self.ProgressRole, self.DetailRole,
                               self.OutputPathRole, self.ElapsedSecondsRole, self.ProcessedFramesRole,
                               self.ProcessingTotalFramesRole, self.ProcessingFpsRole, self.RemainingSecondsRole])
