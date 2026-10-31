"""The Taxes page, the new/edit tax page with its example panel, and other taxes (ADR-0022)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.recurrence import Frequency, RecurrenceRule
from assetboard.domain.taxes import PaymentTiming, TaxBase, TaxKind
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, TransactionDirection, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_day_of_month
from assetboard.i18n.numbers import (
    format_amount_input,
    format_money,
    format_quantity_input,
    format_rate_basis_points,
    format_rate_short,
    parse_amount,
)
from assetboard.services import scheduled, taxes
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    return message


ERRORS = {
    "invalid_name": _("Enter a name."),
    "invalid_rule": _("Choose who pays the tax to the tax office."),
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "invalid_rate": _("Enter a non-negative percentage with at most two decimal places."),
    "invalid_value": _("Enter a non-negative fixed amount in the workspace currency."),
    "invalid_schedule": _("Enter a payment day between 1 and 31 and choose the month."),
    "invalid_category": _("Choose an expense category that is not a transfer."),
    "version_not_newer": _("A rate change must start after the latest rate."),
    "archived": _("Archived taxes cannot be changed."),
    "kind_locked": _(
        "Income already posted was calculated with this tax, so who pays it and its base "
        "cannot change. Create a new tax instead."
    ),
    "invalid_other_tax": _("Enter a name, a positive amount, a due date and a category."),
    "failed": _("The tax could not be saved. Check the form and try again."),
}

# "How often" choices for other taxes: (frequency, interval).
OTHER_FREQUENCIES: dict[str, tuple[Frequency, int]] = {
    "yearly": (Frequency.YEARLY, 1),
    "quarterly": (Frequency.MONTHLY, 3),
    "monthly": (Frequency.MONTHLY, 1),
    "once": (Frequency.ONCE, 1),
}


def _require_taxes(ctx: Context) -> None:
    """Tax pages exist only while the workspace's Taxes switch is on."""
    if not ctx.workspace.taxes_enabled:
        raise NotFound


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _optional_id(value: str) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


async def _values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def _value(values: dict[str, str], locale: str, currency: str) -> PercentOrFixed:
    mode = values.get("mode")
    if mode == "percent":
        try:
            # A locale-aware percentage ("23", "12,5", "0.01") with at most two decimals,
            # stored as integer basis points.
            percent = parse_amount(values.get("rate_percent", ""), locale)
            basis_points = percent.scaleb(2)
            if percent < 0 or basis_points != basis_points.to_integral_value():
                raise ValueError
            return PercentOrFixed.percent(int(basis_points))
        except ValueError:
            raise taxes.TaxRuleError("invalid_rate") from None
    if mode == "fixed":
        try:
            amount = parse_amount(values.get("fixed_amount", ""), locale)
            if amount < 0:
                raise ValueError
            minor = 0 if amount == 0 else amount_minor(amount, currency)
            if minor > MAX_AMOUNT_MINOR:
                raise ValueError
            return PercentOrFixed.fixed_amount(Money(minor, currency))
        except ValueError:
            raise taxes.TaxRuleError("invalid_value") from None
    raise taxes.TaxRuleError("invalid_value")


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise taxes.TaxRuleError("invalid_date") from None


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except ValueError:
        return default


def _error(request: Request, code: str | None) -> str | None:
    if not code:
        return None
    return gettext(ERRORS.get(code, ERRORS["failed"]), request.state.locale)


def _redirect(request: Request, error: str | None = None, form: str | None = None) -> Response:
    """Back to the Taxes page; ``form`` names the collapsed form to reopen with the error."""
    target = request.url_for("tax_rules").path
    query = ({"error": error} | ({"form": form} if form else {})) if error else {}
    return RedirectResponse(target + ("?" + urlencode(query) if query else ""), status_code=303)


def _rate_label(value: PercentOrFixed | None, locale: str) -> str:
    if value is None:
        return ""
    if value.mode == "percent":
        assert value.rate_bp is not None
        return format_rate_short(value.rate_bp, locale)
    assert value.fixed is not None
    return format_money(value.fixed, locale)


def _common(request: Request) -> dict[str, object]:
    return {
        "format_day": format_day,
        "format_day_of_month": format_day_of_month,
        "format_money": format_money,
        "format_amount_input": format_amount_input,
        "format_rate_basis_points": format_rate_basis_points,
        "rate_label": _rate_label,
    }


@router.get("/taxes", name="tax_rules")
async def rules_page(request: Request, ctx: CurrentContext) -> Response:
    _require_taxes(ctx)
    factory = request.app.state.session_factory
    rows, others, categories = await run_in_threadpool(
        lambda: (
            taxes.list_rules(factory, ctx),
            scheduled.list_tax_items(factory, ctx),
            list_categories(factory, ctx),
        )
    )
    error = request.query_params.get("error")
    return render_template(
        request,
        "taxes/rules.html",
        context={
            "rules": [rule for rule in rows if not rule.archived],
            "archived_rules": [rule for rule in rows if rule.archived],
            "others": others,
            "expense_categories": [
                category
                for category in categories.expense
                if category.treatment != "transfer" and not category.archived
            ],
            "can_edit": has_role(ctx, Role.EDITOR),
            "error": _error(request, error),
            "open_form": request.query_params.get("form") if error else None,
            "today": ctx.clock.today(ctx.workspace.timezone).isoformat(),
            **_common(request),
        },
    )


@router.get("/taxes/calculator", name="tax_calculator")
async def calculator(request: Request, ctx: CurrentContext) -> Response:
    """The former calculator page: its function is the example panel of the tax form now."""
    return RedirectResponse(request.url_for("tax_rules").path, status_code=303)


def _form_values(values: dict[str, str]) -> dict[str, str]:
    """The tax form's submitted values with defaults, for re-rendering and the example."""
    return {
        "name": values.get("name", ""),
        "kind": values.get("kind", TaxKind.SELF_PAID.value),
        "base": values.get("base", TaxBase.NET.value),
        "mode": values.get("mode", "percent"),
        "rate_percent": values.get("rate_percent", ""),
        "fixed_amount": values.get("fixed_amount", ""),
        "payment_day": values.get("payment_day", "10"),
        "payment_timing": values.get("payment_timing", PaymentTiming.NEXT_MONTH.value),
        "payment_category_id": values.get("payment_category_id", ""),
    }


def _example(request: Request, ctx: Context, values: dict[str, str]) -> taxes.TaxExample | None:
    """The example for the form's values; None while they are incomplete or invalid."""
    try:
        return taxes.example(
            ctx,
            name=values["name"].strip() or gettext(_("This tax"), request.state.locale),
            kind=TaxKind(values["kind"]),
            base=TaxBase(values["base"]),
            value=_value(values, request.state.locale, ctx.workspace.base_currency),
            payment_day=_int(values["payment_day"], 10),
            payment_timing=PaymentTiming(values["payment_timing"]),
        )
    except (taxes.TaxRuleError, ValueError):
        return None


async def _render_form(
    request: Request,
    ctx: Context,
    values: dict[str, str],
    *,
    rule: taxes.TaxRuleView | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    categories = await run_in_threadpool(list_categories, request.app.state.session_factory, ctx)
    return render_template(
        request,
        "taxes/form.html",
        context={
            "values": values,
            "rule": rule,
            "example": _example(request, ctx, values),
            "expense_categories": [
                category
                for category in categories.expense
                if category.treatment != "transfer"
                and (not category.archived or str(category.id) == values["payment_category_id"])
            ],
            "error": _error(request, error),
            **_common(request),
        },
        status_code=status_code,
    )


@router.get("/taxes/new", name="tax_rule_new")
async def new_page(request: Request, ctx: CurrentContext) -> Response:
    _require_taxes(ctx)
    return await _render_form(request, ctx, _form_values({}))


@router.post("/taxes/example", name="tax_example")
async def example_partial(request: Request, ctx: CurrentContext) -> Response:
    """The example panel for the form's current values (htmx swaps it on change)."""
    _require_taxes(ctx)
    values = _form_values(await _values(request))
    return render_template(
        request,
        "taxes/_example.html",
        context={"example": _example(request, ctx, values), "values": values, **_common(request)},
    )


@router.post("/taxes", name="tax_rule_create")
async def create(request: Request, ctx: CurrentContext) -> Response:
    _require_taxes(ctx)
    values = _form_values(await _values(request))
    try:
        value = _value(values, request.state.locale, ctx.workspace.base_currency)
        await run_in_threadpool(
            lambda: taxes.create_rule(
                request.app.state.session_factory,
                ctx,
                value=value,
                name=values["name"],
                kind=values["kind"],
                base=values["base"],
                payment_day=_int(values["payment_day"]),
                payment_timing=values["payment_timing"],
                payment_category_id=_optional_id(values["payment_category_id"]),
            )
        )
    except taxes.TaxRuleError as exc:
        return await _render_form(request, ctx, values, error=exc.code, status_code=400)
    return _redirect(request)


def _edit_values(rule: taxes.TaxRuleView, locale: str) -> dict[str, str]:
    current = rule.current
    rate = ""
    fixed = ""
    if current is not None and current.mode == "percent" and current.rate_bp is not None:
        rate = format_quantity_input(Decimal(current.rate_bp).scaleb(-2), locale)
    elif current is not None and current.fixed is not None:
        fixed = format_amount_input(current.fixed, locale)
    return {
        "name": rule.name,
        "kind": rule.kind.value,
        "base": rule.base.value,
        "mode": current.mode if current is not None else "percent",
        "rate_percent": rate,
        "fixed_amount": fixed,
        "payment_day": str(rule.payment_day),
        "payment_timing": rule.payment_timing.value,
        "payment_category_id": str(rule.payment_category_id or ""),
    }


async def _rule_or_404(request: Request, ctx: Context, rule_id: str) -> taxes.TaxRuleView:
    rule = await run_in_threadpool(
        taxes.get_rule, request.app.state.session_factory, ctx, _id(rule_id)
    )
    if rule.archived:
        raise NotFound
    return rule


@router.get("/taxes/{rule_id}/edit", name="tax_rule_edit")
async def edit_page(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    _require_taxes(ctx)
    rule = await _rule_or_404(request, ctx, rule_id)
    return await _render_form(request, ctx, _edit_values(rule, request.state.locale), rule=rule)


@router.post("/taxes/{rule_id}/edit", name="tax_rule_update")
async def update(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    _require_taxes(ctx)
    rule = await _rule_or_404(request, ctx, rule_id)
    submitted = await _values(request)
    values = _form_values(submitted)
    # The rate is not edited here (rate changes add versions): keep the stored one.
    stored = _edit_values(rule, request.state.locale)
    values.update({key: stored[key] for key in ("mode", "rate_percent", "fixed_amount")})
    if rule.locked:
        values.update({"kind": stored["kind"], "base": stored["base"]})
    try:
        await run_in_threadpool(
            lambda: taxes.update_rule(
                request.app.state.session_factory,
                ctx,
                rule.id,
                name=values["name"],
                kind=values["kind"],
                base=values["base"],
                notes=rule.notes,
                payment_day=_int(values["payment_day"]),
                payment_timing=values["payment_timing"],
                payment_category_id=_optional_id(values["payment_category_id"]),
            )
        )
    except taxes.TaxRuleError as exc:
        return await _render_form(request, ctx, values, rule=rule, error=exc.code, status_code=400)
    return _redirect(request)


@router.post("/taxes/{rule_id}/versions", name="tax_rule_version")
async def add_version(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    _require_taxes(ctx)
    values = await _values(request)
    try:
        await run_in_threadpool(
            lambda: taxes.add_version(
                request.app.state.session_factory,
                ctx,
                _id(rule_id),
                effective_from=_day(values.get("effective_from", "")),
                value=_value(values, request.state.locale, ctx.workspace.base_currency),
            )
        )
    except taxes.TaxRuleError as exc:
        return _redirect(request, exc.code, f"rates-{rule_id}")
    except ValueError:
        return _redirect(request, "invalid_value", f"rates-{rule_id}")
    return _redirect(request)


@router.post("/taxes/{rule_id}/archive", name="tax_rule_archive")
async def archive(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    _require_taxes(ctx)
    try:
        await run_in_threadpool(
            taxes.archive_rule, request.app.state.session_factory, ctx, _id(rule_id)
        )
    except taxes.TaxRuleError as exc:
        return _redirect(request, exc.code)
    return _redirect(request)


def _other_rule(due: date, how_often: str) -> RecurrenceRule:
    freq, interval = OTHER_FREQUENCIES[how_often]
    if freq == Frequency.ONCE:
        return RecurrenceRule(start_date=due, freq=freq)
    return RecurrenceRule(
        start_date=due,
        freq=freq,
        interval=interval,
        month_day=due.day,
        month=due.month,
    )


@router.post("/taxes/other", name="tax_other_create")
async def create_other(request: Request, ctx: CurrentContext) -> Response:
    """ "Add another tax": a fixed-amount expense recurring item marked as a tax."""
    _require_taxes(ctx)
    values = await _values(request)
    currency = ctx.workspace.base_currency
    try:
        due = date.fromisoformat(values.get("due_date", ""))
        amount = parse_amount(values.get("amount", ""), request.state.locale)
        minor = amount_minor(amount, currency)
        item = scheduled.ScheduledInput(
            name=values.get("name", ""),
            direction=TransactionDirection.EXPENSE,
            category_id=_id(values.get("category_id", "")),
            amount=Money(minor, currency),
            rule=_other_rule(due, values.get("how_often", "")),
        )
        await run_in_threadpool(
            lambda: scheduled.save_item(
                request.app.state.session_factory,
                ctx,
                item,
                request.app.state.settings,
                require_kind="expense",
                is_tax=True,
            )
        )
    except (ValueError, KeyError, ArithmeticError, NotFound):
        return _redirect(request, "invalid_other_tax", "other-tax")
    return _redirect(request)
