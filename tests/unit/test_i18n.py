from __future__ import annotations

from datetime import date
from pathlib import Path

from babel.messages.pofile import read_po


def test_polish_catalog_has_no_empty_or_fuzzy_translations() -> None:
    catalog_path = Path(__file__).parents[2] / "src/assetboard/locales/pl/LC_MESSAGES/messages.po"
    with catalog_path.open("rb") as catalog_file:
        catalog = read_po(catalog_file)

    translated_messages = [message for message in catalog if message.id]
    assert translated_messages
    for message in translated_messages:
        assert "fuzzy" not in message.flags
        translations = message.string
        if isinstance(translations, str):
            translations = (translations,)
        assert translations is not None
        assert all(translation.strip() for translation in translations)


def test_polish_runtime_catalog_is_compiled_rather_than_an_english_fallback() -> None:
    # .mo files are built (`make i18n-compile` / the wheel build hook), not committed; gettext
    # would silently fall back to English without them.
    from gettext import GNUTranslations

    from assetboard.i18n import gettext, translations_for

    assert isinstance(translations_for("pl"), GNUTranslations)
    assert gettext("Form expired", "pl") == "Formularz wygasł"


def test_month_names_keep_cldr_case_and_capitalise_only_headings() -> None:
    from assetboard.domain.months import YearMonth
    from assetboard.i18n.dates import format_month, format_month_heading

    october = YearMonth(2026, 10)
    assert format_month(october, "pl") == "październik 2026"
    assert format_month_heading(october, "pl") == "Październik 2026"
    assert format_month(october, "en") == format_month_heading(october, "en") == "October 2026"
    assert format_month(october, "xx-unknown") == "October 2026"  # unknown locale: English


def test_a_month_after_from_uses_the_inflected_form() -> None:
    from assetboard.domain.months import YearMonth
    from assetboard.i18n.dates import format_month_after_from

    september = YearMonth(2026, 9)
    assert format_month_after_from(september, "pl") == "września 2026"  # "z września 2026"
    assert format_month_after_from(september, "en") == "September 2026"


def test_calendar_weekday_names_and_long_dates_come_from_cldr() -> None:
    from assetboard.i18n.dates import format_day_long, weekday_names

    assert weekday_names("en") == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert weekday_names("pl")[0] == "pon."
    assert format_day_long(date(2026, 3, 2), "en") == "Monday, March 2, 2026"
    assert format_day_long(date(2026, 3, 2), "pl") == "poniedziałek, 2 marca 2026"
