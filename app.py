from __future__ import annotations

from pathlib import Path

# Show LOADING immediately before heavy imports (gradio etc.) to avoid black screen after start.bat
import time as _early_time

try:
    from src.core.terminal import TerminalUI as _EarlyTerminalUI

    _early_ui = _EarlyTerminalUI(Path(__file__).with_name("logs"))
    _early_ui.enable_vt_mode()
    _early_time.sleep(0.05)
    _early_ui.render_loading()
    _EARLY_TS = _early_time.time()
except Exception:
    _early_ui = None
    _EARLY_TS = 0.0

import gradio as gr

from src.about.ui import build_about_tab
from src.compare.grid_ui import bind_grid_events, build_grid_tab
from src.compare.ui import bind_comparison_events, build_compare_tab
from src.core.cache_cleanup import (
    CACHE_MAX_AGE_SECONDS,
    CACHE_SWEEP_INTERVAL_SECONDS,
    cleanup_old_caches,
)
from src.core.paths import LIVE_DIR, LOGS, OUTPUTS
from src.core.runtime import prepare_runtime
from src.core.terminal import init_console
from src.frame_interpolation.ui import build_frame_interpolation_tab
from src.live.ui import build_live_tab
from src.neural_rendering.image.decoder import initialize_image_runtime
from src.neural_rendering.ui import build_neural_rendering_tab
from src.settings.ui import bind_settings_events, build_settings_tab, initialize_settings
from src.upscale.ui import build_upscale_tab

APP_CSS = r"""
/* Center the About contents in the space below the app title and tab bar. */
#about-content {
    display: grid;
    place-items: center;
    min-height: calc(100dvh - 12rem);
    padding: 2rem 1rem;
    box-sizing: border-box;
    text-align: center;
}

#about-content .about-details {
    max-width: 36rem;
}

#about-content h2 {
    margin: 0;
    font-size: 1.5rem;
    font-weight: 600;
    line-height: 1.35;
}

#about-content .about-version {
    margin: 0.6rem 0 0;
}

#about-content .about-links {
    margin-top: 2rem;
}

#about-content .about-links p {
    margin: 0 0 1.25rem;
}

#about-content .about-description {
    display: block;
    margin-top: 0.4rem;
    color: var(--body-text-color-subdued);
    font-size: 0.875rem;
}

#about-content .about-copyright {
    margin: 2rem 0 0;
}

/* Keep project links the same color after visiting them. */
#about-content a,
#about-content a:visited,
#about-content a:hover,
#about-content a:active {
    color: #00bfff !important;
    opacity: 1 !important;
    font-weight: 600;
}

#about-content a:hover {
    text-decoration: underline;
}

/* Match every image/video drop zone to the 16:9 input preview surface. */
.media-upload-surface {
    aspect-ratio: 16 / 9;
    height: auto !important;
    min-height: 0 !important;
}

/* Keep uploaded batches inside that surface and scroll when needed. */
#image-upload-list .file-preview-holder,
#video-upload-list .file-preview-holder,
#upscale-image-upload-list .file-preview-holder,
#upscale-upload-list .file-preview-holder,
#frame-interpolation-upload-list .file-preview-holder {
    max-height: 100% !important;
    overflow-x: hidden !important;
    overflow-y: auto !important;
    overscroll-behavior: contain;
    scrollbar-gutter: stable;
}

#image-upload-list .file-preview,
#video-upload-list .file-preview,
#upscale-image-upload-list .file-preview,
#upscale-upload-list .file-preview,
#frame-interpolation-upload-list .file-preview {
    max-height: none !important;
}

/* A single input or output preview is a full 16:9 viewport without scrolling. */
#image-input-preview:has(.gallery-item:only-child),
#image-output-preview:has(.gallery-item:only-child),
#upscale-image-input-preview:has(.gallery-item:only-child),
#upscale-image-output-preview:has(.gallery-item:only-child) {
    aspect-ratio: 16 / 9;
    height: auto !important;
    min-height: 0 !important;
}

#image-input-preview:has(.gallery-item:only-child) .gallery-container,
#image-input-preview:has(.gallery-item:only-child) .grid-wrap,
#image-input-preview:has(.gallery-item:only-child) .grid-container,
#image-input-preview:has(.gallery-item:only-child) .gallery-item,
#image-input-preview:has(.gallery-item:only-child) .thumbnail-lg,
#image-output-preview:has(.gallery-item:only-child) .gallery-container,
#image-output-preview:has(.gallery-item:only-child) .grid-wrap,
#image-output-preview:has(.gallery-item:only-child) .grid-container,
#image-output-preview:has(.gallery-item:only-child) .gallery-item,
#image-output-preview:has(.gallery-item:only-child) .thumbnail-lg,
#upscale-image-input-preview:has(.gallery-item:only-child) .gallery-container,
#upscale-image-input-preview:has(.gallery-item:only-child) .grid-wrap,
#upscale-image-input-preview:has(.gallery-item:only-child) .grid-container,
#upscale-image-input-preview:has(.gallery-item:only-child) .gallery-item,
#upscale-image-input-preview:has(.gallery-item:only-child) .thumbnail-lg,
#upscale-image-output-preview:has(.gallery-item:only-child) .gallery-container,
#upscale-image-output-preview:has(.gallery-item:only-child) .grid-wrap,
#upscale-image-output-preview:has(.gallery-item:only-child) .grid-container,
#upscale-image-output-preview:has(.gallery-item:only-child) .gallery-item,
#upscale-image-output-preview:has(.gallery-item:only-child) .thumbnail-lg {
    box-sizing: border-box;
    height: 100% !important;
    min-height: 0 !important;
}

#image-input-preview:has(.gallery-item:only-child) .grid-wrap,
#image-output-preview:has(.gallery-item:only-child) .grid-wrap,
#upscale-image-input-preview:has(.gallery-item:only-child) .grid-wrap,
#upscale-image-output-preview:has(.gallery-item:only-child) .grid-wrap {
    overflow: hidden !important;
}

#image-input-preview:has(.gallery-item:only-child) .grid-container,
#image-output-preview:has(.gallery-item:only-child) .grid-container,
#upscale-image-input-preview:has(.gallery-item:only-child) .grid-container,
#upscale-image-output-preview:has(.gallery-item:only-child) .grid-container {
    grid-template-rows: minmax(0, 1fr) !important;
    grid-auto-rows: minmax(0, 1fr) !important;
}

#image-input-preview:has(.gallery-item:only-child) img,
#image-output-preview:has(.gallery-item:only-child) img,
#upscale-image-input-preview:has(.gallery-item:only-child) img,
#upscale-image-output-preview:has(.gallery-item:only-child) img {
    height: 100% !important;
    width: 100% !important;
    object-fit: contain !important;
}

/* The replacement and clear actions share the media preview width. */
.media-input-actions {
    width: 100% !important;
    max-width: none !important;
}

.media-input-actions > .media-select-button,
.media-input-actions > .media-clear-button {
    flex: 1 1 0 !important;
    min-width: 0 !important;
}

.media-select-button button,
.media-clear-button {
    width: 100% !important;
}

"""


def build_app() -> gr.Blocks:
    """Build the UI from cached settings and feature-owned tab modules."""
    prepared = prepare_runtime()
    initialize_image_runtime()
    settings, _gpu_warning, ai_gpu_choices, video_gpu_choices = initialize_settings(prepared)
    with gr.Blocks(
        title="DLSS 5 Visual Enhancer",
        # Official Gradio cache cleanup (see guides/resource-cleanup): every
        # CACHE_SWEEP_INTERVAL_SECONDS, delete tracked temp files older than
        # CACHE_MAX_AGE_SECONDS; full wipe on graceful shutdown. Crash orphans
        # are covered by cleanup_old_caches() at startup in main().
        delete_cache=(CACHE_SWEEP_INTERVAL_SECONDS, CACHE_MAX_AGE_SECONDS),
    ) as demo:
        gr.Markdown(
            "# DLSS 5 Visual Enhancer",
            elem_id="app-title",
        )
        with gr.Tabs(selected="neural-rendering") as tabs:
            with gr.Tab("Neural Rendering", id="neural-rendering"):
                neural_rendering_tab = build_neural_rendering_tab(settings)
            with gr.Tab("Upscale", id="upscale"):
                upscale_tab = build_upscale_tab(settings)
            with gr.Tab("Frame Interpolation", id="frame-interpolation"):
                frame_tab = build_frame_interpolation_tab(settings)
            with gr.Tab("Live", id="live"):
                live_tab = build_live_tab(settings)
            with gr.Tab("Comparison", id="compare"):
                compare_tab = build_compare_tab()
            with gr.Tab("Grid", id="grid"):
                grid_tab = build_grid_tab(settings)
            with gr.Tab("Settings", id="settings"):
                settings_tab = build_settings_tab(settings, ai_gpu_choices, video_gpu_choices)
            with gr.Tab("About", id="about"):
                build_about_tab()

        bind_settings_events(
            settings_tab,
            neural_rendering_tab.image,
            neural_rendering_tab.video,
            frame_tab,
            live_tab,
            upscale_tab,
        )
        # Video, Frame Interpolation, Live, and Upscale aren't wired into Comparison yet
        # (each needs a synced player or a different flow, not the image before/after
        # slider) — that's a later phase.
        bind_comparison_events(compare_tab, tabs, image_tab=neural_rendering_tab.image, grid_tab=grid_tab)
        bind_grid_events(grid_tab, neural_rendering_tab.image)
    return demo

def main() -> None:
    OUTPUTS.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    LIVE_DIR.mkdir(exist_ok=True)
    # Drop leftover Live session dirs from dead runs (previous process is gone).
    try:
        from src.live.pipeline import sweep_stale_live_dirs

        sweep_stale_live_dirs()
    except Exception:
        pass
    # Remove stale Gradio caches / temp leftovers from previous (possibly
    # crashed) runs before serving. Best effort: never blocks startup.
    try:
        cleanup_old_caches()
    except Exception:
        pass
    # Reuse early loading UI if it was already rendered at import time (avoids second flash and keeps alt buffer)
    global _early_ui  # type: ignore
    if "_EARLY_UI" in globals() and _early_ui is not None and getattr(_early_ui, "_alt_active", False):
        ui = _early_ui  # type: ignore
        # Complete init that early block did not do (listener + redirect)
        try:
            ui.start_input_listener()
        except Exception:
            pass
        try:
            ui.silence_and_redirect()
        except Exception:
            pass
        try:
            import atexit

            atexit.register(ui.restore_cursor)
        except Exception:
            pass
    else:
        ui = init_console(LOGS)
    try:
        prepare_runtime()
    except Exception as exc:
        with open(LOGS / "startup_error.log", "a", encoding="utf-8") as f:
            f.write(f"Startup preparation failed: {exc}\n")
        raise SystemExit(1) from exc

    demo = build_app()
    # Replace loading screen with final DLSS 5 Visual Enhancer splash
    try:
        ui.render_screen()
    except Exception:
        pass
    try:
        demo.queue(default_concurrency_limit=1).launch(
            css=APP_CSS,
            theme=gr.themes.Ocean(),
            server_name="127.0.0.1",
            inbrowser=True,
            share=False,
            allowed_paths=[str(OUTPUTS.resolve())],
            show_error=True,
            quiet=True,
        )
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
