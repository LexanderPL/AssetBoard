from __future__ import annotations

from datetime import date
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed
from assetboard.domain.taxes import RecordAs
from assetboard.domain.transactions import MAX_AMOUNT_MINOR, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_amount_input, format_rate_basis_points, parse_amount
from assetboard.services import taxes
from assetboard.services.context import Role, has_role
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    return message


ERRORS = {
    "invalid_name": _("Enter a name."),
    "invalid_rule": _("Choose a valid tax effect and base."),
    "invalid_date": _("Enter a valid effective date between 1900 and 2199."),
    "invalid_rate": _("Enter a non-negative percentage with at most two decimal places."),
    "invalid_value": _("Enter a non-negative fixed amount in the workspace currency."),
    "invalid_amount": _("Enter a non-negative net amount in the workspace currency."),
    "version_not_newer": _("A new version must start after the latest existing version."),
    "archived": _("Archived tax rules cannot be changed."),
    "invalid_record_as": _("Choose how the income is recorded."),
    "failed": _("The tax rule could not be saved. Check the form and try again."),
}


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


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


def _redirect(request: Request, error: str | None = None) -> Response:
    target = request.url_for("tax_rules").path
    return RedirectResponse(
        target + ("?" + urlencode({"error": error}) if error else ""), status_code=303
    )


@router.get("/taxes", name="tax_rules")
async def rules_page(request: Request, ctx: CurrentContext) -> Response:
    rows = await run_in_threadpool(taxes.list_rules, request.app.state.session_factory, ctx)
    error = request.query_params.get("error")
    return render_template(
        request,
        "taxes/rules.html",
        context={
            "rules": rows,
            "can_edit": has_role(ctx, Role.EDITOR),
            "error": gettext(ERRORS.get(error, ERRORS["failed"]), request.state.locale)
            if error
            else None,
            "today": ctx.clock.today(ctx.workspace.timezone).isoformat(),
            "format_day": format_day,
            "format_amount_input": format_amount_input,
            "format_rate_basis_points": format_rate_basis_points,
        },
    )


@router.post("/taxes", name="tax_rule_create")
async def create(request: Request, ctx: CurrentContext) -> Response:
    values = await _values(request)
    try:
        await run_in_threadpool(
            lambda: taxes.create_rule(
                request.app.state.session_factory,
                ctx,
                name=values.get("name", ""),
                effect=values.get("effect", ""),
                base=values.get("base", ""),
                notes=values.get("notes", ""),
                effective_from=_day(values.get("effective_from", "")),
                value=_value(values, request.state.locale, ctx.workspace.base_currency),
            )
        )
    except taxes.TaxRuleError as exc:
        return _redirect(request, exc.code)
    except ValueError:
        return _redirect(request, "invalid_rule")
    return _redirect(request)


@router.post("/taxes/{rule_id}/edit", name="tax_rule_update")
async def update(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    values = await _values(request)
    try:
        await run_in_threadpool(
            lambda: taxes.update_rule(
                request.app.state.session_factory,
                ctx,
                _id(rule_id),
                name=values.get("name", ""),
                notes=values.get("notes", ""),
            )
        )
    except taxes.TaxRuleError as exc:
        return _redirect(request, exc.code)
    return _redirect(request)


@router.post("/taxes/{rule_id}/versions", name="tax_rule_version")
async def add_version(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
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
        return _redirect(request, exc.code)
    except ValueError:
        return _redirect(request, "invalid_value")
    return _redirect(request)


@router.post("/taxes/{rule_id}/archive", name="tax_rule_archive")
async def archive(request: Request, ctx: CurrentContext, rule_id: str) -> Response:
    try:
        await run_in_threadpool(
            taxes.archive_rule, request.app.state.session_factory, ctx, _id(rule_id)
        )
    except taxes.TaxRuleError as exc:
        return _redirect(request, exc.code)
    return _redirect(request)


@router.get("/taxes/calculator", name="tax_calculator")
async def calculator(request: Request, ctx: CurrentContext) -> Response:
    values = {
        "net": request.query_params.get("net", ""),
        "day": request.query_params.get("day", ctx.clock.today(ctx.workspace.timezone).isoformat()),
        "record_as": request.query_params.get("record_as", RecordAs.CASH_RECEIVED.value),
    }
    calculation = None
    error = request.query_params.get("error")
    try:
        record_as = RecordAs(values["record_as"])
    except ValueError:
        record_as = None
        error = "invalid_record_as"
    if values["net"] and record_as is not None:
        try:
            net_decimal = parse_amount(values["net"], request.state.locale)
            if net_decimal < 0:
                raise ValueError
            net_minor = (
                0 if net_decimal == 0 else amount_minor(net_decimal, ctx.workspace.base_currency)
            )
            calculation = await run_in_threadpool(
                lambda: taxes.preview(
                    request.app.state.session_factory,
                    ctx,
                    net=Money(net_minor, ctx.workspace.base_currency),
                    day=_day(values["day"]),
                    record_as=record_as,
                )
            )
        except taxes.TaxRuleError as exc:
            error = exc.code
        except ValueError:
            error = "invalid_amount"
    return render_template(
        request,
        "taxes/calculator.html",
        context={
            "values": values,
            "calculation": calculation,
            "error": gettext(ERRORS.get(error, ERRORS["failed"]), request.state.locale)
            if error
            else None,
            "record_as_options": [
                (RecordAs.CASH_RECEIVED.value, _("Cash received (gross)")),
                (RecordAs.NET.value, _("Net")),
                (RecordAs.TAKE_HOME.value, _("Take-home")),
            ],
            "format_day": format_day,
            "format_amount_input": format_amount_input,
        },
    )


@router.post("/taxes/calculator", name="tax_calculate")
async def calculate(request: Request, ctx: CurrentContext) -> Response:
    values = await _values(request)
    params = {
        "net": values.get("net", ""),
        "day": values.get("day", ""),
        "record_as": values.get("record_as", ""),
    }
    try:
        RecordAs(params["record_as"])
    except ValueError:
        params["error"] = "invalid_record_as"
    return RedirectResponse(
        request.url_for("tax_calculator").path + "?" + urlencode(params), status_code=303
    )
