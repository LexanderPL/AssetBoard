"""Net-worth page (F5.1): asset/liability tabs, items, categories and valuations.

Authorization happens in ``services.networth`` (viewers read, editors and owners write). Every
state-changing route is covered by the router-level ``require_csrf`` dependency. HTMX requests
get the ``#networth-body`` fragment; plain form posts redirect (303) after success.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.networth import (
    MAX_BULK_BYTES,
    MAX_BULK_LINES,
    MAX_ITEM_NAME_LENGTH,
    MAX_NOTES_LENGTH,
    Side,
)
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_amount_input, format_money
from assetboard.services.context import Context, Role, has_role
from assetboard.services.networth import (
    BulkPreview,
    BulkRejected,
    CategoryView,
    InvalidNetWorth,
    ItemView,
    NetWorthError,
    add_valuation,
    archive_category,
    archive_item,
    create_bulk,
    create_category,
    create_item,
    delete_category,
    get_category,
    get_item,
    list_networth,
    move_category,
    preview_bulk,
    restore_category,
    restore_item,
    seed_categories,
    update_category,
    update_item,
)
from assetboard.services.workspace_options import currency_codes
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
DEFAULT_COLOR = "#64748b"
DEFAULT_SIDE = Side.ASSET


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


SIDE_LABELS = {Side.ASSET.value: _("Assets"), Side.LIABILITY.value: _("Liabilities")}
ERRORS = {
    "invalid_name": _("Enter a name of 1 to 200 characters, without control characters."),
    "invalid_notes": _("Enter notes of at most 200 characters."),
    "invalid_amount": _("Enter a valid amount."),
    "value_negative": _("Values cannot be negative."),
    "too_many_decimals": _("The amount has too many decimal places."),
    "amount_too_large": _("The value is too large."),
    "invalid_currency": _("Choose a supported currency."),
    "currency_locked": _("The currency cannot change after the first value."),
    "invalid_date": _("Enter a valid date between 1900 and 2199."),
    "invalid_category": _("Choose a category."),
    "wrong_side": _("Choose a category from this tab."),
    "category_archived": _("This category is archived; choose an active one."),
    "invalid_side": _("Choose assets or liabilities."),
    "invalid_direction": _("The change could not be made. Reload the page and try again."),
    "linked_not_supported": _("Linked values arrive in a later version."),
    "archived": _("Restore the category before changing it."),
    "item_archived": _("Restore the item before changing it."),
    "duplicate_name": _("Another active category of this side already has this name."),
    "already_seeded": _("The suggested categories can only be added to an empty side."),
    "bulk_too_large": _("Paste at most 64 KiB of text."),
    "too_many_lines": _("Paste at most 500 lines."),
}
FALLBACK_ERROR = _("The change could not be made. Reload the page and try again.")
NOTICES = {
    "category_created": _("Category added."),
    "category_saved": _("Category saved."),
    "category_archived": _("Category archived."),
    "category_archived_in_use": _("Archived because it still has items."),
    "category_restored": _("Category restored."),
    "category_deleted": _("Category deleted."),
    "categories_seeded": _("Suggested categories added."),
    "item_created": _("Item added."),
    "item_saved": _("Item saved."),
    "item_archived": _("Item archived."),
    "item_restored": _("Item restored."),
    "valuation_added": _("Value added."),
    "bulk_created": _("Items added."),
}
BULK_ERRORS = {
    "wrong_format": _("Use name; value; category on each line."),
    "name_required": _("Enter a name."),
    "value_required": _("Enter a value."),
    "category_required": _("Enter a category."),
    "invalid_name": _("Enter a name of 1 to 200 characters, without control characters."),
    "invalid_category": _("Enter a category name of 1 to 80 characters."),
    "invalid_amount": _("Enter a valid amount."),
    "value_negative": _("Values cannot be negative."),
    "too_many_decimals": _("The amount has too many decimal places."),
    "amount_too_large": _("The value is too large."),
    "category_unknown": _("No active category with this name."),
    "category_archived": _("This category is archived; choose an active one."),
}
BULK_WARNINGS = {
    "duplicate_name": _("This name appears more than once in the batch."),
    "existing_name": _("An active item with this name already exists."),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _form_uuid(value: str, field: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise InvalidNetWorth(field, "invalid_category") from None


def _parse_date(value: str, field: str) -> date | None:
    if not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise InvalidNetWorth(field, "invalid_date") from None


def _selected_side(value: str) -> Side:
    try:
        return Side(value)
    except ValueError:
        return DEFAULT_SIDE


def _checked(value: str) -> bool:
    return value == "1"


def _item_values(item: ItemView | None = None, *, base_currency: str = "EUR") -> dict[str, str]:
    if item is None:
        return {
            "name": "",
            "category_id": "",
            "currency": base_currency,
            "notes": "",
            "acquired_on": "",
            "include_in_net_worth": "1",
            "investable": "",
        }
    return {
        "name": item.name,
        "category_id": str(item.category_id),
        "currency": item.currency,
        "notes": item.notes,
        "acquired_on": item.acquired_on.isoformat() if item.acquired_on else "",
        "include_in_net_worth": "1" if item.include_in_net_worth else "",
        "investable": "1" if item.investable else "",
    }


def _create_item_values(base_currency: str, today: str) -> dict[str, str]:
    return {
        "name": "",
        "category_id": "",
        "value": "",
        "currency": base_currency,
        "notes": "",
        "acquired_on": "",
        "include_in_net_worth": "1",
        "investable": "",
        "as_of": today,
        "valuation_note": "",
    }


def _category_values(category: CategoryView | None = None) -> dict[str, str]:
    if category is None:
        return {"name": "", "color": DEFAULT_COLOR}
    return {"name": category.name, "color": category.color}


def _body_context(
    request: Request,
    ctx: Context,
    page: Any,
    *,
    side: Side,
    notice: str | None = None,
    error: str | None = None,
    create_item_values: dict[str, str] | None = None,
    create_item_errors: dict[str, str] | None = None,
    edit_item: ItemView | None = None,
    edit_item_values: dict[str, str] | None = None,
    edit_item_errors: dict[str, str] | None = None,
    valuation_item_id: UUID | None = None,
    valuation_values: dict[str, str] | None = None,
    valuation_errors: dict[str, str] | None = None,
    create_category_values: dict[str, str] | None = None,
    create_category_errors: dict[str, str] | None = None,
    edit_category: CategoryView | None = None,
    edit_category_values: dict[str, str] | None = None,
    edit_category_errors: dict[str, str] | None = None,
    bulk_text: str = "",
    bulk_preview: BulkPreview | None = None,
) -> dict[str, object]:
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone).isoformat()
    return {
        "page": page,
        "view": page.side_view(side),
        "side": side,
        "side_value": side.value,
        "side_labels": {code: _t(request, label) for code, label in SIDE_LABELS.items()},
        "can_edit": has_role(ctx, Role.EDITOR),
        "base_currency": ctx.workspace.base_currency,
        "currency_options": [(code, code) for code in currency_codes()],
        "today": today,
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "error": _t(request, error) if error else None,
        "create_item_values": create_item_values
        or _create_item_values(ctx.workspace.base_currency, today),
        "create_item_errors": {
            field: _t(request, message) for field, message in (create_item_errors or {}).items()
        },
        "edit_item": edit_item,
        "edit_item_values": edit_item_values
        or (_item_values(edit_item, base_currency=ctx.workspace.base_currency)),
        "edit_item_errors": {
            field: _t(request, message) for field, message in (edit_item_errors or {}).items()
        },
        "valuation_item_id": valuation_item_id,
        "valuation_values": valuation_values or {"value": "", "as_of": today, "note": ""},
        "valuation_errors": {
            field: _t(request, message) for field, message in (valuation_errors or {}).items()
        },
        "create_category_values": create_category_values or _category_values(),
        "create_category_errors": {
            field: _t(request, message) for field, message in (create_category_errors or {}).items()
        },
        "edit_category": edit_category,
        "edit_category_values": edit_category_values or _category_values(edit_category),
        "edit_category_errors": {
            field: _t(request, message) for field, message in (edit_category_errors or {}).items()
        },
        "bulk_text": bulk_text,
        "bulk_preview": bulk_preview,
        "bulk_error_labels": {code: _t(request, label) for code, label in BULK_ERRORS.items()},
        "bulk_warning_labels": {code: _t(request, label) for code, label in BULK_WARNINGS.items()},
        "max_item_name_length": MAX_ITEM_NAME_LENGTH,
        "max_notes_length": MAX_NOTES_LENGTH,
        "max_bulk_lines": MAX_BULK_LINES,
        "max_bulk_bytes": MAX_BULK_BYTES,
        "amount_input": lambda amount: format_amount_input(amount, locale),
        "format_day": lambda day: format_day(day, locale),
        "format_money": lambda money: format_money(money, locale),
    }


def _render(
    request: Request,
    ctx: Context,
    *,
    side: Side,
    status_code: int = 200,
    **kwargs: Any,
) -> Response:
    page = list_networth(request.app.state.session_factory, ctx)
    context = _body_context(request, ctx, page, side=side, **kwargs)
    template = "networth/_body.html" if is_htmx_fragment_request(request) else "networth/list.html"
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _done(request: Request, ctx: Context, notice: str, side: Side) -> Response:
    if is_htmx_fragment_request(request):
        return _render(request, ctx, side=side, notice=notice)
    return RedirectResponse(
        request.url_for("networth").path + f"?side={side.value}&notice={notice}",
        status_code=303,
    )


def _failed(
    request: Request,
    ctx: Context,
    side: Side,
    code: str,
    *,
    status_code: int = 409,
    **kwargs: Any,
) -> Response:
    return _render(
        request,
        ctx,
        side=side,
        status_code=status_code,
        error=ERRORS.get(code, FALLBACK_ERROR),
        **kwargs,
    )


@router.get("/net-worth", name="networth")
async def networth_page(
    request: Request,
    ctx: CurrentContext,
    notice: str = "",
    error: str = "",
    side: str = "asset",
    edit_item: str = "",
    edit_category: str = "",
) -> Response:
    selected = _selected_side(side)
    item_view: ItemView | None = None
    category_view: CategoryView | None = None
    if edit_item and has_role(ctx, Role.EDITOR):
        item_view = await run_in_threadpool(
            get_item, request.app.state.session_factory, ctx, _parse_uuid(edit_item)
        )
        if item_view.archived:
            item_view = None
        else:
            selected = Side(item_view.side)
    elif edit_category and has_role(ctx, Role.EDITOR):
        category_view = await run_in_threadpool(
            get_category,
            request.app.state.session_factory,
            ctx,
            _parse_uuid(edit_category),
        )
        if category_view.archived:
            category_view = None
        else:
            selected = Side(category_view.side)
    error_message = ERRORS.get(error) if error in ERRORS else None
    return await run_in_threadpool(
        lambda: _render(
            request,
            ctx,
            side=selected,
            notice=notice or None,
            error=error_message,
            edit_item=item_view,
            edit_category=category_view,
        )
    )


# Categories


@router.post("/net-worth/categories", name="networth_categories_create")
async def networth_categories_create(
    request: Request,
    ctx: CurrentContext,
    side: FormStr = "",
    name: FormStr = "",
    color: FormStr = "",
) -> Response:
    selected = _selected_side(side)
    values = {"name": name, "color": color}
    factory = request.app.state.session_factory
    try:
        await run_in_threadpool(
            lambda: create_category(factory, ctx, side=side, name=name, color=color)
        )
    except NetWorthError as exc:
        field, code = exc.field or "name", exc.code
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=selected,
                status_code=400,
                create_category_values=values,
                create_category_errors={field: ERRORS.get(code, FALLBACK_ERROR)},
            )
        )
    return await run_in_threadpool(lambda: _done(request, ctx, "category_created", selected))


@router.post("/net-worth/categories/seed", name="networth_categories_seed")
async def networth_categories_seed(
    request: Request, ctx: CurrentContext, side: FormStr = ""
) -> Response:
    selected = _selected_side(side)
    locale = request.state.locale
    try:
        await run_in_threadpool(
            lambda: seed_categories(
                request.app.state.session_factory, ctx, side=side, locale=locale
            )
        )
    except NetWorthError as exc:
        code = exc.code
        return await run_in_threadpool(lambda: _failed(request, ctx, selected, code))
    return await run_in_threadpool(lambda: _done(request, ctx, "categories_seeded", selected))


@router.post("/net-worth/categories/{category_id}", name="networth_category_update")
async def networth_category_update(
    request: Request,
    ctx: CurrentContext,
    category_id: str,
    side: FormStr = "",
    name: FormStr = "",
    color: FormStr = "",
) -> Response:
    target = _parse_uuid(category_id)
    values = {"name": name, "color": color}
    factory = request.app.state.session_factory
    try:
        category = await run_in_threadpool(
            lambda: update_category(factory, ctx, target, name=name, color=color)
        )
    except NetWorthError as exc:
        field, code = exc.field or "name", exc.code
        category = await run_in_threadpool(get_category, factory, ctx, target)
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=Side(category.side),
                status_code=400,
                edit_category=category,
                edit_category_values=values,
                edit_category_errors={field: ERRORS.get(code, FALLBACK_ERROR)},
            )
        )
    return await run_in_threadpool(
        lambda: _done(request, ctx, "category_saved", Side(category.side))
    )


@router.post("/net-worth/categories/{category_id}/move", name="networth_category_move")
async def networth_category_move(
    request: Request,
    ctx: CurrentContext,
    category_id: str,
    side: FormStr = "",
    direction: FormStr = "",
) -> Response:
    target = _parse_uuid(category_id)
    selected = _selected_side(side)
    factory = request.app.state.session_factory
    try:
        await run_in_threadpool(lambda: move_category(factory, ctx, target, direction))
    except NetWorthError as exc:
        code = exc.code
        status_code = 400 if code == "invalid_direction" else 409
        return await run_in_threadpool(
            lambda: _failed(request, ctx, selected, code, status_code=status_code)
        )
    if is_htmx_fragment_request(request):
        return await run_in_threadpool(lambda: _render(request, ctx, side=selected))
    return RedirectResponse(
        request.url_for("networth").path + f"?side={selected.value}", status_code=303
    )


@router.post("/net-worth/categories/{category_id}/archive", name="networth_category_archive")
async def networth_category_archive(
    request: Request, ctx: CurrentContext, category_id: str, side: FormStr = ""
) -> Response:
    target = _parse_uuid(category_id)
    selected = _selected_side(side)
    await run_in_threadpool(archive_category, request.app.state.session_factory, ctx, target)
    return await run_in_threadpool(lambda: _done(request, ctx, "category_archived", selected))


@router.post("/net-worth/categories/{category_id}/restore", name="networth_category_restore")
async def networth_category_restore(
    request: Request, ctx: CurrentContext, category_id: str, side: FormStr = ""
) -> Response:
    target = _parse_uuid(category_id)
    selected = _selected_side(side)
    try:
        await run_in_threadpool(restore_category, request.app.state.session_factory, ctx, target)
    except NetWorthError as exc:
        code = exc.code
        return await run_in_threadpool(lambda: _failed(request, ctx, selected, code))
    return await run_in_threadpool(lambda: _done(request, ctx, "category_restored", selected))


@router.post("/net-worth/categories/{category_id}/delete", name="networth_category_delete")
async def networth_category_delete(
    request: Request, ctx: CurrentContext, category_id: str, side: FormStr = ""
) -> Response:
    target = _parse_uuid(category_id)
    selected = _selected_side(side)
    outcome = await run_in_threadpool(
        lambda: delete_category(request.app.state.session_factory, ctx, target)
    )
    notice = "category_deleted" if outcome == "deleted" else "category_archived_in_use"
    return await run_in_threadpool(lambda: _done(request, ctx, notice, selected))


@router.get("/net-worth/categories/{category_id}/edit", name="networth_category_edit")
async def networth_category_edit(
    request: Request, ctx: CurrentContext, category_id: str, side: str = ""
) -> Response:
    target = _parse_uuid(category_id)
    category = await run_in_threadpool(get_category, request.app.state.session_factory, ctx, target)
    selected = Side(category.side)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(
            request.url_for("networth").path + f"?side={selected.value}&edit_category={target}",
            status_code=303,
        )
    return await run_in_threadpool(
        lambda: _render(
            request,
            ctx,
            side=selected,
            edit_category=None if category.archived else category,
        )
    )


@router.get("/net-worth/categories/{category_id}/row", name="networth_category_row")
async def networth_category_row(
    request: Request, ctx: CurrentContext, category_id: str, side: str = ""
) -> Response:
    target = _parse_uuid(category_id)
    category = await run_in_threadpool(get_category, request.app.state.session_factory, ctx, target)
    selected = Side(category.side)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(
            request.url_for("networth").path + f"?side={selected.value}",
            status_code=303,
        )
    return await run_in_threadpool(lambda: _render(request, ctx, side=selected))


# Items


@router.post("/net-worth/items", name="networth_items_create")
async def networth_items_create(
    request: Request,
    ctx: CurrentContext,
    side: FormStr = "",
    category_id: FormStr = "",
    name: FormStr = "",
    value: FormStr = "",
    currency: FormStr = "",
    notes: FormStr = "",
    acquired_on: FormStr = "",
    include_in_net_worth: FormStr = "",
    investable: FormStr = "",
    as_of: FormStr = "",
    valuation_note: FormStr = "",
) -> Response:
    selected = _selected_side(side)
    factory = request.app.state.session_factory
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone).isoformat()
    values = {
        "name": name,
        "category_id": category_id,
        "value": value,
        "currency": currency,
        "notes": notes,
        "acquired_on": acquired_on,
        "include_in_net_worth": include_in_net_worth,
        "investable": investable,
        "as_of": as_of or today,
        "valuation_note": valuation_note,
    }
    try:
        item = await run_in_threadpool(
            lambda: create_item(
                factory,
                ctx,
                locale=locale,
                side=side,
                category_id=_form_uuid(category_id, "category"),
                name=name,
                value=value,
                currency=currency,
                notes=notes,
                acquired_on=_parse_date(acquired_on, "acquired_on"),
                include_in_net_worth=_checked(include_in_net_worth),
                investable=_checked(investable),
                as_of=_parse_date(as_of, "as_of"),
                valuation_note=valuation_note,
            )
        )
    except NetWorthError as exc:
        field, code = exc.field or "name", exc.code
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=selected,
                status_code=400,
                create_item_values=values,
                create_item_errors={field: ERRORS.get(code, FALLBACK_ERROR)},
            )
        )
    return await run_in_threadpool(lambda: _done(request, ctx, "item_created", Side(item.side)))


@router.post("/net-worth/items/{item_id}", name="networth_item_update")
async def networth_item_update(
    request: Request,
    ctx: CurrentContext,
    item_id: str,
    side: FormStr = "",
    category_id: FormStr = "",
    name: FormStr = "",
    currency: FormStr = "",
    notes: FormStr = "",
    acquired_on: FormStr = "",
    include_in_net_worth: FormStr = "",
    investable: FormStr = "",
) -> Response:
    target = _parse_uuid(item_id)
    factory = request.app.state.session_factory
    values = {
        "name": name,
        "category_id": category_id,
        "currency": currency,
        "notes": notes,
        "acquired_on": acquired_on,
        "include_in_net_worth": include_in_net_worth,
        "investable": investable,
    }
    try:
        item = await run_in_threadpool(
            lambda: update_item(
                factory,
                ctx,
                target,
                category_id=_form_uuid(category_id, "category"),
                name=name,
                currency=currency,
                notes=notes,
                acquired_on=_parse_date(acquired_on, "acquired_on"),
                include_in_net_worth=_checked(include_in_net_worth),
                investable=_checked(investable),
            )
        )
    except NetWorthError as exc:
        field, code = exc.field or "name", exc.code
        item = await run_in_threadpool(get_item, factory, ctx, target)
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=Side(item.side),
                status_code=400,
                edit_item=item,
                edit_item_values=values,
                edit_item_errors={field: ERRORS.get(code, FALLBACK_ERROR)},
            )
        )
    return await run_in_threadpool(lambda: _done(request, ctx, "item_saved", Side(item.side)))


@router.post("/net-worth/items/{item_id}/archive", name="networth_item_archive")
async def networth_item_archive(
    request: Request, ctx: CurrentContext, item_id: str, side: FormStr = ""
) -> Response:
    target = _parse_uuid(item_id)
    selected = _selected_side(side)
    await run_in_threadpool(archive_item, request.app.state.session_factory, ctx, target)
    return await run_in_threadpool(lambda: _done(request, ctx, "item_archived", selected))


@router.post("/net-worth/items/{item_id}/restore", name="networth_item_restore")
async def networth_item_restore(
    request: Request, ctx: CurrentContext, item_id: str, side: FormStr = ""
) -> Response:
    target = _parse_uuid(item_id)
    selected = _selected_side(side)
    await run_in_threadpool(restore_item, request.app.state.session_factory, ctx, target)
    return await run_in_threadpool(lambda: _done(request, ctx, "item_restored", selected))


@router.post("/net-worth/items/{item_id}/valuations", name="networth_item_valuation")
async def networth_item_valuation(
    request: Request,
    ctx: CurrentContext,
    item_id: str,
    side: FormStr = "",
    value: FormStr = "",
    as_of: FormStr = "",
    note: FormStr = "",
) -> Response:
    target = _parse_uuid(item_id)
    factory = request.app.state.session_factory
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone).isoformat()
    try:
        item = await run_in_threadpool(
            lambda: add_valuation(
                factory,
                ctx,
                target,
                locale=locale,
                value=value,
                as_of=_parse_date(as_of, "as_of"),
                note=note,
            )
        )
    except NetWorthError as exc:
        code, field = exc.code, exc.field or "value"
        item = await run_in_threadpool(get_item, factory, ctx, target)
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=Side(item.side),
                status_code=400,
                error=ERRORS.get(code, FALLBACK_ERROR),
                valuation_item_id=item.id,
                valuation_values={"value": value, "as_of": as_of or today, "note": note},
                valuation_errors={field: ERRORS.get(code, FALLBACK_ERROR)},
            )
        )
    return await run_in_threadpool(lambda: _done(request, ctx, "valuation_added", Side(item.side)))


@router.get("/net-worth/items/{item_id}/edit", name="networth_item_edit")
async def networth_item_edit(
    request: Request, ctx: CurrentContext, item_id: str, side: str = ""
) -> Response:
    target = _parse_uuid(item_id)
    item = await run_in_threadpool(get_item, request.app.state.session_factory, ctx, target)
    selected = Side(item.side)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(
            request.url_for("networth").path + f"?side={selected.value}&edit_item={target}",
            status_code=303,
        )
    return await run_in_threadpool(
        lambda: _render(request, ctx, side=selected, edit_item=None if item.archived else item)
    )


@router.get("/net-worth/items/{item_id}/row", name="networth_item_row")
async def networth_item_row(
    request: Request, ctx: CurrentContext, item_id: str, side: str = ""
) -> Response:
    target = _parse_uuid(item_id)
    item = await run_in_threadpool(get_item, request.app.state.session_factory, ctx, target)
    selected = Side(item.side)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(
            request.url_for("networth").path + f"?side={selected.value}", status_code=303
        )
    return await run_in_threadpool(lambda: _render(request, ctx, side=selected))


# Bulk add


@router.post("/net-worth/bulk/preview", name="networth_bulk_preview")
async def networth_bulk_preview(
    request: Request, ctx: CurrentContext, side: FormStr = "", text: FormStr = ""
) -> Response:
    selected = _selected_side(side)
    locale = request.state.locale
    try:
        preview = await run_in_threadpool(
            lambda: preview_bulk(
                request.app.state.session_factory, ctx, side=side, text=text, locale=locale
            )
        )
    except NetWorthError as exc:
        code = exc.code
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=selected,
                status_code=400,
                error=ERRORS.get(code, FALLBACK_ERROR),
                bulk_text=text,
            )
        )
    return await run_in_threadpool(
        lambda: _render(request, ctx, side=selected, bulk_text=text, bulk_preview=preview)
    )


@router.post("/net-worth/bulk", name="networth_bulk_create")
async def networth_bulk_create(
    request: Request, ctx: CurrentContext, side: FormStr = "", text: FormStr = ""
) -> Response:
    selected = _selected_side(side)
    locale = request.state.locale
    try:
        await run_in_threadpool(
            lambda: create_bulk(
                request.app.state.session_factory, ctx, side=side, text=text, locale=locale
            )
        )
    except BulkRejected as exc:
        preview = exc.preview
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=selected,
                status_code=400,
                bulk_text=text,
                bulk_preview=preview,
            )
        )
    except NetWorthError as exc:
        code = exc.code
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                side=selected,
                status_code=400,
                error=ERRORS.get(code, FALLBACK_ERROR),
                bulk_text=text,
            )
        )
    return await run_in_threadpool(lambda: _done(request, ctx, "bulk_created", selected))
