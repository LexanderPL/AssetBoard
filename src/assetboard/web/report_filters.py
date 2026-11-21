"""Remembered Reports filter values: the one-year browser cookie behind the filter panel.

The filters themselves are still a plain GET form and the URL stays authoritative. This module
only maps the last resolved set to and from a cookie, so a bare ``/reports`` opens the view the
browser used last. The cookie is a display preference: every value is resolved and validated by
``services.reports`` again, and category ids from another workspace are ignored there.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

from fastapi import Request

from assetboard.services.reports import ReportFilters
from assetboard.web.security import report_filters_cookie_name

# The query parameters that belong to the report filters. An unknown-only query is not explicit.
REPORT_FILTER_KEYS = frozenset(
    {"from", "to", "step", "currency", "category", "savings", "year", "trend"}
)
# Cookie headers are limited to about 4 KB; leave room for the name, quotes and attributes.
MAX_REPORT_FILTER_COOKIE_LENGTH = 3800
_MAX_FIELDS = 256


@dataclass(frozen=True, slots=True)
class ReportFilterSource:
    """Raw filter values from an explicit URL or the remembered cookie."""

    from_year: str = ""
    to_year: str = ""
    step: str = ""
    currency: str = ""
    category_ids: tuple[str, ...] = ()
    savings: str = ""
    year: str = ""
    trend: str = ""
    explicit: bool = False


def parse_report_filter_cookie(raw: str) -> ReportFilterSource:
    """The remembered filters in ``raw``; junk, an absent or an oversized value means defaults.

    The values are returned as text on purpose: the service resolves years, steps, currencies,
    categories and the trend window, so URL and cookie requests share one validation path.
    """
    if not raw or len(raw) > MAX_REPORT_FILTER_COOKIE_LENGTH:
        return ReportFilterSource()
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=_MAX_FIELDS)
    except ValueError:
        return ReportFilterSource()
    values: dict[str, str] = {}
    categories: list[str] = []
    for key, value in pairs:
        if key == "category":
            categories.append(value)
        elif key in REPORT_FILTER_KEYS:
            values[key] = value
    return ReportFilterSource(
        from_year=values.get("from", ""),
        to_year=values.get("to", ""),
        step=values.get("step", ""),
        currency=values.get("currency", ""),
        category_ids=tuple(categories),
        savings=values.get("savings", ""),
        year=values.get("year", ""),
        trend=values.get("trend", ""),
    )


def read_report_filter_cookie(request: Request) -> ReportFilterSource:
    """The browser's remembered Reports filters, or an empty source when there is none."""
    name = report_filters_cookie_name(request.app.state.settings)
    return parse_report_filter_cookie(request.cookies.get(name, ""))


def serialize_report_filters(filters: ReportFilters) -> str:
    """The resolved filters as the cookie's URL-encoded query-string value."""
    pairs: list[tuple[str, str]] = [
        ("from", str(filters.start_year)),
        ("to", str(filters.end_year)),
        ("step", filters.step.value),
        ("currency", filters.currency),
    ]
    pairs.extend(("category", str(category_id)) for category_id in filters.category_ids)
    if filters.savings:
        pairs.append(("savings", "1"))
    pairs.append(("year", str(filters.year)))
    pairs.append(("trend", str(filters.trend_months)))
    return urlencode(pairs)


__all__ = [
    "MAX_REPORT_FILTER_COOKIE_LENGTH",
    "REPORT_FILTER_KEYS",
    "ReportFilterSource",
    "parse_report_filter_cookie",
    "read_report_filter_cookie",
    "serialize_report_filters",
]
