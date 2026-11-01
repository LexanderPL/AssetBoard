from __future__ import annotations

from collections.abc import Callable
from datetime import date
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.notifications import MAX_LEAD_DAYS
from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.domain.scheduled import AmountMode, ItemAction
from assetboard.domain.transactions import TransactionDirection, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, month_names
from assetboard.i18n.numbers import format_amount_input, parse_amount
from assetboard.services import scheduled
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.schedule_text import describe_rule
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    values["weekdays"] = ",".join(
        value for value in form.getlist("weekday") if isinstance(value, str)
    )
    return _canonical(values)


# The guided form's "How often" choices that have their own interval field.
INTERVAL_FIELDS = {
    "weekly": "interval_weeks",
    "every_months": "interval_months",
    "yearly": "interval_years",
}


def _canonical(values: dict[str, str]) -> dict[str, str]:
    """Add the rule fields (``freq``, ``interval``, ...) for a post of the guided form.

    The guided form (marked ``guided=1``) has a "How often" choice with an interval field per
    choice, a separate day for yearly rules, an "Ends" choice and an estimate checkbox. Other
    posts (scripts, older clients) send the rule fields directly and are left as they are.
    """
    if values.get("guided") != "1":
        return values
    result = dict(values)
    result["amount_mode"] = (
        AmountMode.ESTIMATE if values.get("estimate") == "1" else AmountMode.FIXED
    )
    repeat = values.get("repeat", "monthly")
    result["freq"] = {
        "weekly": Frequency.WEEKLY,
        "yearly": Frequency.YEARLY,
        "once": Frequency.ONCE,
    }.get(repeat, Frequency.MONTHLY)
    interval_field = INTERVAL_FIELDS.get(repeat)
    # An empty interval is an error here, not a silent 1.
    result["interval"] = (values.get(interval_field, "").strip() or "0") if interval_field else "1"
    if repeat == "yearly":
        result["month_day"] = values.get("year_day", "1")
    if repeat != "weekly":
        result["weekdays"] = ""
    ends = values.get("ends", "never")
    if ends != "on_date":
        result["end_date"] = ""
    elif not values.get("end_date"):
        result["end_date"] = "missing"
    if ends != "after":
        result["max_count"] = ""
    elif not values.get("max_count"):
        result["max_count"] = "0"
    return result


def _repeat_choice(freq: str, interval: str) -> str:
    if freq in (Frequency.ONCE, Frequency.WEEKLY, Frequency.YEARLY):
        return freq
    return "monthly" if interval in ("", "1") else "every_months"


def _display(values: dict[str, str]) -> dict[str, str]:
    """Fill the guided form's fields from the rule fields (an edit, or a re-shown older post);
    fields the post already has are kept, so a refused form shows what was typed."""
    result = dict(values)
    interval = values.get("interval", "1") or "1"
    repeat = _repeat_choice(values.get("freq", "monthly"), interval)
    month_day = values.get("month_day", "1")
    defaults = {
        "repeat": repeat,
        "interval_weeks": interval if repeat == "weekly" else "1",
        "interval_months": interval if repeat == "every_months" else "2",
        "interval_years": interval if repeat == "yearly" else "1",
        "year_day": month_day if repeat == "yearly" and month_day.isdigit() else "1",
        "estimate": "1" if values.get("amount_mode") == AmountMode.ESTIMATE else "",
        # A rule with both an end date and a count shows the date (docs/recurring.md).
        "ends": "on_date"
        if values.get("end_date")
        else "after"
        if values.get("max_count")
        else "never",
    }
    for key, value in defaults.items():
        result.setdefault(key, value)
    if result.get("end_date") == "missing":
        result["end_date"] = ""
    if result.get("ends") != "after" and result.get("max_count") == "0":
        result["max_count"] = ""
    return result


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


FIELD_ERRORS = {
    "name": _("Enter a name."),
    "category_id": _("Choose an active category of the chosen kind."),
    "amount": _("Enter a positive amount in the workspace currency."),
    "start_date": _("Enter a valid start date."),
    "end_date": _("Enter an end date on or after the start date."),
    "max_count": _("Enter how many times: a whole number of 1 or more."),
    "interval": _("Enter a whole number of 1 or more."),
    "weekdays": _("Choose at least one weekday for a weekly rule."),
    "month_day": _("This day does not exist in the chosen month."),
    "month": _("Choose a month."),
    "weekend_policy": _(
        "Holiday adjustments need a workspace country with holiday data, or no country."
    ),
    "reminder_lead_days": _("Enter a number of days from 0 to 60, or leave it empty."),
}
ACTION_ERRORS = {
    "archived_item": _("This recurring item is archived and cannot change."),
    "unsupported_holidays": _(
        "This item adjusts dates to working days, and the workspace country has no holiday data."
    ),
}
GENERIC_ERROR = _("The recurring item could not be saved. Check the form and try again.")
# Service error codes that belong to a form field.
SERVICE_FIELDS = {
    "invalid_category": "category_id",
    "invalid_currency": "amount",
    "unsupported_holidays": "weekend_policy",
    "no_working_day": "month_day",
    "category_kind_mismatch": "category_id",
    "invalid_reminder": "reminder_lead_days",
}


# How many next dates the form's live panel shows.
PREVIEW_DATES = 5


class InvalidField(ValueError):
    def __init__(self, field: str) -> None:
        super().__init__(field)
        self.field = field


def _parse[T](values: dict[str, str], field: str, convert: Callable[[str], T], default: str) -> T:
    try:
        return convert(values.get(field, default) or default)
    except ValueError:
        raise InvalidField(field) from None


def _optional[T](values: dict[str, str], field: str, convert: Callable[[str], T]) -> T | None:
    return _parse(values, field, convert, "") if values.get(field) else None


def _rule(values: dict[str, str]) -> RecurrenceRule:
    choice = values.get("month_day", "1")
    try:
        month_day: int | MonthDay = (
            int(choice) if choice.lstrip("-").isdigit() else MonthDay(choice)
        )
    except ValueError:
        raise InvalidField("month_day") from None
    start = _parse(values, "start_date", date.fromisoformat, "")
    freq = _parse(values, "freq", Frequency, "monthly")
    policy = _parse(values, "weekend_policy", WeekendPolicy, "none")
    if freq == Frequency.ONCE:
        # One time: the repeat fields are ignored, whatever the (hidden) inputs hold.
        try:
            return RecurrenceRule(start_date=start, freq=freq, weekend_policy=policy)
        except ValueError:
            raise InvalidField("start_date") from None
    try:
        return RecurrenceRule(
            start_date=start,
            end_date=_optional(values, "end_date", date.fromisoformat),
            max_count=_optional(values, "max_count", int),
            freq=freq,
            interval=_parse(values, "interval", int, "1"),
            weekdays=_parse(
                values,
                "weekdays",
                lambda text: tuple(int(day) for day in text.split(",") if day),
                "",
            ),
            month_day=month_day,
            month=_parse(values, "month", int, "1"),
            weekend_policy=policy,
        )
    except InvalidField:
        raise
    except ValueError as exc:
        # RecurrenceRule names the invalid field in its error.
        raise InvalidField(str(exc) if str(exc) in FIELD_ERRORS else "start_date") from None


def _reminder_lead_days(values: dict[str, str]) -> int | None:
    lead = _optional(values, "reminder_lead_days", int)
    if lead is not None and not 0 <= lead <= MAX_LEAD_DAYS:
        raise InvalidField("reminder_lead_days")
    return lead


def _input(values: dict[str, str], ctx: Context, locale: str) -> scheduled.ScheduledInput:
    """Parse the form; raises ``InvalidField`` for the first field that is wrong."""
    if not values.get("name", "").strip():
        raise InvalidField("name")
    currency = ctx.workspace.base_currency
    try:
        minor = amount_minor(parse_amount(values.get("amount", ""), locale), currency)
    except ValueError:
        raise InvalidField("amount") from None
    return scheduled.ScheduledInput(
        name=values.get("name", ""),
        direction=_parse(values, "direction", TransactionDirection, "expense"),
        category_id=_parse(values, "category_id", UUID, ""),
        amount=Money(minor, currency),
        rule=_rule(values),
        amount_mode=_parse(values, "amount_mode", AmountMode, "fixed"),
        notes=values.get("notes", ""),
        auto_post=values.get("auto_post") == "1",
        reminder_lead_days=_reminder_lead_days(values),
    )


async def _form(
    request: Request,
    ctx: Context,
    values: dict[str, str],
    *,
    item_id: str | None = None,
    errors: dict[str, str] | None = None,
) -> Response:
    require_role(ctx, Role.EDITOR)
    locale = request.state.locale
    categories = await run_in_threadpool(list_categories, request.app.state.session_factory, ctx)
    # An item whose category was archived keeps showing it (disabled), never another one.
    archived_category = next(
        (c for c in categories.archived if str(c.id) == values.get("category_id")), None
    )
    return render_template(
        request,
        "scheduled/form.html",
        context={
            "values": _display(values),
            "item_id": item_id,
            "categories": categories.expense + categories.income,
            "archived_category": archived_category,
            "errors": {
                field: gettext(message, locale) for field, message in (errors or {}).items()
            },
            "days": list(range(1, 32)),
            "months": list(enumerate(month_names(locale), start=1)),
            "weekdays": [
                ("0", gettext("Monday", locale)),
                ("1", gettext("Tuesday", locale)),
                ("2", gettext("Wednesday", locale)),
                ("3", gettext("Thursday", locale)),
                ("4", gettext("Friday", locale)),
                ("5", gettext("Saturday", locale)),
                ("6", gettext("Sunday", locale)),
            ],
        },
        status_code=400 if errors else 200,
    )


@router.get("/recurring", name="scheduled_items")
async def items(request: Request, ctx: CurrentContext) -> Response:
    rows = await run_in_threadpool(scheduled.list_items, request.app.state.session_factory, ctx)
    error = request.query_params.get("error")
    locale = request.state.locale
    today = ctx.clock.today(ctx.workspace.timezone)
    # One-time items live on the Upcoming page; archived items are history only (their
    # transactions stay), so neither is listed here.
    shown = [row for row in rows if row.values.rule.freq != Frequency.ONCE and not row.archived]
    return render_template(
        request,
        "scheduled/list.html",
        context={
            "sections": [
                (
                    direction.value,
                    [
                        (row, describe_rule(row.values.rule, locale, today))
                        for row in shown
                        if row.values.direction == direction
                    ],
                )
                for direction in (TransactionDirection.EXPENSE, TransactionDirection.INCOME)
            ],
            "has_items": bool(shown),
            "format_day": format_day,
            "can_edit": has_role(ctx, Role.EDITOR),
            "error": gettext(ACTION_ERRORS.get(error, GENERIC_ERROR), locale) if error else None,
        },
    )


@router.get("/recurring/new", name="scheduled_new")
async def new(request: Request, ctx: CurrentContext) -> Response:
    return await _form(
        request,
        ctx,
        {
            "start_date": ctx.clock.today(ctx.workspace.timezone).isoformat(),
            "guided": "1",
            "interval": "1",
            "freq": "monthly",
            "month_day": "1",
            "month": "1",
            "weekend_policy": "none",
            "amount_mode": "fixed",
            "direction": "expense",
        },
    )


@router.get("/recurring/{item_id}/edit", name="scheduled_edit")
async def edit(request: Request, ctx: CurrentContext, item_id: str) -> Response:
    require_role(ctx, Role.EDITOR)
    item = await run_in_threadpool(
        scheduled.get_item, request.app.state.session_factory, ctx, _id(item_id)
    )
    rule = item.rule
    return await _form(
        request,
        ctx,
        {
            "name": item.name,
            "direction": item.direction,
            "category_id": str(item.category_id),
            "amount": format_amount_input(item.amount, request.state.locale),
            "notes": item.notes,
            "amount_mode": item.amount_mode,
            "auto_post": "1" if item.auto_post else "",
            "reminder_lead_days": (
                "" if item.reminder_lead_days is None else str(item.reminder_lead_days)
            ),
            "start_date": rule.start_date.isoformat(),
            "end_date": rule.end_date.isoformat() if rule.end_date else "",
            "max_count": str(rule.max_count) if rule.max_count else "",
            "freq": rule.freq,
            "interval": str(rule.interval),
            "weekdays": ",".join(map(str, rule.weekdays)),
            "month_day": str(rule.month_day),
            "month": str(rule.month),
            "weekend_policy": rule.weekend_policy,
        },
        item_id=item_id,
    )


@router.post("/recurring/preview", name="scheduled_preview")
async def preview(request: Request, ctx: CurrentContext) -> Response:
    values = await _form_data(request)
    locale = request.state.locale
    try:
        rule = _rule(values)
        dates = await run_in_threadpool(
            scheduled.preview, request.app.state.session_factory, ctx, rule
        )
    except ValueError:
        return render_template(
            request,
            "scheduled/preview.html",
            context={"dates": [], "invalid": True, "sentence": ""},
        )
    shown = [(day, format_day(day, locale)) for day in dates[:PREVIEW_DATES]]
    sentence = describe_rule(rule, locale, ctx.clock.today(ctx.workspace.timezone))
    return render_template(
        request,
        "scheduled/preview.html",
        context={"dates": shown, "invalid": False, "sentence": sentence},
    )


async def _save(request: Request, ctx: Context, item_id: str | None) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    try:
        data = _input(values, ctx, request.state.locale)
    except InvalidField as exc:
        return await _form(
            request, ctx, values, item_id=item_id, errors={exc.field: FIELD_ERRORS[exc.field]}
        )
    try:
        await run_in_threadpool(
            lambda: scheduled.save_item(
                request.app.state.session_factory,
                ctx,
                data,
                request.app.state.settings,
                item_id=_id(item_id) if item_id else None,
                include_past=values.get("include_past") == "1",
                # The chosen kind must match the category's (the category decides it).
                require_kind=data.direction.value if values.get("direction") else None,
            )
        )
    except ValueError as exc:
        field = SERVICE_FIELDS.get(str(exc))
        if str(exc) == "invalid_item":
            field = "amount" if data.name.strip() else "name"
        errors = {field: FIELD_ERRORS[field]} if field else {"form": GENERIC_ERROR}
        return await _form(request, ctx, values, item_id=item_id, errors=errors)
    return RedirectResponse(request.url_for("scheduled_items").path, status_code=303)


@router.post("/recurring", name="scheduled_create")
async def create(request: Request, ctx: CurrentContext) -> Response:
    return await _save(request, ctx, None)


@router.post("/recurring/{item_id}/edit", name="scheduled_update")
async def update(request: Request, ctx: CurrentContext, item_id: str) -> Response:
    return await _save(request, ctx, item_id)


@router.post("/recurring/{item_id}/{action}", name="scheduled_action")
async def action(request: Request, ctx: CurrentContext, item_id: str, action: str) -> Response:
    try:
        operation = ItemAction(action)
    except ValueError:
        raise NotFound from None
    try:
        await run_in_threadpool(
            scheduled.change_state,
            request.app.state.session_factory,
            ctx,
            _id(item_id),
            operation,
            request.app.state.settings,
        )
    except ValueError as exc:
        code = str(exc) if str(exc) in ACTION_ERRORS else "failed"
        target = request.url_for("scheduled_items").path + "?" + urlencode({"error": code})
        return RedirectResponse(target, status_code=303)
    return RedirectResponse(request.url_for("scheduled_items").path, status_code=303)
