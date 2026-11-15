"""The F8.9 setup wizard's suggestion copy.

The service (``services.fire_linked``) returns data and codes; this module turns the block codes
and warnings into translated sentences. It is shared with F8.10's edit-mode suggestion cards.
"""

from __future__ import annotations

from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_day
from assetboard.services.fire_linked import LinkedWarning


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


SUGGESTION_BLOCK_MESSAGES: dict[str, str] = {
    "no_income_data": _(
        "No regular income transactions fall in the last 12 full months, so there is no income "
        "suggestion. Enter an amount manually or add income to the ledger."
    ),
    "no_expense_data": _(
        "No regular expense transactions fall in the last 12 full months, so there is no expense "
        "suggestion. Enter an amount manually or add expenses to the ledger."
    ),
    "expenses_all_excluded": _(
        "Every regular expense category in the window is excluded, so there is no expense "
        "suggestion. Enter an amount manually."
    ),
    "ledger_currency_mismatch": _(
        "Some ledger rows in the window use another currency, so there is no suggestion. Enter "
        "an amount manually."
    ),
    "scenario_currency_mismatch": _(
        "The workspace base currency no longer matches the plan currency, so there is no "
        "suggestion. Enter an amount manually."
    ),
    "plan_currency_mismatch": _(
        "The workspace base currency no longer matches the plan currency, so there is no "
        "suggestion. Reset the plan and set it up again."
    ),
    "no_linked_portfolio_data": _(
        "No investable portfolio classes or investable manual net-worth items were found, so "
        "there is no portfolio suggestion. Add data or enter an amount manually."
    ),
    "portfolio_price_missing": _(
        "An investable holding has no current price, so there is no portfolio suggestion. Add a "
        "price or enter an amount manually."
    ),
    "portfolio_fx_missing": _(
        "An investable holding has no FX rate to the plan currency, so there is no portfolio "
        "suggestion. Add a rate or enter an amount manually."
    ),
    "item_value_missing": _(
        "An investable net-worth item has no value effective today, so there is no portfolio "
        "suggestion. Add a valuation or enter an amount manually."
    ),
    "item_fx_missing": _(
        "An investable net-worth item has no FX rate to the plan currency, so there is no "
        "portfolio suggestion. Add a rate or enter an amount manually."
    ),
}

BLOCK_FALLBACK = _("This suggestion is unavailable. Enter an amount manually.")

WARNING_FALLBACK = _("Review this suggestion before using it.")

SKIPPED_REASON_MESSAGES: dict[str, str] = {
    "non_investable_class": _("Non-investable asset class; its value is not counted."),
    "linked_item_double_count": _(
        "Linked net-worth item; its value is already counted through its source."
    ),
}


def block_messages(locale: str) -> dict[str, str]:
    """Every translated suggestion block message, keyed by the service code."""
    return {code: gettext(message, locale) for code, message in SUGGESTION_BLOCK_MESSAGES.items()}


def skipped_reason_labels(locale: str) -> dict[str, str]:
    """Every translated skipped-source reason, keyed by the service code."""
    return {code: gettext(message, locale) for code, message in SKIPPED_REASON_MESSAGES.items()}


def warning_text(warning: LinkedWarning, locale: str) -> str:
    """One translated suggestion warning; unknown codes get a generic sentence."""
    if warning.code == "scaled_months":
        return gettext(
            "Only %(months)d of the last 12 full months have data. The yearly amount is "
            "scaled from those months.",
            locale,
        ) % {"months": warning.months}
    if warning.code == "linked_data_older":
        return gettext(
            "The latest included data is from %(month)s, before the window ends on %(end)s.",
            locale,
        ) % {
            "month": format_day(warning.month, locale) if warning.month else "",
            "end": format_day(warning.window_end, locale) if warning.window_end else "",
        }
    if warning.code == "start_year_not_current":
        return gettext(
            "The plan starts in %(start)d but the data is as of %(current)d.",
            locale,
        ) % {"start": warning.start_year or 0, "current": warning.current_year or 0}
    if warning.code == "stale_price":
        return gettext("The latest price for %(name)s is stale.", locale) % {"name": warning.detail}
    if warning.code == "stale_fx":
        return gettext("The latest FX rate for %(name)s is stale.", locale) % {
            "name": warning.detail
        }
    if warning.code == "linked_portfolio_zero":
        return gettext(
            "The portfolio sources sum to zero today. The suggestion is zero; enter a value "
            "manually if that is not what you expect.",
            locale,
        )
    if warning.code == "non_investable_classes":
        return ngettext(
            "%(count)d non-investable asset class is excluded from the portfolio suggestion.",
            "%(count)d non-investable asset classes are excluded from the portfolio suggestion.",
            warning.count,
            locale,
        ) % {"count": warning.count}
    if warning.code == "linked_items_skipped":
        return ngettext(
            "%(count)d linked net-worth item is skipped to avoid double counting.",
            "%(count)d linked net-worth items are skipped to avoid double counting.",
            warning.count,
            locale,
        ) % {"count": warning.count}
    return gettext(WARNING_FALLBACK, locale)


__all__ = [
    "BLOCK_FALLBACK",
    "SKIPPED_REASON_MESSAGES",
    "SUGGESTION_BLOCK_MESSAGES",
    "WARNING_FALLBACK",
    "block_messages",
    "skipped_reason_labels",
    "warning_text",
]
