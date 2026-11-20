from __future__ import annotations

from functools import lru_cache
from gettext import GNUTranslations, NullTranslations, translation
from pathlib import Path

from assetboard.i18n.locale import normalize_locale

_LOCALE_DIR = Path(__file__).resolve().parents[1] / "locales"


@lru_cache(maxsize=2)
def translations_for(locale: str) -> GNUTranslations | NullTranslations:
    normalized = normalize_locale(locale)
    if normalized == "en":
        return NullTranslations()
    return translation("messages", localedir=_LOCALE_DIR, languages=[normalized], fallback=True)


def gettext(message: str, locale: str) -> str:
    """Translate ``message``; an empty message stays empty.

    ``GNUTranslations.gettext("")`` returns the catalog header (the PO metadata), not an empty
    string. Callers use an empty string as "no label" (for example an optional risk-tier
    subtitle), so the wrapper must not leak the header into the UI.
    """
    if not message:
        return ""
    return translations_for(locale).gettext(message)


def ngettext(singular: str, plural: str, n: int, locale: str) -> str:
    """The plural form for ``n`` in ``locale`` (Polish has three); empty stays empty."""
    if not singular:
        return ""
    return translations_for(locale).ngettext(singular, plural, n)


__all__ = ["gettext", "ngettext", "translations_for"]
