"""The retired net-worth history URL (F5.9): redirect to the Summary trend."""

from __future__ import annotations

import re

import pytest

from tests.conftest import OwnerApp


@pytest.mark.parametrize("range_code", ["1y", "3y", "5y", "all"])
def test_history_redirects_to_summary_trend_preserving_range(
    owner: OwnerApp, range_code: str
) -> None:
    response = owner.client.get(f"/net-worth/history?range={range_code}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == (
        f"/net-worth/summary?range={range_code}#net-worth-trend"
    )


@pytest.mark.parametrize("range_code", ["", "unknown", "10y", "ALL"])
def test_unknown_history_range_redirects_to_one_year(owner: OwnerApp, range_code: str) -> None:
    query = f"?range={range_code}" if range_code else ""
    response = owner.client.get(f"/net-worth/history{query}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/net-worth/summary?range=1y#net-worth-trend"


def test_history_redirect_is_not_permanent(owner: OwnerApp) -> None:
    # A 301 would be cached by browsers after the feature is removed; 302 stays changeable.
    response = owner.client.get("/net-worth/history", follow_redirects=False)
    assert response.status_code == 302


@pytest.mark.parametrize(
    "path",
    ["/net-worth/history/snapshot", "/net-worth/history/backfill"],
)
def test_snapshot_and_backfill_post_routes_are_gone(owner: OwnerApp, path: str) -> None:
    response = owner.client.post(path, data={})
    assert response.status_code in {404, 405}


def test_history_tab_is_absent_everywhere(owner: OwnerApp) -> None:
    for path in ("/net-worth", "/net-worth/summary"):
        page = owner.client.get(path).text
        assert re.search(r"data-history-tab(?![a-z-])", page) is None
        tab_strip = re.search(r'<nav class="tab-strip".*?</nav>', page, re.DOTALL)
        assert tab_strip is not None and "History" not in tab_strip.group(0)
    page = owner.client.get("/net-worth/summary").text
    assert 'data-summary-tab aria-current="page"' in page
