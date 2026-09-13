from __future__ import annotations

from pathlib import Path
import os

# Resolve the portable Gradio cache without importing the ``src.core`` package:
# importing that package executes its __init__ and may load heavy/media modules.
# Gradio reads GRADIO_TEMP_DIR during import in some modules, so this must be
# the first application setup performed.
_APP_ROOT = Path(__file__).resolve().parent
_APP_GRADIO_TEMP = _APP_ROOT / "temp" / "gradio"
_APP_GRADIO_TEMP.mkdir(parents=True, exist_ok=True)
os.environ["GRADIO_TEMP_DIR"] = str(_APP_GRADIO_TEMP)

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
from src.core.i18n import translator
from src.core.batch_ui import bind_input_surface_reactivation
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
from src.neural_rendering.composition_ui import bind_composition_mask_events
from src.settings.ui import bind_settings_events, build_settings_tab, initialize_settings
from src.upscale.ui import build_upscale_tab

APP_CSS = r"""
/* Internal lazy-ZIP download targets must always remain in the DOM for the
   one-click browser download trigger, but must never appear as UI controls. */
.internal-zip-download {
    display: none !important;
}

/* Stateful mode trees stay mounted. The radio's client guard stores the latest
   requested mode on <html>; these selectors remain authoritative even if an
   older backend visibility response arrives later. The correct panel can be
   briefly blank while loading, but the wrong panel can never be exposed. */
html[data-neural-rendering-mode="Video"] #neural-rendering-image,
html[data-neural-rendering-mode="Image"] #neural-rendering-video,
html[data-upscale-mode="Video"] #upscale-image,
html[data-upscale-mode="Image"] #upscale-video {
    display: none !important;
}

/* First paint has no dataset yet (the guards only run on mode.change), so the
   rules above match nothing and both panels would stack. Hide the Video panel
   by default; the load-time JS below immediately corrects this when the saved
   Upscale mode is Video, and the change-guards take over afterwards. */
html:not([data-neural-rendering-mode]) #neural-rendering-video,
html:not([data-upscale-mode]) #upscale-video {
    display: none !important;
}

/* Keep the app brand and native top-level tabs on one horizontal header line.
   Gradio measures a visually-hidden copy of the tab buttons for overflow, so
   shift that measurement surface by the same fixed brand width as the visible
   tab list to preserve the built-in overflow menu calculation. */
#main-tabs {
    --main-brand-width: 8.75rem;
}

#main-tabs > .tab-wrapper {
    justify-content: flex-start;
    min-width: 0;
}

#main-tabs > .tab-wrapper::before {
    content: "DLSS 5 VE";
    display: flex;
    flex: 0 0 var(--main-brand-width);
    align-self: stretch;
    align-items: center;
    box-sizing: border-box;
    padding-right: var(--size-3);
    color: var(--body-text-color);
    font-size: var(--text-lg);
    font-weight: 700;
    white-space: nowrap;
}

#main-tabs > .tab-wrapper > .tab-container[role="tablist"] {
    flex: 1 1 auto;
    width: auto !important;
    min-width: 0;
}

#main-tabs > .tab-wrapper > .tab-container.visually-hidden {
    left: var(--main-brand-width);
}

/* Image / Video is a compact second navigation line in workflows that support
   both modes. Keep the native Gradio radio buttons and their behavior, but
   remove the surrounding form/label and prevent the two choices from wrapping. */
#neural-rendering-mode,
#upscale-mode {
    flex: 0 0 auto !important;
    width: max-content !important;
    max-width: 100%;
    min-width: 0 !important;
}

#neural-rendering-mode .wrap,
#upscale-mode .wrap {
    flex-wrap: nowrap !important;
}

/* On Windows Chromium, hovering a compact radio choice can briefly make the
   max-content radio surface report horizontal overflow and expose a native
   scrollbar. These two-choice selectors never need scrolling, so keep the
   overflow paintable without creating a scroll container. */
#neural-rendering-mode,
#upscale-mode,
#neural-rendering-mode .wrap,
#upscale-mode .wrap {
    overflow: visible !important;
    scrollbar-width: none;
}

#neural-rendering-mode::-webkit-scrollbar,
#upscale-mode::-webkit-scrollbar,
#neural-rendering-mode .wrap::-webkit-scrollbar,
#upscale-mode .wrap::-webkit-scrollbar {
    display: none;
}

/* Center the About contents in the space below the compact one-row header. */
#about-content {
    display: grid;
    place-items: center;
    min-height: calc(100dvh - 8rem);
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

/* Neural Rendering is structurally split into several Gradio Forms by its
   two slider Rows. Visually merge only those Forms into one native-looking
   block without changing any child component sizing, spacing, or controls. */
.neural-controls-unified {
    background: var(--block-background-fill);
    border-radius: var(--block-radius);
    box-shadow: var(--block-shadow);
    overflow: hidden;
}

.neural-controls-unified::after {
    content: "";
    position: absolute;
    inset: 0;
    box-sizing: border-box;
    border: var(--block-border-width) solid var(--block-border-color);
    border-radius: inherit;
    pointer-events: none;
    z-index: 1;
}

.neural-controls-unified .form {
    background: transparent !important;
    border-color: transparent !important;
    border-radius: 0 !important;
    box-shadow: none !important;
}

"""


def build_app() -> gr.Blocks:
    """Build the UI from cached settings and feature-owned tab modules."""
    prepared = prepare_runtime()
    initialize_image_runtime()
    settings, _gpu_warning, ai_gpu_choices, video_gpu_choices = initialize_settings(prepared)
    ui_t = translator(settings.language).t
    with gr.Blocks(
        title=ui_t("app.title"),
        # Official Gradio cache cleanup (see guides/resource-cleanup): every
        # CACHE_SWEEP_INTERVAL_SECONDS, delete tracked temp files older than
        # CACHE_MAX_AGE_SECONDS; full wipe on graceful shutdown. Crash orphans
        # are covered by cleanup_old_caches() at startup in main().
        delete_cache=(CACHE_SWEEP_INTERVAL_SECONDS, CACHE_MAX_AGE_SECONDS),
    ) as demo:
        # One runtime source of truth for the global Neural Rendering processing
        # path.  The visible control lives in Settings, while render/start events
        # consume this State so each queued operation snapshots the selected mode.
        processing_engine_state = gr.State(value=settings.nr_gpu_mode)
        nr_mask_state = gr.State(value=None)
        with gr.Tabs(selected="neural-rendering", elem_id="main-tabs"):
            # Keep every tab tree mounted from first paint. Stateful File/Gallery/Video
            # components otherwise get lazily mounted when a tab is first selected,
            # which can briefly restore their construction-time visibility/value state.
            with gr.Tab(ui_t("app.tab.neural_rendering"), id="neural-rendering", render_children=True) as neural_root_tab:
                neural_rendering_tab = build_neural_rendering_tab(settings, processing_engine_state, nr_mask_state)
            with gr.Tab(ui_t("app.tab.upscale"), id="upscale", render_children=True) as upscale_root_tab:
                upscale_tab = build_upscale_tab(settings)
            with gr.Tab(ui_t("app.tab.frame_interpolation"), id="frame-interpolation", render_children=True) as frame_root_tab:
                frame_tab = build_frame_interpolation_tab(settings)
            with gr.Tab(ui_t("app.tab.live"), id="live", render_children=True) as live_root_tab:
                live_tab = build_live_tab(settings, processing_engine_state, nr_mask_state)
            with gr.Tab(ui_t("app.tab.settings"), id="settings", render_children=True) as settings_root_tab:
                settings_tab = build_settings_tab(
                    settings, ai_gpu_choices, video_gpu_choices, processing_engine_state
                )
            with gr.Tab(ui_t("app.tab.about"), id="about", render_children=True) as about_root_tab:
                build_about_tab(settings.language)

        # Mounted video elements can otherwise keep decoding/playing while their
        # workflow is hidden. Pause media whenever the user changes top-level tabs.
        pause_media_js = """() => {
            document.querySelectorAll('video').forEach((video) => {
                try { video.pause(); } catch (_) {}
            });
        }"""
        for root_tab in (
            neural_root_tab, upscale_root_tab, frame_root_tab,
            live_root_tab, settings_root_tab, about_root_tab,
        ):
            root_tab.select(
                None, js=pause_media_js, queue=False, show_progress="hidden",
                trigger_mode="always_last",
            )

        # Gradio can restore construction-time child visibility when a mounted
        # top-level tab is shown again. Reconcile only the input surface state;
        # rendered outputs, save controls, and thumbnails are left untouched.
        bind_input_surface_reactivation(neural_root_tab, neural_rendering_tab.image, kind="image", event_name="select")
        bind_input_surface_reactivation(neural_root_tab, neural_rendering_tab.video, kind="video", event_name="select")
        bind_input_surface_reactivation(upscale_root_tab, upscale_tab.image, kind="image", event_name="select")
        bind_input_surface_reactivation(upscale_root_tab, upscale_tab.video, kind="video", event_name="select")
        bind_input_surface_reactivation(frame_root_tab, frame_tab, kind="video", event_name="select")

        bind_settings_events(
            settings_tab,
            neural_rendering_tab.image,
            neural_rendering_tab.video,
            frame_tab,
            live_tab,
            upscale_tab,
        )
        bind_composition_mask_events(
            nr_mask_state,
            neural_rendering_tab.image.composition,
            neural_rendering_tab.video.composition,
            live_tab.composition,
        )

        # First paint has no dataset for the CSS guards above (they only run on
        # mode.change), so both Image and Video panels would stack until the
        # first toggle. Seed both attributes on load from the construction-time
        # radio values; Neural Rendering always starts on Image, Upscale honors
        # the persisted setting. Existing change-guards remain authoritative
        # afterwards. Trees stay mounted; this only affects CSS visibility.
        initial_upscale_mode = settings.upscale_mode if settings.upscale_mode in ("Image", "Video") else "Image"
        init_mode_js = (
            "() => {"
            " try {"
            "  var html = document.documentElement;"
            "  if (!html.hasAttribute('data-neural-rendering-mode')) {"
            "   html.setAttribute('data-neural-rendering-mode', 'Image');"
            "  }"
            f"  if (!html.hasAttribute('data-upscale-mode')) {{"
            f"   html.setAttribute('data-upscale-mode', '{initial_upscale_mode}');"
            "  }"
            "  var hiddenIds = [];"
            "  if (html.getAttribute('data-neural-rendering-mode') === 'Video') {"
            "   hiddenIds.push('neural-rendering-image');"
            "  } else {"
            "   hiddenIds.push('neural-rendering-video');"
            "  }"
            "  if (html.getAttribute('data-upscale-mode') === 'Video') {"
            "   hiddenIds.push('upscale-image');"
            "  } else {"
            "   hiddenIds.push('upscale-video');"
            "  }"
            "  hiddenIds.forEach(function (id) {"
            "   var panel = document.getElementById(id);"
            "   if (panel) { panel.querySelectorAll('video').forEach(function (node) {"
            "    try { node.pause(); } catch (_) {}"
            "   }); }"
            "  });"
            " } catch (_) {}"
            "}"
        )
        demo.load(
            None, js=init_mode_js, queue=False, show_progress="hidden",
            trigger_mode="always_last",
        )
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
        prepared = prepare_runtime()
    except Exception as exc:
        try:
            from src.core import app_log

            app_log.error("startup", f"preparation failed: {exc}")
        except Exception:
            pass
        raise SystemExit(1) from exc
    try:
        from src.core import app_log

        app_log.info("startup", f"ready gpu={prepared.gpu.get('display_name', 'GPU')}")
    except Exception:
        pass

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
            inbrowser=os.environ.get("DLSS5_SKIP_BROWSER_OPEN") != "1",
            share=False,
            allowed_paths=[str(OUTPUTS.resolve())],
            show_error=True,
            quiet=True,
        )
    except KeyboardInterrupt:
        pass
    finally:
        try:
            from src.core import app_log

            app_log.info("app", "stop")
        except Exception:
            pass


if __name__ == "__main__":
    main()
