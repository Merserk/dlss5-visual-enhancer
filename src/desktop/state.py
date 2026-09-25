from __future__ import annotations

from dataclasses import dataclass, field


IDLE = "Idle"
SCANNING_INPUTS = "ScanningInputs"
LOADING_METADATA = "LoadingMetadata"
PREVIEW_PREPARING = "PreviewPreparing"
PREVIEW_RUNNING = "PreviewRunning"
PREVIEW_CANCELLING = "PreviewCancelling"
BATCH_PREPARING = "BatchPreparing"
BATCH_RUNNING = "BatchRunning"
BATCH_CANCELLING = "BatchCancelling"
LIVE_STARTING = "LiveStarting"
LIVE_RUNNING = "LiveRunning"
LIVE_STOPPING = "LiveStopping"
SHUTTING_DOWN = "ShuttingDown"

BUSY_STATES = {
    SCANNING_INPUTS,
    LOADING_METADATA,
    PREVIEW_PREPARING,
    PREVIEW_RUNNING,
    PREVIEW_CANCELLING,
    BATCH_PREPARING,
    BATCH_RUNNING,
    BATCH_CANCELLING,
    LIVE_STARTING,
    LIVE_STOPPING,
    SHUTTING_DOWN,
}


@dataclass(slots=True)
class PreviewState:
    input_url: str = ""
    output_url: str = ""
    poster_url: str = ""
    input_is_video: bool = False
    output_is_video: bool = False
    input_info: str = ""
    output_info: str = ""
    source_aspect_ratio: float | None = None
    source_width: int = 0
    source_height: int = 0
    source_fps: float = 0.0
    source_hdr: bool = False
    duration_seconds: float = 0.0
    output_width: int = 0
    output_height: int = 0
    # Numeric source of the video preview: timeline seconds + nominal frame
    # the enhanced output was built from. Lets Input re-show the same frame.
    preview_source_seconds: float = 0.0
    preview_source_frame: int = 0
    # Last user-parked timeline position (seconds) in this context, pushed
    # from QML transport gestures and pauses. Realtime auto-renders use it
    # so a settings tweak previews the parked frame instead of frame 1.
    last_playhead_seconds: float = 0.0
    selected_path: str = ""
    selected_row: int = -1
    generation: int = 0
    # Skip-if-fresh marker for realtime auto-preview: (selected source,
    # settings snapshot) captured when the current output preview finished
    # rendering. Empty = stale (first visit, cleared queue, cancelled
    # render). Tab/mode switches skip the GPU re-render while this matches.
    last_auto_fingerprint: tuple = ()
    # Video preview clips rendered for FI or Upscale, newest last. Each
    # entry is {"start": seconds, "end": seconds, "url": clip file URL}.
    # Overlapping ranges are kept as separate entries (they merge visually
    # by overdraw); playback picks the newest range containing the playhead
    # so re-rendered spans always resolve to the latest clip.
    rendered_ranges: list = field(default_factory=list)

    @property
    def has_output(self) -> bool:
        return bool(self.output_url)
