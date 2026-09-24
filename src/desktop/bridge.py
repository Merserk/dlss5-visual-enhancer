from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import platform
import math
import hashlib
import importlib
import shutil
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import fields as _dataclass_fields, replace
from typing import Any

from PySide6.QtCore import QObject, QThreadPool, QTimer, QUrl, Signal, Slot, Property, QSettings, qVersion, QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication, QImage, QPixmap

from ..core.paths import CONFIG_PATH, OUTPUTS, LOGS, FFMPEG, FFPROBE, MPV, APP_TEMP
from ..core.disk_paths import prepare_output_dir, supported_file as _is_supported_media_file
from ..core.cache_cleanup import cleanup_old_caches
from ..core import app_log
from ..core.power import on_battery
from ..core.gpu_detection import LOW_VRAM_THRESHOLD_MB
from ..core.runtime import prepare_runtime, NR_STYLES, UPSCALING_MODES, resolve_upscaling_mode, resolve_output_size
from ..core.ffmpeg import CODEC_CHOICES, ENCODING_QUALITIES, FIXED_QUALITY_CODECS, container_for_codec, hdr_mode_supported, probe_video
from ..core.naming import RENAME_MODES, validate_rename
from ..core.nr_composition import inspect_nr_mask, mask_status
from ..settings.models import (
    CONTAINER_CHOICES, DEFAULT_SETTINGS, PREVIEW_ENCODING_CHOICES, UPSCALE_MODE_CHOICES,
    UPSCALE_PREVIEW_LENGTH_CHOICES, UISettings,
    automatic_mask_choice, coerce_hdr_mode, live_effect_options, parse_automatic_mask, _validate,
)
from ..settings.storage import SETTINGS_STATE, load_settings, save_settings
from ..settings.presets import export_settings_preset, export_settings_preset_to, import_settings_preset

from .models import BatchListModel
from .image_provider import PreviewImageProvider
from .workers import JobWorker
from .state import (
    PreviewState, IDLE, SCANNING_INPUTS, LOADING_METADATA, PREVIEW_PREPARING,
    PREVIEW_RUNNING, PREVIEW_CANCELLING, BATCH_PREPARING, BATCH_RUNNING,
    BATCH_CANCELLING, LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING, SHUTTING_DOWN, BUSY_STATES,
)

# Import processing functions
from ..neural_rendering.image.batch import convert_images
from ..neural_rendering.image.preview import render_image_preview
from ..neural_rendering.image.models import ImageConversionOptions, RAW_EXTENSIONS, IMAGE_EXTENSIONS
from ..neural_rendering.image.decoder import decode_image_preview, initialize_image_runtime

from ..neural_rendering.video.batch import convert_videos
from ..neural_rendering.video.preview import process_video_preview
from ..neural_rendering.video.models import ConversionOptions

from ..upscale.image.batch import upscale_images
from ..upscale.image.processor import preview_upscale_image
from ..upscale.image.models import ImageUpscaleOptions, options_from_settings as image_upscale_options_from_settings, output_size as image_upscale_output_size, SCALE_FACTORS as IMAGE_SCALE_FACTORS, VSR_QUALITIES as IMAGE_VSR_QUALITIES, SIZE_MODES as IMAGE_SIZE_MODES

from ..upscale.video.batch import upscale_videos
from ..upscale.video.preview import preview_upscale_native
from ..upscale.video.models import UpscaleOptions, options_from_settings as video_upscale_options_from_settings, output_size as video_upscale_output_size, SCALE_FACTORS as VIDEO_SCALE_FACTORS, VSR_QUALITIES as VIDEO_VSR_QUALITIES, SIZE_MODES as VIDEO_SIZE_MODES, HDR_PRECISION_CHOICES

from ..frame_interpolation.batch import interpolate_videos
from ..frame_interpolation.preview import preview_frame_interpolation_native
from ..frame_interpolation.models import FrameInterpolationOptions, FPS_CHOICES, ENGINE_CHOICES, PREVIEW_LENGTH_CHOICES

from ..live.models import LiveOptions, LIVE_MAX_HEIGHTS, LIVE_MAX_HEIGHT_CHOICES, LIVE_SEGMENT_CHOICES, LIVE_FPS_CHOICES, LIVE_SOURCE_QUALITY_CHOICES
from ..live.pipeline import start_live_session, stop_live_session, live_status, is_live_running, update_live_effects, sweep_stale_live_dirs
from ..live.mpv_embed import MpvEmbedController


# Canonical media-type sets used for auto-switching Image/Video modes and for
# filtering dropped / picked inputs. Image aliases (.jpeg, .tif, .bmp, .heic,
# .heif, .svg) match what the Pillow / rawpy / pillow-heif decoder stack in
# neural_rendering.image.decoder can open; FileDialog filters already offer
# most of these.
IMAGE_SUFFIXES = frozenset(
    {s.lower() for s in IMAGE_EXTENSIONS.values()}
    | {s.lower() for s in RAW_EXTENSIONS}
    | {".jpeg", ".tif", ".bmp", ".heic", ".heif", ".svg"}
)
VIDEO_SUFFIXES = frozenset(
    {
        ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts",
        ".mts", ".m2ts", ".wmv", ".flv",
    }
)

# Browser "Copy image" clipboard handling for Ctrl+V. Chromium puts both an
# https URL and bitmap pixels on the clipboard; the pixels must win (no
# network, exact frame the user saw). Remote URLs / <img src> / URL text fall
# back to a capped download into APP_TEMP.
_PASTE_DOWNLOAD_TIMEOUT_S = 15
_PASTE_DOWNLOAD_MAX_BYTES = 30 * 1024 * 1024
_PASTE_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_PASTE_IMG_SRC_RE = re.compile(
    r"<img\b[^>]*?\bsrc\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE
)
# Responsive/original variants: data-* originals first (authoritative full
# resolution), then the largest srcset entry (convention: ascending widths,
# so the last http(s) entry wins). Generic URL matches stay last.
_PASTE_DATA_SRC_RE = re.compile(
    r"data-(?:src|original|large-image|zoom-image|full-image)\s*=\s*[\"'](https?://[^\"']+)[\"']",
    re.IGNORECASE,
)
_PASTE_SRCSET_RE = re.compile(
    r"srcset\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)
_PASTE_CONTENT_TYPE_SUFFIX = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
}
_PASTE_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)


class AppBridge(QObject):
    """Central native bridge between Qt Quick/QML and backend AI runtimes."""

    LIVE_EFFECT_FIELDS = {
        "live_nr_style", "live_nr_intensity", "live_nr_passes", "live_local_tone_strength",
        "live_local_structure_strength", "live_skin_structure_strength", "live_automatic_mask",
        "live_nr_color_strength", "live_tone_preservation", "live_face_skin_protection",
        "live_grain_preservation", "live_shimmer_suppression", "live_mask_feather", "nr_mask",
    }
    LIVE_RESTART_FIELDS = {"ai_gpu_uuid", "video_gpu_uuid", "live_upscaling_factor"}

    # FI settings that invalidate pre-rendered timeline ranges. A tweak
    # here makes existing green spans stale, so they are cleared. Rename /
    # output-only settings are excluded, as is the preview length itself
    # (older spans remain valid footage for their range). Realtime preview
    # stays disabled for Frame Interpolation regardless (manual Preview only).
    FI_RANGE_FIELDS = {
        "frame_interpolation_target_fps", "frame_interpolation_engine",
        "frame_interpolation_codec", "frame_interpolation_quality",
        "frame_interpolation_hdr_mode", "preview_encoding",
    }
    # Cap stored ranges per context so repeated previews can't grow state.
    PREVIEW_RANGE_LIMIT = 32

    # Settings that materially change the visual result shown in the preview.
    # Changes to these fields are debounced and automatically re-render the
    # selected item when Realtime Preview is enabled.  Rename/output-only
    # settings are intentionally excluded so they do not waste GPU work.
    AUTO_PREVIEW_FIELDS = {
        "ai_gpu_uuid", "video_gpu_uuid",
        "nr_style", "nr_intensity", "nr_passes",
        "local_tone_strength", "local_structure_strength",
        "skin_structure_strength", "automatic_mask",
        "nr_color_strength", "tone_preservation",
        "face_skin_protection", "grain_preservation",
        "shimmer_suppression", "mask_feather", "nr_mask",
        "upscaling_factor",
        "preview_encoding",
        "upscale_image_vsr_quality", "upscale_image_size_mode",
        "upscale_image_scale_factor", "upscale_image_width",
        "upscale_image_height", "upscale_image_aspect_lock",
        "upscale_vsr_enabled", "upscale_vsr_quality",
        "upscale_size_mode", "upscale_scale_factor",
        "upscale_width", "upscale_height", "upscale_aspect_lock",
        "upscale_hdr_enabled", "upscale_hdr_contrast",
        "upscale_hdr_saturation", "upscale_hdr_middle_gray",
        "upscale_hdr_peak_luminance", "upscale_hdr_precision",
        "upscale_codec", "upscale_quality",
        "frame_interpolation_target_fps", "frame_interpolation_engine",
        "frame_interpolation_codec", "frame_interpolation_quality",
        "frame_interpolation_hdr_mode",
        "codec", "quality", "hdr_mode",
    }
    UPSCALE_RANGE_FIELDS = {
        "ai_gpu_uuid", "video_gpu_uuid", "preview_encoding",
        "upscale_vsr_enabled", "upscale_vsr_quality",
        "upscale_size_mode", "upscale_scale_factor",
        "upscale_width", "upscale_height", "upscale_aspect_lock",
        "upscale_hdr_enabled", "upscale_hdr_contrast",
        "upscale_hdr_saturation", "upscale_hdr_middle_gray",
        "upscale_hdr_peak_luminance", "upscale_hdr_precision",
        "upscale_codec", "upscale_quality",
    }

    # UI State Signals
    activeTabChanged = Signal()
    nrModeChanged = Signal()
    upscaleModeChanged = Signal()
    mediaPreviewChanged = Signal()
    isProcessingChanged = Signal()
    overallProgressChanged = Signal()
    statusMessageChanged = Signal()
    splitPositionChanged = Signal()
    hasOutputPreviewChanged = Signal()
    previewInputUrlChanged = Signal()
    previewOutputUrlChanged = Signal()
    previewPosterUrlChanged = Signal()
    previewSourceChanged = Signal()
    previewRangesChanged = Signal()
    inputInfoTextChanged = Signal()
    outputInfoTextChanged = Signal()
    logAppended = Signal(str)
    backendLogReceived = Signal(str, str, str)
    operationStateChanged = Signal()
    runtimeStateChanged = Signal()
    layoutChanged = Signal()
    sourceSizeChanged = Signal()
    outputSizeChanged = Signal()
    batchExportRequested = Signal(str)

    # Settings Signals
    settingsUpdated = Signal()
    gpuChoicesChanged = Signal()
    presetStatusChanged = Signal()
    liveStatusTextChanged = Signal()
    isLiveRunningChanged = Signal()
    isLiveStartingChanged = Signal()
    liveTelemetryChanged = Signal()
    autoPreviewChanged = Signal()

    def __init__(self, image_provider: PreviewImageProvider, parent: Any = None) -> None:
        super().__init__(parent)
        self._image_provider = image_provider
        self._thread_pool = QThreadPool.globalInstance()
        self._active_worker: JobWorker | None = None
        self._live_worker: JobWorker | None = None
        self._scan_worker: JobWorker | None = None
        self._metadata_worker: JobWorker | None = None
        self._operation_state = IDLE
        self._operation_id = 0
        self._shutting_down = False
        self._live_effects_dirty = False
        self._stop_live_after_start = False
        # Ctrl+V paste request: when the next addFiles scan adds exactly one
        # new file, auto-select it so its preview loads immediately.
        # Consumed by addFiles; only paste call sites set it.
        self._paste_select_single = False

        # Workflows: "neural-rendering", "upscale", "frame-interpolation", "live", "settings", "help"
        self._active_tab = "neural-rendering"
        self._nr_mode = "Image"

        # Settings are loaded before runtime preparation so the native shell can
        # restore user choices even if runtime initialization later fails.
        self._settings = load_settings(CONFIG_PATH)
        SETTINGS_STATE.current = self._settings
        self._upscale_mode = self._settings.upscale_mode if self._settings.upscale_mode in UPSCALE_MODE_CHOICES else "Image"

        # Processing / presentation state. Preview media is stored independently
        # per workflow context and projected through the public properties below.
        self._is_processing = False
        self._overall_progress = 0.0
        self._status_message = "Initializing runtime…"
        self._split_position = 0.5
        self._preset_status = ""
        self._live_status_text = "Idle. Enter a source and press Start Live."
        self._is_live_running = False
        self._is_live_starting = False
        self._preview_generation = 0
        self._live_source_fps = 0.0
        self._live_target_fps = 0.0
        self._live_effective_fps = 0.0
        self._live_guide_ms = 0.0
        self._live_dlss_ms = 0.0
        self._live_encode_ms = 0.0
        self._live_dropped_frames = 0
        self._live_rebuffer_events = 0
        self._live_av_sync_ms = 0.0
        self._live_output_size = ""
        self._live_encoder = ""
        self._live_player_started = False
        self._live_embed_wid = 0
        self._main_window: Any = None
        self._mpv_embed = MpvEmbedController(self)
        try:
            self._mpv_embed.embedError.connect(self._on_mpv_embed_error)
        except Exception:
            pass

        self._contexts: dict[str, PreviewState] = {
            "nr-image": PreviewState(),
            "nr-video": PreviewState(),
            "upscale-image": PreviewState(),
            "upscale-video": PreviewState(),
            "fi-video": PreviewState(),
        }

        # Queues per workflow
        self._nr_image_queue = BatchListModel(self)
        self._nr_video_queue = BatchListModel(self)
        self._upscale_image_queue = BatchListModel(self)
        self._upscale_video_queue = BatchListModel(self)
        self._fi_queue = BatchListModel(self)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(450)
        self._save_timer.timeout.connect(self._flush_settings)

        # Hourly temp sweep (startup sweep lives in app.launch_desktop).
        # Only files older than 24h are removed; staged paste sources in a
        # still-open session are kept fresh via _touch_if_staged on select.
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setSingleShot(False)
        self._sweep_timer.setInterval(3600 * 1000)
        self._sweep_timer.timeout.connect(self._sweep_temp_caches)
        self._sweep_timer.start()

        # The launch path prepares the runtime before loading the QML window.
        self._prepared = None
        self._runtime_state = "Initializing"
        self._runtime_error = ""
        self._startup_warnings: list[str] = []

        # Persistent desktop layout is deliberately separate from render presets.
        # Renamed in v10: "DLSS 5 Visual Enhancer" -> "Visual Enhancer".
        # One-time migration so existing window/layout settings survive.
        self._ui_settings = QSettings("Merserk", "Visual Enhancer")
        try:
            if not self._ui_settings.allKeys():
                legacy = QSettings("Merserk", "DLSS 5 Visual Enhancer")
                for key in legacy.allKeys():
                    self._ui_settings.setValue(key, legacy.value(key))
                if legacy.allKeys():
                    self._ui_settings.sync()
        except Exception:
            pass
        self._auto_preview_enabled = str(
            self._ui_settings.value("preview/realtimeEnabled", "true")
        ).lower() in {"1", "true", "yes", "on"}
        self._auto_preview_pending = False
        self._inspector_width = int(self._ui_settings.value("layout/inspectorWidth", 400))
        self._queue_height = int(self._ui_settings.value("layout/queueHeight", 190))
        self._log_height = int(self._ui_settings.value("layout/logHeight", 220))
        self._focus_preview = False
        self._window_width = int(self._ui_settings.value("window/width", 1440))
        self._window_height = int(self._ui_settings.value("window/height", 920))
        self._window_x = int(self._ui_settings.value("window/x", -1))
        self._window_y = int(self._ui_settings.value("window/y", -1))
        self._window_maximized = str(self._ui_settings.value("window/maximized", "false")).lower() in {"1", "true", "yes"}

        self._auto_preview_timer = QTimer(self)
        self._auto_preview_timer.setSingleShot(True)
        self._auto_preview_timer.setInterval(360)
        self._auto_preview_timer.timeout.connect(self._run_auto_preview)

        # Scrub-realtime: timeline moves in Output/2-Up debounce here before a
        # refresh render starts, so scrubbing never melts the GPU.
        self._scrub_preview_timer = QTimer(self)
        self._scrub_preview_timer.setSingleShot(True)
        self._scrub_preview_timer.setInterval(750)
        self._scrub_preview_timer.timeout.connect(self._fire_scrub_preview)
        self._scrub_pending_pos: float | None = None

        self._preview_cache = Path(tempfile.gettempdir()) / "dlss5-visual-enhancer" / "native-preview"
        try:
            shutil.rmtree(self._preview_cache, ignore_errors=True)
            self._preview_cache.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._preview_cache = OUTPUTS

        # Telemetry timer for Live mode
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(500)
        self._live_timer.timeout.connect(self._poll_live_status)

        # Log bridge. Backend workers may log from arbitrary threads, so they
        # emit into this QObject and are appended on the GUI thread.
        self._logs: list[str] = []
        self.backendLogReceived.connect(self._append_backend_log)
        self._backend_log_listener = lambda level, tag, msg: self.backendLogReceived.emit(str(level), str(tag), str(msg))
        app_log.add_listener(self._backend_log_listener)
        try:
            app_log.init_session()
        except Exception:
            pass

    def _context_key(self) -> str:
        if self._active_tab == "neural-rendering":
            return "nr-image" if self._nr_mode == "Image" else "nr-video"
        if self._active_tab == "upscale":
            return "upscale-image" if self._upscale_mode == "Image" else "upscale-video"
        if self._active_tab == "frame-interpolation":
            return "fi-video"
        return "nr-image"

    def _context(self, key: str | None = None) -> PreviewState:
        return self._contexts[key or self._context_key()]

    def _emit_context(self) -> None:
        self.previewInputUrlChanged.emit()
        self.previewOutputUrlChanged.emit()
        self.previewPosterUrlChanged.emit()
        self.previewSourceChanged.emit()
        self.previewRangesChanged.emit()
        self.mediaPreviewChanged.emit()
        self.hasOutputPreviewChanged.emit()
        self.inputInfoTextChanged.emit()
        self.outputInfoTextChanged.emit()
        self.sourceSizeChanged.emit()
        self.outputSizeChanged.emit()

    def _set_operation(self, state: str, message: str | None = None) -> int:
        self._operation_state = state
        self._is_processing = state in BUSY_STATES
        self._operation_id += 1
        if message is not None:
            self._status_message = message
            self.statusMessageChanged.emit()
        self.operationStateChanged.emit()
        self.isProcessingChanged.emit()
        return self._operation_id

    def _finish_operation(self, operation_id: int, message: str | None = None) -> bool:
        if operation_id != self._operation_id or self._shutting_down:
            return False
        self._operation_state = LIVE_RUNNING if self._is_live_running else IDLE
        self._is_processing = False
        self._active_worker = None
        if message is not None:
            self._status_message = message
            self.statusMessageChanged.emit()
        self.operationStateChanged.emit()
        self.isProcessingChanged.emit()
        return True

    def _invalidate_preview_generation(self) -> int:
        self._preview_generation += 1
        self._context().generation = self._preview_generation
        return self._preview_generation

    def _fi_preview_length_seconds(self) -> float:
        """Selected FI preview clip length in seconds (3/5/10/20/30)."""
        try:
            length = float(self._settings.frame_interpolation_preview_length)
        except (TypeError, ValueError):
            return 3.0
        return length if (length and math.isfinite(length) and length > 0) else 3.0

    def _upscale_preview_length_seconds(self) -> float:
        """Return the selected manual Upscale preview clip length."""
        selected = self._settings.upscale_preview_length
        try:
            return float(selected) if selected in PREVIEW_LENGTH_CHOICES else 3.0
        except (TypeError, ValueError):
            return 3.0

    def _add_rendered_range(self, context_key: str, start_seconds: float, media_path: str,
                            length_seconds: float) -> None:
        """Record a rendered video preview span for the green timeline overlay."""
        if context_key not in {"fi-video", "upscale-video"}:
            return
        ctx = self._contexts.get(context_key)
        if ctx is None:
            return
        try:
            start = max(0.0, float(start_seconds or 0.0))
        except (TypeError, ValueError):
            start = 0.0
        try:
            span = max(0.0, float(length_seconds or 0.0))
        except (TypeError, ValueError):
            span = 0.0
        if not (span > 0):
            return
        try:
            duration = float(ctx.duration_seconds or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        end = start + span
        if duration > 0:
            end = min(end, duration)
        if not (end > start):
            return
        try:
            url = QUrl.fromLocalFile(str(media_path)).toString()
        except Exception:
            return
        if not url:
            return
        ranges = list(getattr(ctx, "rendered_ranges", None) or [])
        ranges.append({"start": start, "end": end, "url": url})
        # Newest last; drop the oldest beyond the cap. Overlapping spans are
        # kept as separate entries so each keeps its own clip; they merge
        # visually by overdraw and playback prefers the newest match.
        if len(ranges) > int(self.PREVIEW_RANGE_LIMIT):
            ranges = ranges[-int(self.PREVIEW_RANGE_LIMIT):]
        ctx.rendered_ranges = ranges
        if context_key == self._context_key():
            self.previewRangesChanged.emit()

    def _clear_rendered_ranges(self, context_key: str, emit: bool = True) -> None:
        """Drop spans rendered with stale settings or replaced by a frame preview."""
        ctx = self._contexts.get(context_key)
        if ctx is None:
            return
        if not ctx.rendered_ranges:
            return
        ctx.rendered_ranges = []
        if emit and self._context_key() == context_key:
            self.previewRangesChanged.emit()

    # =========================================================================
    # Properties: General UI State
    # =========================================================================
    @Property(str, notify=activeTabChanged)
    def activeTab(self) -> str:
        return self._active_tab

    @activeTab.setter
    def activeTab(self, tab: str) -> None:
        if tab not in {"neural-rendering", "upscale", "frame-interpolation", "live", "settings", "help"}:
            return
        if self._active_tab != tab:
            self._active_tab = tab
            self.activeTabChanged.emit()
            # The Live video is a native child window (MPV --wid) that paints
            # above the Qt Quick scene and ignores StackLayout/clip. Hide it
            # whenever the user leaves the Live tab so it can never overlay
            # Upscale/Neural/Frame-Interpolation; the session keeps running.
            try:
                self._mpv_embed.setTabActive(tab == "live")
            except Exception:
                pass
            self._emit_context()
            self._schedule_auto_preview(180)

    @Property(str, notify=nrModeChanged)
    def nrMode(self) -> str:
        return self._nr_mode

    @nrMode.setter
    def nrMode(self, mode: str) -> None:
        if mode not in {"Image", "Video"}:
            return
        if self._nr_mode != mode:
            self._nr_mode = mode
            self.nrModeChanged.emit()
            self._emit_context()
            self._schedule_auto_preview(180)

    @Property(str, notify=upscaleModeChanged)
    def upscaleMode(self) -> str:
        return self._upscale_mode

    @upscaleMode.setter
    def upscaleMode(self, mode: str) -> None:
        if mode not in UPSCALE_MODE_CHOICES:
            return
        if self._upscale_mode != mode:
            if not self._save_setting(upscale_mode=mode):
                return
            self._upscale_mode = mode
            self.upscaleModeChanged.emit()
            self._emit_context()
            self._schedule_auto_preview(180)

    @Property(str, notify=settingsUpdated)
    def upscalePreviewLength(self) -> str:
        return self._settings.upscale_preview_length

    @upscalePreviewLength.setter
    def upscalePreviewLength(self, value: str) -> None:
        if str(value) in UPSCALE_PREVIEW_LENGTH_CHOICES:
            self._save_setting(upscale_preview_length=str(value))

    @Property(bool, notify=isProcessingChanged)
    def isProcessing(self) -> bool:
        return self._is_processing

    @Property(float, notify=overallProgressChanged)
    def overallProgress(self) -> float:
        return self._overall_progress

    @Property(str, notify=statusMessageChanged)
    def statusMessage(self) -> str:
        return self._status_message

    @Property(float, notify=splitPositionChanged)
    def splitPosition(self) -> float:
        return self._split_position

    @splitPosition.setter
    def splitPosition(self, val: float) -> None:
        val = max(0.0, min(1.0, float(val)))
        if abs(self._split_position - val) > 0.001:
            self._split_position = val
            self.splitPositionChanged.emit()

    @Property(bool, notify=hasOutputPreviewChanged)
    def hasOutputPreview(self) -> bool:
        return self._context().has_output

    @Property(str, notify=previewInputUrlChanged)
    def previewInputUrl(self) -> str:
        return self._context().input_url

    @Property(str, notify=previewOutputUrlChanged)
    def previewOutputUrl(self) -> str:
        return self._context().output_url

    @Property(str, notify=previewPosterUrlChanged)
    def previewPosterUrl(self) -> str:
        return self._context().poster_url

    @Property(float, notify=previewSourceChanged)
    def previewSourceSeconds(self) -> float:
        return float(self._context().preview_source_seconds or 0.0)

    @Property(int, notify=previewSourceChanged)
    def previewSourceFrame(self) -> int:
        return int(self._context().preview_source_frame or 0)

    @Property(list, notify=previewRangesChanged)
    def previewRenderedRanges(self) -> list[dict]:
        """Rendered timeline spans for FI and timed Upscale previews, newest last.

        Each entry is {"start": seconds, "end": seconds, "url": clip URL}.
        Empty for frame and image previews. The timeline paints spans green;
        playback shows the processed clip inside and the source outside.
        """
        try:
            ranges = getattr(self._context(), "rendered_ranges", None) or []
        except Exception:
            return []
        out: list[dict] = []
        for entry in ranges:
            try:
                out.append({
                    "start": max(0.0, float(entry.get("start", 0.0))),
                    "end": max(0.0, float(entry.get("end", 0.0))),
                    "url": str(entry.get("url", "")),
                })
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    @Property(str, notify=inputInfoTextChanged)
    def inputInfoText(self) -> str:
        return self._context().input_info

    @Property(str, notify=outputInfoTextChanged)
    def outputInfoText(self) -> str:
        return self._context().output_info

    @Property(str, notify=presetStatusChanged)
    def presetStatus(self) -> str:
        return self._preset_status

    @Property(str, notify=liveStatusTextChanged)
    def liveStatusText(self) -> str:
        return self._live_status_text

    @Property(bool, notify=isLiveRunningChanged)
    def isLiveRunning(self) -> bool:
        return self._is_live_running

    @Property(bool, notify=isLiveStartingChanged)
    def isLiveStarting(self) -> bool:
        return self._is_live_starting

    @Property(float, notify=liveTelemetryChanged)
    def liveSourceFps(self) -> float:
        return self._live_source_fps

    @Property(float, notify=liveTelemetryChanged)
    def liveTargetFps(self) -> float:
        return self._live_target_fps

    @Property(float, notify=liveTelemetryChanged)
    def liveEffectiveFps(self) -> float:
        return self._live_effective_fps

    @Property(float, notify=liveTelemetryChanged)
    def liveGuideMs(self) -> float:
        return self._live_guide_ms

    @Property(float, notify=liveTelemetryChanged)
    def liveDlssMs(self) -> float:
        return self._live_dlss_ms

    @Property(float, notify=liveTelemetryChanged)
    def liveEncodeMs(self) -> float:
        return self._live_encode_ms

    @Property(int, notify=liveTelemetryChanged)
    def liveDroppedFrames(self) -> int:
        return self._live_dropped_frames

    @Property(int, notify=liveTelemetryChanged)
    def liveRebufferEvents(self) -> int:
        return self._live_rebuffer_events

    @Property(float, notify=liveTelemetryChanged)
    def liveAvSyncMs(self) -> float:
        return self._live_av_sync_ms

    @Property(str, notify=liveTelemetryChanged)
    def liveOutputSize(self) -> str:
        return self._live_output_size

    @Property(str, notify=liveTelemetryChanged)
    def liveEncoder(self) -> str:
        return self._live_encoder

    @Property(bool, notify=liveTelemetryChanged)
    def livePlayerStarted(self) -> bool:
        """True once the embedded player renders its first frame."""
        return self._live_player_started

    @Property(QObject, constant=True)
    def mpvEmbed(self) -> QObject:
        """In-tab Live player host (container geometry + pause/mute)."""
        return self._mpv_embed

    def attachMainWindow(self, window: Any) -> None:
        """Remember the QML root window for in-tab MPV hosting."""
        self._main_window = window
        try:
            self._mpv_embed.attach_main_window(window)
        except Exception:
            pass

    @Slot(str)
    def _on_mpv_embed_error(self, message: str) -> None:
        self._log(f"Live player: {message}")

    @Property(str, notify=operationStateChanged)
    def operationState(self) -> str:
        return self._operation_state

    @Property(bool, notify=operationStateChanged)
    def canModifyQueue(self) -> bool:
        return self._operation_state == IDLE and not self._shutting_down

    @Property(bool, notify=operationStateChanged)
    def canPreview(self) -> bool:
        return self._operation_state == IDLE and self._runtime_state == "Ready" and not self._is_live_running

    @Property(bool, notify=operationStateChanged)
    def canRender(self) -> bool:
        return self._operation_state == IDLE and self._runtime_state == "Ready" and not self._is_live_running and self._active_render_configuration_valid()

    @Property(bool, notify=operationStateChanged)
    def canStartLive(self) -> bool:
        return self._operation_state == IDLE and self._runtime_state == "Ready" and not self._is_live_running

    @Property(bool, notify=operationStateChanged)
    def canStop(self) -> bool:
        return self._operation_state in {PREVIEW_RUNNING, PREVIEW_PREPARING, BATCH_RUNNING, BATCH_PREPARING, LIVE_STARTING, LIVE_RUNNING}

    @Property(str, notify=runtimeStateChanged)
    def runtimeState(self) -> str:
        return self._runtime_state

    @Property(str, notify=runtimeStateChanged)
    def runtimeError(self) -> str:
        return self._runtime_error

    @Property(str, notify=runtimeStateChanged)
    def startupWarnings(self) -> str:
        return "\n".join(self._startup_warnings)

    @Property(int, notify=sourceSizeChanged)
    def sourceWidth(self) -> int:
        return self._context().source_width

    @Property(int, notify=sourceSizeChanged)
    def sourceHeight(self) -> int:
        return self._context().source_height

    @Property(float, notify=sourceSizeChanged)
    def sourceFps(self) -> float:
        return self._context().source_fps

    @Property(float, notify=sourceSizeChanged)
    def sourceDuration(self) -> float:
        return self._context().duration_seconds

    @Property(int, notify=outputSizeChanged)
    def outputWidth(self) -> int:
        return self._context().output_width

    @Property(int, notify=outputSizeChanged)
    def outputHeight(self) -> int:
        return self._context().output_height

    @Property(int, notify=layoutChanged)
    def inspectorWidth(self) -> int:
        return self._inspector_width

    @inspectorWidth.setter
    def inspectorWidth(self, value: int) -> None:
        value = max(300, min(560, int(value)))
        if value != self._inspector_width:
            self._inspector_width = value
            self._ui_settings.setValue("layout/inspectorWidth", value)
            self.layoutChanged.emit()

    @Property(int, notify=layoutChanged)
    def queueHeight(self) -> int:
        return self._queue_height

    @queueHeight.setter
    def queueHeight(self, value: int) -> None:
        value = max(120, min(360, int(value)))
        if value != self._queue_height:
            self._queue_height = value
            self._ui_settings.setValue("layout/queueHeight", value)
            self.layoutChanged.emit()

    @Property(int, notify=layoutChanged)
    def logHeight(self) -> int:
        return self._log_height

    @logHeight.setter
    def logHeight(self, value: int) -> None:
        value = max(120, min(420, int(value)))
        if value != self._log_height:
            self._log_height = value
            self._ui_settings.setValue("layout/logHeight", value)
            self.layoutChanged.emit()

    @Property(bool, notify=layoutChanged)
    def focusPreview(self) -> bool:
        return self._focus_preview

    @focusPreview.setter
    def focusPreview(self, value: bool) -> None:
        value = bool(value)
        if value != self._focus_preview:
            self._focus_preview = value
            self.layoutChanged.emit()

    @Property(bool, notify=autoPreviewChanged)
    def autoPreviewEnabled(self) -> bool:
        return self._auto_preview_enabled

    @autoPreviewEnabled.setter
    def autoPreviewEnabled(self, value: bool) -> None:
        value = bool(value)
        if value == self._auto_preview_enabled:
            return
        self._auto_preview_enabled = value
        self._ui_settings.setValue("preview/realtimeEnabled", value)
        self.autoPreviewChanged.emit()
        if value:
            self._schedule_auto_preview(120)
        else:
            self._auto_preview_pending = False
            self._auto_preview_timer.stop()

    def _auto_preview_fingerprint(self, context_key: str | None = None) -> tuple:
        """Snapshot what the current output preview was rendered from.

        Returns ``(selected source, relevant settings snapshot)`` for the given
        context. Compared against ``PreviewState.last_auto_fingerprint`` to
        decide whether a scheduled auto-preview is redundant (e.g. returning
        to a tab with unchanged settings). Shared fields edited on another
        tab (AI/video GPU, preview encoding) still mark this context stale.
        Upscale's manual clip duration is excluded because it does not alter
        already rendered footage. Returns ``()`` on error.
        """
        try:
            key = context_key or self._context_key()
            queue = self._queue_for_context(key)
            source = queue.selected_path() or ""
            try:
                snapshot = tuple(
                    getattr(self._settings, field.name, None)
                    for field in _dataclass_fields(self._settings)
                    if not (key == "upscale-video" and field.name == "upscale_preview_length")
                )
            except Exception:
                snapshot = ()
            return (os.path.normcase(os.path.abspath(source)) if source else "", snapshot)
        except Exception:
            return ()

    def _schedule_auto_preview(self, delay_ms: int = 360) -> None:
        """Debounce an automatic preview for the currently selected media.

        Slider drags can emit dozens of values per second; a single GPU preview
        is launched only after edits settle.  If an older preview is still
        running it is cancelled and its generation is invalidated so it can
        never overwrite the newer result.

        Frame Interpolation is manual-only ("Preview"): realtime is
        disabled for fi-video even when the global switch is on, so settings
        tweaks and tab switches never trigger automatic renders there.
        """
        if self._context_key() == "fi-video":
            self._auto_preview_pending = False
            try:
                self._auto_preview_timer.stop()
            except Exception:
                pass
            return
        if (
            not self._auto_preview_enabled
            or self._shutting_down
            or self._runtime_state != "Ready"
            or self._active_tab not in {"neural-rendering", "upscale", "frame-interpolation"}
        ):
            return
        try:
            source = self._active_queue().selected_path()
        except Exception:
            source = ""
        if not source:
            return

        # Skip-if-fresh: returning to a tab (or mode) whose output preview
        # already matches the selected source and current settings must not
        # burn a GPU render. Settings edits change the live snapshot, so
        # they mismatch and still render; a new selection mismatches too.
        try:
            key = self._context_key()
            ctx = self._contexts[key]
            if ctx.has_output and ctx.last_auto_fingerprint:
                if ctx.last_auto_fingerprint == self._auto_preview_fingerprint(key):
                    self._auto_preview_pending = False
                    try:
                        self._auto_preview_timer.stop()
                    except Exception:
                        pass
                    return
        except Exception:
            pass

        self._auto_preview_pending = True
        if self._operation_state in {PREVIEW_PREPARING, PREVIEW_RUNNING}:
            self._invalidate_preview_generation()
            worker = self._active_worker
            if worker is not None:
                try:
                    worker.controller.stop()
                except Exception:
                    pass
            self._operation_state = PREVIEW_CANCELLING
            self._status_message = "Updating realtime preview…"
            self.operationStateChanged.emit()
            self.statusMessageChanged.emit()
        self._auto_preview_timer.start(max(80, int(delay_ms)))

    def _run_auto_preview(self) -> None:
        if not self._auto_preview_pending or not self._auto_preview_enabled or self._shutting_down:
            return
        if self._operation_state == IDLE:
            self._auto_preview_pending = False
            self.renderPreview()
            return
        # Metadata loading / cancellation should finish quickly.  Poll without
        # blocking the GUI thread and launch the latest preview afterwards.
        if self._operation_state in {LOADING_METADATA, PREVIEW_PREPARING, PREVIEW_RUNNING, PREVIEW_CANCELLING}:
            self._auto_preview_timer.start(120)

    # =========================================================================
    # System Info Properties
    # =========================================================================
    def _display_gpu(self) -> dict[str, Any]:
        """Return the GPU record that should be presented in the desktop UI.

        This is an internal Python helper, not a Qt property.  Keeping it as a
        normal method avoids exposing a Python ``dict`` through the Qt meta
        object and allows the public string properties below to call it safely.
        """
        if self._prepared is None:
            return {}
        selected = self._settings.ai_gpu_uuid
        if selected and selected != "auto":
            for gpu in self._prepared.gpus:
                if str(gpu.get("uuid", "")) == selected:
                    return gpu
        return self._prepared.gpu

    def _gpu_profile_text(self) -> str:
        gpu = self._display_gpu()
        if not gpu:
            return "unknown"
        parts = ["Laptop GPU" if gpu.get("is_laptop") else "Desktop GPU"]
        if gpu.get("low_vram"):
            parts.append("low-VRAM (8 GB class)")
        battery = on_battery()
        if battery is not None:
            parts.append("on battery" if battery else "on AC power")
        return ", ".join(parts)

    def _laptop_startup_warnings(self) -> list[str]:
        """Actionable hints for notebook GPUs; never blocks startup."""
        gpu = self._display_gpu()
        if not gpu.get("is_laptop"):
            return []
        warnings: list[str] = []
        if on_battery():
            warnings.append(
                f"{gpu.get('display_name', 'The laptop GPU')} is running on battery. Windows and "
                "the NVIDIA driver limit GPU power on battery; plug in the charger for full "
                "Neural Rendering, RTX Video, and Frame Generation performance."
            )
        mb = int(gpu.get("memory_mb", 0) or 0)
        if 0 < mb <= LOW_VRAM_THRESHOLD_MB:
            warnings.append(
                f"{round(mb / 1024)} GB laptop GPU detected. For smooth results keep Neural "
                "Rendering at 1 pass and Source/75% scale for 4K sources, Live at 1080p or "
                "lower, and close other GPU-heavy applications while rendering."
            )
        return warnings

    @Property(str, notify=runtimeStateChanged)
    def gpuName(self) -> str:
        if self._prepared is None:
            return "Initializing GPU…" if self._runtime_state == "Initializing" else "GPU unavailable"
        return self._display_gpu().get("display_name", "NVIDIA GPU")

    @Property(str, notify=runtimeStateChanged)
    def gpuVram(self) -> str:
        if self._prepared is None:
            return ""
        gpu = self._display_gpu()
        mb = int(gpu.get("memory_total_mb", 0) or gpu.get("memory_mb", 0) or 0)
        return f"{round(mb / 1024)} GB" if mb else ""

    @Property(int, notify=runtimeStateChanged)
    def gpuCount(self) -> int:
        return len(self._prepared.gpus) if self._prepared is not None else 0

    @Property(QObject, constant=True)
    def nrImageQueue(self) -> QObject:
        return self._nr_image_queue

    @Property(QObject, constant=True)
    def nrVideoQueue(self) -> QObject:
        return self._nr_video_queue

    @Property(QObject, constant=True)
    def upscaleImageQueue(self) -> QObject:
        return self._upscale_image_queue

    @Property(QObject, constant=True)
    def upscaleVideoQueue(self) -> QObject:
        return self._upscale_video_queue

    @Property(QObject, constant=True)
    def fiQueue(self) -> QObject:
        return self._fi_queue

    @Property(bool, notify=mediaPreviewChanged)
    def previewInputIsVideo(self) -> bool:
        return self._context().input_is_video

    @Property(bool, notify=mediaPreviewChanged)
    def previewOutputIsVideo(self) -> bool:
        return self._context().output_is_video

    @Property(int, notify=layoutChanged)
    def windowWidth(self) -> int:
        return max(1080, self._window_width)

    @Property(int, notify=layoutChanged)
    def windowHeight(self) -> int:
        return max(700, self._window_height)

    @Property(int, notify=layoutChanged)
    def windowX(self) -> int:
        return self._window_x

    @Property(int, notify=layoutChanged)
    def windowY(self) -> int:
        return self._window_y

    @Property(bool, notify=layoutChanged)
    def windowMaximized(self) -> bool:
        return self._window_maximized

    @Slot(int, int, int, int, bool)
    def saveWindowLayout(self, x: int, y: int, width: int, height: int, maximized: bool) -> None:
        if not maximized and width >= 1080 and height >= 700:
            self._window_width, self._window_height = int(width), int(height)
            self._window_x, self._window_y = int(x), int(y)
            self._ui_settings.setValue("window/width", self._window_width)
            self._ui_settings.setValue("window/height", self._window_height)
            self._ui_settings.setValue("window/x", self._window_x)
            self._ui_settings.setValue("window/y", self._window_y)
        self._window_maximized = bool(maximized)
        self._ui_settings.setValue("window/maximized", self._window_maximized)
        self._ui_settings.sync()

    @Property(str, constant=True)
    def appVersion(self) -> str:
        return "v10"

    @Property(str, notify=runtimeStateChanged)
    def diagnosticsText(self) -> str:
        lines = [
            f"Application: Visual Enhancer {self.appVersion}",
            f"Runtime state: {self._runtime_state}",
            f"Python: {platform.python_version()}",
            f"Qt: {qVersion()}",
            f"OS: {platform.platform()}",
            f"GPU: {self.gpuName} {self.gpuVram}".rstrip(),
            f"GPU profile: {self._gpu_profile_text()}",
            f"AI GPU selection: {self._settings.ai_gpu_uuid}",
            f"Video GPU selection: {self._settings.video_gpu_uuid}",
            f"FFmpeg: {'Available' if Path(FFMPEG).is_file() else 'Missing'} ({FFMPEG})",
            f"FFprobe: {'Available' if Path(FFPROBE).is_file() else 'Missing'} ({FFPROBE})",
            f"MPV: {'Available' if Path(MPV).is_file() else 'Missing'} ({MPV})",
            f"Outputs: {OUTPUTS}",
            f"Logs: {LOGS}",
            f"Config: {CONFIG_PATH}",
        ]
        if self._runtime_error:
            lines.append(f"Runtime error: {self._runtime_error}")
        lines.extend(f"Startup warning: {warning}" for warning in self._startup_warnings)
        return "\n".join(lines)

    @Property(list, constant=True)
    def nrStyleChoices(self) -> list[str]:
        return list(NR_STYLES)

    @Property(list, constant=True)
    def nrScaleChoices(self) -> list[dict[str, float]]:
        return [{"label": mode["label"], "value": factor} for factor, mode in UPSCALING_MODES.items()]

    @Property(list, constant=True)
    def imageFormatChoices(self) -> list[str]:
        return ["PNG", "JPEG", "WebP", "AVIF", "TIFF"]

    @Property(list, constant=True)
    def containerChoices(self) -> list[str]:
        return list(CONTAINER_CHOICES)

    @Property(list, constant=True)
    def codecChoices(self) -> list[str]:
        return list(CODEC_CHOICES)

    @Property(list, constant=True)
    def fixedQualityCodecs(self) -> list[str]:
        return sorted(FIXED_QUALITY_CODECS)

    @Property(list, constant=True)
    def encodingQualityChoices(self) -> list[str]:
        return list(ENCODING_QUALITIES)

    @Property(list, constant=True)
    def renameModeChoices(self) -> list[str]:
        return list(RENAME_MODES)

    @Property(list, constant=True)
    def fiFpsChoices(self) -> list[dict[str, str]]:
        return [{"label": f"{v} FPS", "value": v} for v in FPS_CHOICES]

    @Property(list, constant=True)
    def fiEngineChoices(self) -> list[str]:
        return list(ENGINE_CHOICES)

    @Property(list, constant=True)
    def fiPreviewLengthChoices(self) -> list[dict[str, str]]:
        return [{"label": f"{v}s", "value": v} for v in PREVIEW_LENGTH_CHOICES]

    @Property(list, constant=True)
    def upscalePreviewLengthChoices(self) -> list[dict[str, str]]:
        return [{"label": f"{value}s", "value": value} for value in PREVIEW_LENGTH_CHOICES]

    @Property(list, constant=True)
    def previewEncodingChoices(self) -> list[str]:
        return list(PREVIEW_ENCODING_CHOICES)

    @Property(list, constant=True)
    def liveSourceQualityChoices(self) -> list[str]:
        return list(LIVE_SOURCE_QUALITY_CHOICES)

    @Property(list, constant=True)
    def liveMaxHeightChoices(self) -> list[dict[str, str]]:
        return [{"label": f"{v}p", "value": v} for v in LIVE_MAX_HEIGHT_CHOICES]

    @Property(list, constant=True)
    def liveSegmentChoices(self) -> list[dict[str, str]]:
        return [{"label": f"{v} sec", "value": v} for v in LIVE_SEGMENT_CHOICES]

    @Property(list, constant=True)
    def liveFpsChoices(self) -> list[str]:
        return list(LIVE_FPS_CHOICES)

    @Property(list, constant=True)
    def imageSizeModeChoices(self) -> list[str]:
        return list(IMAGE_SIZE_MODES)

    @Property(list, constant=True)
    def videoSizeModeChoices(self) -> list[str]:
        return list(VIDEO_SIZE_MODES)

    @Property(list, constant=True)
    def imageScaleFactorChoices(self) -> list[dict[str, float]]:
        return [{"label": label, "value": value} for label, value in IMAGE_SCALE_FACTORS]

    @Property(list, constant=True)
    def videoScaleFactorChoices(self) -> list[dict[str, float]]:
        return [{"label": label, "value": value} for label, value in VIDEO_SCALE_FACTORS]

    @Property(list, constant=True)
    def vsrQualityChoices(self) -> list[dict[str, int]]:
        return [{"label": label, "value": value} for label, value in VIDEO_VSR_QUALITIES]

    @Property(list, constant=True)
    def hdrPrecisionChoices(self) -> list[dict[str, str]]:
        return [{"label": label, "value": value} for label, value in HDR_PRECISION_CHOICES]

    @Property(list, notify=gpuChoicesChanged)
    def aiGpuChoices(self) -> list[dict[str, str]]:
        choices = [{"label": "Automatic (Best Available)", "value": "auto"}]
        for g in (() if self._prepared is None else self._prepared.gpus):
            if g.get("ai_compatible"):
                name = g.get("display_name", "GPU")
                mb = int(g.get("memory_total_mb", 0) or g.get("memory_mb", 0) or 0)
                vram = f" | {round(mb / 1024)} GB" if mb else ""
                choices.append({"label": f"{name}{vram}", "value": str(g.get("uuid"))})
        return choices

    @Property(list, notify=gpuChoicesChanged)
    def videoGpuChoices(self) -> list[dict[str, str]]:
        choices = [{"label": "Automatic (Best Available)", "value": "auto"}]
        for g in (() if self._prepared is None else self._prepared.gpus):
            if g.get("cuda_ordinal") is not None:
                name = g.get("display_name", "GPU")
                choices.append({"label": name, "value": str(g.get("uuid"))})
        return choices

    @Slot()
    def initializeRuntime(self) -> None:
        """Prepare all input-independent backends before the first QML frame."""
        if self._runtime_state != "Initializing":
            return

        try:
            sweep_stale_live_dirs()
        except Exception as exc:
            self._startup_warnings.append(f"Live cache cleanup: {exc}")

        try:
            initialize_image_runtime()
            self._prepared = prepare_runtime()
            self._normalize_gpu_selections()
        except Exception as exc:
            message = str(exc)
            self._runtime_state = "Failed"
            self._runtime_error = message
            self.runtimeStateChanged.emit()
            self.operationStateChanged.emit()
            self._status_message = f"Runtime initialization failed: {message}"
            self.statusMessageChanged.emit()
            self._log(self._status_message)
            app_log.error("startup", self._status_message)
            return

        # Load optional native bridges without selecting an adapter. Their
        # absence must not prevent the other rendering modes from opening.
        for name, module_name in (
            ("Frame Interpolation", "..frame_interpolation.native"),
            ("RTX Video Upscale", "..upscale.video.native"),
        ):
            try:
                importlib.import_module(module_name, __package__).preload_bridge()
            except Exception as exc:
                self._startup_warnings.append(f"{name}: {exc}")

        self._runtime_state = "Ready"
        self._runtime_error = ""
        self._status_message = "Ready."
        self.runtimeStateChanged.emit()
        self.gpuChoicesChanged.emit()
        self.operationStateChanged.emit()
        self.statusMessageChanged.emit()
        self._log(f"Runtime ready on {self.gpuName}.")
        for warning in self._startup_warnings:
            self._log(f"Startup warning: {warning}")
            app_log.error("startup", warning)
        for hint in self._laptop_startup_warnings():
            self._log(f"Laptop GPU: {hint}")
            app_log.info("startup", hint)

    def _normalize_gpu_selections(self) -> None:
        if self._prepared is None:
            return
        ai_values = {item["value"] for item in self.aiGpuChoices}
        video_values = {item["value"] for item in self.videoGpuChoices}
        changes: dict[str, Any] = {}
        messages: list[str] = []
        if self._settings.ai_gpu_uuid not in ai_values:
            changes["ai_gpu_uuid"] = "auto"
            messages.append("Previously selected AI GPU is unavailable; Automatic is being used.")
        if self._settings.video_gpu_uuid not in video_values:
            changes["video_gpu_uuid"] = "auto"
            messages.append("Previously selected video GPU is unavailable; Automatic is being used.")
        if changes:
            self._save_setting(**changes)
            for message in messages:
                self._log(message)

    # =========================================================================
    # Settings Properties (Synchronized with UISettings)
    # =========================================================================
    def _save_setting(self, **kwargs: Any) -> bool:
        """Validate and publish a settings transaction atomically.

        QML never observes a configuration that the backend considers invalid.
        Codec/container/HDR dependencies are normalized as part of the same
        transaction, and rejected edits immediately re-emit the canonical state.
        """
        changed = {k: v for k, v in kwargs.items() if hasattr(self._settings, k) and getattr(self._settings, k) != v}
        if not changed:
            return True
        editable_states = {
            IDLE, LIVE_RUNNING, LOADING_METADATA, PREVIEW_PREPARING,
            PREVIEW_RUNNING, PREVIEW_CANCELLING,
        }
        if self._operation_state not in editable_states and not self._shutting_down:
            self._status_message = "Stop the current operation before changing processing settings."
            self.statusMessageChanged.emit()
            self.settingsUpdated.emit()
            return False

        if "codec" in changed:
            changed["container"] = container_for_codec(str(changed["codec"]))
            if not hdr_mode_supported(str(changed["codec"])):
                changed["hdr_mode"] = False
        if "frame_interpolation_codec" in changed:
            changed["frame_interpolation_container"] = container_for_codec(str(changed["frame_interpolation_codec"]))
            if not hdr_mode_supported(str(changed["frame_interpolation_codec"])):
                changed["frame_interpolation_hdr_mode"] = False
        if "upscale_codec" in changed:
            changed["upscale_container"] = container_for_codec(str(changed["upscale_codec"]))
            if not hdr_mode_supported(str(changed["upscale_codec"])):
                changed["upscale_hdr_enabled"] = False

        candidate = replace(self._settings, **changed)
        if not (candidate.upscale_vsr_enabled or candidate.upscale_hdr_enabled):
            self._status_message = "Upscale Video requires VSR or RTX Video HDR to remain enabled."
            self.statusMessageChanged.emit()
            self.settingsUpdated.emit()
            return False
        try:
            candidate = _validate(candidate)
        except Exception as exc:
            self._status_message = f"Invalid setting: {exc}"
            self.statusMessageChanged.emit()
            self.settingsUpdated.emit()
            self._log(self._status_message)
            return False

        self._settings = candidate
        SETTINGS_STATE.current = self._settings
        if self._is_live_running and any(field in self.LIVE_EFFECT_FIELDS for field in changed):
            self._live_effects_dirty = True
        if self._is_live_running and any(field in self.LIVE_RESTART_FIELDS for field in changed):
            self._status_message = "Setting saved; this change applies the next time Live is started."
            self.statusMessageChanged.emit()
        self.settingsUpdated.emit()
        if "ai_gpu_uuid" in changed:
            self.runtimeStateChanged.emit()
        self.operationStateChanged.emit()
        self._save_timer.start()
        if any(field in self.FI_RANGE_FIELDS for field in changed):
            # These spans were built with older settings: drop them so the
            # green overlay never claims stale footage as processed.
            self._clear_rendered_ranges("fi-video")
        if any(field in self.UPSCALE_RANGE_FIELDS for field in changed):
            self._clear_rendered_ranges("upscale-video")
        if any(field in self.AUTO_PREVIEW_FIELDS for field in changed):
            self._schedule_auto_preview()
        return True

    def _flush_settings(self) -> None:
        try:
            save_settings(CONFIG_PATH, self._settings)
        except Exception as exc:
            self._log(f"Failed to save settings: {exc}")
        if self._is_live_running and self._live_effects_dirty:
            try:
                update_live_effects(live_effect_options(self._settings))
                self._live_effects_dirty = False
            except Exception as exc:
                self._log(f"Live effect update failed: {exc}")

    @Property(str, notify=settingsUpdated)
    def nrStyle(self) -> str:
        return self._settings.nr_style

    @nrStyle.setter
    def nrStyle(self, val: str) -> None:
        if val in NR_STYLES and val != self._settings.nr_style:
            self._save_setting(nr_style=val)

    @Property(float, notify=settingsUpdated)
    def upscalingFactor(self) -> float:
        return self._settings.upscaling_factor

    @upscalingFactor.setter
    def upscalingFactor(self, val: float) -> None:
        try:
            factor, _ = resolve_upscaling_mode(val)
            if factor != self._settings.upscaling_factor:
                self._save_setting(upscaling_factor=factor)
        except ValueError:
            pass

    @Property(float, notify=settingsUpdated)
    def nrIntensity(self) -> float:
        return self._settings.nr_intensity

    @nrIntensity.setter
    def nrIntensity(self, val: float) -> None:
        self._save_setting(nr_intensity=round(val, 2))

    @Property(int, notify=settingsUpdated)
    def nrPasses(self) -> int:
        return self._settings.nr_passes

    @nrPasses.setter
    def nrPasses(self, val: int) -> None:
        self._save_setting(nr_passes=int(val))

    @Property(float, notify=settingsUpdated)
    def localToneStrength(self) -> float:
        return self._settings.local_tone_strength

    @localToneStrength.setter
    def localToneStrength(self, val: float) -> None:
        self._save_setting(local_tone_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def localStructureStrength(self) -> float:
        return self._settings.local_structure_strength

    @localStructureStrength.setter
    def localStructureStrength(self, val: float) -> None:
        self._save_setting(local_structure_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def skinStructureStrength(self) -> float:
        return self._settings.skin_structure_strength

    @skinStructureStrength.setter
    def skinStructureStrength(self, val: float) -> None:
        val = round(val, 2)
        auto_mask = self._settings.automatic_mask
        if val > -1.0 and not auto_mask:
            auto_mask = True
        self._save_setting(skin_structure_strength=val, automatic_mask=auto_mask)

    @Property(bool, notify=settingsUpdated)
    def automaticMask(self) -> bool:
        return self._settings.automatic_mask

    @automaticMask.setter
    def automaticMask(self, val: bool) -> None:
        self._save_setting(automatic_mask=bool(val))

    @Property(float, notify=settingsUpdated)
    def nrColorStrength(self) -> float:
        return self._settings.nr_color_strength

    @nrColorStrength.setter
    def nrColorStrength(self, val: float) -> None:
        self._save_setting(nr_color_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def tonePreservation(self) -> float:
        return self._settings.tone_preservation

    @tonePreservation.setter
    def tonePreservation(self, val: float) -> None:
        self._save_setting(tone_preservation=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def faceSkinProtection(self) -> float:
        return self._settings.face_skin_protection

    @faceSkinProtection.setter
    def faceSkinProtection(self, val: float) -> None:
        self._save_setting(face_skin_protection=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def grainPreservation(self) -> float:
        return self._settings.grain_preservation

    @grainPreservation.setter
    def grainPreservation(self, val: float) -> None:
        self._save_setting(grain_preservation=round(val, 2))

    @Property(int, notify=settingsUpdated)
    def maskFeather(self) -> int:
        return self._settings.mask_feather

    @maskFeather.setter
    def maskFeather(self, val: int) -> None:
        self._save_setting(mask_feather=int(val))

    @Property(float, notify=settingsUpdated)
    def shimmerSuppression(self) -> float:
        return self._settings.shimmer_suppression

    @shimmerSuppression.setter
    def shimmerSuppression(self, val: float) -> None:
        self._save_setting(shimmer_suppression=round(val, 2))

    # Independent Live-tab mirrors of the NR controls above. The Live tab
    # binds only these, so tuning or resetting Live never touches the
    # Neural Rendering tab (and vice versa).
    @Property(str, notify=settingsUpdated)
    def liveNrStyle(self) -> str:
        return self._settings.live_nr_style

    @liveNrStyle.setter
    def liveNrStyle(self, val: str) -> None:
        if val in NR_STYLES and val != self._settings.live_nr_style:
            self._save_setting(live_nr_style=val)

    @Property(float, notify=settingsUpdated)
    def liveUpscalingFactor(self) -> float:
        return self._settings.live_upscaling_factor

    @liveUpscalingFactor.setter
    def liveUpscalingFactor(self, val: float) -> None:
        try:
            factor, _ = resolve_upscaling_mode(val)
            if factor != self._settings.live_upscaling_factor:
                self._save_setting(live_upscaling_factor=factor)
        except ValueError:
            pass

    @Property(float, notify=settingsUpdated)
    def liveNrIntensity(self) -> float:
        return self._settings.live_nr_intensity

    @liveNrIntensity.setter
    def liveNrIntensity(self, val: float) -> None:
        self._save_setting(live_nr_intensity=round(val, 2))

    @Property(int, notify=settingsUpdated)
    def liveNrPasses(self) -> int:
        return self._settings.live_nr_passes

    @liveNrPasses.setter
    def liveNrPasses(self, val: int) -> None:
        self._save_setting(live_nr_passes=int(val))

    @Property(float, notify=settingsUpdated)
    def liveLocalToneStrength(self) -> float:
        return self._settings.live_local_tone_strength

    @liveLocalToneStrength.setter
    def liveLocalToneStrength(self, val: float) -> None:
        self._save_setting(live_local_tone_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def liveLocalStructureStrength(self) -> float:
        return self._settings.live_local_structure_strength

    @liveLocalStructureStrength.setter
    def liveLocalStructureStrength(self, val: float) -> None:
        self._save_setting(live_local_structure_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def liveSkinStructureStrength(self) -> float:
        return self._settings.live_skin_structure_strength

    @liveSkinStructureStrength.setter
    def liveSkinStructureStrength(self, val: float) -> None:
        val = round(val, 2)
        auto_mask = self._settings.live_automatic_mask
        if val > -1.0 and not auto_mask:
            auto_mask = True
        self._save_setting(live_skin_structure_strength=val, live_automatic_mask=auto_mask)

    @Property(bool, notify=settingsUpdated)
    def liveAutomaticMask(self) -> bool:
        return self._settings.live_automatic_mask

    @liveAutomaticMask.setter
    def liveAutomaticMask(self, val: bool) -> None:
        self._save_setting(live_automatic_mask=bool(val))

    @Property(float, notify=settingsUpdated)
    def liveNrColorStrength(self) -> float:
        return self._settings.live_nr_color_strength

    @liveNrColorStrength.setter
    def liveNrColorStrength(self, val: float) -> None:
        self._save_setting(live_nr_color_strength=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def liveTonePreservation(self) -> float:
        return self._settings.live_tone_preservation

    @liveTonePreservation.setter
    def liveTonePreservation(self, val: float) -> None:
        self._save_setting(live_tone_preservation=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def liveFaceSkinProtection(self) -> float:
        return self._settings.live_face_skin_protection

    @liveFaceSkinProtection.setter
    def liveFaceSkinProtection(self, val: float) -> None:
        self._save_setting(live_face_skin_protection=round(val, 2))

    @Property(float, notify=settingsUpdated)
    def liveGrainPreservation(self) -> float:
        return self._settings.live_grain_preservation

    @liveGrainPreservation.setter
    def liveGrainPreservation(self, val: float) -> None:
        self._save_setting(live_grain_preservation=round(val, 2))

    @Property(int, notify=settingsUpdated)
    def liveMaskFeather(self) -> int:
        return self._settings.live_mask_feather

    @liveMaskFeather.setter
    def liveMaskFeather(self, val: int) -> None:
        self._save_setting(live_mask_feather=int(val))

    @Property(float, notify=settingsUpdated)
    def liveShimmerSuppression(self) -> float:
        return self._settings.live_shimmer_suppression

    @liveShimmerSuppression.setter
    def liveShimmerSuppression(self, val: float) -> None:
        self._save_setting(live_shimmer_suppression=round(val, 2))

    @Property(str, notify=settingsUpdated)
    def customMaskStatus(self) -> str:
        return mask_status(self._settings.nr_mask)

    # Output & Format settings
    @Property(str, notify=settingsUpdated)
    def imageFormat(self) -> str:
        return self._settings.image_format

    @imageFormat.setter
    def imageFormat(self, val: str) -> None:
        self._save_setting(image_format=val)

    @Property(int, notify=settingsUpdated)
    def imageQuality(self) -> int:
        return self._settings.image_quality

    @imageQuality.setter
    def imageQuality(self, val: int) -> None:
        self._save_setting(image_quality=int(val))

    @Property(str, notify=settingsUpdated)
    def imageRenameMode(self) -> str:
        return self._settings.image_rename_mode

    @imageRenameMode.setter
    def imageRenameMode(self, val: str) -> None:
        self._save_setting(image_rename_mode=val)

    @Property(str, notify=settingsUpdated)
    def imageCustomSuffix(self) -> str:
        return self._settings.image_custom_suffix

    @imageCustomSuffix.setter
    def imageCustomSuffix(self, val: str) -> None:
        self._save_setting(image_custom_suffix=val)

    @Property(str, notify=settingsUpdated)
    def videoCodec(self) -> str:
        return self._settings.codec

    @videoCodec.setter
    def videoCodec(self, val: str) -> None:
        container = container_for_codec(val)
        hdr = self._settings.hdr_mode and hdr_mode_supported(val)
        self._save_setting(codec=val, container=container, hdr_mode=hdr)

    @Property(str, notify=settingsUpdated)
    def videoContainer(self) -> str:
        return container_for_codec(self._settings.codec)

    @Property(str, notify=settingsUpdated)
    def videoQuality(self) -> str:
        return self._settings.quality

    @videoQuality.setter
    def videoQuality(self, val: str) -> None:
        self._save_setting(quality=val)

    @Property(bool, notify=settingsUpdated)
    def videoHdrMode(self) -> bool:
        return self._settings.hdr_mode

    @videoHdrMode.setter
    def videoHdrMode(self, val: bool) -> None:
        self._save_setting(hdr_mode=coerce_hdr_mode(self._settings.codec, val))

    @Property(str, notify=settingsUpdated)
    def videoRenameMode(self) -> str:
        return self._settings.video_rename_mode

    @videoRenameMode.setter
    def videoRenameMode(self, val: str) -> None:
        self._save_setting(video_rename_mode=val)

    @Property(str, notify=settingsUpdated)
    def videoCustomSuffix(self) -> str:
        return self._settings.video_custom_suffix

    @videoCustomSuffix.setter
    def videoCustomSuffix(self, val: str) -> None:
        self._save_setting(video_custom_suffix=val)

    # Upscale Image & Video
    @Property(int, notify=settingsUpdated)
    def upscaleImageVsrQuality(self) -> int:
        return self._settings.upscale_image_vsr_quality

    @upscaleImageVsrQuality.setter
    def upscaleImageVsrQuality(self, val: int) -> None:
        self._save_setting(upscale_image_vsr_quality=int(val))

    @Property(str, notify=settingsUpdated)
    def upscaleImageSizeMode(self) -> str:
        return self._settings.upscale_image_size_mode

    @upscaleImageSizeMode.setter
    def upscaleImageSizeMode(self, val: str) -> None:
        self._save_setting(upscale_image_size_mode=val)

    @Property(float, notify=settingsUpdated)
    def upscaleImageScaleFactor(self) -> float:
        return self._settings.upscale_image_scale_factor

    @upscaleImageScaleFactor.setter
    def upscaleImageScaleFactor(self, val: float) -> None:
        self._save_setting(upscale_image_scale_factor=float(val))

    @Property(int, notify=settingsUpdated)
    def upscaleImageWidth(self) -> int:
        return self._settings.upscale_image_width

    @upscaleImageWidth.setter
    def upscaleImageWidth(self, val: int) -> None:
        width = max(1, int(val))
        if self._settings.upscale_image_aspect_lock and self._context("upscale-image").source_aspect_ratio:
            height = max(1, round(width / self._context("upscale-image").source_aspect_ratio))
            self._save_setting(upscale_image_width=width, upscale_image_height=height)
        else:
            self._save_setting(upscale_image_width=width)

    @Property(int, notify=settingsUpdated)
    def upscaleImageHeight(self) -> int:
        return self._settings.upscale_image_height

    @upscaleImageHeight.setter
    def upscaleImageHeight(self, val: int) -> None:
        height = max(1, int(val))
        if self._settings.upscale_image_aspect_lock and self._context("upscale-image").source_aspect_ratio:
            width = max(1, round(height * self._context("upscale-image").source_aspect_ratio))
            self._save_setting(upscale_image_width=width, upscale_image_height=height)
        else:
            self._save_setting(upscale_image_height=height)

    @Property(bool, notify=settingsUpdated)
    def upscaleImageAspectLock(self) -> bool:
        return self._settings.upscale_image_aspect_lock

    @upscaleImageAspectLock.setter
    def upscaleImageAspectLock(self, val: bool) -> None:
        self._save_setting(upscale_image_aspect_lock=bool(val))

    @Property(str, notify=settingsUpdated)
    def upscaleImageOutputFormat(self) -> str:
        return self._settings.upscale_image_output_format

    @upscaleImageOutputFormat.setter
    def upscaleImageOutputFormat(self, val: str) -> None:
        self._save_setting(upscale_image_output_format=val)

    @Property(int, notify=settingsUpdated)
    def upscaleImageQuality(self) -> int:
        return self._settings.upscale_image_quality

    @upscaleImageQuality.setter
    def upscaleImageQuality(self, val: int) -> None:
        self._save_setting(upscale_image_quality=int(val))

    @Property(bool, notify=settingsUpdated)
    def upscaleImagePreserveMetadata(self) -> bool:
        return self._settings.upscale_image_preserve_metadata

    @upscaleImagePreserveMetadata.setter
    def upscaleImagePreserveMetadata(self, val: bool) -> None:
        self._save_setting(upscale_image_preserve_metadata=bool(val))

    # Upscale Video
    @Property(bool, notify=settingsUpdated)
    def upscaleVsrEnabled(self) -> bool:
        return self._settings.upscale_vsr_enabled

    @upscaleVsrEnabled.setter
    def upscaleVsrEnabled(self, val: bool) -> None:
        self._save_setting(upscale_vsr_enabled=bool(val))

    @Property(int, notify=settingsUpdated)
    def upscaleVsrQuality(self) -> int:
        return self._settings.upscale_vsr_quality

    @upscaleVsrQuality.setter
    def upscaleVsrQuality(self, val: int) -> None:
        self._save_setting(upscale_vsr_quality=int(val))

    @Property(str, notify=settingsUpdated)
    def upscaleSizeMode(self) -> str:
        return self._settings.upscale_size_mode

    @upscaleSizeMode.setter
    def upscaleSizeMode(self, val: str) -> None:
        self._save_setting(upscale_size_mode=val)

    @Property(float, notify=settingsUpdated)
    def upscaleScaleFactor(self) -> float:
        return self._settings.upscale_scale_factor

    @upscaleScaleFactor.setter
    def upscaleScaleFactor(self, val: float) -> None:
        self._save_setting(upscale_scale_factor=float(val))

    @Property(int, notify=settingsUpdated)
    def upscaleWidth(self) -> int:
        return self._settings.upscale_width

    @upscaleWidth.setter
    def upscaleWidth(self, val: int) -> None:
        width = max(2, int(val))
        if self._settings.upscale_aspect_lock and self._context("upscale-video").source_aspect_ratio:
            height = max(2, round(width / self._context("upscale-video").source_aspect_ratio))
            self._save_setting(upscale_width=width, upscale_height=height)
        else:
            self._save_setting(upscale_width=width)

    @Property(int, notify=settingsUpdated)
    def upscaleHeight(self) -> int:
        return self._settings.upscale_height

    @upscaleHeight.setter
    def upscaleHeight(self, val: int) -> None:
        height = max(2, int(val))
        if self._settings.upscale_aspect_lock and self._context("upscale-video").source_aspect_ratio:
            width = max(2, round(height * self._context("upscale-video").source_aspect_ratio))
            self._save_setting(upscale_width=width, upscale_height=height)
        else:
            self._save_setting(upscale_height=height)

    @Property(bool, notify=settingsUpdated)
    def upscaleAspectLock(self) -> bool:
        return self._settings.upscale_aspect_lock

    @upscaleAspectLock.setter
    def upscaleAspectLock(self, val: bool) -> None:
        self._save_setting(upscale_aspect_lock=bool(val))

    @Property(bool, notify=settingsUpdated)
    def upscaleHdrEnabled(self) -> bool:
        return self._settings.upscale_hdr_enabled

    @upscaleHdrEnabled.setter
    def upscaleHdrEnabled(self, val: bool) -> None:
        self._save_setting(upscale_hdr_enabled=bool(val))

    @Property(int, notify=settingsUpdated)
    def upscaleHdrContrast(self) -> int:
        return self._settings.upscale_hdr_contrast

    @upscaleHdrContrast.setter
    def upscaleHdrContrast(self, val: int) -> None:
        self._save_setting(upscale_hdr_contrast=int(val))

    @Property(int, notify=settingsUpdated)
    def upscaleHdrSaturation(self) -> int:
        return self._settings.upscale_hdr_saturation

    @upscaleHdrSaturation.setter
    def upscaleHdrSaturation(self, val: int) -> None:
        self._save_setting(upscale_hdr_saturation=int(val))

    @Property(int, notify=settingsUpdated)
    def upscaleHdrMiddleGray(self) -> int:
        return self._settings.upscale_hdr_middle_gray

    @upscaleHdrMiddleGray.setter
    def upscaleHdrMiddleGray(self, val: int) -> None:
        self._save_setting(upscale_hdr_middle_gray=int(val))

    @Property(int, notify=settingsUpdated)
    def upscaleHdrPeakLuminance(self) -> int:
        return self._settings.upscale_hdr_peak_luminance

    @upscaleHdrPeakLuminance.setter
    def upscaleHdrPeakLuminance(self, val: int) -> None:
        self._save_setting(upscale_hdr_peak_luminance=int(val))

    @Property(str, notify=settingsUpdated)
    def upscaleHdrPrecision(self) -> str:
        return self._settings.upscale_hdr_precision

    @upscaleHdrPrecision.setter
    def upscaleHdrPrecision(self, val: str) -> None:
        self._save_setting(upscale_hdr_precision=val)

    @Property(str, notify=settingsUpdated)
    def upscaleCodec(self) -> str:
        return self._settings.upscale_codec

    @upscaleCodec.setter
    def upscaleCodec(self, val: str) -> None:
        container = container_for_codec(val)
        hdr = self._settings.upscale_hdr_enabled and hdr_mode_supported(val)
        self._save_setting(upscale_codec=val, upscale_container=container, upscale_hdr_enabled=hdr)

    @Property(str, notify=settingsUpdated)
    def upscaleContainer(self) -> str:
        return container_for_codec(self._settings.upscale_codec)

    @Property(str, notify=settingsUpdated)
    def upscaleQuality(self) -> str:
        return self._settings.upscale_quality

    @upscaleQuality.setter
    def upscaleQuality(self, val: str) -> None:
        if val in ENCODING_QUALITIES:
            self._save_setting(upscale_quality=val)

    @Property(str, notify=settingsUpdated)
    def upscaleImageRenameMode(self) -> str:
        return self._settings.upscale_image_rename_mode

    @upscaleImageRenameMode.setter
    def upscaleImageRenameMode(self, val: str) -> None:
        if val in RENAME_MODES:
            self._save_setting(upscale_image_rename_mode=val)

    @Property(str, notify=settingsUpdated)
    def upscaleImageCustomSuffix(self) -> str:
        return self._settings.upscale_image_custom_suffix

    @upscaleImageCustomSuffix.setter
    def upscaleImageCustomSuffix(self, val: str) -> None:
        self._save_setting(upscale_image_custom_suffix=val)

    @Property(str, notify=settingsUpdated)
    def upscaleRenameMode(self) -> str:
        return self._settings.upscale_rename_mode

    @upscaleRenameMode.setter
    def upscaleRenameMode(self, val: str) -> None:
        if val in RENAME_MODES:
            self._save_setting(upscale_rename_mode=val)

    @Property(str, notify=settingsUpdated)
    def upscaleCustomSuffix(self) -> str:
        return self._settings.upscale_custom_suffix

    @upscaleCustomSuffix.setter
    def upscaleCustomSuffix(self, val: str) -> None:
        self._save_setting(upscale_custom_suffix=val)

    # Frame Interpolation
    @Property(str, notify=settingsUpdated)
    def fiTargetFps(self) -> str:
        return self._settings.frame_interpolation_target_fps

    @fiTargetFps.setter
    def fiTargetFps(self, val: str) -> None:
        self._save_setting(frame_interpolation_target_fps=val)

    @Property(str, notify=settingsUpdated)
    def fiEngine(self) -> str:
        return self._settings.frame_interpolation_engine

    @fiEngine.setter
    def fiEngine(self, val: str) -> None:
        self._save_setting(frame_interpolation_engine=val)

    @Property(str, notify=settingsUpdated)
    def fiCodec(self) -> str:
        return self._settings.frame_interpolation_codec

    @fiCodec.setter
    def fiCodec(self, val: str) -> None:
        container = container_for_codec(val)
        hdr = self._settings.frame_interpolation_hdr_mode and hdr_mode_supported(val)
        self._save_setting(frame_interpolation_codec=val, frame_interpolation_container=container, frame_interpolation_hdr_mode=hdr)

    @Property(str, notify=settingsUpdated)
    def fiContainer(self) -> str:
        return container_for_codec(self._settings.frame_interpolation_codec)

    @Property(str, notify=settingsUpdated)
    def fiQuality(self) -> str:
        return self._settings.frame_interpolation_quality

    @fiQuality.setter
    def fiQuality(self, val: str) -> None:
        self._save_setting(frame_interpolation_quality=val)

    @Property(str, notify=settingsUpdated)
    def fiPreviewLength(self) -> str:
        return self._settings.frame_interpolation_preview_length

    @fiPreviewLength.setter
    def fiPreviewLength(self, val: str) -> None:
        if str(val) in PREVIEW_LENGTH_CHOICES:
            self._save_setting(frame_interpolation_preview_length=str(val))

    @Property(bool, notify=settingsUpdated)
    def fiHdrMode(self) -> bool:
        return self._settings.frame_interpolation_hdr_mode

    @fiHdrMode.setter
    def fiHdrMode(self, val: bool) -> None:
        self._save_setting(frame_interpolation_hdr_mode=coerce_hdr_mode(self._settings.frame_interpolation_codec, val))

    @Property(str, notify=settingsUpdated)
    def fiRenameMode(self) -> str:
        return self._settings.frame_interpolation_rename_mode

    @fiRenameMode.setter
    def fiRenameMode(self, val: str) -> None:
        if val in RENAME_MODES:
            self._save_setting(frame_interpolation_rename_mode=val)

    @Property(str, notify=settingsUpdated)
    def fiCustomSuffix(self) -> str:
        return self._settings.frame_interpolation_custom_suffix

    @fiCustomSuffix.setter
    def fiCustomSuffix(self, val: str) -> None:
        self._save_setting(frame_interpolation_custom_suffix=val)

    @Property(bool, notify=settingsUpdated)
    def videoHdrSupported(self) -> bool:
        return hdr_mode_supported(self._settings.codec)

    @Property(bool, notify=settingsUpdated)
    def fiHdrSupported(self) -> bool:
        return hdr_mode_supported(self._settings.frame_interpolation_codec)

    @Property(bool, notify=settingsUpdated)
    def upscaleHdrSupported(self) -> bool:
        return hdr_mode_supported(self._settings.upscale_codec)

    # Global Preferences
    @Property(str, notify=settingsUpdated)
    def aiGpuUuid(self) -> str:
        return self._settings.ai_gpu_uuid

    @aiGpuUuid.setter
    def aiGpuUuid(self, val: str) -> None:
        self._save_setting(ai_gpu_uuid=val)

    @Property(str, notify=settingsUpdated)
    def videoGpuUuid(self) -> str:
        return self._settings.video_gpu_uuid

    @videoGpuUuid.setter
    def videoGpuUuid(self, val: str) -> None:
        self._save_setting(video_gpu_uuid=val)

    @Property(str, notify=settingsUpdated)
    def previewEncoding(self) -> str:
        return self._settings.preview_encoding

    @previewEncoding.setter
    def previewEncoding(self, val: str) -> None:
        self._save_setting(preview_encoding=val)

    def _active_render_configuration_valid(self) -> bool:
        try:
            _validate(self._settings)
            if self._active_tab == "upscale" and self._upscale_mode == "Video":
                video_upscale_options_from_settings(self._settings).validate(for_render=True)
            elif self._active_tab == "upscale" and self._upscale_mode == "Image":
                image_upscale_options_from_settings(self._settings).validate(for_render=True)
            return True
        except Exception:
            return False

    @Property(str, notify=settingsUpdated)
    def renderValidationMessage(self) -> str:
        try:
            _validate(self._settings)
            if self._active_tab == "upscale" and self._upscale_mode == "Video":
                video_upscale_options_from_settings(self._settings).validate(for_render=True)
            elif self._active_tab == "upscale" and self._upscale_mode == "Image":
                image_upscale_options_from_settings(self._settings).validate(for_render=True)
            return ""
        except Exception as exc:
            return str(exc)

    @Property(str, notify=settingsUpdated)
    def outputEstimate(self) -> str:
        context = self._context()
        width, height = context.source_width, context.source_height
        if width <= 0 or height <= 0:
            return ""
        try:
            if self._active_tab == "neural-rendering":
                ow, oh = resolve_output_size(width, height, self._settings.upscaling_factor)
                return f"{width}×{height} → {ow}×{oh}"
            if self._active_tab == "upscale" and self._upscale_mode == "Image":
                ow, oh = image_upscale_output_size(width, height, image_upscale_options_from_settings(self._settings))
                return f"{width}×{height} → {ow}×{oh}"
            if self._active_tab == "upscale" and self._upscale_mode == "Video":
                ow, oh, note = video_upscale_output_size(width, height, video_upscale_options_from_settings(self._settings))
                return f"{width}×{height} → {ow}×{oh}{note}"
            if self._active_tab == "frame-interpolation":
                return f"Target {self._settings.frame_interpolation_target_fps} FPS"
        except Exception as exc:
            return f"Output unavailable: {exc}"
        return ""

    # =========================================================================
    # Methods & Slots: Presets & Defaults
    # =========================================================================
    @Slot()
    def applyDetailOnly(self) -> None:
        self._save_setting(nr_color_strength=0.0, tone_preservation=1.0)
        self._log("Applied Detail-Only preset.")

    @Slot(str)
    def selectCustomMask(self, file_url_or_path: str) -> None:
        path = self._clean_path(file_url_or_path)
        try:
            selection = inspect_nr_mask(path)
            self._save_setting(nr_mask=selection)
            self._log(f"Custom NR Mask loaded: {path}")
        except Exception as exc:
            self._log(f"Error loading mask: {exc}")

    @Slot()
    def clearCustomMask(self) -> None:
        self._save_setting(nr_mask=None)
        self._log("Custom NR Mask cleared.")

    @Slot()
    def resetToDefaults(self) -> None:
        if self._operation_state not in {IDLE, LIVE_RUNNING}:
            self._preset_status = "Stop the current operation before resetting settings."
            self.presetStatusChanged.emit()
            return
        self._settings = DEFAULT_SETTINGS
        SETTINGS_STATE.current = self._settings
        self._clear_rendered_ranges("fi-video")
        self._clear_rendered_ranges("upscale-video")
        if self._upscale_mode != DEFAULT_SETTINGS.upscale_mode:
            self._upscale_mode = DEFAULT_SETTINGS.upscale_mode
            self.upscaleModeChanged.emit()
            self._emit_context()
        if self._is_live_running:
            self._live_effects_dirty = True
        self._flush_settings()
        self.settingsUpdated.emit()
        self.runtimeStateChanged.emit()
        self._preset_status = "All settings were reset to factory defaults."
        self.presetStatusChanged.emit()
        self._log("Reset all settings to factory defaults.")

    # Per-tab Reset buttons (action bars). Prefix groups track the dataclass
    # automatically; the Neural group is explicit (sidebar cards + export).
    TAB_RESET_PREFIXES = {
        "upscale": "upscale_",
        "frame-interpolation": "frame_interpolation_",
        "live": "live_",
    }
    TAB_RESET_NEURAL_FIELDS = (
        "nr_style", "nr_intensity", "nr_passes",
        "local_tone_strength", "local_structure_strength",
        "skin_structure_strength", "automatic_mask",
        "nr_color_strength", "tone_preservation",
        "face_skin_protection", "grain_preservation",
        "shimmer_suppression", "mask_feather", "nr_mask",
        "upscaling_factor",
        "codec", "quality", "hdr_mode",
        "image_format", "image_quality",
        "image_rename_mode", "image_custom_suffix",
        "video_rename_mode", "video_custom_suffix",
    )
    TAB_RESET_LABELS = {
        "neural-rendering": "Neural Rendering",
        "upscale": "Upscale",
        "frame-interpolation": "Frame Interpolation",
        "live": "Live",
    }

    @Slot(str)
    def resetTabSettings(self, tab: str) -> None:
        """Restore one tab's sidebar settings to factory defaults."""
        tab = tab or ""
        if tab in self.TAB_RESET_PREFIXES:
            prefix = self.TAB_RESET_PREFIXES[tab]
            names = [f.name for f in _dataclass_fields(UISettings) if f.name.startswith(prefix)]
        elif tab == "neural-rendering":
            names = list(self.TAB_RESET_NEURAL_FIELDS)
        else:
            return
        if self._operation_state not in {IDLE, LIVE_RUNNING}:
            self._preset_status = "Stop the current operation before resetting settings."
            self.presetStatusChanged.emit()
            return
        values = {name: getattr(DEFAULT_SETTINGS, name) for name in names}
        if not self._save_setting(**values):
            return
        if tab == "upscale" and self._upscale_mode != self._settings.upscale_mode:
            self._upscale_mode = self._settings.upscale_mode
            self.upscaleModeChanged.emit()
            self._emit_context()
        label = self.TAB_RESET_LABELS[tab]
        self._preset_status = f"{label} settings were reset to defaults."
        self.presetStatusChanged.emit()
        self._flush_settings()
        self._log(f"Reset {label} settings to defaults.")

    @Slot(str, result=str)
    def exportPreset(self, name: str) -> str:
        if not name.strip():
            self._preset_status = "Please enter a preset name."
            self.presetStatusChanged.emit()
            return ""
        try:
            path = export_settings_preset(name, self._settings)
            self._preset_status = f"Exported preset '{name}' to {path.name}"
            self.presetStatusChanged.emit()
            self._log(f"Exported preset '{name}' to {path}")
            return str(path)
        except Exception as exc:
            self._preset_status = f"Export failed: {exc}"
            self.presetStatusChanged.emit()
            return ""

    @Slot(str, str, result=str)
    def exportPresetTo(self, name: str, file_url_or_path: str) -> str:
        if not name.strip():
            self._preset_status = "Please enter a preset name."
            self.presetStatusChanged.emit()
            return ""
        path = self._clean_path(file_url_or_path)
        if not path:
            return ""
        try:
            written = export_settings_preset_to(name, self._settings, path)
            self._preset_status = f"Exported preset '{name}' to {written.name}"
            self.presetStatusChanged.emit()
            self._log(f"Exported preset '{name}' to {written}")
            return str(written)
        except Exception as exc:
            self._preset_status = f"Export failed: {exc}"
            self.presetStatusChanged.emit()
            return ""

    @Slot(str, result=bool)
    def importPreset(self, file_url_or_path: str) -> bool:
        if self._operation_state not in {IDLE, LIVE_RUNNING}:
            self._preset_status = "Stop the current operation before importing a preset."
            self.presetStatusChanged.emit()
            return False
        path = self._clean_path(file_url_or_path)
        try:
            name, imported = import_settings_preset(path, self._settings)
            imported = _validate(imported)
            self._settings = imported
            SETTINGS_STATE.current = self._settings
            if self._upscale_mode != imported.upscale_mode:
                self._upscale_mode = imported.upscale_mode
                self.upscaleModeChanged.emit()
                self._emit_context()
            if self._is_live_running:
                self._live_effects_dirty = True
            self._flush_settings()
            self.settingsUpdated.emit()
            self.runtimeStateChanged.emit()
            self._preset_status = f"Imported preset '{name}' successfully."
            self.presetStatusChanged.emit()
            self._log(f"Imported preset '{name}' from {path}")
            return True
        except Exception as exc:
            self._preset_status = f"Import failed: {exc}"
            self.presetStatusChanged.emit()
            return False

    # =========================================================================
    # Queue Management
    # =========================================================================
    def _active_queue(self) -> BatchListModel:
        if self._active_tab == "neural-rendering":
            return self._nr_image_queue if self._nr_mode == "Image" else self._nr_video_queue
        if self._active_tab == "upscale":
            return self._upscale_image_queue if self._upscale_mode == "Image" else self._upscale_video_queue
        if self._active_tab == "frame-interpolation":
            return self._fi_queue
        return self._nr_image_queue

    def _queue_for_context(self, key: str) -> BatchListModel:
        return {
            "nr-image": self._nr_image_queue,
            "nr-video": self._nr_video_queue,
            "upscale-image": self._upscale_image_queue,
            "upscale-video": self._upscale_video_queue,
            "fi-video": self._fi_queue,
        }[key]

    def _active_accepts_images(self) -> bool:
        return self._context_key() in {"nr-image", "upscale-image"}

    @staticmethod
    def _classify_media_suffix(suffix: str) -> str | None:
        """Classify a file suffix as "image", "video", or None (unsupported)."""
        s = (suffix or "").lower()
        if s in IMAGE_SUFFIXES:
            return "image"
        if s in VIDEO_SUFFIXES:
            return "video"
        return None

    def _detect_batch_kind(self, file_urls: list[str]) -> tuple[int, int]:
        """Peek dropped/picked inputs and count (image_files, video_files).

        Folders are scanned (early-exits as soon as both kinds are seen).
        Unsupported files are ignored. Never raises.
        """
        images = 0
        videos = 0
        try:
            for value in file_urls or []:
                if images > 0 and videos > 0:
                    break
                clean = self._clean_path(value)
                if not clean:
                    continue
                path = Path(clean)
                try:
                    if path.is_dir():
                        for item in path.rglob("*"):
                            if images > 0 and videos > 0:
                                break
                            try:
                                if not item.is_file():
                                    continue
                            except OSError:
                                continue
                            kind = self._classify_media_suffix(item.suffix)
                            if kind == "image":
                                images += 1
                            elif kind == "video":
                                videos += 1
                    elif path.is_file():
                        kind = self._classify_media_suffix(path.suffix)
                        if kind == "image":
                            images += 1
                        elif kind == "video":
                            videos += 1
                except (OSError, PermissionError):
                    continue
        except Exception:
            pass
        return images, videos

    @staticmethod
    def _supported_file(path: Path, accepts_images: bool) -> bool:
        if not path.is_file():
            return False
        suffix = path.suffix.lower()
        if accepts_images:
            return suffix in IMAGE_SUFFIXES
        return suffix in VIDEO_SUFFIXES

    def _expand_inputs(self, file_urls: list[str], accepts_images: bool, controller=None, progress=None) -> list[str]:
        collected: list[str] = []
        roots = list(file_urls or [])
        total_roots = max(1, len(roots))
        for root_index, value in enumerate(roots):
            if controller is not None and controller.cancel.is_set():
                return []
            clean = self._clean_path(value)
            if not clean:
                continue
            path = Path(clean)
            if path.is_dir():
                try:
                    candidates = []
                    for item in path.rglob("*"):
                        if controller is not None and controller.cancel.is_set():
                            return []
                        if self._supported_file(item, accepts_images):
                            candidates.append(item)
                    candidates.sort(key=lambda x: str(x).casefold())
                    collected.extend(str(item.resolve()) for item in candidates)
                except (OSError, PermissionError):
                    continue
            elif self._supported_file(path, accepts_images):
                collected.append(str(path.resolve()))
            if progress is not None:
                progress((root_index + 1) / total_roots, "Scanning inputs…")

        seen: set[str] = set()
        unique: list[str] = []
        for item in collected:
            key = os.path.normcase(os.path.abspath(item))
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    @Slot(list)
    def addFiles(self, file_urls: list[str]) -> None:
        if not file_urls:
            return
        if not self.canModifyQueue:
            self._status_message = "Queue changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            self._paste_select_single = False
            return
        # Consume the Ctrl+V auto-select request (paste call sites only).
        # When the scan below adds exactly one new file, it gets selected
        # so its preview loads immediately.
        select_pasted_single = self._paste_select_single
        self._paste_select_single = False
        # Auto-switch Image/Video mode from the dropped/picked content.
        # Manual segmented buttons remain untouched and still work as override.
        # Mixed image+video uploads are rejected: only one type per upload.
        auto_switched: str | None = None
        if self._active_tab in {"neural-rendering", "upscale"}:
            image_count, video_count = self._detect_batch_kind(list(file_urls))
            if image_count > 0 and video_count > 0:
                self._status_message = "Mixed images and videos are not allowed — please add only one type at a time."
                self.statusMessageChanged.emit()
                self._log(self._status_message)
                return
            if image_count > 0 or video_count > 0:
                target = "Image" if image_count > 0 else "Video"
                if self._active_tab == "upscale":
                    if self._upscale_mode != target:
                        self.upscaleMode = target
                        if self._upscale_mode == target:
                            auto_switched = target
                            self._log(f"Detected {'image' if target == 'Image' else 'video'} input — switched to {target} mode.")
                else:
                    if self._nr_mode != target:
                        self.nrMode = target
                        if self._nr_mode == target:
                            auto_switched = target
                            self._log(f"Detected {'image' if target == 'Image' else 'video'} input — switched to {target} mode.")
        context_key = self._context_key()
        queue = self._queue_for_context(context_key)
        accepts_images = context_key in {"nr-image", "upscale-image"}
        operation_id = self._set_operation(SCANNING_INPUTS, "Scanning selected inputs…")

        worker = JobWorker(self._expand_inputs, list(file_urls), accepts_images)
        self._scan_worker = worker

        def on_progress(fraction: float, message: str) -> None:
            if operation_id != self._operation_id:
                return
            self._overall_progress = fraction
            self._status_message = message
            self.overallProgressChanged.emit()
            self.statusMessageChanged.emit()

        def done(valid_paths: list[str]) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._scan_worker = None
            before = queue.count
            queue.add_items(valid_paths)
            added = queue.count - before
            kind = "images" if accepts_images else "videos"
            message = f"Added {added} supported file(s)." if added else f"No supported {kind} were found."
            if auto_switched and added:
                message = f"Switched to {auto_switched} mode. {message}"
            self._finish_operation(operation_id, message)
            if self._context_key() == context_key:
                if added == 1 and select_pasted_single:
                    # Single Ctrl+V paste: select the newly added item
                    # (appended at the end) so its preview loads at once.
                    self.selectQueueItem(queue.count - 1)
                elif added == 0 and select_pasted_single and len(valid_paths) == 1:
                    # Re-pasted duplicate: select the existing row instead.
                    try:
                        needle = os.path.normcase(os.path.abspath(valid_paths[0]))
                    except Exception:
                        needle = ""
                    if needle:
                        for row, existing in enumerate(queue.get_paths()):
                            try:
                                if os.path.normcase(os.path.abspath(existing)) == needle:
                                    self.selectQueueItem(row)
                                    break
                            except Exception:
                                continue
                elif added:
                    row = queue.selectedIndex if queue.selectedIndex >= 0 else 0
                    self.selectQueueItem(row)
            self._log(message)

        def failed(message: str) -> None:
            self._scan_worker = None
            self._finish_operation(operation_id, f"Input scan failed: {message}")
            self._log(f"Input scan failed: {message}")

        worker.signals.progress.connect(on_progress)
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(lambda: self._finish_operation(operation_id, "Input scan cancelled."))
        self._thread_pool.start(worker)

    @Slot(int)
    def _sweep_temp_caches(self) -> None:
        """Hourly best-effort sweep of stale temp caches. Never raises."""
        try:
            cleanup_old_caches()
        except Exception:
            pass

    @staticmethod
    def _touch_if_staged(path_str: str) -> None:
        """Refresh mtime of staged paste sources so the hourly temp sweep
        never deletes a file still referenced by a live queue. Only touches
        files under APP_TEMP; user files elsewhere are never modified."""
        try:
            if not path_str:
                return
            path = Path(path_str)
            if not path.is_file():
                return
            try:
                staged = path.resolve()
                root = APP_TEMP.resolve()
            except OSError:
                return
            if root in staged.parents:
                os.utime(staged, None)
        except Exception:
            pass

    @Slot(int)
    def selectQueueItem(self, row: int) -> None:
        if self._operation_state != IDLE:
            self._status_message = "Selection changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            return
        queue = self._active_queue()
        paths = queue.get_paths()
        if not (0 <= row < len(paths)):
            return
        queue.select(row)
        self._touch_if_staged(paths[row])
        self._load_input_preview_async(paths[row], row, self._context_key())

    @Slot(int)
    def removeQueueItem(self, row: int) -> None:
        if not self.canModifyQueue:
            self._status_message = "Queue changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            return
        queue = self._active_queue()
        key = self._context_key()
        queue.remove_item(row)
        selected = queue.selected_path()
        if selected:
            self._load_input_preview_async(selected, queue.selectedIndex, key)
        else:
            self._clear_preview_state(key)

    @Slot()
    def clearActiveQueue(self) -> None:
        if not self.canModifyQueue:
            self._status_message = "Queue changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            return
        key = self._context_key()
        self._active_queue().clear()
        self._clear_preview_state(key)

    @Slot()
    def clearSelectedPreviewItem(self) -> None:
        """Remove the currently selected item (preview right-click menu).

        Delegates to removeQueueItem so selection advance / preview reload /
        empty-state clearing behave exactly like the queue's own remove
        button. Acts on the active tab/mode queue only.
        """
        if not self.canModifyQueue:
            self._status_message = "Queue changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            return
        queue = self._active_queue()
        row = queue.selectedIndex
        if not (0 <= row < queue.count):
            self._status_message = "No file is selected to clear."
            self.statusMessageChanged.emit()
            return
        self.removeQueueItem(row)

    @Slot()
    def showSelectedInExplorer(self) -> None:
        """Reveal the currently selected input file in Explorer (selected).

        Highlights the file with ``explorer /select,`` on Windows instead
        of merely opening the containing folder like openFolder does.
        Falls back to opening the parent folder when highlighting is
        unavailable. Never raises.
        """
        selected = ""
        try:
            selected = self._active_queue().selected_path() or ""
        except Exception:
            selected = ""
        if not selected:
            try:
                selected = self._context().selected_path or ""
            except Exception:
                selected = ""
        if not selected:
            self._status_message = "No file is selected to show in Explorer."
            self.statusMessageChanged.emit()
            return
        path = Path(selected)
        if not path.is_file():
            self._status_message = f"File no longer exists: {path.name}"
            self.statusMessageChanged.emit()
            return
        try:
            if os.name == "nt":
                subprocess.Popen(
                    ["explorer", "/select,", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            elif sys.platform == "darwin":
                subprocess.Popen(
                    ["open", "-R", str(path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl as _QUrl

                if not QDesktopServices.openUrl(_QUrl.fromLocalFile(str(path.parent))):
                    raise OSError("Could not open file manager")
        except Exception as exc:
            try:
                parent = path.parent
                if parent.is_dir():
                    if os.name == "nt":
                        os.startfile(parent)
                    else:
                        from PySide6.QtGui import QDesktopServices
                        from PySide6.QtCore import QUrl as _QUrl

                        QDesktopServices.openUrl(_QUrl.fromLocalFile(str(parent)))
                    self._status_message = f"Opened containing folder for {path.name}."
                    self.statusMessageChanged.emit()
                    return
            except Exception:
                pass
            self._status_message = f"Could not show in Explorer: {exc}"
            self.statusMessageChanged.emit()

    @Slot()
    def clearCompletedQueueItems(self) -> None:
        if not self.canModifyQueue:
            return
        self._active_queue().clear_completed()

    @Slot()
    def retryFailedQueueItems(self) -> None:
        if not self.canModifyQueue:
            return
        self._active_queue().retry_failed()

    def _clear_preview_state(self, key: str | None = None) -> None:
        key = key or self._context_key()
        self._contexts[key] = PreviewState(generation=self._preview_generation + 1)
        self._preview_generation += 1
        self._image_provider.clear(f"input-{key}")
        self._image_provider.clear(f"output-{key}")
        self._image_provider.clear(f"poster-{key}")
        if key == self._context_key():
            self._emit_context()

    @staticmethod
    def _image_dimensions(preview: Any) -> tuple[int, int]:
        if isinstance(preview, (str, Path)):
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(preview) as image:
                    return int(image.width), int(image.height)
            except (OSError, ValueError, TypeError):
                return 0, 0
        width = getattr(preview, "width", 0)
        height = getattr(preview, "height", 0)
        if callable(width):
            width = width()
        if callable(height):
            height = height()
        if (not width or not height) and hasattr(preview, "shape"):
            shape = getattr(preview, "shape", ())
            if len(shape) >= 2:
                height, width = int(shape[0]), int(shape[1])
        try:
            return int(width or 0), int(height or 0)
        except (TypeError, ValueError):
            return 0, 0

    def _load_input_preview_async(self, path_str: str, row: int, context_key: str) -> None:
        path = Path(path_str)
        if not path.is_file():
            return
        queue = self._queue_for_context(context_key)
        context = self._contexts[context_key]
        context.selected_path = str(path)
        context.selected_row = row
        context.output_url = ""
        context.output_is_video = False
        context.output_info = ""
        context.output_width = 0
        context.output_height = 0
        context.preview_source_seconds = 0.0
        context.preview_source_frame = 0
        context.last_playhead_seconds = 0.0
        context.rendered_ranges = []
        context.poster_url = ""
        self._preview_generation += 1
        generation = self._preview_generation
        context.generation = generation
        self._image_provider.clear(f"poster-{context_key}")
        if context_key == self._context_key():
            self._emit_context()

        if self._metadata_worker is not None:
            try:
                self._metadata_worker.controller.stop()
            except Exception:
                pass

        operation_id = self._set_operation(LOADING_METADATA, "Loading media…")
        accepts_images = context_key in {"nr-image", "upscale-image"}

        def task(controller=None, progress=None) -> dict[str, Any]:
            suffix = path.suffix.lower()
            is_image = suffix in IMAGE_SUFFIXES
            if accepts_images and is_image:
                if progress:
                    progress(0.15, "Decoding image preview…")
                preview = decode_image_preview(str(path))
                width, height = self._image_dimensions(preview)
                return {"kind": "image", "preview": preview, "width": width, "height": height}
            if progress:
                progress(0.15, "Probing video metadata…")
            meta = probe_video(str(path), count_mode="metadata")
            width = int(meta.get("display_width") or meta.get("width") or 0)
            height = int(meta.get("display_height") or meta.get("height") or 0)
            rotation = int(meta.get("rotation") or 0)
            if rotation in {90, 270} and not meta.get("display_width"):
                width, height = height, width
            # ffmpeg poster guarantees a visible frame even if QtMultimedia
            # fails to load the file (black-screen fallback in QML).
            poster_jpeg: bytes | None = None
            if progress:
                progress(0.55, "Extracting poster frame…")
            try:
                from ..core.ffmpeg.preview import grab_video_poster_jpeg
                poster_jpeg = grab_video_poster_jpeg(str(path), seconds=0.0, max_width=960)
            except Exception as exc:
                self._log(f"Poster extraction warning for {path.name}: {exc}")
            return {
                "kind": "video",
                "width": width,
                "height": height,
                "fps": float(meta.get("fps") or 0.0),
                "duration": float(meta.get("duration") or 0.0),
                "meta": meta,
                "poster_jpeg": poster_jpeg,
            }

        worker = JobWorker(task)
        self._metadata_worker = worker

        def progress(fraction: float, message: str) -> None:
            if generation != self._contexts[context_key].generation:
                return
            self._status_message = message
            self.statusMessageChanged.emit()

        def done(result: dict[str, Any]) -> None:
            if generation != self._contexts[context_key].generation or self._shutting_down:
                return
            self._metadata_worker = None
            ctx = self._contexts[context_key]
            width = int(result.get("width") or 0)
            height = int(result.get("height") or 0)
            ctx.source_width = width
            ctx.source_height = height
            ctx.source_aspect_ratio = (width / height) if width and height else None
            ctx.source_fps = float(result.get("fps") or 0.0)
            ctx.duration_seconds = float(result.get("duration") or 0.0)
            dimensions = f"{width}×{height}" if width and height else ""
            if result.get("kind") == "image":
                digest = hashlib.sha1(str(path.resolve()).encode("utf-8", "surrogatepass")).hexdigest()[:16]
                image_id = f"thumb-{context_key}-{digest}"
                self._image_provider.set_image(image_id, result.get("preview"))
                ctx.input_url = f"image://preview/{image_id}?g={generation}"
                ctx.input_is_video = False
                ctx.input_info = f"{path.name} | {dimensions}" if dimensions else path.name
                queue.update_metadata(row, input_dimensions=dimensions, thumbnail_url=ctx.input_url)
            else:
                ctx.input_url = QUrl.fromLocalFile(str(path)).toString()
                ctx.input_is_video = True
                fps = float(result.get("fps") or 0.0)
                dur = float(result.get("duration") or 0.0)
                ctx.input_info = f"{path.name} | {dimensions}" if dimensions else path.name
                if fps:
                    ctx.input_info += f" | {fps:.3f} FPS".replace(".000", "")
                if dur:
                    ctx.input_info += f" | {dur:.1f}s"
                queue.update_metadata(row, input_dimensions=dimensions)
                poster_jpeg = result.get("poster_jpeg")
                if poster_jpeg:
                    try:
                        from PIL import Image as _PILImage
                        import io as _io
                        pil = _PILImage.open(_io.BytesIO(bytes(poster_jpeg)))
                        poster_id = f"poster-{context_key}"
                        self._image_provider.set_image(poster_id, pil)
                        ctx.poster_url = f"image://preview/{poster_id}?g={generation}"
                    except Exception as exc:
                        self._log(f"Poster decode warning for {path.name}: {exc}")
            if context_key == self._context_key():
                self._emit_context()
                self.settingsUpdated.emit()
            if self._finish_operation(operation_id, "Ready."):
                self._schedule_auto_preview(140)

        def failed(message: str) -> None:
            if generation != self._contexts[context_key].generation:
                return
            self._metadata_worker = None
            ctx = self._contexts[context_key]
            ctx.input_url = QUrl.fromLocalFile(str(path)).toString()
            ctx.input_is_video = context_key not in {"nr-image", "upscale-image"}
            ctx.input_info = path.name
            if context_key == self._context_key():
                self._emit_context()
            self._finish_operation(operation_id, f"Metadata unavailable for {path.name}.")
            self._log(f"Media metadata/preview failed for {path.name}: {message}")

        worker.signals.progress.connect(progress)
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(lambda: self._finish_operation(operation_id, "Media loading cancelled."))
        self._thread_pool.start(worker)

    # =========================================================================
    # Batch Processing & Worker Dispatch
    # =========================================================================
    @Slot()
    def startActiveBatch(self) -> None:
        """Compatibility entry point for batch actions; show export choices first."""
        self.requestActiveBatchExport()

    @Slot(result=bool)
    def requestActiveBatchExport(self) -> bool:
        context_key = self._context_key()
        if context_key not in {"nr-image", "nr-video", "upscale-image", "upscale-video", "fi-video"} or self._active_tab not in {"neural-rendering", "upscale", "frame-interpolation"}:
            self._status_message = "Select a batch processing tab before exporting."
        elif self._operation_state != IDLE or self._runtime_state != "Ready" or self._is_live_running:
            self._status_message = "Stop the current operation before starting a batch."
        elif not self._active_queue().get_paths():
            self._status_message = "No files in queue to process."
        else:
            self.batchExportRequested.emit(context_key)
            return True
        self.statusMessageChanged.emit()
        return False

    @Slot(str, str, str, str, str, result=str)
    def startActiveBatchWithExport(self, context_key: str, destination_mode: str,
                                   folder_url_or_path: str, rename_mode: str,
                                   custom_suffix: str) -> str:
        """Validate the dialog's choices, then launch exactly one batch."""
        if context_key != self._context_key() or self._active_tab not in {"neural-rendering", "upscale", "frame-interpolation"}:
            return self._batch_export_error("The active workflow changed. Reopen Export Path.")
        if self._operation_state != IDLE or self._runtime_state != "Ready" or self._is_live_running:
            return self._batch_export_error("Another operation is active; stop it before starting a batch.")
        queue = self._active_queue()
        paths = list(queue.get_paths())
        if not paths:
            return self._batch_export_error("No files in queue to process.")
        rename_fields = {
            "nr-image": ("image_rename_mode", "image_custom_suffix"),
            "nr-video": ("video_rename_mode", "video_custom_suffix"),
            "upscale-image": ("upscale_image_rename_mode", "upscale_image_custom_suffix"),
            "upscale-video": ("upscale_rename_mode", "upscale_custom_suffix"),
            "fi-video": ("frame_interpolation_rename_mode", "frame_interpolation_custom_suffix"),
        }
        if context_key not in rename_fields:
            return self._batch_export_error("This workspace does not support batch rendering.")
        try:
            validate_rename(rename_mode, custom_suffix)
            mode_field, suffix_field = rename_fields[context_key]
            candidate = _validate(replace(self._settings, **{
                mode_field: rename_mode, suffix_field: custom_suffix,
            }))
            if context_key == "upscale-image":
                image_upscale_options_from_settings(candidate).validate(for_render=True)
            elif context_key == "upscale-video":
                video_upscale_options_from_settings(candidate).validate(for_render=True)

            if destination_mode == "input":
                for parent in {Path(path).resolve().parent for path in paths}:
                    prepare_output_dir(parent)
                output_dir = None
                same_as_input = True
            elif destination_mode == "output":
                output_dir = prepare_output_dir(OUTPUTS)
                same_as_input = False
            elif destination_mode == "folder":
                folder = self._clean_path(folder_url_or_path)
                if not folder or not Path(folder).is_dir():
                    raise ValueError("Choose an existing export folder.")
                output_dir = prepare_output_dir(folder)
                same_as_input = False
            else:
                raise ValueError("Choose an Export Path option.")
        except Exception as exc:
            return self._batch_export_error(str(exc))

        if not self._save_setting(**{mode_field: rename_mode, suffix_field: custom_suffix}):
            return self._batch_export_error(self._status_message)

        self._flush_settings()
        queue.reset_all_states()
        self._overall_progress = 0.0
        self.overallProgressChanged.emit()
        context_key = self._context_key()
        settings = self._settings
        operation_id = self._set_operation(BATCH_PREPARING, "Preparing batch processing…")

        try:
            if context_key == "nr-image":
                worker = self._nr_image_batch_worker(paths, settings, output_dir, same_as_input)
            elif context_key == "nr-video":
                worker = self._nr_video_batch_worker(paths, settings, output_dir, same_as_input)
            elif context_key == "upscale-image":
                worker = self._upscale_image_batch_worker(paths, settings, output_dir, same_as_input)
            elif context_key == "upscale-video":
                worker = self._upscale_video_batch_worker(paths, settings, output_dir, same_as_input)
            elif context_key == "fi-video":
                worker = self._fi_batch_worker(paths, settings, output_dir, same_as_input)
            else:
                raise RuntimeError("This workspace does not support batch rendering.")
        except Exception as exc:
            self._finish_operation(operation_id, f"Cannot start batch: {exc}")
            return f"Cannot start batch: {exc}"
        try:
            self._setup_worker(worker, queue, operation_id, context_key)
        except Exception as exc:
            self._finish_operation(operation_id, f"Cannot start batch: {exc}")
            return f"Cannot start batch: {exc}"
        return ""

    def _batch_export_error(self, message: str) -> str:
        self._status_message = message
        self.statusMessageChanged.emit()
        return message

    def _setup_worker(self, worker: JobWorker, queue: BatchListModel, operation_id: int, context_key: str) -> None:
        self._active_worker = worker
        self._operation_state = BATCH_RUNNING
        self.operationStateChanged.emit()

        def on_progress(fraction: float, message: str) -> None:
            if operation_id != self._operation_id:
                return
            self._overall_progress = max(0.0, min(1.0, float(fraction)))
            self._status_message = message
            self.overallProgressChanged.emit()
            self.statusMessageChanged.emit()

        def on_item_update(idx: int, state: str, prog: float, detail: str, out_p: str, elap: float) -> None:
            if operation_id != self._operation_id or idx < 0:
                return
            queue.update_item_progress(idx, state, prog, detail, out_p, elap)
            normalized = {"Complete": "Completed", "Processing": "Running"}.get(state, state)
            if normalized == "Completed" and out_p and Path(out_p).is_file() and idx == queue.selectedIndex:
                self._set_context_output_from_path(context_key, out_p, "Rendered output")

        def on_finished(result: Any) -> None:
            if operation_id != self._operation_id:
                return
            successes = list(getattr(result, "successes", []) or [])
            failures = list(getattr(result, "failures", []) or [])
            cancelled = bool(getattr(result, "cancelled", False))
            self._overall_progress = 1.0 if not cancelled else self._overall_progress
            self.overallProgressChanged.emit()
            if cancelled:
                message = f"Batch cancelled: {len(successes)} completed, {len(failures)} failed/skipped."
            elif failures:
                message = f"Completed with warnings: {len(successes)} completed, {len(failures)} failed."
                first_error = getattr(failures[0], "error", "") if failures else ""
                if first_error:
                    message += f" First error: {first_error}"
            else:
                message = f"Batch complete: {len(successes) or len(queue.get_paths())} completed."
            self._finish_operation(operation_id, message)
            self._log(message)

        def on_failed(error_msg: str) -> None:
            if operation_id != self._operation_id:
                return
            message = f"Batch failed: {error_msg}"
            self._finish_operation(operation_id, message)
            self._log(message)

        def on_cancelled() -> None:
            if operation_id != self._operation_id:
                return
            message = "Batch stopped by user."
            self._finish_operation(operation_id, message)
            self._log(message)

        worker.signals.progress.connect(on_progress)
        worker.signals.itemUpdated.connect(on_item_update)
        worker.signals.finished.connect(on_finished)
        worker.signals.failed.connect(on_failed)
        worker.signals.cancelled.connect(on_cancelled)
        self._thread_pool.start(worker)

    def _set_context_output_from_path(self, context_key: str, output_path: str, label: str = "Output") -> None:
        path = Path(output_path)
        if not path.is_file():
            return
        context = self._contexts[context_key]
        ext = path.suffix.lower()
        image_exts = set(IMAGE_EXTENSIONS.values()) | {".jpg", ".jpeg", ".png", ".webp", ".avif", ".tif", ".tiff"}
        if ext in image_exts:
            image_id = f"output-{context_key}"
            self._image_provider.set_image(image_id, str(path))
            context.output_url = f"image://preview/{image_id}?g={context.generation}"
            context.output_is_video = False
            try:
                from PIL import Image
                with Image.open(path) as image:
                    context.output_width, context.output_height = image.size
            except Exception:
                context.output_width = context.output_height = 0
        else:
            context.output_url = QUrl.fromLocalFile(str(path)).toString()
            context.output_is_video = True
        context.output_info = f"{label} | {path.name}"
        if context_key == self._context_key():
            self._emit_context()

    def _nr_image_batch_worker(self, paths: list[str], settings: UISettings,
                               output_dir: Path | None, same_as_input: bool) -> JobWorker:
        opts = ImageConversionOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid,
            nr_style=settings.nr_style,
            nr_intensity=settings.nr_intensity,
            nr_passes=settings.nr_passes,
            local_tone_strength=settings.local_tone_strength,
            local_structure_strength=settings.local_structure_strength,
            skin_structure_strength=settings.skin_structure_strength,
            automatic_mask=settings.automatic_mask,
            nr_color_strength=settings.nr_color_strength,
            tone_preservation=settings.tone_preservation,
            face_skin_protection=settings.face_skin_protection,
            grain_preservation=settings.grain_preservation,
            mask_feather=settings.mask_feather,
            nr_mask=settings.nr_mask,
            upscaling_factor=settings.upscaling_factor,
            output_format=settings.image_format,
            quality=settings.image_quality,
            rename_mode=settings.image_rename_mode,
            custom_suffix=settings.image_custom_suffix,
        )
        return JobWorker(convert_images, paths, opts, output_dir=output_dir,
                         generate_previews=True, create_zip=False, same_as_input=same_as_input)

    def _nr_video_batch_worker(self, paths: list[str], settings: UISettings,
                               output_dir: Path | None, same_as_input: bool) -> JobWorker:
        opts = ConversionOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid,
            video_gpu_uuid=settings.video_gpu_uuid,
            nr_style=settings.nr_style,
            nr_intensity=settings.nr_intensity,
            nr_passes=settings.nr_passes,
            local_tone_strength=settings.local_tone_strength,
            local_structure_strength=settings.local_structure_strength,
            skin_structure_strength=settings.skin_structure_strength,
            automatic_mask=settings.automatic_mask,
            nr_color_strength=settings.nr_color_strength,
            tone_preservation=settings.tone_preservation,
            face_skin_protection=settings.face_skin_protection,
            grain_preservation=settings.grain_preservation,
            shimmer_suppression=settings.shimmer_suppression,
            mask_feather=settings.mask_feather,
            nr_mask=settings.nr_mask,
            upscaling_factor=settings.upscaling_factor,
            codec=settings.codec,
            container=container_for_codec(settings.codec),
            quality=settings.quality,
            preserve_hdr=coerce_hdr_mode(settings.codec, settings.hdr_mode),
            rename_mode=settings.video_rename_mode,
            custom_suffix=settings.video_custom_suffix,
        )
        return JobWorker(convert_videos, paths, opts, output_dir=output_dir,
                         create_archive=False, same_as_input=same_as_input)

    def _upscale_image_batch_worker(self, paths: list[str], settings: UISettings,
                                    output_dir: Path | None, same_as_input: bool) -> JobWorker:
        opts = image_upscale_options_from_settings(settings)
        opts.validate(for_render=True)
        return JobWorker(upscale_images, paths, opts, output_dir=output_dir,
                         generate_previews=True, same_as_input=same_as_input)

    def _upscale_video_batch_worker(self, paths: list[str], settings: UISettings,
                                    output_dir: Path | None, same_as_input: bool) -> JobWorker:
        opts = video_upscale_options_from_settings(settings)
        opts.validate(for_render=True)
        return JobWorker(upscale_videos, paths, opts, output_dir=output_dir,
                         same_as_input=same_as_input)

    def _fi_batch_worker(self, paths: list[str], settings: UISettings,
                         output_dir: Path | None, same_as_input: bool) -> JobWorker:
        opts = FrameInterpolationOptions(
            ai_gpu_uuid=settings.ai_gpu_uuid,
            video_gpu_uuid=settings.video_gpu_uuid,
            target_fps=settings.frame_interpolation_target_fps,
            engine=settings.frame_interpolation_engine,
            codec=settings.frame_interpolation_codec,
            container=container_for_codec(settings.frame_interpolation_codec),
            quality=settings.frame_interpolation_quality,
            hdr_mode=coerce_hdr_mode(settings.frame_interpolation_codec, settings.frame_interpolation_hdr_mode),
            rename_mode=settings.frame_interpolation_rename_mode,
            custom_suffix=settings.frame_interpolation_custom_suffix,
        )
        return JobWorker(interpolate_videos, paths, opts, output_dir=output_dir,
                         same_as_input=same_as_input)

    @Slot()
    def stopActiveBatch(self) -> None:
        if self._operation_state in {PREVIEW_PREPARING, PREVIEW_RUNNING} and self._active_worker is not None:
            self._operation_state = PREVIEW_CANCELLING
            self.operationStateChanged.emit()
            self._active_worker.controller.stop()
            self._status_message = "Cancelling preview…"
            self.statusMessageChanged.emit()
            return
        if self._operation_state in {BATCH_PREPARING, BATCH_RUNNING} and self._active_worker is not None:
            self._operation_state = BATCH_CANCELLING
            self.operationStateChanged.emit()
            self._active_worker.controller.stop()
            self._status_message = "Cancelling current batch…"
            self.statusMessageChanged.emit()
            self._log("User requested batch cancellation.")
            return
        if self._operation_state in {LIVE_STARTING, LIVE_RUNNING}:
            self._status_message = "Stopping Live session…"
            self.statusMessageChanged.emit()
            self.stopLive()
            return

    # =========================================================================
    # Previews
    # =========================================================================
    @Slot()
    def renderPreview(self) -> None:
        # Realtime/auto and menu-driven preview renders the last parked
        # timeline frame (frame 1 when nothing was ever parked).
        ctx = self._context()
        try:
            parked_ms = max(0.0, float(ctx.last_playhead_seconds or 0.0) * 1000.0)
        except (TypeError, ValueError):
            parked_ms = 0.0
        start_seconds, frame_index = self._quantize_playhead(parked_ms)
        self._render_preview_impl(start_seconds, frame_index)

    @Slot(float)
    def notePlayheadMs(self, position_ms: float = 0.0) -> None:
        """Remember a user-parked timeline position for realtime renders.

        Called from QML transport gestures (seek/step/jump) and pauses only —
        never from playback ticks — so the stored frame is always a deliberate
        parking spot. Deliberately signal-free: safe at scrub-tick rates.
        """
        try:
            pos = max(0.0, float(position_ms or 0.0))
        except (TypeError, ValueError):
            return
        if not math.isfinite(pos):
            return
        self._context().last_playhead_seconds = pos / 1000.0

    def _quantize_playhead(self, position_ms: float = 0.0) -> tuple[float, int]:
        """Snap a raw playhead position to the nominal timeline frame.

        Returns ``(start_seconds, frame_index)`` where frame N is
        ``floor(position*fps)+1`` occupying ``[(N-1)/fps, N/fps)``. A raw
        ``position/1000`` passed straight to ``ffmpeg -ss`` selects the first
        frame with ``pts >= START``, i.e. N+1 whenever the playhead sits
        strictly inside frame N — hence the quantization.
        """
        try:
            raw_seconds = max(0.0, float(position_ms or 0.0) / 1000.0)
        except (TypeError, ValueError):
            raw_seconds = 0.0
        ctx = self._context()
        try:
            fps = float(ctx.source_fps or 0.0)
        except (TypeError, ValueError):
            fps = 0.0
        if not (fps and math.isfinite(fps) and fps > 0.01):
            fps = 30.0
        try:
            duration = float(ctx.duration_seconds or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        # Clamp to probed duration so a stale playhead past EOS falls back
        # to the last decodable second instead of failing extraction.
        if duration > 0:
            raw_seconds = min(raw_seconds, max(0.0, duration - 0.05))
        total_frames = max(0, int(round(duration * fps))) if duration > 0 else 0
        frame_index = int(math.floor(raw_seconds * fps)) + 1
        if total_frames > 0:
            frame_index = max(1, min(total_frames, frame_index))
        else:
            frame_index = max(1, frame_index)
        start_seconds = (frame_index - 1) / fps if fps > 0 else raw_seconds
        if duration > 0:
            start_seconds = min(start_seconds, max(0.0, duration - 0.05))
        return start_seconds, frame_index

    @Slot(float)
    def renderPreviewAt(self, position_ms: float = 0.0) -> None:
        """Explicit Preview: render the parked frame or selected Upscale clip."""
        if self.canPreview:
            # A pending realtime frame must not overwrite a manual clip as
            # soon as it finishes (or erase its green timeline range).
            self._auto_preview_pending = False
            self._auto_preview_timer.stop()
            self._scrub_pending_pos = None
            self._scrub_preview_timer.stop()
        start_seconds, frame_index = self._quantize_playhead(position_ms)
        self.notePlayheadMs(position_ms)
        clip_seconds = (self._upscale_preview_length_seconds()
                        if self._context_key() == "upscale-video" else None)
        self._render_preview_impl(start_seconds, frame_index, clip_seconds=clip_seconds)

    @Slot(float)
    def schedulePreviewAt(self, position_ms: float = 0.0) -> None:
        """Debounced scrub-realtime: refresh the preview at the playhead.

        Called on every user timeline move in Output/2-Up; the actual render
        starts only after scrubbing settles (~750 ms idle), so scrubbing never
        melts the GPU. Only refreshes an existing video preview while Realtime
        is enabled — never during batches, Live, or metadata loading. Frame
        Interpolation is excluded (manual "Preview" only).
        """
        if (
            self._shutting_down
            or not self._auto_preview_enabled
            or self._runtime_state != "Ready"
            or self._is_live_running
        ):
            return
        if self._context_key() not in {"nr-video", "upscale-video"}:
            return
        ctx = self._context()
        if not ctx.has_output or not ctx.output_is_video:
            return
        try:
            source = self._active_queue().selected_path()
        except Exception:
            source = ""
        if not source:
            return
        if self._operation_state in {BATCH_PREPARING, BATCH_RUNNING, BATCH_CANCELLING,
                                     LIVE_STARTING, LIVE_RUNNING, LIVE_STOPPING}:
            return
        try:
            self._scrub_pending_pos = max(0.0, float(position_ms or 0.0))
        except (TypeError, ValueError):
            self._scrub_pending_pos = 0.0
        if self._operation_state in {PREVIEW_PREPARING, PREVIEW_RUNNING}:
            # A render is in flight: invalidate it so only the latest settled
            # position is ever processed, then debounce the replacement.
            self._invalidate_preview_generation()
            worker = self._active_worker
            if worker is not None:
                try:
                    worker.controller.stop()
                except Exception:
                    pass
            self._operation_state = PREVIEW_CANCELLING
            self.operationStateChanged.emit()
        self._scrub_preview_timer.start(750)

    def _fire_scrub_preview(self) -> None:
        """Launch the debounced scrub-refresh render, if still applicable."""
        if (
            self._shutting_down
            or not self._auto_preview_enabled
            or self._runtime_state != "Ready"
            or self._is_live_running
            or self._scrub_pending_pos is None
        ):
            self._scrub_pending_pos = None
            return
        if self._operation_state in {PREVIEW_PREPARING, PREVIEW_RUNNING, PREVIEW_CANCELLING,
                                     LOADING_METADATA, SCANNING_INPUTS}:
            # Still busy: retry shortly instead of dropping the scrub.
            self._scrub_preview_timer.start(400)
            return
        if self._operation_state != IDLE:
            self._scrub_pending_pos = None
            return
        if self._context_key() not in {"nr-video", "upscale-video"}:
            self._scrub_pending_pos = None
            return
        ctx = self._context()
        if not ctx.has_output or not ctx.output_is_video:
            self._scrub_pending_pos = None
            return
        pos_ms, self._scrub_pending_pos = self._scrub_pending_pos, None
        start_seconds, frame_index = self._quantize_playhead(pos_ms)
        self._render_preview_impl(start_seconds, frame_index)

    @Slot(float)
    def refreshVideoPoster(self, position_ms: float = 0.0) -> None:
        """Refresh the poster still (used when the native player stalls)."""
        context_key = self._context_key()
        context = self._contexts[context_key]
        source = context.selected_path or self._active_queue().selected_path()
        if not source or not Path(source).is_file():
            return
        try:
            seconds = max(0.0, float(position_ms or 0.0) / 1000.0)
        except (TypeError, ValueError):
            seconds = 0.0
        generation = context.generation

        def task(controller=None, progress=None):
            from ..core.ffmpeg.preview import grab_video_poster_jpeg
            return grab_video_poster_jpeg(source, seconds=seconds, max_width=960)

        def done(jpeg: bytes) -> None:
            if generation != self._contexts[context_key].generation or self._shutting_down:
                return
            try:
                from PIL import Image as _PILImage
                import io as _io
                pil = _PILImage.open(_io.BytesIO(bytes(jpeg)))
                poster_id = f"poster-{context_key}"
                self._image_provider.set_image(poster_id, pil)
                self._contexts[context_key].poster_url = f"image://preview/{poster_id}?g={generation}"
                if context_key == self._context_key():
                    self.previewPosterUrlChanged.emit()
            except Exception as exc:
                self._log(f"Poster refresh failed: {exc}")

        def failed(message: str) -> None:
            self._log(f"Poster refresh failed: {message}")

        worker = JobWorker(task, inject_callbacks=False)
        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        self._thread_pool.start(worker)

    def _render_preview_impl(self, start_seconds: float = 0.0, frame_index: int = 1,
                             *, clip_seconds: float | None = None) -> None:
        if not self.canPreview:
            self._status_message = "Preview is unavailable while another operation is active."
            self.statusMessageChanged.emit()
            return
        queue = self._active_queue()
        source = queue.selected_path()
        if not source:
            self._status_message = "Add and select a file first to preview."
            self.statusMessageChanged.emit()
            return

        context_key = self._context_key()
        context = self._contexts[context_key]
        self._preview_generation += 1
        generation = self._preview_generation
        context.generation = generation
        # Previews always land in place: the viewer stays on whichever tab
        # opened the render (Input, Output, or 2-Up) and the finished output
        # swaps atomically into it. No tab is ever switched programmatically.
        settings = self._settings
        # Fingerprint of what this render is built from (selected source +
        # settings snapshot). Stored on success so later tab/mode switches
        # can skip the re-render while nothing changed.
        preview_fingerprint = self._auto_preview_fingerprint(context_key)
        is_video_context = context_key in {"nr-video", "upscale-video", "fi-video"}
        try:
            _fps = float(context.source_fps or 0.0)
        except (TypeError, ValueError):
            _fps = 0.0
        if not (_fps and math.isfinite(_fps) and _fps > 0.01):
            _fps = 30.0
        try:
            _dur = float(context.duration_seconds or 0.0)
        except (TypeError, ValueError):
            _dur = 0.0
        _total = max(0, int(round(_dur * _fps))) if _dur > 0 else 0
        try:
            frame_index = max(1, int(frame_index or 1))
        except (TypeError, ValueError):
            frame_index = 1
        if _total > 0:
            frame_index = min(_total, frame_index)
        if is_video_context and start_seconds > 0.05 and _total > 0:
            stamp_label = f" f{frame_index}/{_total} @ {start_seconds:.2f}s"
        elif is_video_context and start_seconds > 0.05:
            stamp_label = f" @ {start_seconds:.2f}s"
        else:
            stamp_label = ""
        operation_id = self._set_operation(PREVIEW_PREPARING, f"Rendering preview{stamp_label}…")
        # Keep the last completed preview visible while the next realtime
        # generation is rendering.  The output is swapped atomically only
        # after a successful result, eliminating the blank/flicker between
        # slider edits.  Selecting a different source still clears the output.

        try:
            if context_key == "nr-image":
                opts = ImageConversionOptions(
                    ai_gpu_uuid=settings.ai_gpu_uuid,
                    nr_style=settings.nr_style,
                    nr_intensity=settings.nr_intensity,
                    nr_passes=settings.nr_passes,
                    local_tone_strength=settings.local_tone_strength,
                    local_structure_strength=settings.local_structure_strength,
                    skin_structure_strength=settings.skin_structure_strength,
                    automatic_mask=settings.automatic_mask,
                    nr_color_strength=settings.nr_color_strength,
                    tone_preservation=settings.tone_preservation,
                    face_skin_protection=settings.face_skin_protection,
                    grain_preservation=settings.grain_preservation,
                    mask_feather=settings.mask_feather,
                    nr_mask=settings.nr_mask,
                    upscaling_factor=settings.upscaling_factor,
                    output_format="PNG",
                    quality=100,
                )
                def task(controller=None, progress=None):
                    return render_image_preview(
                        source, opts, controller=controller, progress=progress,
                    )
                result_kind = "image"
            elif context_key == "upscale-image":
                opts = image_upscale_options_from_settings(settings)
                opts.validate(for_render=True)
                def task(controller=None, progress=None):
                    return preview_upscale_image(
                        source, opts, controller=controller, progress=progress,
                    )
                result_kind = "image"
            elif context_key == "nr-video":
                opts = ConversionOptions(
                    ai_gpu_uuid=settings.ai_gpu_uuid,
                    video_gpu_uuid=settings.video_gpu_uuid,
                    nr_style=settings.nr_style,
                    nr_intensity=settings.nr_intensity,
                    nr_passes=settings.nr_passes,
                    local_tone_strength=settings.local_tone_strength,
                    local_structure_strength=settings.local_structure_strength,
                    skin_structure_strength=settings.skin_structure_strength,
                    automatic_mask=settings.automatic_mask,
                    nr_color_strength=settings.nr_color_strength,
                    tone_preservation=settings.tone_preservation,
                    face_skin_protection=settings.face_skin_protection,
                    grain_preservation=settings.grain_preservation,
                    shimmer_suppression=settings.shimmer_suppression,
                    mask_feather=settings.mask_feather,
                    nr_mask=settings.nr_mask,
                    upscaling_factor=settings.upscaling_factor,
                    codec=settings.codec,
                    container=container_for_codec(settings.codec),
                    quality=settings.quality,
                    preserve_hdr=settings.hdr_mode,
                )
                def task(controller=None, progress=None):
                    return process_video_preview(
                        source, opts, preview_frames=1,
                        preview_encoding=settings.preview_encoding,
                        progress=progress, output_dir=self._preview_cache,
                        controller=controller, ephemeral_preview=False,
                        start_seconds=start_seconds, frame_index=frame_index,
                    )
                result_kind = "video"
            elif context_key == "upscale-video":
                opts = video_upscale_options_from_settings(settings)
                opts.validate(for_render=True)
                def task(controller=None, progress=None):
                    return preview_upscale_native(
                        source, opts, one_frame=clip_seconds is None, progress=progress,
                        controller=controller, output_dir=self._preview_cache,
                        preview_encoding=settings.preview_encoding,
                        start_seconds=start_seconds, frame_index=frame_index,
                        preview_seconds=clip_seconds,
                    )
                result_kind = "video"
            elif context_key == "fi-video":
                opts = FrameInterpolationOptions(
                    ai_gpu_uuid=settings.ai_gpu_uuid,
                    video_gpu_uuid=settings.video_gpu_uuid,
                    target_fps=settings.frame_interpolation_target_fps,
                    engine=settings.frame_interpolation_engine,
                    codec=settings.frame_interpolation_codec,
                    container=container_for_codec(settings.frame_interpolation_codec),
                    quality=settings.frame_interpolation_quality,
                    hdr_mode=settings.frame_interpolation_hdr_mode,
                )
                def task(controller=None, progress=None):
                    return preview_frame_interpolation_native(
                        source, opts, preview_encoding=settings.preview_encoding,
                        progress=progress, output_dir=self._preview_cache,
                        controller=controller, start_seconds=start_seconds,
                        frame_index=frame_index,
                        preview_seconds=self._fi_preview_length_seconds(),
                    )
                result_kind = "video"
            else:
                raise RuntimeError("Preview is unavailable for this workspace.")
        except Exception as exc:
            self._finish_operation(operation_id, f"Preview configuration error: {exc}")
            return

        worker = JobWorker(task)
        self._active_worker = worker
        self._operation_state = PREVIEW_RUNNING
        self.operationStateChanged.emit()

        def on_progress(fraction: float, message: str) -> None:
            if operation_id != self._operation_id or generation != self._contexts[context_key].generation:
                return
            self._overall_progress = fraction
            self._status_message = message
            self.overallProgressChanged.emit()
            self.statusMessageChanged.emit()

        def finished(result: Any) -> None:
            if operation_id != self._operation_id:
                return
            if generation != self._contexts[context_key].generation:
                self._finish_operation(operation_id, "Preview superseded by newer settings.")
                if self._auto_preview_pending:
                    self._auto_preview_timer.start(80)
                return
            ctx = self._contexts[context_key]
            media, status = result
            rendered_ok = False
            if result_kind == "image":
                if media is not None:
                    image_id = f"output-{context_key}"
                    self._image_provider.set_image(image_id, media)
                    ctx.output_url = f"image://preview/{image_id}?g={generation}"
                    ctx.output_is_video = False
                    ctx.output_width, ctx.output_height = self._image_dimensions(media)
                    ctx.output_info = (
                        f"Enhanced preview | {ctx.output_width}×{ctx.output_height}"
                        if ctx.output_width and ctx.output_height else "Enhanced preview"
                    )
                    rendered_ok = True
            else:
                if media and Path(str(media)).is_file():
                    ctx.output_url = QUrl.fromLocalFile(str(media)).toString()
                    ctx.output_is_video = True
                    ctx.preview_source_seconds = max(0.0, float(start_seconds or 0.0))
                    try:
                        ctx.preview_source_frame = max(0, int(frame_index or 0))
                    except (TypeError, ValueError):
                        ctx.preview_source_frame = 0
                    if context_key == "fi-video":
                        # Successful clip: paint its span green, clamped to
                        # the source duration.
                        self._add_rendered_range(context_key, start_seconds, str(media),
                                                 self._fi_preview_length_seconds())
                    elif context_key == "upscale-video":
                        if clip_seconds is not None:
                            self._add_rendered_range(context_key, start_seconds, str(media), clip_seconds)
                        else:
                            self._clear_rendered_ranges(context_key)
                    if start_seconds > 0.05 and _total > 0:
                        ctx.output_info = f"Preview f{frame_index}/{_total} @ {start_seconds:.2f}s | {Path(str(media)).name}"
                    elif start_seconds > 0.05:
                        ctx.output_info = f"Preview @ {start_seconds:.2f}s | {Path(str(media)).name}"
                    else:
                        ctx.output_info = f"Preview | {Path(str(media)).name}"
                    rendered_ok = True
            if rendered_ok and preview_fingerprint:
                # Current output now matches this source + settings: later
                # tab/mode switches skip the redundant GPU re-render.
                ctx.last_auto_fingerprint = preview_fingerprint
            if context_key == self._context_key():
                self._emit_context()
            self._overall_progress = 1.0
            self.overallProgressChanged.emit()
            # Status bar shows only the main status; the full render detail
            # goes to the log drawer so no information is lost.
            self._finish_operation(operation_id, f"Preview complete{stamp_label}.")
            if status:
                for line in str(status).splitlines():
                    line = line.strip()
                    if line:
                        self._log(line)

        def failed(message: str) -> None:
            if operation_id != self._operation_id:
                return
            text = f"Preview failed: {message}"
            self._finish_operation(operation_id, text)
            self._log(text)
            if self._auto_preview_pending:
                self._auto_preview_timer.start(120)

        def cancelled() -> None:
            if operation_id != self._operation_id:
                return
            self._finish_operation(operation_id, "Preview cancelled.")
            if self._auto_preview_pending:
                self._auto_preview_timer.start(80)

        worker.signals.progress.connect(on_progress)
        worker.signals.finished.connect(finished)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(cancelled)
        self._thread_pool.start(worker)

    # =========================================================================
    # Live Workflow
    # =========================================================================
    @Slot(str, str, str, str, str, str, str, float)
    def startLive(
        self,
        source_mode: str,
        online_url: str,
        local_video_path: str,
        source_quality: str,
        max_height: str,
        segment_seconds: str,
        target_fps: str,
        buffer_seconds: float,
    ) -> None:
        if not self.canStartLive:
            self._live_status_text = "Live is unavailable while another operation is active."
            self.liveStatusTextChanged.emit()
            return

        if source_mode == "Local":
            cleaned = self._clean_path(local_video_path)
            if not cleaned or not Path(cleaned).is_file():
                self._live_status_text = "Select a valid local video file."
                self.liveStatusTextChanged.emit()
                return
            try:
                if not _is_supported_media_file(Path(cleaned), "video"):
                    self._live_status_text = "Select a valid local video file (MP4/MKV/MOV/AVI/WebM...)."
                    self.liveStatusTextChanged.emit()
                    return
            except Exception:
                if Path(cleaned).suffix.lower() not in VIDEO_SUFFIXES:
                    self._live_status_text = "Select a valid local video file (MP4/MKV/MOV/AVI/WebM...)."
                    self.liveStatusTextChanged.emit()
                    return
            selected_source = cleaned
        else:
            selected_source = online_url.strip()
            if not selected_source:
                self._live_status_text = "Enter a valid online URL (Twitch, YouTube, HLS, etc.)."
                self.liveStatusTextChanged.emit()
                return

        settings = self._settings
        # In-tab player: prepare the embed container now (GUI thread) so the
        # session worker can hand its handle straight to MPV at launch time.
        # ``wid == 0`` degrades to a detached player window, never a failure.
        ipc_pipe = (
            f"\\\\.\\pipe\\dlss5-live-{time.strftime('%Y%m%d-%H%M%S')}"
            f"-{os.getpid()}-{int(time.time() * 1000) % 100000:d}"
        )
        wid = 0
        try:
            wid, _ = self._mpv_embed.prepare(ipc_pipe)
        except Exception as exc:
            self._log(f"Live embed warning: {exc}")
        self._live_embed_wid = int(wid or 0)
        self._live_player_started = False
        try:
            h = int(max_height) if max_height.isdigit() else 720
            seg = int(segment_seconds) if segment_seconds.isdigit() else 2
            if source_quality not in LIVE_SOURCE_QUALITY_CHOICES:
                source_quality = "Auto"
            if target_fps not in LIVE_FPS_CHOICES:
                target_fps = "Auto"
            opts = LiveOptions(
                source=selected_source,
                nr_style=settings.live_nr_style,
                nr_intensity=settings.live_nr_intensity,
                nr_passes=settings.live_nr_passes,
                local_tone_strength=settings.live_local_tone_strength,
                local_structure_strength=settings.live_local_structure_strength,
                skin_structure_strength=settings.live_skin_structure_strength,
                automatic_mask=settings.live_automatic_mask,
                nr_color_strength=settings.live_nr_color_strength,
                tone_preservation=settings.live_tone_preservation,
                face_skin_protection=settings.live_face_skin_protection,
                grain_preservation=settings.live_grain_preservation,
                shimmer_suppression=settings.live_shimmer_suppression,
                mask_feather=settings.live_mask_feather,
                nr_mask=settings.nr_mask,
                max_height=h if h in LIVE_MAX_HEIGHTS else 720,
                upscaling_factor=settings.live_upscaling_factor,
                segment_seconds=seg if str(seg) in LIVE_SEGMENT_CHOICES else 2,
                target_fps=target_fps,
                open_mpv=True,
                mpv_wid=int(wid),
                mpv_ipc_server=ipc_pipe,
                buffer_seconds=max(2.0, min(30.0, float(buffer_seconds))),
                source_quality=source_quality,
            )
        except Exception as exc:
            self._live_status_text = f"Invalid Live configuration: {exc}"
            self.liveStatusTextChanged.emit()
            return

        self._flush_settings()
        self._is_live_starting = True
        self._stop_live_after_start = False
        self.isLiveStartingChanged.emit()
        operation_id = self._set_operation(LIVE_STARTING, "Resolving source and starting Live pipeline…")
        self._live_status_text = "Resolving source and starting Live pipeline…"
        self.liveStatusTextChanged.emit()

        worker = JobWorker(lambda: start_live_session(opts), inject_callbacks=False)
        self._live_worker = worker

        def done(info: Any) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._live_worker = None
            self._is_live_starting = False
            self._is_live_running = bool(getattr(info, "running", False) or is_live_running())
            self.isLiveStartingChanged.emit()
            self.isLiveRunningChanged.emit()
            if self._stop_live_after_start:
                self._stop_live_after_start = False
                self.stopLive()
                return
            self._live_status_text = getattr(info, "status", "Live session started.")
            self.liveStatusTextChanged.emit()
            if self._is_live_running:
                self._live_timer.start()
                self._finish_operation(operation_id, "Live session running.")
            else:
                self._finish_operation(operation_id, self._live_status_text)
            self._log(f"Live session started: {selected_source}")

        def failed(message: str) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._live_worker = None
            self._is_live_starting = False
            self._is_live_running = False
            self._stop_live_after_start = False
            self._live_status_text = f"Live failed: {message}"
            self.isLiveStartingChanged.emit()
            self.isLiveRunningChanged.emit()
            self.liveStatusTextChanged.emit()
            try:
                self._mpv_embed.set_session_active(False)
            except Exception:
                pass
            self._finish_operation(operation_id, self._live_status_text)
            self._log(self._live_status_text)

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(lambda: failed("Start cancelled."))
        self._thread_pool.start(worker)

    @Slot()
    def stopLive(self) -> None:
        if self._operation_state == LIVE_STARTING:
            # Do not cancel the starter worker mid-construction: the backend may
            # publish its LiveSession after a premature stop check, orphaning it.
            # Instead, complete construction and stop immediately in ``done``.
            self._stop_live_after_start = True
            self._live_status_text = "Cancel requested; stopping as soon as Live finishes starting…"
            self.liveStatusTextChanged.emit()
            self._status_message = "Cancelling Live startup…"
            self.statusMessageChanged.emit()
            return
        if not (self._is_live_running or is_live_running()):
            return
        if self._operation_state == LIVE_STOPPING:
            return

        self._live_timer.stop()
        operation_id = self._set_operation(LIVE_STOPPING, "Stopping Live session…")
        self._live_status_text = "Stopping Live session…"
        self.liveStatusTextChanged.emit()
        worker = JobWorker(stop_live_session, inject_callbacks=False)
        self._live_worker = worker

        def done(info: Any) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._live_worker = None
            self._is_live_starting = False
            self._is_live_running = False
            self._live_effects_dirty = False
            self._live_status_text = getattr(info, "status", "Stopped.")
            self.isLiveStartingChanged.emit()
            self.isLiveRunningChanged.emit()
            self.liveStatusTextChanged.emit()
            try:
                self._mpv_embed.set_session_active(False)
            except Exception:
                pass
            self._finish_operation(operation_id, "Live session stopped.")
            self._log("Live session stopped.")

        def failed(message: str) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._live_worker = None
            self._is_live_starting = False
            self._is_live_running = bool(is_live_running())
            self._live_status_text = f"Stop error: {message}"
            self.isLiveStartingChanged.emit()
            self.isLiveRunningChanged.emit()
            self.liveStatusTextChanged.emit()
            if not self._is_live_running:
                try:
                    self._mpv_embed.set_session_active(False)
                except Exception:
                    pass
            self._finish_operation(operation_id, self._live_status_text)
            self._log(self._live_status_text)

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(lambda: failed("Stop cancelled."))
        self._thread_pool.start(worker)

    def _poll_live_status(self) -> None:
        try:
            info = live_status()
            self._live_source_fps = float(info.source_fps or 0.0)
            self._live_target_fps = float(info.target_fps or 0.0)
            self._live_effective_fps = float(info.effective_fps or 0.0)
            self._live_guide_ms = float(info.guide_ms or 0.0)
            self._live_dlss_ms = float(info.dlss_ms or 0.0)
            self._live_encode_ms = float(info.encode_ms or 0.0)
            self._live_dropped_frames = int(info.player_dropped_frames or info.dropped_frames or 0)
            self._live_rebuffer_events = int(info.rebuffer_events or 0)
            self._live_av_sync_ms = float(info.av_sync_ms or 0.0)
            self._live_output_size = str(info.output_size or "")
            self._live_encoder = str(info.encoder or "")
            self.liveTelemetryChanged.emit()
            was_running = self._is_live_running
            self._is_live_running = bool(info.running or is_live_running())
            if self._is_live_running != was_running:
                self.isLiveRunningChanged.emit()
            # Reveal the in-tab player container only once the player reports
            # its first rendered frame; before that the unpainted native
            # child window would show the desktop through the app. Hide it
            # again the moment the session is gone (mpv is owned by it).
            # A zero handle means detached-window fallback: never reveal.
            self._live_player_started = bool(getattr(info, "player_started", False))
            try:
                if (self._is_live_running and self._live_player_started
                        and self._live_embed_wid
                        and not self._mpv_embed.isActive):
                    self._mpv_embed.set_session_active(True)
                elif not self._is_live_running and self._mpv_embed.isActive:
                    self._mpv_embed.set_session_active(False)
            except Exception:
                pass
            parts = [info.status]
            if info.playlist_url:
                parts.append(f"Playlist: {info.playlist_url}")
            if info.mpv_running:
                parts.append(
                    f"MPV: {info.player_dropped_frames} dropped | "
                    f"{info.rebuffer_events} rebuffer events | A/V sync {info.av_sync_ms:+.1f} ms"
                )
            if info.output_size:
                parts.append(
                    f"Received {info.source_size} -> Input {info.input_size} -> "
                    f"Output {info.output_size} | {info.encoder}"
                )
            if info.source_quality_note:
                parts.append(info.source_quality_note)
            parts.append(f"DLSS effects: {info.effects_status}")
            if info.source_fps:
                parts.append(
                    f"Source: {info.source_fps:.1f} fps | Guide: {info.guide_ms:.1f} ms | "
                    f"DLSS: {info.dlss_ms:.1f} ms | Encode: {info.encode_ms:.1f} ms"
                )
            self._live_status_text = "\n".join(parts)
            self.liveStatusTextChanged.emit()
            if not self._is_live_running:
                self._live_timer.stop()
                if self._operation_state == LIVE_RUNNING:
                    self._operation_state = IDLE
                    self.operationStateChanged.emit()
                    self.isProcessingChanged.emit()
        except Exception as exc:
            self._log(f"Live telemetry warning: {exc}")

    @Slot()
    def shutdown(self) -> None:
        """Flush persistent state and stop owned work/processes during application exit."""
        if self._shutting_down:
            return
        self._shutting_down = True
        app_log.info("shutdown", "begin")
        self._operation_state = SHUTTING_DOWN
        self.operationStateChanged.emit()
        self.isProcessingChanged.emit()
        self._save_timer.stop()
        self._sweep_timer.stop()
        self._auto_preview_timer.stop()
        self._auto_preview_pending = False
        self._scrub_preview_timer.stop()
        self._scrub_pending_pos = None
        try:
            save_settings(CONFIG_PATH, self._settings)
        except Exception as exc:
            self._log(f"Failed to save settings during shutdown: {exc}")
        for worker in (self._active_worker, self._scan_worker, self._metadata_worker, self._live_worker):
            if worker is not None:
                try:
                    worker.controller.stop()
                except Exception:
                    pass
        self._live_timer.stop()
        # Jobs own Qt signal objects and may still be inside native GPU code.
        # A timed wait can return with a live QRunnable; destroying QML/Qt at
        # that point leaves those objects and their callbacks without an owner.
        if not self._thread_pool.waitForDone(5000):
            app_log.info("shutdown", "workers still active after 5s; waiting for completion")
            self._thread_pool.waitForDone()
        # The start worker may have published a LiveSession while shutdown was
        # waiting for workers. Stop after synchronization so no session escapes.
        if is_live_running():
            try:
                stop_live_session(timeout=None)
            except Exception as exc:
                self._log(f"Live shutdown warning: {exc}")
        try:
            self._mpv_embed.shutdown()
        except Exception:
            pass
        self._main_window = None
        try:
            app_log.remove_listener(self._backend_log_listener)
        except Exception:
            pass
        try:
            if self._preview_cache != OUTPUTS:
                shutil.rmtree(self._preview_cache, ignore_errors=True)
        except OSError:
            pass
        app_log.info("shutdown", "complete")

    # =========================================================================
    # Helpers
    # =========================================================================
    def _clean_path(self, path_or_url: str) -> str:
        if not path_or_url:
            return ""
        p = path_or_url.strip()
        if p.startswith("file:///"):
            p = QUrl(p).toLocalFile()
        elif p.startswith("file:"):
            p = QUrl(p).toLocalFile()
        return str(Path(p).resolve()) if p else ""

    @Slot(str)
    def openFolder(self, path_str: str) -> None:
        clean = self._clean_path(path_str) if path_str else str(OUTPUTS.resolve())
        p = Path(clean)
        folder = p if p.is_dir() else p.parent
        if folder.exists():
            os.startfile(folder)

    @Slot(str)
    def copyToClipboard(self, text: str) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
            self._status_message = "Copied to clipboard."
            self.statusMessageChanged.emit()

    @staticmethod
    def _coerce_clipboard_qimage(value: Any) -> QImage | None:
        """Normalize clipboard pixel payloads to a QImage (or None).

        Browsers/toolkits may expose the bitmap as QImage or QPixmap
        depending on platform; screenshots arrive as QImage. Never raises.
        """
        try:
            if value is None:
                return None
            if isinstance(value, QImage):
                return value
            if isinstance(value, QPixmap):
                try:
                    return value.toImage()
                except Exception:
                    return None
            to_image = getattr(value, "toImage", None)
            if callable(to_image):
                try:
                    converted = to_image()
                except Exception:
                    return None
                return converted if isinstance(converted, QImage) else None
        except Exception:
            return None
        return None

    def _clipboard_pixels(self, mime: Any, clipboard: Any) -> QImage | None:
        """Return clipboard bitmap pixels, preferring mime imageData. Never raises.

        Falls back from Qt-coerced image objects to raw encoded bytes
        (``image/png`` / ``image/bmp`` / ``image/jpeg``), which Chromium
        exposes even when ``imageData()`` comes back null for its DIB.
        """
        try:
            if mime is not None and mime.hasImage():
                image = self._coerce_clipboard_qimage(mime.imageData())
                if image is not None and not image.isNull():
                    return image
        except Exception:
            pass
        try:
            if mime is not None:
                for fmt in ("image/png", "image/bmp", "image/jpeg", "image/jpg"):
                    try:
                        raw = bytes(mime.data(fmt) or b"")
                    except Exception:
                        continue
                    if not raw:
                        continue
                    try:
                        decoded = QImage.fromData(raw)
                    except Exception:
                        continue
                    if decoded is not None and not decoded.isNull():
                        return decoded
        except Exception:
            pass
        try:
            if clipboard is not None:
                image = self._coerce_clipboard_qimage(clipboard.image())
                if image is not None and not image.isNull():
                    return image
        except Exception:
            pass
        return None

    @staticmethod
    def _paste_remote_candidates(
        url_strings: list[str], html: str, text: str
    ) -> list[str]:
        """Collect unique http(s) image URL candidates, most-likely first."""
        seen: set[str] = set()
        ordered: list[str] = []

        def push(url: str) -> None:
            url = (url or "").strip().rstrip(".,;)]")
            if not url or url in seen:
                return
            try:
                scheme = urllib.parse.urlparse(url).scheme.lower()
            except Exception:
                return
            if scheme not in {"http", "https"}:
                return
            seen.add(url)
            ordered.append(url)

        for url in url_strings or []:
            push(url)
        try:
            for match in _PASTE_DATA_SRC_RE.findall(html or ""):
                push(match)
        except Exception:
            pass
        try:
            for match in _PASTE_IMG_SRC_RE.findall(html or ""):
                push(match)
        except Exception:
            pass
        try:
            for srcset in _PASTE_SRCSET_RE.findall(html or ""):
                try:
                    entries = [
                        part.strip().split()[0]
                        for part in str(srcset).split(",")
                        if part.strip().split()
                    ]
                except Exception:
                    continue
                for entry in reversed(entries):
                    if str(entry).lower().startswith(("http://", "https://")):
                        push(entry)
                        break
        except Exception:
            pass
        try:
            for match in _PASTE_URL_RE.findall(html or ""):
                push(match)
        except Exception:
            pass
        try:
            for match in _PASTE_URL_RE.findall(text or ""):
                push(match)
        except Exception:
            pass
        return ordered[:8]

    @staticmethod
    def _paste_extension_for(url: str, content_type: str) -> str:
        ctype = (content_type or "").split(";")[0].strip().lower()
        if ctype in _PASTE_CONTENT_TYPE_SUFFIX:
            return _PASTE_CONTENT_TYPE_SUFFIX[ctype]
        try:
            suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
        except Exception:
            suffix = ""
        if suffix in IMAGE_SUFFIXES:
            return suffix
        return ".jpg"

    def _fetch_first_remote_paste_image(self, urls: list[str]) -> str:
        """Download the first URL that returns image bytes (worker thread).

        Returns the staged local file path. Raises OSError if every URL
        fails. Must not touch Qt state; called via JobWorker.
        """
        last_error = "no URLs"
        for url in urls or []:
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": _PASTE_BROWSER_UA,
                        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                        "Referer": "https://www.pinterest.com/",
                    },
                )
                with urllib.request.urlopen(
                    request, timeout=_PASTE_DOWNLOAD_TIMEOUT_S
                ) as response:
                    try:
                        content_type = (
                            response.headers.get_content_type()
                            if hasattr(response.headers, "get_content_type")
                            else str(
                                response.headers.get("Content-Type", "")
                            ).split(";")[0].strip()
                        )
                    except Exception:
                        content_type = ""
                    if not str(content_type or "").lower().startswith("image/"):
                        raise OSError(
                            f"URL did not return an image "
                            f"({content_type or 'unknown type'})."
                        )
                    try:
                        declared = int(
                            str(response.headers.get("Content-Length", "") or "").strip()
                        )
                    except (TypeError, ValueError):
                        declared = 0
                    if declared > _PASTE_DOWNLOAD_MAX_BYTES:
                        raise OSError("Web image is too large (>30 MB).")
                    chunks: list[bytes] = []
                    total = 0
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _PASTE_DOWNLOAD_MAX_BYTES:
                            raise OSError("Web image is too large (>30 MB).")
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    if not data:
                        raise OSError("Downloaded file is empty.")
                    # Fail fast on undecodable payloads (error pages, truncated
                    # bodies) instead of queueing a corpse that errors later
                    # at metadata stage. SVG bypasses the probe: Qt decodes it
                    # through a separate renderer stack.
                    if "svg" not in str(content_type or "").lower():
                        try:
                            probe = QImage.fromData(data)
                        except Exception:
                            probe = None
                        if probe is None or probe.isNull():
                            raise OSError("Downloaded data is not a readable image.")
                    extension = self._paste_extension_for(url, str(content_type))
                    APP_TEMP.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha1(data).hexdigest()[:16]
                    target = APP_TEMP / f"paste-dl-{digest}{extension}"
                    if target.is_file():
                        try:
                            os.utime(target, None)
                        except OSError:
                            pass
                    else:
                        target.write_bytes(data)
                    return str(target)
            except Exception as exc:
                last_error = str(exc) or type(exc).__name__
                continue
        raise OSError(f"Web image download failed: {last_error}")

    def _stage_pasted_qimage(self, image: QImage) -> str:
        """Stage clipboard pixels as PNG under APP_TEMP. Returns file URL.

        The filename is a content hash, so re-pasting identical pixels
        resolves to the same path: the queue's duplicate check then selects
        the existing entry instead of adding another copy.
        """
        APP_TEMP.mkdir(parents=True, exist_ok=True)
        try:
            buffer = QBuffer()
            buffer.open(QIODevice.WriteOnly)
            ok = bool(image.save(buffer, "PNG"))
            data = bytes(buffer.data()) if ok else b""
            buffer.close()
        except Exception:
            data = b""
        if not data:
            raise OSError("Could not stage the pasted image.")
        digest = hashlib.sha1(data).hexdigest()[:16]
        target = APP_TEMP / f"paste-{digest}.png"
        if not target.is_file():
            target.write_bytes(data)
        else:
            try:
                os.utime(target, None)
            except OSError:
                pass
        return QUrl.fromLocalFile(str(target)).toString()

    def _start_paste_download(self, candidates: list[str]) -> None:
        """Fetch a web image off the GUI thread, then feed it to addFiles."""
        operation_id = self._set_operation(SCANNING_INPUTS, "Downloading web image…")
        worker = JobWorker(
            self._fetch_first_remote_paste_image,
            list(candidates),
            inject_callbacks=False,
        )
        self._scan_worker = worker

        def done(local_path: str) -> None:
            if operation_id != self._operation_id or self._shutting_down:
                return
            self._scan_worker = None
            # Return to Idle first: addFiles refuses queue changes while an
            # operation is active, and our download op is still that blocker.
            if not self._finish_operation(operation_id, "Download complete. Scanning…"):
                return
            try:
                self._paste_select_single = True
                self.addFiles([QUrl.fromLocalFile(str(local_path)).toString()])
            except Exception as exc:
                self._status_message = f"Paste failed: {exc}"
                self.statusMessageChanged.emit()
                self._log(f"Paste failed: {exc}")

        def failed(message: str) -> None:
            self._scan_worker = None
            self._finish_operation(operation_id, f"Web image download failed: {message}")
            self._log(f"Web image download failed: {message}")

        worker.signals.finished.connect(done)
        worker.signals.failed.connect(failed)
        worker.signals.cancelled.connect(
            lambda: self._finish_operation(operation_id, "Download cancelled.")
        )
        self._thread_pool.start(worker)

    @Slot()
    def pasteFromClipboard(self) -> None:
        """Paste images/video into the active batch tab from the clipboard.

        Priority (fixes browser "Copy image" shadowing):
        1. Bitmap pixels (screenshots, Pinterest/Chrome bitmap) — offline.
        2. Local file URLs/paths (Explorer copy).
        3. Remote http(s) URLs, HTML <img src>, URL text — async download.
        Reuses addFiles so scanning, type checks and mode switching behave
        identically. QML gates this to NR/Upscale/FI tabs; the tab guard
        below is defense in depth.
        """
        if self._active_tab not in {"neural-rendering", "upscale", "frame-interpolation"}:
            return
        if not self.canModifyQueue:
            self._status_message = "Queue changes are disabled while an operation is active."
            self.statusMessageChanged.emit()
            return
        try:
            clipboard = QGuiApplication.clipboard()
            mime = clipboard.mimeData() if clipboard is not None else None
        except Exception as exc:
            self._log(f"Could not read clipboard: {exc}")
            return
        if mime is None:
            return
        # 1. Pixels first: exact frame the user saw, no network involved.
        try:
            pixels = self._clipboard_pixels(mime, clipboard)
            if pixels is not None:
                staged_url = self._stage_pasted_qimage(pixels)
                self._paste_select_single = True
                self.addFiles([staged_url])
                return
        except Exception as exc:
            self._paste_select_single = False
            self._log(f"Clipboard bitmap unavailable, trying file/URL paths: {exc}")
        # 2. Split clipboard URLs into local files vs remote web links.
        # Browser copies expose https URLs here; passing those to addFiles
        # is what used to produce "No supported images were found."
        local_urls: list[str] = []
        remote_urls: list[str] = []
        try:
            if mime.hasUrls():
                for item in mime.urls():
                    try:
                        if not item.isValid():
                            continue
                        text = item.toString()
                        if not text:
                            continue
                        scheme = item.scheme().lower() if hasattr(item, "scheme") else ""
                        if not scheme:
                            try:
                                scheme = urllib.parse.urlparse(text).scheme.lower()
                            except Exception:
                                scheme = ""
                        if scheme in {"http", "https"}:
                            remote_urls.append(text)
                        elif item.isLocalFile() if hasattr(item, "isLocalFile") else text.startswith("file:"):
                            local_urls.append(text)
                        else:
                            local_urls.append(text)
                    except Exception:
                        continue
        except Exception:
            pass
        if local_urls:
            try:
                self._paste_select_single = True
                self.addFiles(local_urls)
            except Exception as exc:
                self._status_message = f"Paste failed: {exc}"
                self.statusMessageChanged.emit()
                self._log(f"Paste failed: {exc}")
            return
        # Copied file-path text (e.g. "C:\\pics\\a.jpg" lines) still counts.
        try:
            if mime.hasText():
                paths: list[str] = []
                for line in str(mime.text() or "").splitlines():
                    candidate = line.strip().strip("\"'")
                    if not candidate or candidate.lower().startswith(("http://", "https://")):
                        continue
                    try:
                        if Path(self._clean_path(candidate)).is_file():
                            paths.append(candidate)
                    except Exception:
                        continue
                if paths:
                    self._paste_select_single = True
                    self.addFiles(paths)
                    return
        except Exception:
            pass
        # 3. Remote web image (Pinterest "Copy image" / "Copy image address").
        try:
            html = str(mime.html()) if mime.hasHtml() else ""
        except Exception:
            html = ""
        try:
            text = str(mime.text()) if mime.hasText() else ""
        except Exception:
            text = ""
        candidates = self._paste_remote_candidates(remote_urls, html, text)
        if candidates:
            self._start_paste_download(candidates)
            return
        try:
            formats = list(mime.formats() or [])[:12]
        except Exception:
            formats = []
        self._status_message = "Clipboard has no images or files to paste."
        self.statusMessageChanged.emit()
        self._log(f"Paste found no usable data (formats: {formats}).")

    @Slot(str, str, str)
    def _append_backend_log(self, level: str, tag: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = f"[{stamp}] {level} {tag}: {message}"
        self._logs.append(entry)
        if len(self._logs) > 1000:
            del self._logs[:200]
        self.logAppended.emit(entry)

    @Property(str, notify=logAppended)
    def currentLogPath(self) -> str:
        try:
            return app_log.session_path()
        except Exception:
            return str(LOGS)

    @Slot()
    def clearLogDisplay(self) -> None:
        self._logs.clear()
        self.logAppended.emit("")

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = f"[{stamp}] {message}"
        self._logs.append(entry)
        if len(self._logs) > 200:
            self._logs.pop(0)
        self.logAppended.emit(entry)

    @Property(str, notify=logAppended)
    def fullLog(self) -> str:
        return "\n".join(self._logs)
