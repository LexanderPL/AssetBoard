"""Page identity: every page names its destination in exactly one ``h1``, and its document
title names the page rather than repeating the section heading."""

from __future__ import annotations

import re

import pytest

from assetboard.branding import APP_NAME
from tests.conftest import OwnerApp

PAGES = [
    ("/", "Home", "Home"),
    ("/investments/portfolio", "Investments", "Portfolio"),
    ("/investments/performance", "Investments", "Performance"),
    ("/investments/providers", "Investments", "Price providers"),
    ("/investments/income", "Investment income", "Investment income"),
]


def _headings(html: str) -> list[str]:
    matches = re.findall(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    return [re.sub(r"<[^>]+>", "", match).strip() for match in matches]


@pytest.mark.parametrize(("path", "heading", "title"), PAGES)
def test_pages_have_one_h1_and_a_page_specific_title(
    owner: OwnerApp, path: str, heading: str, title: str
) -> None:
    response = owner.client.get(path)
    assert response.status_code == 200
    assert _headings(response.text) == [heading]
    assert f"<title>{title} · {APP_NAME}</title>" in response.text


def test_home_shows_the_viewed_month_as_a_subtitle(owner: OwnerApp) -> None:
    page = owner.client.get("/").text
    subtitle = re.search(r'<p class="[^"]*" data-home-month>(.*?)</p>', page, re.DOTALL)
    assert subtitle is not None and re.fullmatch(r"\w+ \d{4}", subtitle.group(1).strip())


def test_investment_income_has_distinct_translated_headings(owner: OwnerApp) -> None:
    owner.client.headers["Accept-Language"] = "pl"
    page = owner.client.get("/investments/income").text
    assert "<title>Przychód z inwestycji · " in page
    assert ">Przychód z inwestycji</h1>" in page
    assert ">Przychody z inwestycji</h2>" in page
