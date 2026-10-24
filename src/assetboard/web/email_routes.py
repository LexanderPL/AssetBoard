"""Open registration with a verified email, and forgotten passwords (ADR-0017).

``/register`` and ``/password/forgot`` take only an email address and answer the same page
for every address; the mail queued for that address (if any) goes out after the response.
The links (``/register/complete/<token>``, ``/password/reset/<token>``) are consumed by POST
only, so link scanners in mail clients cannot use them. Every route here answers 404 while
its feature is off.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.domain.identity import MAX_EMAIL_LENGTH
from assetboard.i18n import gettext
from assetboard.services.account_email import (
    MailRequestOutcome,
    PasswordResetUnavailable,
    complete_registration_async,
    password_reset_available,
    registration_email,
    request_password_reset,
    reset_password_async,
    reset_target,
    start_registration,
)
from assetboard.services.auth import (
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    RegistrationDisabled,
    create_session_for,
)
from assetboard.services.instance_settings import registration_open
from assetboard.web.auth_routes import ERROR_FIELDS, ERROR_MESSAGES, signed_in_redirect, too_long
from assetboard.web.rendering import render_template
from assetboard.web.security import client_host, require_csrf, web_session

router = APIRouter(dependencies=[Depends(require_csrf)])
FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


CHECK_INBOX = _("Check your inbox")
REGISTER_SENT = _(
    "If this address can be registered, we sent it a link to finish creating your account. "
    "The link works for 24 hours. Nothing arrived? Check your spam folder, or ask again later."
)
FORGOT_SENT = _(
    "If an account uses this address, we sent it an email with the next step. A password "
    "reset link works for 60 minutes. Nothing arrived? Check your spam folder, or ask again "
    "later."
)
LINK_INVALID_TITLE = _("Link not valid")
LINK_INVALID = _(
    "This link is not valid: it may have expired or been used already. Ask for a new one."
)
RESET_DONE_TITLE = _("Password changed")
RESET_DONE = _(
    "Your password was changed, and every device signed in to this account was signed out. "
    "Sign in with the new password."
)
REGISTER_THROTTLED = _("Too many registration attempts from your network. Try again later.")
FORGOT_THROTTLED = _("Too many requests from your network. Try again later.")


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _state(request: Request) -> Any:
    return request.app.state


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _accepted(request: Request, outcome: MailRequestOutcome, message: str) -> Response:
    """The one answer for every address. The outbox gets the request in a background task,
    which runs only after the response body was sent (ADR-0017)."""
    response = _notice_page(request, CHECK_INBOX, message)
    outbox = _state(request).mail_outbox
    if outcome.request is not None and outbox is not None:
        response.background = BackgroundTask(outbox.enqueue, outcome.request)
    return response


def _notice_page(
    request: Request,
    title: str,
    message: str,
    *,
    status_code: int = 200,
    sign_in_link: bool = False,
) -> Response:
    return render_template(
        request,
        "auth/email_notice.html",
        context={
            "title": _t(request, title),
            "message": _t(request, message),
            "sign_in_link": sign_in_link,
        },
        status_code=status_code,
    )


def _link_invalid(request: Request) -> Response:
    return _notice_page(request, LINK_INVALID_TITLE, LINK_INVALID, status_code=404)


# Registration


async def _require_registration(request: Request) -> None:
    state = _state(request)

    def check() -> bool:
        with state.session_factory() as session:
            return registration_open(session, state.settings)

    if not await run_in_threadpool(check):
        raise HTTPException(status_code=404)


def _register_page(
    request: Request,
    *,
    email: str = "",
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "auth/register.html",
        context={
            "email": email,
            "error": _t(request, error) if error else None,
            "notice": _t(request, notice) if notice else None,
        },
        status_code=status_code,
    )


@router.get("/register", name="register")
async def register_form(request: Request) -> Response:
    await _require_registration(request)
    if web_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _register_page(request)


@router.post("/register")
async def register_submit(request: Request, email: FormStr = "") -> Response:
    await _require_registration(request)
    if len(email) > MAX_EMAIL_LENGTH:
        return _register_page(
            request,
            email=email[:MAX_EMAIL_LENGTH],
            error=ERROR_MESSAGES["too_long"],
            status_code=400,
        )
    state = _state(request)
    try:
        outcome = await run_in_threadpool(
            lambda: start_registration(
                state.session_factory,
                email=email,
                settings=state.settings,
                clock=state.clock,
                host=client_host(request),
                locale=request.state.locale,
                log_key=state.mail_log_key,
            )
        )
    except RegistrationDisabled as exc:
        raise HTTPException(status_code=404) from exc
    if outcome.status == "invalid_email":
        return _register_page(
            request, email=email, error=ERROR_MESSAGES["invalid_email"], status_code=400
        )
    if outcome.status == "throttled":
        return _register_page(request, email=email, notice=REGISTER_THROTTLED, status_code=429)
    return _accepted(request, outcome, REGISTER_SENT)


def _complete_page(
    request: Request,
    token: str,
    email: str,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "auth/register_complete.html",
        context={
            "token": token,
            "email": email,
            "values": {"display_name": "", "workspace_name": ""} | (values or {}),
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "notice": _t(request, notice) if notice else None,
        },
        status_code=status_code,
    )


@router.get("/register/complete/{token}", name="register_complete")
async def register_complete_form(request: Request, token: str) -> Response:
    await _require_registration(request)
    if web_session(request) is not None:
        # Finishing a registration signs in; never on top of another account's session.
        return RedirectResponse("/", status_code=303)
    state = _state(request)
    email = await run_in_threadpool(registration_email, state.session_factory, token, state.clock)
    if email is None:
        return _link_invalid(request)
    return _complete_page(request, token, email)


@router.post("/register/complete/{token}")
async def register_complete_submit(
    request: Request,
    token: str,
    display_name: FormStr = "",
    password: FormStr = "",
    workspace_name: FormStr = "",
) -> Response:
    await _require_registration(request)
    if web_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    state = _state(request)
    values = {"display_name": display_name, "workspace_name": workspace_name}
    long_field = too_long(
        {
            "display_name": (display_name, MAX_NAME_LENGTH),
            "workspace_name": (workspace_name, MAX_NAME_LENGTH),
            "password": (password, MAX_PASSWORD_LENGTH),
        }
    )
    email = await run_in_threadpool(registration_email, state.session_factory, token, state.clock)
    if email is None:
        return _link_invalid(request)
    if long_field:
        return _complete_page(
            request,
            token,
            email,
            values=values,
            errors={long_field: ERROR_MESSAGES["too_long"]},
            status_code=400,
        )
    host = client_host(request)
    try:
        outcome = await complete_registration_async(
            state.session_factory,
            state.hash_gate,
            token=token,
            display_name=display_name,
            password=password,
            workspace_name=workspace_name,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=_user_agent(request),
            locale=request.state.locale,
        )
    except RegistrationDisabled as exc:
        raise HTTPException(status_code=404) from exc
    if outcome.status == "throttled":
        return _complete_page(
            request, token, email, values=values, notice=REGISTER_THROTTLED, status_code=429
        )
    if outcome.status in {"link_invalid", "rejected"}:
        # Only the owner of the address has this link; an account created meanwhile (by this
        # link in another tab, or otherwise) makes it spent.
        return _link_invalid(request)
    if outcome.status != "created" or outcome.user is None:
        code = outcome.error.code if outcome.error else ""
        field = ERROR_FIELDS.get(code, "password")
        return _complete_page(
            request,
            token,
            email,
            values=values,
            errors={field: ERROR_MESSAGES.get(code, ERROR_MESSAGES["invalid"])},
            status_code=400,
        )
    user_id = outcome.user.id
    session_id, expires_at = await run_in_threadpool(
        lambda: create_session_for(
            state.session_factory,
            user_id,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=_user_agent(request),
        )
    )
    return signed_in_redirect(request, "/", session_id, expires_at)


# Forgotten password


def _require_reset(request: Request) -> None:
    if not password_reset_available(_state(request).settings):
        raise HTTPException(status_code=404)


def _forgot_page(
    request: Request,
    *,
    email: str = "",
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "auth/forgot.html",
        context={
            "email": email,
            "error": _t(request, error) if error else None,
            "notice": _t(request, notice) if notice else None,
        },
        status_code=status_code,
    )


@router.get("/password/forgot", name="password_forgot")
async def forgot_form(request: Request) -> Response:
    _require_reset(request)
    return _forgot_page(request)


@router.post("/password/forgot")
async def forgot_submit(request: Request, email: FormStr = "") -> Response:
    _require_reset(request)
    if len(email) > MAX_EMAIL_LENGTH:
        return _forgot_page(
            request,
            email=email[:MAX_EMAIL_LENGTH],
            error=ERROR_MESSAGES["too_long"],
            status_code=400,
        )
    state = _state(request)
    try:
        outcome = await run_in_threadpool(
            lambda: request_password_reset(
                state.session_factory,
                email=email,
                settings=state.settings,
                clock=state.clock,
                host=client_host(request),
                locale=request.state.locale,
                log_key=state.mail_log_key,
            )
        )
    except PasswordResetUnavailable as exc:
        raise HTTPException(status_code=404) from exc
    if outcome.status == "invalid_email":
        return _forgot_page(
            request, email=email, error=ERROR_MESSAGES["invalid_email"], status_code=400
        )
    if outcome.status == "throttled":
        return _forgot_page(request, email=email, notice=FORGOT_THROTTLED, status_code=429)
    return _accepted(request, outcome, FORGOT_SENT)


def _reset_page(
    request: Request,
    token: str,
    email: str,
    *,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "auth/reset.html",
        context={
            "token": token,
            "email": email,
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "notice": _t(request, notice) if notice else None,
        },
        status_code=status_code,
    )


@router.get("/password/reset/{token}", name="password_reset")
async def reset_form(request: Request, token: str) -> Response:
    _require_reset(request)
    state = _state(request)
    target = await run_in_threadpool(reset_target, state.session_factory, token, state.clock)
    if target is None:
        return _link_invalid(request)
    return _reset_page(request, token, target.email)


@router.post("/password/reset/{token}")
async def reset_submit(
    request: Request, token: str, new_password: FormStr = "", confirm_password: FormStr = ""
) -> Response:
    _require_reset(request)
    state = _state(request)
    target = await run_in_threadpool(reset_target, state.session_factory, token, state.clock)
    if target is None:
        return _link_invalid(request)
    if len(new_password) > MAX_PASSWORD_LENGTH or len(confirm_password) > MAX_PASSWORD_LENGTH:
        return _reset_page(
            request,
            token,
            target.email,
            errors={"new_password": ERROR_MESSAGES["too_long"]},
            status_code=400,
        )
    outcome = await reset_password_async(
        state.session_factory,
        state.hash_gate,
        token=token,
        password=new_password,
        confirm_password=confirm_password,
        settings=state.settings,
        clock=state.clock,
        host=client_host(request),
        user_agent=_user_agent(request),
    )
    if outcome.status == "throttled":
        return _reset_page(request, token, target.email, notice=FORGOT_THROTTLED, status_code=429)
    if outcome.status == "link_invalid":
        return _link_invalid(request)
    if outcome.status == "mismatch":
        return _reset_page(
            request,
            token,
            target.email,
            errors={"confirm_password": ERROR_MESSAGES["password_mismatch"]},
            status_code=400,
        )
    if outcome.status == "invalid":
        code = outcome.error.code if outcome.error else ""
        return _reset_page(
            request,
            token,
            target.email,
            errors={"new_password": ERROR_MESSAGES.get(code, ERROR_MESSAGES["invalid"])},
            status_code=400,
        )
    return _notice_page(request, RESET_DONE_TITLE, RESET_DONE, sign_in_link=True)
