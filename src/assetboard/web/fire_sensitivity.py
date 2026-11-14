"""Server-prepared F8.5 sensitivity table: headers, tone classes, titles and screen-reader text.

The template only maps a tone code to fixed Tailwind classes and prints the labels prepared
here. Every rate, amount and sentence is translated and formatted on the server, so no math or
locale logic runs in the template or JavaScript.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from assetboard.domain.fire import SensitivityCell, SensitivityGrid
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_quantity_input, format_rate_short


@dataclass(frozen=True, slots=True)
class SensitivityHeader:
    return_delta_bp: int
    effective_return_bp: int
    delta_label: str
    rate_label: str
    is_base: bool


@dataclass(frozen=True, slots=True)
class SensitivityCellView:
    age_label: str
    tone: str
    is_base: bool
    title: str
    sr_text: str


@dataclass(frozen=True, slots=True)
class SensitivityRowView:
    expense_factor_bp: int
    factor_label: str
    factor_delta_label: str
    cells: tuple[SensitivityCellView, ...]


@dataclass(frozen=True, slots=True)
class SensitivityView:
    headers: tuple[SensitivityHeader, ...]
    rows: tuple[SensitivityRowView, ...]
    base_age: int | None
    horizon_age: int
    min_fire_age: int | None
    max_fire_age: int | None


def _points_label(delta_bp: int, locale: str) -> str:
    """A signed percentage-point difference, e.g. ``−2 pp`` (Babel minus in the locale)."""
    value = format_quantity_input(Decimal(delta_bp).scaleb(-2), locale)
    return gettext("%(value)s pp", locale) % {"value": value}


def _expense_delta_label(delta_bp: int, locale: str) -> str:
    """A signed percentage of the saved expenses, e.g. ``−20%`` or ``+10%`` (not points)."""
    label = format_rate_short(delta_bp, locale)
    return f"+{label}" if delta_bp > 0 else label


def _tone(cell: SensitivityCell, base_age: int | None) -> str:
    if cell.fire_age is None:
        return "never"
    if cell.is_base:
        return "base"
    if base_age is None or cell.fire_age < base_age:
        return "better"
    if cell.fire_age == base_age:
        return "same"
    return "worse"


def _cell_view(
    cell: SensitivityCell,
    *,
    base_age: int | None,
    horizon_age: int,
    rate_label: str,
    factor_label: str,
    locale: str,
) -> SensitivityCellView:
    if cell.fire_age is None:
        title = gettext(
            "Never before age %(horizon)d; real return %(rate)s; expenses %(factor)s",
            locale,
        ) % {"horizon": horizon_age, "rate": rate_label, "factor": factor_label}
        sr_text = gettext(
            "No FIRE before age %(horizon)d with a real return of %(rate)s and expenses at "
            "%(factor)s of the saved plan.",
            locale,
        ) % {"horizon": horizon_age, "rate": rate_label, "factor": factor_label}
        age_label = "—"
    else:
        assert cell.fire_year is not None
        title = gettext(
            "Age %(age)d in %(year)d; real return %(rate)s; expenses %(factor)s",
            locale,
        ) % {
            "age": cell.fire_age,
            "year": cell.fire_year,
            "rate": rate_label,
            "factor": factor_label,
        }
        if cell.is_base:
            sr_text = gettext(
                "Base case: FIRE at age %(age)d in %(year)d with a real return of %(rate)s "
                "and expenses at %(factor)s of the saved plan.",
                locale,
            ) % {
                "age": cell.fire_age,
                "year": cell.fire_year,
                "rate": rate_label,
                "factor": factor_label,
            }
        else:
            sr_text = gettext(
                "FIRE at age %(age)d in %(year)d with a real return of %(rate)s and expenses "
                "at %(factor)s of the saved plan.",
                locale,
            ) % {
                "age": cell.fire_age,
                "year": cell.fire_year,
                "rate": rate_label,
                "factor": factor_label,
            }
        age_label = str(cell.fire_age)
    return SensitivityCellView(
        age_label=age_label,
        tone=_tone(cell, base_age),
        is_base=cell.is_base,
        title=title,
        sr_text=sr_text,
    )


def build_sensitivity_view(
    grid: SensitivityGrid, *, horizon_age: int, locale: str
) -> SensitivityView:
    """Prepare the full table from the pure grid; never cells get ``—`` and the never tone."""
    base_age = grid.base_cell.fire_age
    headers = tuple(
        SensitivityHeader(
            return_delta_bp=delta,
            effective_return_bp=grid.base_cell.effective_return_bp + delta,
            delta_label=_points_label(delta, locale),
            rate_label=format_rate_short(grid.base_cell.effective_return_bp + delta, locale),
            is_base=delta == 0,
        )
        for delta in grid.return_deltas_bp
    )
    rows: list[SensitivityRowView] = []
    for grid_row in grid.rows:
        factor_label = format_rate_short(grid_row.expense_factor_bp, locale)
        cells = tuple(
            _cell_view(
                cell,
                base_age=base_age,
                horizon_age=horizon_age,
                rate_label=format_rate_short(cell.effective_return_bp, locale),
                factor_label=factor_label,
                locale=locale,
            )
            for cell in grid_row.cells
        )
        rows.append(
            SensitivityRowView(
                expense_factor_bp=grid_row.expense_factor_bp,
                factor_label=factor_label,
                factor_delta_label=_expense_delta_label(
                    grid_row.expense_factor_bp - 10_000, locale
                ),
                cells=cells,
            )
        )
    return SensitivityView(
        headers=headers,
        rows=tuple(rows),
        base_age=base_age,
        horizon_age=horizon_age,
        min_fire_age=grid.min_fire_age,
        max_fire_age=grid.max_fire_age,
    )


__all__ = [
    "SensitivityCellView",
    "SensitivityHeader",
    "SensitivityRowView",
    "SensitivityView",
    "build_sensitivity_view",
]
