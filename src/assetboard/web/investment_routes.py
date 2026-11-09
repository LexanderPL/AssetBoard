"""Investments page (F6.1): asset classes, instruments and allocation weights.

Authorization lives in ``services.investments``. Every state-changing route is covered by the
router-level ``require_csrf`` dependency. HTMX requests get the tab's body fragment; plain form
posts redirect (303) after success. Allocation weights are entered as percentages with at most
two decimals and stored as integer basis points; the server validates the total, while the page's
small script mirrors it for the user.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.instruments import (
    MAX_ASSET_CLASS_NAME_LENGTH,
    MAX_INSTRUMENT_NAME_LENGTH,
    MAX_SYMBOL_LENGTH,
    MAX_UNIT_LABEL_LENGTH,
    RISK_TIER_LABELS,
    RISK_TIER_SUBTITLES,
    TYPE_LABELS,
    InstrumentType,
    InvalidAllocation,
    RiskTier,
    percent_to_bp,
)
from assetboard.i18n import gettext
from assetboard.i18n.numbers import (
    decimal_symbol,
    format_money,
    format_quantity_input,
    parse_amount,
)
from assetboard.services.context import Context, Role, has_role
from assetboard.services.investment_income import investment_income_page
from assetboard.services.investments import (
    AssetClassView,
    InstrumentView,
    InvestmentError,
    InvestmentPage,
    apply_suggested_risk_tiers,
    archive_asset_class,
    archive_instrument,
    create_asset_class,
    create_instrument,
    get_instrument,
    investment_page,
    move_asset_class,
    restore_asset_class,
    restore_instrument,
    seed_asset_classes,
    update_asset_class,
    update_instrument,
)
from assetboard.services.workspace_options import currency_codes
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
DEFAULT_COLOR = "#64748b"


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


CLASS_ERRORS = {
    "invalid_name": _("Enter a name of 1 to 80 characters, without control characters."),
    "duplicate_name": _("Another active asset class already has this name."),
    "invalid_color": _("Choose a colour."),
    "archived": _("Restore the asset class before changing it."),
    "already_seeded": _("The suggested asset classes can only be added when none are active."),
    "invalid_direction": _("The change could not be made. Reload the page and try again."),
    "invalid_risk_tier": _("Choose a valid risk tier."),
}
INSTRUMENT_ERRORS = {
    "invalid_name": _("Enter a name of 1 to 200 characters, without control characters."),
    "duplicate_name": _("Another active instrument already has this name."),
    "invalid_symbol": _("Enter a symbol of at most 32 characters."),
    "invalid_type": _("Choose an instrument type."),
    "invalid_currency": _("Choose a supported currency."),
    "invalid_unit_label": _("Enter a unit label of 1 to 32 characters."),
    "allocation_required": _("Assign at least one asset class."),
    "allocation_total": _("Allocation weights must total exactly 100%."),
    "invalid_weight": _("Enter a weight between 0 and 100% with at most two decimals."),
    "duplicate_class": _("Each asset class can appear only once."),
    "class_unknown": _("Choose an asset class from this workspace."),
    "class_archived": _("This asset class is archived; restore it before allocating to it."),
    "archived": _("Restore the instrument before changing it."),
    "currency_locked": _("The quote currency cannot change: transactions exist."),
    "type_locked": _("The type cannot change: transactions exist."),
    "invalid_provider": _("Choose a valid price provider for this instrument."),
    "provider_disabled": _("Enable this provider in Providers before assigning it."),
    "provider_unsupported": _(
        "This provider cannot price this instrument type, currency or symbol."
    ),
    "provider_symbol_required": _("Enter the provider symbol."),
    "invalid_provider_symbol": _("Enter a valid provider symbol."),
    "invalid_risk_tier": _("Choose a valid risk tier override."),
}
FALLBACK_ERROR = _("The change could not be made. Reload the page and try again.")
CLASS_NOTICES = {
    "class_created": _("Asset class added."),
    "class_saved": _("Asset class saved."),
    "class_archived": _("Asset class archived."),
    "class_restored": _("Asset class restored."),
    "classes_seeded": _("Suggested asset classes added."),
    "risk_tiers_suggested": _("Suggested risk tiers applied."),
    "risk_tiers_complete": _("Every matching asset class already has a risk tier."),
}
INSTRUMENT_NOTICES = {
    "instrument_created": _("Instrument added."),
    "instrument_saved": _("Instrument saved."),
    "instrument_archived": _("Instrument archived."),
    "instrument_restored": _("Instrument restored."),
    "classes_seeded": _("Suggested asset classes added."),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _type_options(request: Request) -> list[tuple[str, str]]:
    return [
        (instrument_type.value, _t(request, TYPE_LABELS[instrument_type]))
        for instrument_type in InstrumentType
    ]


def _risk_tier_options(request: Request, *, blank_label: str) -> list[tuple[str, str]]:
    return [("", blank_label)] + [
        (tier.value, _t(request, RISK_TIER_LABELS[tier])) for tier in RiskTier
    ]


def _risk_tier_labels(request: Request) -> dict[str, str]:
    return {tier.value: _t(request, RISK_TIER_LABELS[tier]) for tier in RiskTier}


def _risk_tier_subtitles(request: Request) -> dict[str, str]:
    return {tier.value: _t(request, RISK_TIER_SUBTITLES[tier]) for tier in RISK_TIER_SUBTITLES}


def _weight_text(weight_bp: int, locale: str) -> str:
    return format_quantity_input(Decimal(weight_bp).scaleb(-2), locale)


def _class_values(item: AssetClassView | None = None) -> dict[str, object]:
    if item is None:
        return {
            "name": "",
            "color": DEFAULT_COLOR,
            "investable": True,
            "risk_tier": "",
        }
    return {
        "name": item.name,
        "color": item.color,
        "investable": item.investable,
        "risk_tier": item.risk_tier or "",
    }


def _instrument_values(
    item: InstrumentView | None, base_currency: str, locale: str
) -> dict[str, object]:
    if item is None:
        return {
            "name": "",
            "symbol": "",
            "type": InstrumentType.EQUITY.value,
            "quote_currency": base_currency,
            "unit_label": "",
            "weights": {},
            "provider_key": "",
            "provider_symbol": "",
            "risk_tier_override": "",
        }
    return {
        "name": item.name,
        "symbol": item.symbol or "",
        "type": item.type,
        "quote_currency": item.quote_currency,
        "unit_label": item.unit_label,
        "provider_key": item.provider_key or "",
        "provider_symbol": item.provider_symbol or "",
        "risk_tier_override": item.risk_tier_override or "",
        "weights": {
            str(allocation.asset_class_id): _weight_text(allocation.weight_bp, locale)
            for allocation in item.allocations
        },
    }


def _weights(values: dict[str, object]) -> dict[str, str]:
    raw = values.get("weights")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _form_classes(
    page: InvestmentPage,
    item: InstrumentView | None,
    weights: dict[str, str],
) -> list[tuple[AssetClassView, str, bool]]:
    """Every active class plus archived classes already allocated to this instrument."""
    allocated = {allocation.asset_class_id for allocation in item.allocations} if item else set()
    rows = [
        (asset_class, str(weights.get(str(asset_class.id), "")), False)
        for asset_class in page.classes
    ]
    rows.extend(
        (
            asset_class,
            str(weights.get(str(asset_class.id), "")),
            True,
        )
        for asset_class in page.archived_classes
        if asset_class.id in allocated
    )
    return rows


def _class_body_context(
    request: Request,
    ctx: Context,
    page: InvestmentPage,
    *,
    notice: str | None = None,
    error: str | None = None,
    create_values: dict[str, object] | None = None,
    create_errors: dict[str, str] | None = None,
    editing: UUID | None = None,
    edit_values: dict[str, object] | None = None,
    edit_errors: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "page": page,
        "can_edit": has_role(ctx, Role.EDITOR),
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": page.coingecko_enabled,
        "notice": _t(request, CLASS_NOTICES[notice]) if notice in CLASS_NOTICES else None,
        "error": _t(request, error) if error else None,
        "create_values": create_values or _class_values(),
        "create_errors": {f: _t(request, m) for f, m in (create_errors or {}).items()},
        "editing": editing,
        "edit_values": edit_values,
        "edit_errors": {f: _t(request, m) for f, m in (edit_errors or {}).items()},
        "max_name_length": MAX_ASSET_CLASS_NAME_LENGTH,
        "risk_tier_options": _risk_tier_options(request, blank_label=_("Unassigned")),
        "risk_tier_labels": _risk_tier_labels(request),
    }


def _instrument_body_context(
    request: Request,
    ctx: Context,
    page: InvestmentPage,
    *,
    notice: str | None = None,
    error: str | None = None,
    create_values: dict[str, object] | None = None,
    create_errors: dict[str, str] | None = None,
    editing: UUID | None = None,
    edit_values: dict[str, object] | None = None,
    edit_errors: dict[str, str] | None = None,
    editing_instrument: InstrumentView | None = None,
) -> dict[str, object]:
    values = create_values or _instrument_values(
        None, ctx.workspace.base_currency, request.state.locale
    )
    edit_weights = _weights(edit_values) if edit_values else {}
    create_weights = _weights(values)
    return {
        "page": page,
        "can_edit": has_role(ctx, Role.EDITOR),
        "can_manage_providers": has_role(ctx, Role.OWNER),
        "coingecko_enabled": page.coingecko_enabled,
        "provider_choices": page.provider_choices,
        "notice": _t(request, INSTRUMENT_NOTICES[notice]) if notice in INSTRUMENT_NOTICES else None,
        "error": _t(request, error) if error else None,
        "create_values": values,
        "create_errors": {f: _t(request, m) for f, m in (create_errors or {}).items()},
        "editing": editing,
        "editing_instrument": editing_instrument,
        "edit_values": edit_values,
        "edit_errors": {f: _t(request, m) for f, m in (edit_errors or {}).items()},
        "create_rows": _form_classes(page, None, create_weights),
        "edit_rows": _form_classes(page, editing_instrument, edit_weights) if edit_values else [],
        "max_name_length": MAX_INSTRUMENT_NAME_LENGTH,
        "max_symbol_length": MAX_SYMBOL_LENGTH,
        "max_unit_label_length": MAX_UNIT_LABEL_LENGTH,
        "type_options": _type_options(request),
        "risk_tier_options": _risk_tier_options(request, blank_label=_("Use the class tier")),
        "risk_tier_labels": _risk_tier_labels(request),
        "risk_tier_subtitles": _risk_tier_subtitles(request),
        "currency_options": currency_codes(),
        "weight_text": lambda weight_bp: _weight_text(weight_bp, request.state.locale),
        "decimal_separator": decimal_symbol(request.state.locale),
    }


def _render_classes(
    request: Request,
    ctx: Context,
    status_code: int = 200,
    **kwargs: Any,
) -> Response:
    page = investment_page(request.app.state.session_factory, ctx, locale=request.state.locale)
    context = _class_body_context(request, ctx, page, **kwargs)
    template = (
        "investments/_classes_body.html"
        if is_htmx_fragment_request(request)
        else "investments/classes.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _render_instruments(
    request: Request,
    ctx: Context,
    status_code: int = 200,
    **kwargs: Any,
) -> Response:
    page = investment_page(request.app.state.session_factory, ctx, locale=request.state.locale)
    context = _instrument_body_context(request, ctx, page, **kwargs)
    template = (
        "investments/_instruments_body.html"
        if is_htmx_fragment_request(request)
        else "investments/instruments.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _classes_done(request: Request, ctx: Context, notice: str) -> Response:
    if is_htmx_fragment_request(request):
        return _render_classes(request, ctx, notice=notice)
    return RedirectResponse(
        request.url_for("investments_classes").path + f"?notice={notice}", status_code=303
    )


def _queue_provider_backfill(request: Request, ctx: Context, provider_key: str) -> None:
    key = provider_key.strip().lower()
    if not key:
        return
    jobs = getattr(request.app.state, "recurring_jobs", None)
    if jobs is not None:
        jobs.request_backfill(ctx.workspace.id, key)


def _instruments_done(request: Request, ctx: Context, notice: str) -> Response:
    if is_htmx_fragment_request(request):
        return _render_instruments(request, ctx, notice=notice)
    return RedirectResponse(
        request.url_for("investments_instruments").path + f"?notice={notice}", status_code=303
    )


def _error_message(mapping: dict[str, str], exc: InvestmentError) -> str:
    return mapping.get(exc.code, FALLBACK_ERROR)


def _find_page_item(page: InvestmentPage, item_id: UUID) -> Any:
    for item in (*page.classes, *page.archived_classes):
        if item.id == item_id:
            return item
    raise NotFound


def _instrument_or_404(
    request: Request, ctx: Context, instrument_id: UUID, *, locale: str
) -> InstrumentView:
    return get_instrument(request.app.state.session_factory, ctx, instrument_id, locale=locale)


async def _allocation_input(
    request: Request, locale: str
) -> tuple[list[tuple[UUID, int]], dict[str, str], str | None]:
    """Parse ``weight_<class id>`` fields; blank and ``0`` mean "not allocated"."""
    form = await request.form()
    rows: list[tuple[UUID, int]] = []
    raw: dict[str, str] = {}
    for key, value in form.multi_items():
        if not key.startswith("weight_") or not isinstance(value, str):
            continue
        text = key.removeprefix("weight_")
        try:
            class_id = UUID(text)
        except ValueError:
            return [], raw, "class_unknown"
        raw[text] = value
        stripped = value.strip()
        if not stripped:
            continue
        try:
            weight_bp = percent_to_bp(parse_amount(stripped, locale))
        except (ValueError, InvalidAllocation):
            return [], raw, "invalid_weight"
        if weight_bp:
            rows.append((class_id, weight_bp))
    return rows, raw, None


@router.get("/investments", name="investments")
async def investments_root(request: Request, ctx: CurrentContext) -> Response:
    return RedirectResponse(request.url_for("investments_instruments").path, status_code=307)


@router.get("/investments/income", name="investments_income")
async def income_page(request: Request, ctx: CurrentContext) -> Response:
    page = await run_in_threadpool(investment_income_page, request.app.state.session_factory, ctx)
    return await run_in_threadpool(
        lambda: render_template(
            request,
            "investments/income.html",
            context={
                "page": page,
                "locale": request.state.locale,
                "format_money": format_money,
            },
        )
    )


@router.get("/investments/instruments", name="investments_instruments")
async def instruments_page(
    request: Request, ctx: CurrentContext, notice: str = "", edit: str = ""
) -> Response:
    locale = request.state.locale
    page = await run_in_threadpool(
        lambda: investment_page(request.app.state.session_factory, ctx, locale=locale)
    )
    editing: UUID | None = None
    values: dict[str, object] | None = None
    editing_instrument: InstrumentView | None = None
    if edit and has_role(ctx, Role.EDITOR):
        editing = _parse_uuid(edit)
        item = next(
            (row for row in page.instruments if row.id == editing),
            None,
        )
        if item is not None:
            editing_instrument = item
            values = _instrument_values(item, ctx.workspace.base_currency, locale)
        else:
            editing = None
    return await run_in_threadpool(
        lambda: _render_instruments(
            request,
            ctx,
            notice=notice or None,
            editing=editing,
            edit_values=values,
            editing_instrument=editing_instrument,
        )
    )


@router.get(
    "/investments/instruments/{instrument_id}/edit",
    name="investments_instrument_edit",
)
async def instrument_edit(request: Request, ctx: CurrentContext, instrument_id: str) -> Response:
    """The instrument's row as an inline edit form (HTMX); a plain request gets the full page
    with the form in that row, scrolled to it by its anchor."""
    item_id = _parse_uuid(instrument_id)
    locale = request.state.locale
    if not is_htmx_fragment_request(request):
        path = request.url_for("investments_instruments").path
        return RedirectResponse(f"{path}?edit={item_id}#instrument-{item_id}", status_code=303)
    item = await run_in_threadpool(_instrument_or_404, request, ctx, item_id, locale=locale)
    if item.archived or not has_role(ctx, Role.EDITOR):
        return await run_in_threadpool(lambda: _render_instruments(request, ctx))
    return await run_in_threadpool(
        lambda: _render_instruments(
            request,
            ctx,
            editing=item.id,
            edit_values=_instrument_values(item, ctx.workspace.base_currency, locale),
            editing_instrument=item,
        )
    )


@router.get(
    "/investments/instruments/{instrument_id}/row",
    name="investments_instrument_row",
)
async def instrument_row(request: Request, ctx: CurrentContext, instrument_id: str) -> Response:
    """Cancel an inline edit: the list without the form (HTMX), or the plain page."""
    item_id = _parse_uuid(instrument_id)
    if not is_htmx_fragment_request(request):
        path = request.url_for("investments_instruments").path
        return RedirectResponse(f"{path}#instrument-{item_id}", status_code=303)
    await run_in_threadpool(_instrument_or_404, request, ctx, item_id, locale=request.state.locale)
    return await run_in_threadpool(lambda: _render_instruments(request, ctx))


@router.post("/investments/instruments", name="investments_instrument_create")
async def instrument_create(
    request: Request,
    ctx: CurrentContext,
    name: FormStr = "",
    symbol: FormStr = "",
    type: FormStr = "",
    quote_currency: FormStr = "",
    unit_label: FormStr = "",
    provider_key: FormStr = "",
    provider_symbol: FormStr = "",
    risk_tier_override: FormStr = "",
) -> Response:
    locale = request.state.locale
    rows, raw_weights, weight_error = await _allocation_input(request, locale)
    values = {
        "name": name,
        "symbol": symbol,
        "type": type,
        "quote_currency": quote_currency,
        "unit_label": unit_label,
        "provider_key": provider_key,
        "provider_symbol": provider_symbol,
        "risk_tier_override": risk_tier_override,
        "weights": raw_weights,
    }
    if weight_error:
        return await run_in_threadpool(
            lambda: _render_instruments(
                request,
                ctx,
                status_code=400,
                create_values=values,
                create_errors={"allocation": INSTRUMENT_ERRORS[weight_error]},
            )
        )
    try:
        view = await run_in_threadpool(
            lambda: create_instrument(
                request.app.state.session_factory,
                ctx,
                name=name,
                symbol=symbol,
                type=type,
                quote_currency=quote_currency,
                unit_label=unit_label,
                allocations=rows,
                locale=locale,
                provider_key=provider_key,
                provider_symbol=provider_symbol,
                risk_tier_override=risk_tier_override,
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_instruments(
                request,
                ctx,
                status_code=400,
                create_values=values,
                create_errors={error.field or "name": _error_message(INSTRUMENT_ERRORS, error)},
            )
        )
    _queue_provider_backfill(request, ctx, view.provider_key or "")
    return await run_in_threadpool(_instruments_done, request, ctx, "instrument_created")


@router.post(
    "/investments/instruments/{instrument_id}",
    name="investments_instrument_update",
)
async def instrument_update(
    request: Request,
    ctx: CurrentContext,
    instrument_id: str,
    name: FormStr = "",
    symbol: FormStr = "",
    type: FormStr = "",
    quote_currency: FormStr = "",
    unit_label: FormStr = "",
    provider_key: FormStr = "",
    provider_symbol: FormStr = "",
    risk_tier_override: FormStr = "",
) -> Response:
    locale = request.state.locale
    item_id = _parse_uuid(instrument_id)
    rows, raw_weights, weight_error = await _allocation_input(request, locale)
    values = {
        "name": name,
        "symbol": symbol,
        "type": type,
        "quote_currency": quote_currency,
        "unit_label": unit_label,
        "provider_key": provider_key,
        "provider_symbol": provider_symbol,
        "risk_tier_override": risk_tier_override,
        "weights": raw_weights,
    }
    if weight_error:
        item = await run_in_threadpool(_instrument_or_404, request, ctx, item_id, locale=locale)
        return await run_in_threadpool(
            lambda: _render_instruments(
                request,
                ctx,
                status_code=400,
                editing=item.id,
                edit_values=values,
                editing_instrument=item,
                edit_errors={"allocation": INSTRUMENT_ERRORS[weight_error]},
            )
        )
    try:
        view = await run_in_threadpool(
            lambda: update_instrument(
                request.app.state.session_factory,
                ctx,
                item_id,
                name=name,
                symbol=symbol,
                type=type,
                quote_currency=quote_currency,
                unit_label=unit_label,
                allocations=rows,
                locale=locale,
                provider_key=provider_key,
                provider_symbol=provider_symbol,
                risk_tier_override=risk_tier_override,
            )
        )
    except InvestmentError as exc:
        item = await run_in_threadpool(_instrument_or_404, request, ctx, item_id, locale=locale)
        if item.archived:
            return await run_in_threadpool(
                lambda error=exc: _render_instruments(
                    request,
                    ctx,
                    status_code=409,
                    error=_error_message(INSTRUMENT_ERRORS, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _render_instruments(
                request,
                ctx,
                status_code=400,
                editing=item.id,
                edit_values=values,
                editing_instrument=item,
                edit_errors={error.field or "name": _error_message(INSTRUMENT_ERRORS, error)},
            )
        )
    _queue_provider_backfill(request, ctx, view.provider_key or "")
    return await run_in_threadpool(_instruments_done, request, ctx, "instrument_saved")


@router.post(
    "/investments/instruments/{instrument_id}/archive",
    name="investments_instrument_archive",
)
async def instrument_archive(request: Request, ctx: CurrentContext, instrument_id: str) -> Response:
    try:
        await run_in_threadpool(
            lambda: archive_instrument(
                request.app.state.session_factory, ctx, _parse_uuid(instrument_id)
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_instruments(
                request,
                ctx,
                status_code=409,
                error=_error_message(INSTRUMENT_ERRORS, error),
            )
        )
    return await run_in_threadpool(_instruments_done, request, ctx, "instrument_archived")


@router.post(
    "/investments/instruments/{instrument_id}/restore",
    name="investments_instrument_restore",
)
async def instrument_restore(request: Request, ctx: CurrentContext, instrument_id: str) -> Response:
    try:
        await run_in_threadpool(
            lambda: restore_instrument(
                request.app.state.session_factory, ctx, _parse_uuid(instrument_id)
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_instruments(
                request,
                ctx,
                status_code=409,
                error=_error_message(INSTRUMENT_ERRORS, error),
            )
        )
    return await run_in_threadpool(_instruments_done, request, ctx, "instrument_restored")


@router.get("/investments/classes", name="investments_classes")
async def classes_page(
    request: Request, ctx: CurrentContext, notice: str = "", edit: str = ""
) -> Response:
    page = await run_in_threadpool(
        lambda: investment_page(request.app.state.session_factory, ctx, locale=request.state.locale)
    )
    editing: UUID | None = None
    values: dict[str, object] | None = None
    if edit and has_role(ctx, Role.EDITOR):
        editing = _parse_uuid(edit)
        item = next((row for row in page.classes if row.id == editing), None)
        if item is not None:
            values = _class_values(item)
        else:
            editing = None
    return await run_in_threadpool(
        lambda: _render_classes(
            request, ctx, notice=notice or None, editing=editing, edit_values=values
        )
    )


@router.post("/investments/classes/seed", name="investments_classes_seed")
async def classes_seed(request: Request, ctx: CurrentContext, return_to: FormStr = "") -> Response:
    try:
        await run_in_threadpool(
            lambda: seed_asset_classes(
                request.app.state.session_factory, ctx, locale=request.state.locale
            )
        )
    except InvestmentError as exc:
        if (
            is_htmx_fragment_request(request)
            and request.headers.get("HX-Target") == "investments-body"
        ):
            return await run_in_threadpool(
                lambda error=exc: _render_instruments(
                    request,
                    ctx,
                    status_code=409,
                    error=_error_message(CLASS_ERRORS, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request,
                ctx,
                status_code=409,
                error=_error_message(CLASS_ERRORS, error),
            )
        )
    if is_htmx_fragment_request(request) and request.headers.get("HX-Target") == "investments-body":
        return await run_in_threadpool(_instruments_done, request, ctx, "classes_seeded")
    if not is_htmx_fragment_request(request) and return_to == "instruments":
        return RedirectResponse(
            request.url_for("investments_instruments").path + "?notice=classes_seeded",
            status_code=303,
        )
    return await run_in_threadpool(_classes_done, request, ctx, "classes_seeded")


@router.post("/investments/classes", name="investments_class_create")
async def class_create(
    request: Request,
    ctx: CurrentContext,
    name: FormStr = "",
    color: FormStr = "",
    investable: FormStr = "",
    risk_tier: FormStr = "",
) -> Response:
    values = {
        "name": name,
        "color": color,
        "investable": investable == "1",
        "risk_tier": risk_tier,
    }
    try:
        await run_in_threadpool(
            lambda: create_asset_class(
                request.app.state.session_factory,
                ctx,
                name=name,
                color=color,
                investable=investable == "1",
                risk_tier=risk_tier,
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request,
                ctx,
                status_code=400,
                create_values=values,
                create_errors={error.field or "name": _error_message(CLASS_ERRORS, error)},
            )
        )
    return await run_in_threadpool(_classes_done, request, ctx, "class_created")


@router.post("/investments/classes/apply-risk-tiers", name="investments_classes_apply_risk_tiers")
async def class_apply_risk_tiers(request: Request, ctx: CurrentContext) -> Response:
    count = await run_in_threadpool(
        apply_suggested_risk_tiers,
        request.app.state.session_factory,
        ctx,
    )
    return await run_in_threadpool(
        _classes_done,
        request,
        ctx,
        "risk_tiers_suggested" if count else "risk_tiers_complete",
    )


@router.post("/investments/classes/{class_id}", name="investments_class_update")
async def class_update(
    request: Request,
    ctx: CurrentContext,
    class_id: str,
    name: FormStr = "",
    color: FormStr = "",
    investable: FormStr = "",
    risk_tier: FormStr = "",
) -> Response:
    item_id = _parse_uuid(class_id)
    values = {
        "name": name,
        "color": color,
        "investable": investable == "1",
        "risk_tier": risk_tier,
    }
    try:
        await run_in_threadpool(
            lambda: update_asset_class(
                request.app.state.session_factory,
                ctx,
                item_id,
                name=name,
                color=color,
                investable=investable == "1",
                risk_tier=risk_tier,
            )
        )
    except InvestmentError as exc:
        page = await run_in_threadpool(
            lambda: investment_page(
                request.app.state.session_factory, ctx, locale=request.state.locale
            )
        )
        item = _find_page_item(page, item_id)
        if item.archived:
            return await run_in_threadpool(
                lambda error=exc: _render_classes(
                    request,
                    ctx,
                    status_code=409,
                    error=_error_message(CLASS_ERRORS, error),
                )
            )
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request,
                ctx,
                status_code=400,
                editing=item.id,
                edit_values=values,
                edit_errors={error.field or "name": _error_message(CLASS_ERRORS, error)},
            )
        )
    return await run_in_threadpool(_classes_done, request, ctx, "class_saved")


@router.post("/investments/classes/{class_id}/archive", name="investments_class_archive")
async def class_archive(request: Request, ctx: CurrentContext, class_id: str) -> Response:
    try:
        await run_in_threadpool(
            lambda: archive_asset_class(
                request.app.state.session_factory, ctx, _parse_uuid(class_id)
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request, ctx, status_code=409, error=_error_message(CLASS_ERRORS, error)
            )
        )
    return await run_in_threadpool(_classes_done, request, ctx, "class_archived")


@router.post("/investments/classes/{class_id}/restore", name="investments_class_restore")
async def class_restore(request: Request, ctx: CurrentContext, class_id: str) -> Response:
    try:
        await run_in_threadpool(
            lambda: restore_asset_class(
                request.app.state.session_factory, ctx, _parse_uuid(class_id)
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request, ctx, status_code=409, error=_error_message(CLASS_ERRORS, error)
            )
        )
    return await run_in_threadpool(_classes_done, request, ctx, "class_restored")


@router.post("/investments/classes/{class_id}/move", name="investments_class_move")
async def class_move(
    request: Request, ctx: CurrentContext, class_id: str, direction: FormStr = ""
) -> Response:
    try:
        await run_in_threadpool(
            lambda: move_asset_class(
                request.app.state.session_factory,
                ctx,
                _parse_uuid(class_id),
                direction,
            )
        )
    except InvestmentError as exc:
        return await run_in_threadpool(
            lambda error=exc: _render_classes(
                request, ctx, status_code=409, error=_error_message(CLASS_ERRORS, error)
            )
        )
    return await run_in_threadpool(lambda: _render_classes(request, ctx))
