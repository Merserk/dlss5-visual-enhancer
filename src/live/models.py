from __future__ import annotations

from dataclasses import dataclass, field

LIVE_MAX_HEIGHTS = (480, 720, 1080, 1440, 2160)
LIVE_MAX_HEIGHT_CHOICES = tuple(str(height) for height in LIVE_MAX_HEIGHTS)
LIVE_SOURCE_QUALITY_CHOICES = ("Auto", *LIVE_MAX_HEIGHT_CHOICES)
LIVE_SEGMENT_CHOICES = ("1", "2", "4")
LIVE_FPS_CHOICES = ("Auto", "Source", "60", "30", "24")

@dataclass(slots=True)
class LiveOptions:
    """Session-scoped Live settings (never persisted to config.ini)."""

    source: str = ""
    max_height: int = 720
    # Shared DLSS values (mirrored with the Neural Rendering modes, persisted
    # globally); effects can update during playback, sizing stays fixed.
    nr_style: str = "Style 0"
    nr_intensity: float = 1.0
    nr_passes: int = 1
    nr_optical_flow_quality: str = "High"
    local_tone_strength: float = 1.0
    local_structure_strength: float = 1.0
    skin_structure_strength: float = 1.0
    nr_mask: object | None = None
    automatic_mask: bool = False
    # Source default: Live renders at the input size; 75%/50%/25% downscale
    # first (Lanczos) and keep the pipe/NVENC cheap on mid-range GPUs.
    upscaling_factor: float = 1.0
    segment_seconds: int = 2
    target_fps: str = "Auto"
    buffer_seconds: float = 6.0
    queue_frames: int = 3
    network_timeout: float = 20.0
    open_mpv: bool = True
    mpv_args: tuple[str, ...] = ()
    # In-tab embedding (Windows): parent-window handle for ``--wid`` plus the
    # JSON IPC pipe for transport. ``mpv_wid == 0`` falls back to a detached
    # player window. Set by the bridge on the GUI thread before launch.
    mpv_wid: int = 0
    mpv_ipc_server: str = ""
    # Debug/diagnostics: keep the session HLS dir instead of sweeping it.
    keep_files: bool = False
    source_quality: str = "Auto"

@dataclass(slots=True)
class ResolvedSource:
    kind: str  # "file" | "direct" | "youtube" | "twitch"
    title: str
    video_url: str
    audio_url: str | None
    is_live: bool
    video_headers: dict[str, str] = field(default_factory=dict)
    audio_headers: dict[str, str] = field(default_factory=dict)

@dataclass(slots=True)
class LiveSessionInfo:
    running: bool = False
    processing: bool = False
    status: str = "Idle."
    playlist_url: str = ""
    segments: int = 0
    processed_frames: int = 0
    dropped_frames: int = 0
    source_frames: int = 0
    sampled_frames: int = 0
    source_fps: float = 0.0
    input_size: str = ""
    output_size: str = ""
    encoder: str = ""
    target_fps: float = 0.0
    media_seconds: float = 0.0
    buffer_seconds: float = 0.0
    guide_ms: float = 0.0
    dlss_ms: float = 0.0
    encode_ms: float = 0.0
    report_path: str = ""
    effective_fps: float = 0.0
    elapsed_seconds: float = 0.0
    mpv_running: bool = False
    # True once the embedded player reported actual playback (first frame).
    # The in-tab container stays hidden until then so no unpainted native
    # window is ever exposed. Sourced from the lua state file's `started`.
    player_started: bool = False
    player_dropped_frames: int = 0
    rebuffer_events: int = 0
    av_sync_ms: float = 0.0
    failures: list[str] = field(default_factory=list)
    source_size: str = ""
    source_quality: str = "Auto"
    source_quality_note: str = ""
    effects_status: str = "Initial settings"
    effects_error: str = ""
    pending_revision: int | None = None
    applied_revision: int = 0
    applied_at_pts: int | None = None
    effect_updates: int = 0
    effect_update_failures: int = 0
