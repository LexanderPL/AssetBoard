"""The Polish translation completeness gate (F9.7): threshold, missing and fuzzy entries."""

from __future__ import annotations

from pathlib import Path

import pytest
from babel.messages.catalog import Catalog
from babel.messages.pofile import write_po
from scripts import check_translations


def _catalog(tmp_path: Path, locale: str = "pl") -> Path:
    path = tmp_path / locale / "LC_MESSAGES" / "messages.po"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write(path: Path, catalog: Catalog) -> None:
    with path.open("wb") as handle:
        write_po(handle, catalog)


def _complete(tmp_path: Path) -> Path:
    catalog = Catalog(locale="pl")
    catalog.add("Hello", "Cześć")
    catalog.add(
        ("One item", "%(num)d items"), ("Jeden element", "%(num)d elementy", "%(num)d elementów")
    )
    path = _catalog(tmp_path)
    _write(path, catalog)
    return path


def test_a_complete_catalog_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _complete(tmp_path)
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl"]) == 0
    assert "2/2 translated (100.0%), 0 missing, 0 fuzzy" in capsys.readouterr().out


def test_a_catalog_below_the_threshold_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog(locale="pl")
    catalog.add("Hello", "Cześć")
    catalog.add("Goodbye", "")
    path = _catalog(tmp_path)
    _write(path, catalog)
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl", "--threshold", "95"]) == 1
    assert "below the 95.0% threshold" in capsys.readouterr().err


def test_a_missing_entry_fails_even_with_a_zero_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog(locale="pl")
    catalog.add("Hello", "")
    path = _catalog(tmp_path)
    _write(path, catalog)
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl", "--threshold", "0"]) == 1
    assert "1 user-facing string(s) are missing" in capsys.readouterr().err


def test_a_fuzzy_entry_fails_even_when_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog(locale="pl")
    catalog.add("Hello", "Cześć", flags=["fuzzy"])
    path = _catalog(tmp_path)
    _write(path, catalog)
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl", "--threshold", "0"]) == 1
    assert "1 user-facing string(s) are marked fuzzy" in capsys.readouterr().err


def test_every_plural_form_must_be_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    catalog = Catalog(locale="pl")
    catalog.add(("One item", "%(num)d items"), ("Jeden element", "", ""))
    path = _catalog(tmp_path)
    _write(path, catalog)
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl", "--threshold", "0"]) == 1
    assert "1 user-facing string(s) are missing" in capsys.readouterr().err


def test_an_empty_catalog_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _catalog(tmp_path)
    _write(path, Catalog(locale="pl"))
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    assert check_translations.main(["--locale", "pl"]) == 0
    assert "0/0 translated (100.0%)" in capsys.readouterr().out


def test_a_missing_catalog_names_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check_translations, "LOCALES", tmp_path)
    with pytest.raises(SystemExit, match="Missing catalog"):
        check_translations.scan("pl")
