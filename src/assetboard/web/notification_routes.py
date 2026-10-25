"""Reminder channels: the member's own Apprise destinations in this workspace (F3.6)."""

from __future__ import annotations

from datetime import time
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.notifications import MAX_LEAD_DAYS
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.services import notifications
from assetboard.services.notifications import ChannelInput, NotificationError
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ERRORS = {
    "invalid_name": _("Enter a name of up to 100 characters."),
    "invalid_url": _("Enter one valid Apprise URL, such as ntfys://ntfy.sh/your-topic."),
    "scheme_not_allowed": _("This kind of URL is not allowed on this server."),
    "invalid_mode": _("Choose one digest a day or one message per reminder."),
    "invalid_lead_days": _("Enter a number of days from 0 to 60."),
    "invalid_time": _("Enter a time such as 08:00."),
    "too_many_channels": _("You have reached the limit of 10 channels in this workspace."),
    "throttled": _("Too many test messages. Try again in a few minutes."),
    "disabled": _("The administrator has turned notifications off on this server."),
    "decrypt_failed": _(
        "The saved URL can no longer be read (the server's secret key changed). "
        "Enter the URL again."
    ),
    "timeout": _("The service did not answer in time."),
    "delivery_failed": _("The service refused the message or could not be reached."),
    "interrupted": _("The last delivery was interrupted; the next daily run retries it."),
    "failed": _("The channel could not be saved. Check the form and try again."),
}
NOTICES = {
    "created": _("Channel saved. Send a test message to check it."),
    "updated": _("Channel saved."),
    "deleted": _("Channel deleted."),
    "test_sent": _("Test message sent. Check your device."),
}
KINDS = {"due_soon": _("Due soon"), "overdue": _("Overdue"), "work_log": _("Work log")}
STATUSES = {"sent": _("Sent"), "failed": _("Failed"), "sending": _("Sending")}


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _redirect(request: Request, **params: str) -> Response:
    target = request.url_for("notifications").path
    return RedirectResponse(target + ("?" + urlencode(params) if params else ""), status_code=303)


async def _input(request: Request) -> ChannelInput:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        digest_time = time.fromisoformat(values.get("digest_time", "") or "08:00")
    except ValueError:
        raise NotificationError("invalid_time") from None
    try:
        lead_days = int(values.get("default_lead_days", "") or "3")
    except ValueError:
        raise NotificationError("invalid_lead_days") from None
    return ChannelInput(
        name=values.get("name", ""),
        url=values.get("url", ""),
        enabled=values.get("enabled") == "1",
        mode=values.get("mode", ""),
        digest_time=digest_time,
        default_lead_days=lead_days,
        notify_due_soon=values.get("notify_due_soon") == "1",
        notify_overdue=values.get("notify_overdue") == "1",
        notify_work_logs=values.get("notify_work_logs") == "1",
        include_amounts=values.get("include_amounts") == "1",
    )


def _message(table: dict[str, str], code: str | None, locale: str) -> str | None:
    if not code:
        return None
    return gettext(table.get(code, ERRORS["failed"]), locale)


@router.get("/notifications", name="notifications")
async def channels_page(request: Request, ctx: CurrentContext) -> Response:
    settings = request.app.state.settings
    view = await run_in_threadpool(
        notifications.page, request.app.state.session_factory, ctx, settings
    )
    locale = request.state.locale
    return render_template(
        request,
        "notifications/channels.html",
        context={
            "page": view,
            "error": _message(ERRORS, request.query_params.get("error"), locale),
            "notice": _message(NOTICES, request.query_params.get("notice"), locale),
            "channel_errors": {code: gettext(message, locale) for code, message in ERRORS.items()},
            "kinds": KINDS,
            "statuses": STATUSES,
            "max_lead_days": MAX_LEAD_DAYS,
            "format_day": format_day,
        },
    )


@router.post("/notifications", name="notification_create")
async def create(request: Request, ctx: CurrentContext) -> Response:
    try:
        values = await _input(request)
        await run_in_threadpool(
            notifications.create_channel,
            request.app.state.session_factory,
            ctx,
            request.app.state.settings,
            request.app.state.notifier,
            values,
        )
    except NotificationError as exc:
        return _redirect(request, error=exc.code)
    return _redirect(request, notice="created")


@router.post("/notifications/{channel_id}", name="notification_update")
async def update(request: Request, ctx: CurrentContext, channel_id: str) -> Response:
    try:
        values = await _input(request)
        await run_in_threadpool(
            notifications.update_channel,
            request.app.state.session_factory,
            ctx,
            request.app.state.settings,
            request.app.state.notifier,
            _id(channel_id),
            values,
        )
    except NotificationError as exc:
        return _redirect(request, error=exc.code)
    return _redirect(request, notice="updated")


@router.post("/notifications/{channel_id}/delete", name="notification_delete")
async def delete(request: Request, ctx: CurrentContext, channel_id: str) -> Response:
    await run_in_threadpool(
        notifications.delete_channel,
        request.app.state.session_factory,
        ctx,
        _id(channel_id),
    )
    return _redirect(request, notice="deleted")


@router.post("/notifications/{channel_id}/test", name="notification_test")
async def send_test(request: Request, ctx: CurrentContext, channel_id: str) -> Response:
    outcome = await run_in_threadpool(
        notifications.send_test,
        request.app.state.session_factory,
        ctx,
        request.app.state.settings,
        request.app.state.notifier,
        _id(channel_id),
    )
    if outcome.ok:
        return _redirect(request, notice="test_sent")
    return _redirect(request, error=outcome.error or "delivery_failed")
