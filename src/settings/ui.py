from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, replace

import gradio as gr

from ..core.i18n import (
    DISPLAY_VALUE_RAM,
    DISPLAY_VALUE_VRAM,
    LANGUAGE_EN,
    LANGUAGE_ZH_HANS,
    language_display_name,
    option_label,
    t,
    translator,
)
from ..core.ffmpeg import container_for_codec, hdr_mode_supported, probe_nvenc_codecs
from ..core.gpu_selection import gpu_choice_label
from ..core.paths import CONFIG_PATH
from ..core.runtime import UPSCALING_MODES, prepare_runtime
from ..core.ffmpeg.preview import normalize_preview_encoding
from .models import (
    DEFAULT_SETTINGS, PREVIEW_ENCODING_CHOICES, UPSCALE_MODE_CHOICES, UISettings,
    automatic_mask_choice, coerce_hdr_mode, parse_automatic_mask,
)
from .presets import export_settings_preset, import_settings_preset, preset_filename
from .storage import SETTINGS_STATE, load_settings, save_settings
from ..upscale.video.models import SETTING_FIELDS, UpscaleOptions
from ..upscale.image.models import SETTING_FIELDS as IMAGE_UPSCALE_FIELDS, ImageUpscaleOptions

_CONFIG_LOCK = SETTINGS_STATE.lock
UPSCALING_CHOICES = tuple((mode["label"], factor) for factor, mode in UPSCALING_MODES.items())
_SKIN_STRUCTURE_INDEX = 5
_AUTOMATIC_MASK_INDEX = 7
_NR_COLOR_INDEX = 8
_TONE_PRESERVATION_INDEX = 9
PROCESSING_ENGINE_CHOICES = (DISPLAY_VALUE_VRAM, DISPLAY_VALUE_RAM)
_RESTART_LOCK = threading.Lock()
_RESTART_PENDING = False


def _language_choices(language: str) -> list[tuple[str, str]]:
    return [
        (language_display_name(LANGUAGE_EN, language), LANGUAGE_EN),
        (language_display_name(LANGUAGE_ZH_HANS, language), LANGUAGE_ZH_HANS),
    ]


def _preview_encoding_choices(language: str) -> list[tuple[str, str]]:
    return [(option_label(choice, language), choice) for choice in PREVIEW_ENCODING_CHOICES]


def _processing_engine_choices(language: str) -> list[tuple[str, str]]:
    return [(option_label(choice, language), choice) for choice in PROCESSING_ENGINE_CHOICES]


def _upscale_mode_choices(language: str) -> list[tuple[str, str]]:
    return [(option_label(choice, language), choice) for choice in UPSCALE_MODE_CHOICES]


def _restart_application_later(delay_seconds: float = 0.6) -> None:
    global _RESTART_PENDING
    with _RESTART_LOCK:
        if _RESTART_PENDING:
            return
        _RESTART_PENDING = True

    def _runner() -> None:
        time.sleep(delay_seconds)
        app_path = CONFIG_PATH.parents[1] / "app.py"
        os.environ["DLSS5_SKIP_BROWSER_OPEN"] = "1"
        os.execv(sys.executable, [sys.executable, str(app_path)])

    threading.Thread(target=_runner, name="language-restart", daemon=True).start()


def restart_service() -> str:
    global _RESTART_PENDING
    with _RESTART_LOCK:
        already_pending = _RESTART_PENDING
    if already_pending:
        return t("settings.language.restart_pending")
    _restart_application_later()
    return t("settings.language.restarting")


def processing_engine_choice(enabled: bool) -> str:
    """Map the stored bool path to its visible VRAM/RAM label."""
    return DISPLAY_VALUE_VRAM if enabled else DISPLAY_VALUE_RAM


def parse_processing_engine(value: object) -> bool:
    """Map the visible VRAM/RAM selection back to its stored bool path."""
    if value not in PROCESSING_ENGINE_CHOICES:
        choices = ", ".join(PROCESSING_ENGINE_CHOICES)
        raise ValueError(t("settings.processing_engine.invalid", choices=choices))
    return value == DISPLAY_VALUE_VRAM


def automatic_mask_for_skin_input(
    skin_structure_strength: float, automatic_mask: str,
) -> str:
    """Make an explicit Skin adjustment effective without overriding later Mask Off."""
    try:
        skin = float(skin_structure_strength)
    except (TypeError, ValueError) as exc:
        raise gr.Error(t("settings.error.skin_structure_range")) from exc
    if not -1.0 <= skin <= 2.0:
        raise gr.Error(t("settings.error.skin_structure_range"))
    return "On" if skin > -1.0 else automatic_mask


def _persist_skin_input(persist, values: tuple) -> tuple:
    effective = list(values)
    effective[_AUTOMATIC_MASK_INDEX] = automatic_mask_for_skin_input(
        effective[_SKIN_STRUCTURE_INDEX], effective[_AUTOMATIC_MASK_INDEX]
    )
    mirrored = persist(*effective)
    return (effective[_AUTOMATIC_MASK_INDEX], *mirrored)


def _neural_values(
    settings: UISettings,
) -> tuple[str, float, int, float, float, float, float, str, float, float, float, float, int]:
    return (
        settings.nr_style,
        settings.nr_intensity,
        settings.nr_passes,
        settings.local_tone_strength,
        settings.local_structure_strength,
        settings.skin_structure_strength,
        settings.upscaling_factor,
        automatic_mask_choice(settings.automatic_mask),
        settings.nr_color_strength,
        settings.tone_preservation,
        settings.face_skin_protection,
        settings.grain_preservation,
        settings.mask_feather,
    )


def _mirrored_dlss_values(settings: UISettings) -> tuple:
    """Image controls followed by temporal Video/Live controls."""
    shared = _neural_values(settings)
    temporal = (*shared, settings.shimmer_suppression)
    return (*shared, *temporal)


def _video_live_values(settings: UISettings) -> tuple:
    return (*_neural_values(settings), settings.shimmer_suppression)


def _notify_live_effects(settings: UISettings) -> None:
    # Call while holding _CONFIG_LOCK: submissions follow the saved commit
    # order, including imports/resets. This only enqueues an immutable snapshot.
    from ..live.pipeline import update_live_effects

    update_live_effects(settings)


def persist_image_settings(
    nr_style: str,
    nr_intensity: float,
    nr_passes: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    nr_color_strength: float,
    tone_preservation: float,
    face_skin_protection: float,
    grain_preservation: float,
    mask_feather: float,
    image_format: str,
    image_quality: float,
    rename_mode: str,
    custom_suffix: str,
) -> tuple:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(
            current,
            nr_style=nr_style,
            nr_intensity=nr_intensity,
            nr_passes=int(nr_passes),
            local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=parse_automatic_mask(automatic_mask),
            nr_color_strength=float(nr_color_strength),
            tone_preservation=float(tone_preservation),
            face_skin_protection=float(face_skin_protection),
            grain_preservation=float(grain_preservation),
            mask_feather=int(mask_feather),
            image_format=image_format,
            image_quality=int(image_quality),
            image_rename_mode=rename_mode,
            image_custom_suffix=custom_suffix,
        )
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)
    temporal = _video_live_values(settings)
    return (*temporal, *temporal)


def persist_video_settings(
    nr_style: str,
    nr_intensity: float,
    nr_passes: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    nr_color_strength: float,
    tone_preservation: float,
    face_skin_protection: float,
    grain_preservation: float,
    mask_feather: float,
    shimmer_suppression: float,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    rename_mode: str,
    custom_suffix: str,
) -> tuple:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        container = container_for_codec(codec)
        coerced_hdr = coerce_hdr_mode(codec, hdr_mode)
        settings = replace(
            current,
            nr_style=nr_style,
            nr_intensity=nr_intensity,
            nr_passes=int(nr_passes),
            local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=parse_automatic_mask(automatic_mask),
            nr_color_strength=float(nr_color_strength),
            tone_preservation=float(tone_preservation),
            face_skin_protection=float(face_skin_protection),
            grain_preservation=float(grain_preservation),
            mask_feather=int(mask_feather),
            shimmer_suppression=float(shimmer_suppression),
            codec=codec,
            container=container,
            quality=quality,
            hdr_mode=coerced_hdr,
            video_rename_mode=rename_mode,
            video_custom_suffix=custom_suffix,
        )
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)
    return (*_neural_values(settings), *_video_live_values(settings))


def persist_live_settings(
    nr_style: str,
    nr_intensity: float,
    nr_passes: float,
    local_tone_strength: float,
    local_structure_strength: float,
    skin_structure_strength: float,
    upscaling_factor: float,
    automatic_mask: str,
    nr_color_strength: float,
    tone_preservation: float,
    face_skin_protection: float,
    grain_preservation: float,
    mask_feather: float,
    shimmer_suppression: float,
) -> tuple:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(
            current,
            nr_style=nr_style,
            nr_intensity=nr_intensity,
            nr_passes=int(nr_passes),
            local_tone_strength=local_tone_strength,
            local_structure_strength=local_structure_strength,
            skin_structure_strength=skin_structure_strength,
            upscaling_factor=upscaling_factor,
            automatic_mask=parse_automatic_mask(automatic_mask),
            nr_color_strength=float(nr_color_strength),
            tone_preservation=float(tone_preservation),
            face_skin_protection=float(face_skin_protection),
            grain_preservation=float(grain_preservation),
            mask_feather=int(mask_feather),
            shimmer_suppression=float(shimmer_suppression),
        )
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)
    return (*_neural_values(settings), *_video_live_values(settings))


def persist_image_skin_settings(*values) -> tuple:
    return _persist_skin_input(persist_image_settings, values)


def persist_video_skin_settings(*values) -> tuple:
    return _persist_skin_input(persist_video_settings, values)


def persist_live_skin_settings(*values) -> tuple:
    return _persist_skin_input(persist_live_settings, values)


def persist_nr_mask(selection: object | None) -> None:
    """Update session-only mask state without writing its temporary path to disk."""
    from ..core.nr_composition import mask_selection

    selected = mask_selection(selection)
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, nr_mask=selected)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)


def apply_detail_only_settings() -> tuple:
    """Apply the editable Detail-Only preset to every mirrored interface."""
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, nr_color_strength=0.0, tone_preservation=1.0)
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)
    shared = _neural_values(settings)
    temporal = _video_live_values(settings)
    return (*shared, *temporal, *temporal)


def persist_language(language: str) -> tuple:
    language = language if language in (LANGUAGE_EN, LANGUAGE_ZH_HANS) else translator(language).language
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, language=language)
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
    return (
        gr.update(value=language, choices=_language_choices(language)),
        translator(language).t("settings.language.saved"),
    )


def persist_frame_interpolation_settings(
    target_fps: str,
    engine: str,
    codec: str,
    container: str,
    quality: str,
    hdr_mode: bool,
    rename_mode: str,
    custom_suffix: str,
) -> None:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        container = container_for_codec(codec)
        coerced_hdr = coerce_hdr_mode(codec, hdr_mode)
        settings = replace(
            current,
            frame_interpolation_target_fps=target_fps,
            frame_interpolation_engine=engine,
            frame_interpolation_codec=codec,
            frame_interpolation_container=container,
            frame_interpolation_quality=quality,
            frame_interpolation_hdr_mode=coerced_hdr,
            frame_interpolation_rename_mode=rename_mode,
            frame_interpolation_custom_suffix=custom_suffix,
        )
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def persist_upscale_settings(*values) -> None:
    chosen = dict(zip(SETTING_FIELDS, values))
    chosen["container"] = container_for_codec(chosen["codec"])
    if not hdr_mode_supported(chosen["codec"]):
        chosen["hdr_enabled"] = False
    options = UpscaleOptions(**chosen)
    options.validate(for_render=False)
    defaults = UpscaleOptions()
    for name in SETTING_FIELDS:
        if type(getattr(defaults, name)) is int:
            chosen[name] = int(chosen[name])
    chosen["scale_factor"] = float(chosen["scale_factor"])
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, **{"upscale_" + name: value for name, value in chosen.items()})
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def persist_image_upscale_settings(*values) -> None:
    chosen = dict(zip(IMAGE_UPSCALE_FIELDS, values))
    ImageUpscaleOptions(**chosen).validate()
    defaults = ImageUpscaleOptions()
    for name in IMAGE_UPSCALE_FIELDS:
        if type(getattr(defaults, name)) is int:
            chosen[name] = int(chosen[name])
    chosen["scale_factor"] = float(chosen["scale_factor"])
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, **{"upscale_image_" + n: v for n, v in chosen.items()})
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def persist_upscale_mode(mode: str) -> None:
    if mode not in UPSCALE_MODE_CHOICES:
        raise gr.Error(t("settings.error.upscale_mode", choices=", ".join(UPSCALE_MODE_CHOICES)))
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, upscale_mode=mode)
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def _settings_component_values(settings: UISettings) -> tuple:
    shared = _neural_values(settings)
    temporal = _video_live_values(settings)
    return (
        gr.update(
            value=settings.language,
            choices=_language_choices(settings.language),
        ),
        *shared,
        *temporal,
        settings.image_format,
        settings.image_quality,
        settings.image_rename_mode,
        gr.update(
            value=settings.image_custom_suffix,
            interactive=settings.image_rename_mode == "Custom",
        ),
        settings.codec,
        container_for_codec(settings.codec),
        settings.quality,
        gr.update(value=settings.hdr_mode, interactive=hdr_mode_supported(settings.codec)),
        settings.video_rename_mode,
        gr.update(
            value=settings.video_custom_suffix,
            interactive=settings.video_rename_mode == "Custom",
        ),
        settings.frame_interpolation_target_fps,
        settings.frame_interpolation_engine,
        settings.frame_interpolation_codec,
        container_for_codec(settings.frame_interpolation_codec),
        settings.frame_interpolation_quality,
        gr.update(value=settings.frame_interpolation_hdr_mode, interactive=hdr_mode_supported(settings.frame_interpolation_codec)),
        settings.frame_interpolation_rename_mode,
        gr.update(
            value=settings.frame_interpolation_custom_suffix,
            interactive=settings.frame_interpolation_rename_mode == "Custom",
        ),
        settings.ai_gpu_uuid,
        settings.video_gpu_uuid,
        gr.update(
            value=settings.preview_encoding,
            choices=_preview_encoding_choices(settings.language),
        ),
        settings.full_size_image_previews,
        gr.update(
            value=processing_engine_choice(settings.nr_gpu_mode),
            choices=_processing_engine_choices(settings.language),
        ),
        settings.nr_gpu_mode,
        *temporal,
        gr.update(
            value=settings.upscale_mode,
            choices=_upscale_mode_choices(settings.language),
        ),
        *(container_for_codec(settings.upscale_codec) if name == "container"
          else getattr(settings, "upscale_" + name) for name in SETTING_FIELDS),
        *(getattr(settings, "upscale_image_" + name) for name in IMAGE_UPSCALE_FIELDS),
    )


def _gpu_choices(prepared) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    automatic = [("Auto", "auto")]
    ai = []
    for gpu in prepared.gpus:
        if not gpu.get("ai_compatible"):
            continue
        ai.append((gpu_choice_label(gpu), str(gpu["uuid"])))
    video = [
        (gpu_choice_label(gpu), str(gpu["uuid"]))
        for gpu in prepared.gpus
        if gpu.get("cuda_ordinal") is not None
        and probe_nvenc_codecs(int(gpu["cuda_ordinal"]))
    ]
    return automatic + ai, automatic + video


def _normalize_gpu_settings(settings: UISettings, prepared) -> tuple[UISettings, str]:
    ai_choices, video_choices = _gpu_choices(prepared)
    ai_values = {value for _label, value in ai_choices}
    video_values = {value for _label, value in video_choices}
    warnings = []
    ai_uuid = settings.ai_gpu_uuid
    video_uuid = settings.video_gpu_uuid
    if ai_uuid not in ai_values:
        warnings.append(translator(settings.language).t("settings.gpu.unavailable_ai"))
        ai_uuid = "auto"
    if video_uuid not in video_values:
        warnings.append(translator(settings.language).t("settings.gpu.unavailable_video"))
        video_uuid = "auto"
    # Old presets may contain manual path flags. They are retained by the
    # storage schema for compatibility but normalized so they cannot affect
    # the now-automatic runtime choice.
    return replace(
        settings,
        ai_gpu_uuid=ai_uuid,
        video_gpu_uuid=video_uuid,
        nr_gpu_mode=True,
        frame_interpolation_gpu_mode=True,
        container=container_for_codec(settings.codec),
        frame_interpolation_container=container_for_codec(settings.frame_interpolation_codec),
        upscale_container=container_for_codec(settings.upscale_codec),
    ), " ".join(warnings)


def persist_preview_encoding(preview_encoding: str) -> None:
    normalized = normalize_preview_encoding(preview_encoding)
    if not isinstance(preview_encoding, str) or preview_encoding.strip() not in PREVIEW_ENCODING_CHOICES:
        raise gr.Error(t("settings.error.preview_encoding", choices=", ".join(PREVIEW_ENCODING_CHOICES)))
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, preview_encoding=normalized)
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def persist_full_size_image_previews(enabled: bool) -> None:
    if not isinstance(enabled, bool):
        raise gr.Error(t("settings.error.full_size_preview_toggle"))
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(current, full_size_image_previews=enabled)
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings


def persist_gpu_settings(ai_gpu_uuid: str, video_gpu_uuid: str) -> str:
    prepared = prepare_runtime()
    ai_choices, video_choices = _gpu_choices(prepared)
    if ai_gpu_uuid not in {value for _label, value in ai_choices}:
        raise gr.Error(t("settings.error.choose_ai_gpu"))
    if video_gpu_uuid not in {value for _label, value in video_choices}:
        raise gr.Error(t("settings.error.choose_video_gpu"))
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        settings = replace(
            current, ai_gpu_uuid=ai_gpu_uuid, video_gpu_uuid=video_gpu_uuid
        )
        if settings != current:
            save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
    ai_name = "Auto" if ai_gpu_uuid == "auto" else next(
        label for label, value in ai_choices if value == ai_gpu_uuid
    )
    video_name = "Auto" if video_gpu_uuid == "auto" else next(
        label for label, value in video_choices if value == video_gpu_uuid
    )
    return t("settings.gpu.saved", ai=ai_name, video=video_name)


def reset_saved_settings() -> tuple:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        # Custom NR Mask is session-scoped, so resetting persisted controls must
        # not silently detach the mask that remains visible in all three tabs.
        settings = replace(DEFAULT_SETTINGS, nr_mask=current.nr_mask, language=current.language)
        save_settings(CONFIG_PATH, settings)
        SETTINGS_STATE.current = settings
        _notify_live_effects(settings)
    message = t("settings.reset.success")
    return (
        *_settings_component_values(settings),
        message,
        message,
        message,
        message,
        message,
    )


def settings_preset_download(name: str) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    try:
        path = export_settings_preset(name, current)
    except ValueError:
        return None
    return str(path)


def settings_preset_export_status(name: str) -> str:
    try:
        filename = preset_filename(name)
    except ValueError as exc:
        return t("settings.preset.export_failed", error=exc)
    return t("settings.preset.exported", name=name.strip(), filename=filename)


def apply_settings_preset(
    uploaded_path: str | None, current_name: str
) -> tuple:
    with _CONFIG_LOCK:
        current = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
        try:
            if not uploaded_path:
                raise ValueError("Choose a JSON preset file before importing.")
            name, imported = import_settings_preset(uploaded_path, current)
            imported, gpu_warning = _normalize_gpu_settings(
                imported, prepare_runtime()
            )
            save_settings(CONFIG_PATH, imported)
        except (OSError, ValueError) as exc:
            return (
                current_name,
                *_settings_component_values(current),
                t("settings.preset.import_failed", error=exc),
            )
        SETTINGS_STATE.current = imported
        _notify_live_effects(imported)
    translated_warning = f" {gpu_warning}" if gpu_warning else ""
    return (
        name,
        *_settings_component_values(imported),
        translator(imported.language).t("settings.preset.imported", name=name, warning=translated_warning),
    )

@dataclass(slots=True)
class SettingsTab:
    language_selector: object
    restart_service_button: object
    ai_gpu_selector: object
    video_gpu_selector: object
    preview_encoding_selector: object
    full_size_image_previews: object
    preset_name: object
    preset_export: object
    preset_import: object
    preset_status: object


def initialize_settings(prepared) -> tuple[UISettings, str, list[tuple[str, str]], list[tuple[str, str]]]:
    settings = SETTINGS_STATE.current or load_settings(CONFIG_PATH)
    normalized, warning = _normalize_gpu_settings(settings, prepared)
    if normalized != settings:
        save_settings(CONFIG_PATH, normalized)
    SETTINGS_STATE.current = normalized
    ai_choices, video_choices = _gpu_choices(prepared)
    return normalized, warning, ai_choices, video_choices


def current_settings() -> UISettings:
    with _CONFIG_LOCK:
        return SETTINGS_STATE.current or load_settings(CONFIG_PATH)


def build_settings_tab(
    settings: UISettings,
    ai_gpu_choices: list[tuple[str, str]],
    video_gpu_choices: list[tuple[str, str]],
) -> SettingsTab:
    ui_t = translator(settings.language).t
    localized_ai_gpu_choices = [
        (option_label(label, settings.language), value) if value == "auto" else (label, value)
        for label, value in ai_gpu_choices
    ]
    localized_video_gpu_choices = [
        (option_label(label, settings.language), value) if value == "auto" else (label, value)
        for label, value in video_gpu_choices
    ]
    gr.Markdown(ui_t("settings.section.language"))
    with gr.Row():
        language_selector = gr.Dropdown(
            choices=_language_choices(settings.language),
            value=settings.language,
            label=ui_t("settings.language.label"),
            scale=4,
        )
        restart_service_button = gr.Button(
            ui_t("settings.language.restart_button"),
            variant="secondary",
            scale=1,
        )
    gr.Markdown(ui_t("settings.section.gpu_selection"))
    with gr.Row():
        ai_gpu_selector = gr.Dropdown(
            choices=localized_ai_gpu_choices,
            value=settings.ai_gpu_uuid,
            label=ui_t("settings.gpu.ai"),
        )
        video_gpu_selector = gr.Dropdown(
            choices=localized_video_gpu_choices,
            value=settings.video_gpu_uuid,
            label=ui_t("settings.gpu.video"),
        )
    gr.Markdown(ui_t("settings.section.processing_engine"))
    gpu_mode = gr.Radio(
        choices=_processing_engine_choices(settings.language),
        value=processing_engine_choice(settings.nr_gpu_mode),
        label=ui_t("settings.processing_engine.label"),
        show_label=False,
    )
    gr.Markdown(ui_t("settings.section.preview_encoding"))
    preview_encoding_selector = gr.Radio(
        choices=_preview_encoding_choices(settings.language),
        value=normalize_preview_encoding(settings.preview_encoding),
        label=ui_t("settings.preview_encoding.label"),
        show_label=False,
    )
    gr.Markdown(ui_t("settings.section.image_preview_quality"))
    full_size_image_previews = gr.Checkbox(
        value=settings.full_size_image_previews,
        label=ui_t("settings.image_preview.full_quality"),
    )
    gr.Markdown(ui_t("settings.section.presets"))
    preset_name = gr.Textbox(
        label=ui_t("settings.presets.name"),
        placeholder=ui_t("settings.presets.placeholder"),
    )
    with gr.Row():
        preset_export = gr.DownloadButton(
            ui_t("settings.presets.export"),
            value=settings_preset_download,
            inputs=preset_name,
            variant="primary",
            scale=1,
        )
        preset_import = gr.UploadButton(
            ui_t("settings.presets.import"),
            file_count="single",
            file_types=[".json"],
            type="filepath",
            variant="primary",
            scale=1,
        )
    preset_status = gr.Markdown("", elem_id="preset-status")
    return SettingsTab(
        language_selector,
        restart_service_button,
        ai_gpu_selector, video_gpu_selector, preview_encoding_selector, full_size_image_previews,
        preset_name, preset_export, preset_import, preset_status
    )



def settings_component_outputs(image_tab, video_tab, frame_tab, settings_tab, live_tab, upscale_tab) -> list[object]:
    return [
        settings_tab.language_selector,
        *image_tab.neural,
        *video_tab.neural,
        image_tab.output_format,
        image_tab.quality,
        image_tab.rename_mode,
        image_tab.custom_suffix,
        video_tab.codec,
        video_tab.container,
        video_tab.quality,
        video_tab.hdr_mode,
        video_tab.rename_mode,
        video_tab.custom_suffix,
        frame_tab.target_fps,
        frame_tab.engine,
        frame_tab.codec,
        frame_tab.container,
        frame_tab.quality,
        frame_tab.hdr_mode,
        frame_tab.rename_mode,
        frame_tab.custom_suffix,
        settings_tab.ai_gpu_selector,
        settings_tab.video_gpu_selector,
        settings_tab.preview_encoding_selector,
        settings_tab.full_size_image_previews,
        *live_tab.neural,
        upscale_tab.mode,
        *upscale_tab.video.settings_inputs,
        *upscale_tab.image.settings_inputs,
    ]


def bind_settings_events(settings_tab, image_tab, video_tab, frame_tab, live_tab, upscale_tab) -> None:
    video_mirror = [*video_tab.neural]
    image_mirror = [*image_tab.neural]
    live_mirror = [*live_tab.neural]
    for component in image_tab.settings_inputs:
        if component is image_tab.neural[_SKIN_STRUCTURE_INDEX]:
            continue
        component.input(
            persist_image_settings,
            inputs=image_tab.settings_inputs,
            outputs=[*video_mirror, *live_mirror],
            queue=False,
        )
    for component in video_tab.settings_inputs:
        if component is video_tab.neural[_SKIN_STRUCTURE_INDEX]:
            continue
        component.input(
            persist_video_settings,
            inputs=video_tab.settings_inputs,
            outputs=[*image_mirror, *live_mirror],
            queue=False,
        )
    for component in live_tab.settings_inputs:
        if component is live_tab.neural[_SKIN_STRUCTURE_INDEX]:
            continue
        component.input(
            persist_live_settings,
            inputs=live_tab.settings_inputs,
            outputs=[*image_mirror, *video_mirror],
            queue=False,
        )
    image_tab.neural[_SKIN_STRUCTURE_INDEX].input(
        persist_image_skin_settings,
        inputs=image_tab.settings_inputs,
        outputs=[image_tab.neural[_AUTOMATIC_MASK_INDEX], *video_mirror, *live_mirror],
        queue=False,
    )
    video_tab.neural[_SKIN_STRUCTURE_INDEX].input(
        persist_video_skin_settings,
        inputs=video_tab.settings_inputs,
        outputs=[video_tab.neural[_AUTOMATIC_MASK_INDEX], *image_mirror, *live_mirror],
        queue=False,
    )
    live_tab.neural[_SKIN_STRUCTURE_INDEX].input(
        persist_live_skin_settings,
        inputs=live_tab.settings_inputs,
        outputs=[live_tab.neural[_AUTOMATIC_MASK_INDEX], *image_mirror, *video_mirror],
        queue=False,
    )
    detail_outputs = [*image_tab.neural, *video_tab.neural, *live_tab.neural]
    for button in (
        image_tab.composition.detail_only,
        video_tab.composition.detail_only,
        live_tab.composition.detail_only,
    ):
        button.click(
            apply_detail_only_settings,
            outputs=detail_outputs,
            queue=False,
            show_progress="hidden",
        )
    for component in frame_tab.settings_inputs:
        component.input(
            persist_frame_interpolation_settings,
            inputs=frame_tab.settings_inputs,
            queue=False,
        )

    for component in upscale_tab.video.settings_inputs:
        component.change(persist_upscale_settings, inputs=upscale_tab.video.settings_inputs, queue=False, show_progress="hidden")
    for component in upscale_tab.image.settings_inputs:
        component.change(persist_image_upscale_settings, inputs=upscale_tab.image.settings_inputs, queue=False, show_progress="hidden")
    upscale_tab.mode.input(
        persist_upscale_mode,
        inputs=upscale_tab.mode,
        queue=False,
        show_progress="hidden",
    )
    outputs = settings_component_outputs(image_tab, video_tab, frame_tab, settings_tab, live_tab, upscale_tab)
    settings_tab.preset_export.click(
        settings_preset_export_status,
        inputs=settings_tab.preset_name,
        outputs=settings_tab.preset_status,
        queue=False,
    )
    settings_tab.preset_import.upload(
        apply_settings_preset,
        inputs=[settings_tab.preset_import, settings_tab.preset_name],
        outputs=[settings_tab.preset_name, *outputs, settings_tab.preset_status],
        queue=False,
    )
    settings_tab.language_selector.change(
        persist_language,
        inputs=settings_tab.language_selector,
        outputs=[settings_tab.language_selector, settings_tab.preset_status],
        queue=False,
    )
    settings_tab.restart_service_button.click(
        restart_service,
        outputs=settings_tab.preset_status,
        queue=False,
    ).success(
        fn=None,
        js="() => { window.setTimeout(() => window.location.reload(), 1200); }",
        queue=False,
        show_progress="hidden",
    )
    for selector in [settings_tab.ai_gpu_selector, settings_tab.video_gpu_selector]:
        selector.input(
            persist_gpu_settings,
            inputs=[settings_tab.ai_gpu_selector, settings_tab.video_gpu_selector],
            queue=False,
        )
    settings_tab.preview_encoding_selector.input(
        persist_preview_encoding,
        inputs=settings_tab.preview_encoding_selector,
        queue=False,
    )
    settings_tab.full_size_image_previews.input(
        persist_full_size_image_previews,
        inputs=settings_tab.full_size_image_previews,
        queue=False,
    )
    reset_outputs = [*outputs, image_tab.status, video_tab.status, frame_tab.status, upscale_tab.video.status, upscale_tab.image.status]
    image_reset = image_tab.reset.click(reset_saved_settings, outputs=reset_outputs, queue=False)
    video_reset = video_tab.reset.click(reset_saved_settings, outputs=reset_outputs, queue=False)
    # Reset writes every shared Neural Rendering control programmatically, which
    # does not emit the direct input/release events used by automatic previews.
    # Refresh both workflows so a prepared preview in the hidden mode cannot
    # remain rendered with the pre-reset settings.
    for reset_event in (image_reset, video_reset):
        image_tab.refresh_realtime_preview_after(reset_event)
        video_tab.refresh_realtime_preview_after(reset_event)
    frame_tab.reset.click(reset_saved_settings, outputs=reset_outputs, queue=False)
    upscale_tab.video.reset.click(reset_saved_settings, outputs=reset_outputs, queue=False)
    upscale_tab.image.reset.click(reset_saved_settings, outputs=reset_outputs, queue=False)
