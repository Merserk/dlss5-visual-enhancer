"""Qt6 translations and desktop language preferences.

Catalogs use one explicit App context so QML and Python share terminology.
The language is a desktop preference, independent of processing presets.
"""
from __future__ import annotations

from pathlib import Path
import re

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QFontDatabase, QGuiApplication


DEFAULT_LANGUAGE = "en_US"
LANGUAGES = (
    ("en_US", "English (US)", "Segoe UI"),
    ("ar", "العربية", "Segoe UI"),
    ("de", "Deutsch", "Segoe UI"),
    ("es", "Español", "Segoe UI"),
    ("fr", "Français", "Segoe UI"),
    ("hi", "हिन्दी", "Nirmala UI"),
    ("ja", "日本語", "Yu Gothic UI"),
    ("ko", "한국어", "Malgun Gothic"),
    ("pl", "Polski", "Segoe UI"),
    ("uk", "Українська", "Segoe UI"),
    ("zh_CN", "中文 (简体)", "Microsoft YaHei UI"),
    ("zh_TW", "中文 (繁體)", "Microsoft JhengHei UI"),
)
LANGUAGE_CODES = frozenset(code for code, _, _ in LANGUAGES)
CATALOG_DIR = Path(__file__).resolve().parent / "translations"


def tr(source: str) -> str:
    return QCoreApplication.translate("App", source)


class UiMessage(str):
    """Retain the English diagnostic plus arguments for live retranslation."""

    def __new__(cls, source: str, *args):
        values = tuple(str(arg) for arg in args)
        obj = super().__new__(cls, _substitute(source, values))
        obj.source = source
        obj.args = args
        return obj


def _substitute(source: str, args: tuple[str, ...]) -> str:
    return re.sub(r"%([1-9]\d*)", lambda match: args[int(match[1]) - 1], source)


def translate_text(value: str) -> str:
    if isinstance(value, UiMessage):
        return _substitute(tr(value.source), tuple(translate_text(arg) if isinstance(arg, UiMessage) else str(arg) for arg in value.args))
    return tr(value)


def join_messages(values: list[str], separator: str = "\n") -> UiMessage:
    return UiMessage(separator.join(f"%{index+1}" for index in range(len(values))), *values)


def language_choices() -> list[dict[str, str]]:
    available = set(QFontDatabase.families())
    fallback = QGuiApplication.font().family()
    return [{"value": code, "label": name, "fontFamily": font if font in available else fallback}
            for code, name, font in LANGUAGES]


class LanguageManager:
    """Keep translators alive and replace them before QML retranslation."""

    def __init__(self, language: str = DEFAULT_LANGUAGE):
        self.language = DEFAULT_LANGUAGE
        self._translators: list[QTranslator] = []
        self.set_language(language if language in LANGUAGE_CODES else DEFAULT_LANGUAGE)

    def close(self) -> None:
        app = QGuiApplication.instance()
        for translator in self._translators:
            if app is not None:
                app.removeTranslator(translator)
            translator.deleteLater()
        self._translators.clear()

    def set_language(self, language: str) -> bool:
        if language not in LANGUAGE_CODES:
            return False
        app = QGuiApplication.instance()
        if app is None:
            raise RuntimeError("Create QGuiApplication before installing translations.")
        translators = []
        locale = QLocale(language)
        if language != DEFAULT_LANGUAGE:
            # Stock Qt strings (dialog buttons, file pickers, context menus).
            qt_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
            for catalog in ("qtbase", "qtdeclarative"):
                translator = QTranslator(app)
                if translator.load(locale, catalog, "_", qt_dir):
                    translators.append(translator)
            translator = QTranslator(app)
            if not translator.load(str(CATALOG_DIR / f"app_{language}.qm")):
                raise RuntimeError(f"Missing language catalog: {language}")
            translators.append(translator)
        self.close()
        for translator in translators:
            app.installTranslator(translator)
        self._translators = translators
        self.language = language
        QLocale.setDefault(locale)
        app.setLayoutDirection(locale.textDirection())
        # Qt performs glyph fallback/shaping; the selected base font also
        # supplies appropriate metrics for Hindi, Japanese, Korean and Chinese.
        preferred = next(font for code, _, font in LANGUAGES if code == language)
        font = app.font()
        if preferred in QFontDatabase.families():
            font.setFamily(preferred)
        app.setFont(font)
        return True
