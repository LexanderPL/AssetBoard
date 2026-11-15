"""Parsing and display helpers for FIRE live loans (F8.12).

The module has no FastAPI or database dependency. The route parses the posted radio group here,
and the templates print translated labels and server-formatted amounts only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from assetboard.domain.money import Money
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_money
from assetboard.services.fire_loans import FireLoanData, FireLoanRow, LoanPlanState
from assetboard.services.fire_scenarios import FireError, LoanSettingInput

LOAN_TOGGLE_KEY = re.compile(r"^loan_include_([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})$")

STATE_LABELS = {
    LoanPlanState.INCLUDED: "Included",
    LoanPlanState.IGNORED: "Ignored",
    LoanPlanState.ARCHIVED: "Archived",
    LoanPlanState.PAID_OFF: "Paid off",
    LoanPlanState.NOT_DRAWN: "Not drawn",
    LoanPlanState.CURRENCY_MISMATCH: "Another currency",
    LoanPlanState.SCHEDULE_ERROR: "Schedule unavailable",
    LoanPlanState.NO_PAYMENT: "No payment",
    LoanPlanState.AMOUNT_INVALID: "Amount unavailable",
    LoanPlanState.INSTALLMENT_PLAN: "Installment plan",
}

REASON_LABELS = {
    LoanPlanState.ARCHIVED: "Archived in Loans; it is not part of the plan.",
    LoanPlanState.PAID_OFF: "Paid off; there is no payment to include.",
    LoanPlanState.NOT_DRAWN: "No drawdown yet; there is no payment to include.",
    LoanPlanState.CURRENCY_MISMATCH: (
        "The loan is in %(loan_currency)s, not the plan currency %(plan_currency)s."
    ),
    LoanPlanState.SCHEDULE_ERROR: (
        "The loan's schedule cannot be built (%(code)s); it is excluded from the plan."
    ),
    LoanPlanState.NO_PAYMENT: "No scheduled payment remains; it is excluded from the plan.",
    LoanPlanState.AMOUNT_INVALID: (
        "The annual payment is outside the supported amount; it is excluded from the plan."
    ),
    LoanPlanState.INSTALLMENT_PLAN: (
        "Installment plans do not have a live loan schedule yet; they are excluded from the plan."
    ),
}

SLOW_LOAN_ERROR_CODE = "The loan's schedule cannot be built; it is excluded from the plan."


def loan_state_labels(locale: str) -> dict[str, str]:
    return {state.value: gettext(label, locale) for state, label in STATE_LABELS.items()}


def parse_loan_toggles(values: dict[str, str]) -> tuple[LoanSettingInput, ...]:
    """Parse ``loan_include_<uuid>=1|0`` radios; missing rows keep their saved choice."""
    parsed: list[LoanSettingInput] = []
    for key, value in values.items():
        match = LOAN_TOGGLE_KEY.match(key)
        if match is None:
            continue
        try:
            loan_id = UUID(match.group(1))
        except ValueError:
            raise FireError("loans", "invalid_loan") from None
        text = value.strip()
        if text not in ("0", "1"):
            raise FireError("loans", "invalid_loan_setting")
        parsed.append(LoanSettingInput(loan_id=loan_id, included=text == "1"))
    return tuple(parsed)


@dataclass(frozen=True, slots=True)
class LoanRowView:
    loan_id: UUID
    name: str
    state: str
    state_label: str
    included: bool
    annual_label: str
    monthly_label: str
    payoff_label: str
    balance_label: str
    effect: str
    reason: str
    note: str
    toggle_name: str
    eligible: bool


@dataclass(frozen=True, slots=True)
class LoanSectionView:
    currency: str
    as_of_label: str
    rows: tuple[LoanRowView, ...]
    excluded_rows: tuple[LoanRowView, ...]
    included_total_label: str
    ignored_total_label: str
    active_total_label: str
    first_payoff_year: int | None
    last_year: int
    expenses_understated: bool
    expenses_understated_message: str
    payoff_names_by_year: Mapping[int, tuple[str, ...]]
    empty_message: str
    state_labels: dict[str, str]


def _money_or_dash(value: Money | None, locale: str) -> str:
    if value is None:
        return "—"
    return format_money(value, locale)


def _payoff_label(row: FireLoanRow, locale: str) -> str:
    if row.payoff_date is None:
        return "—"
    return format_day(row.payoff_date, locale)


def _note(row: FireLoanRow, locale: str) -> str:
    if row.interest_only:
        return gettext("The next payment is interest-only; the payment will change later.", locale)
    if row.future_drawdown:
        return gettext(
            "A future drawdown will change the payment; this is the next scheduled payment.",
            locale,
        )
    return ""


def _effect(row: FireLoanRow, last_year: int, locale: str) -> str:
    if row.state is LoanPlanState.IGNORED:
        return gettext("Stays in expenses through the plan horizon.", locale)
    if row.state is LoanPlanState.INCLUDED and row.payoff_year is not None:
        if row.payoff_year > last_year:
            return gettext(
                "No drop within the plan horizon; the payoff is in %(year)d.",
                locale,
            ) % {"year": row.payoff_year}
        if row.annual_payment is None:
            return ""
        return gettext(
            "Removed from %(year)d onward; %(amount)s a year returns to the plan.",
            locale,
        ) % {"year": row.payoff_year, "amount": format_money(row.annual_payment, locale)}
    return ""


def _reason(row: FireLoanRow, locale: str, plan_currency: str) -> str:
    label = REASON_LABELS.get(LoanPlanState(row.state))
    if label is None:
        return ""
    if row.state is LoanPlanState.CURRENCY_MISMATCH:
        return gettext(label, locale) % {
            "loan_currency": row.currency,
            "plan_currency": plan_currency,
        }
    if row.state is LoanPlanState.SCHEDULE_ERROR:
        return gettext(label, locale) % {"code": row.error_code or SLOW_LOAN_ERROR_CODE}
    return gettext(label, locale)


def _row_view(row: FireLoanRow, *, currency: str, last_year: int, locale: str) -> LoanRowView:
    state = LoanPlanState(row.state)
    return LoanRowView(
        loan_id=row.loan_id,
        name=row.name,
        state=state.value,
        state_label=gettext(STATE_LABELS[state], locale),
        included=row.included and row.eligible,
        annual_label=_money_or_dash(row.annual_payment, locale),
        monthly_label=_money_or_dash(row.monthly_payment, locale),
        payoff_label=_payoff_label(row, locale),
        balance_label=_money_or_dash(row.current_balance, locale),
        effect=_effect(row, last_year, locale),
        reason=_reason(row, locale, currency),
        note=_note(row, locale),
        toggle_name=f"loan_include_{row.loan_id}",
        eligible=row.eligible,
    )


def build_loan_section(
    data: FireLoanData,
    *,
    currency: str,
    last_year: int,
    expenses: Money | None,
    locale: str,
) -> LoanSectionView:
    rows = tuple(
        _row_view(row, currency=currency, last_year=last_year, locale=locale)
        for row in data.eligible_rows
    )
    excluded = tuple(
        _row_view(row, currency=currency, last_year=last_year, locale=locale)
        for row in data.excluded_rows
    )
    understated = (
        expenses is not None
        and data.active_annual_total.amount_minor > expenses.amount_minor
        and data.active_annual_total.amount_minor > 0
    )
    if not data.rows:
        empty = gettext(
            "No active loans. Enter annual expenses that include any loan payments you make.",
            locale,
        )
    elif not rows:
        empty = gettext(
            "No loan is included in the plan. The reasons are listed below.",
            locale,
        )
    else:
        empty = ""
    return LoanSectionView(
        currency=data.currency,
        as_of_label=format_day(data.as_of, locale),
        rows=rows,
        excluded_rows=excluded,
        included_total_label=format_money(data.included_annual_total, locale),
        ignored_total_label=format_money(data.ignored_annual_total, locale),
        active_total_label=format_money(data.active_annual_total, locale),
        first_payoff_year=data.first_payoff_year,
        last_year=last_year,
        expenses_understated=understated,
        expenses_understated_message=(
            gettext(
                "Your annual expenses are lower than the scheduled loan payments. Check that "
                "the expenses include these payments, or ignore the loan if it is not part of "
                "this plan.",
                locale,
            )
            if understated
            else ""
        ),
        payoff_names_by_year=data.payoff_names_by_year,
        empty_message=empty,
        state_labels=loan_state_labels(locale),
    )


__all__ = [
    "LOAN_TOGGLE_KEY",
    "LoanRowView",
    "LoanSectionView",
    "build_loan_section",
    "loan_state_labels",
    "parse_loan_toggles",
]
