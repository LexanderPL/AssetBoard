"""The profile page (F1.5): settings, password, linked sign-in methods.

Linking and unlinking are sensitive: each goes through ``web.confirm.run_confirmed``. Raw
identities are never form input: an unlink names the identity's row id (only the owner's rows
are accepted), a proxy link takes the subject from the trusted proxy's headers on the POST itself.
"""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.db.engine import is_database_locked
from assetboard.db.models.identity import User
from assetboard.i18n import gettext
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.auth import WebSession
from assetboard.services.external_identity import PROXY_PROVIDER
from assetboard.services.oidc import OidcError
from assetboard.services.oidc_flow import FlowState
from assetboard.services.profile import (
    BROWSER_LOCALE,
    THEMES,
    ConfirmMethod,
    has_current_oidc_identity,
    link_identity_to_user,
    linked_identities,
    unlink_identity,
    update_profile,
)
from assetboard.services.proxy_auth import proxy_identity_owner
from assetboard.web.confirm import CONFIRM_FAILED, CONFIRM_STALE, confirm_view, run_confirmed
from assetboard.web.rendering import render_template
from assetboard.web.security import (
    client_host,
    oidc_flow_cookie_name,
    proxy_request,
    require_csrf,
    set_short_cookie,
    web_session,
)

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


LANGUAGE_NAMES = {"en": "English", "pl": "Polski"}
THEME_NAMES = {"system": _("Same as the system"), "light": _("Light"), "dark": _("Dark")}
PROFILE_ERRORS = {
    "display_name": _("Enter a display name of at most 120 characters."),
    "locale": _("Choose one of the listed languages."),
    "theme": _("Choose one of the listed themes."),
}
# ``/account?link=<code>`` after the OIDC round trip: an allowlist (oidc_routes.LINK_CODES).
LINK_MESSAGES: dict[str, tuple[bool, str]] = {
    "linked": (True, _("The sign-in method is linked to your account.")),
    "already_linked": (True, _("This sign-in is already linked to your account.")),
    "failed": (
        False,
        _("Linking failed. Try again; if it keeps failing, contact your administrator."),
    ),
    "cancelled": (False, _("Linking failed: it was cancelled at %(provider)s.")),
    "unavailable": (False, _("Linking failed: %(provider)s cannot be reached right now.")),
    "busy": (False, _("Linking failed: the server is busy. Try again in a moment.")),
}
UNLINK_LAST = _(
    "This is your last way to sign in, so it cannot be removed. Link another sign-in method "
    "or set a password first."
)
BUSY = _("The server is busy. Try again in a moment.")


def _state(request: Request) -> Any:
    return request.app.state


def _t(request: Request, message: str) -> str:
    provider = _state(request).settings.oidc_provider_name
    return gettext(message, request.state.locale) % {"provider": provider}


def _require_session(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:  # the gate redirects first; this guards direct use
        raise HTTPException(status_code=404)
    return current


def _proxy_subject(request: Request) -> str | None:
    proxy = proxy_request(request)
    return proxy.subject if proxy is not None else None


async def _profile_page(
    request: Request,
    *,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    current = _require_session(request)
    state = _state(request)
    settings = state.settings
    proxy_subject = _proxy_subject(request)

    def read() -> dict[str, Any]:
        with state.session_factory() as session:
            user = session.get(User, current.user_id)
            if user is None:
                raise HTTPException(status_code=404)
            identities = linked_identities(session, user.id, settings)
            proxy_owner = proxy_identity_owner(session, proxy_subject) if proxy_subject else None
            return {
                "user": user,
                "identities": identities,
                "can_link_oidc": settings.oidc_enabled
                and not has_current_oidc_identity(session, user.id, settings),
                # Offered only for a subject linked to nobody: one linked to another account
                # shows nothing at all.
                "proxy_link_subject": proxy_subject
                if proxy_subject is not None and proxy_owner is None
                else None,
                "has_password": user.password_hash is not None,
            }

    data = await run_in_threadpool(read)
    user: User = data["user"]
    return render_template(
        request,
        "account/profile.html",
        context={
            **data,
            "values": {
                "display_name": user.display_name,
                "locale": user.locale or BROWSER_LOCALE,
                "theme": user.theme,
            },
            "languages": [(BROWSER_LOCALE, _t(request, _("Browser language")))]
            + [(code, LANGUAGE_NAMES[code]) for code in sorted(SUPPORTED_LOCALES)],
            "themes": [(code, _t(request, THEME_NAMES[code])) for code in THEMES],
            "provider": settings.oidc_provider_name,
            "oidc_issuer": settings.oidc_issuer,
            "issuer_host": _issuer_host,
            "confirm": await confirm_view(request, current),
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


def _issuer_host(issuer: str | None) -> str:
    return (urlsplit(issuer).hostname or issuer or "") if issuer else ""


@router.get("/account", name="account")
async def profile(request: Request, link: str = "") -> Response:
    _require_session(request)
    outcome = LINK_MESSAGES.get(link)
    if outcome is None:
        return await _profile_page(request)
    ok, message = outcome
    text = _t(request, message)
    return await _profile_page(request, notice=text if ok else None, error=None if ok else text)


@router.post("/account/profile", name="account_profile")
async def profile_submit(
    request: Request,
    display_name: FormStr = "",
    locale: FormStr = "",
    theme: FormStr = "",
) -> Response:
    current = _require_session(request)
    state = _state(request)
    problem = await run_in_threadpool(
        lambda: update_profile(
            state.session_factory,
            user_id=current.user_id,
            display_name=display_name[:1000],
            locale=locale,
            theme=theme,
        )
    )
    if problem is not None:
        return await _profile_page(
            request, errors={problem: PROFILE_ERRORS[problem]}, status_code=400
        )
    # Shown with the new language: the next request reads the saved locale.
    return Response(status_code=303, headers={"Location": "/account"})


def _confirm_refusal(request: Request, status: str) -> tuple[str, int]:
    if status == "stale":
        return _t(request, CONFIRM_STALE), 403
    return _t(request, CONFIRM_FAILED), 401


# Linking


@router.post("/account/identities/oidc", name="account_link_oidc")
async def link_oidc(request: Request, confirm_password: FormStr = "") -> Response:
    current = _require_session(request)
    state = _state(request)
    client = state.oidc
    if client is None:
        raise HTTPException(status_code=404)
    outcome = await run_confirmed(
        request,
        current,
        purpose="profile_link_oidc",
        password=confirm_password,
        action=lambda _session, _user, method: method,
    )
    if outcome.status != "confirmed" or outcome.value is None:
        message, status = _confirm_refusal(request, outcome.status)
        return await _profile_page(request, error=message, status_code=status)
    try:
        authorization = await run_in_threadpool(client.begin)
    except OidcError:
        return await _profile_page(
            request, error=_t(request, LINK_MESSAGES["unavailable"][1]), status_code=503
        )
    flow = FlowState(
        authorization.state,
        authorization.nonce,
        authorization.code_verifier,
        "/account",
        link_user=current.user_id,
        link_session=current.session_hash,
        link_confirmed=outcome.value,
    )
    response = render_template(
        request,
        "account/oidc_continue.html",
        context={
            "provider": state.settings.oidc_provider_name,
            "authorization_url": authorization.url,
        },
    )
    set_short_cookie(
        response,
        state.settings,
        oidc_flow_cookie_name(state.settings),
        flow.seal(state.oidc_flow_key, now=state.clock.now()),
    )
    return response


@router.post("/account/identities/proxy", name="account_link_proxy")
async def link_proxy(request: Request, confirm_password: FormStr = "") -> Response:
    current = _require_session(request)
    state = _state(request)
    if not state.settings.proxy_auth_enabled:
        raise HTTPException(status_code=404)
    # The subject comes from the trusted proxy's headers on this very request, never from a
    # form field.
    proxy = proxy_request(request)
    identity = proxy.headers.identity if proxy is not None and proxy.headers else None
    if identity is None:
        return await _profile_page(
            request, error=_t(request, LINK_MESSAGES["failed"][1]), status_code=400
        )

    def link(session: Session, user: User, method: ConfirmMethod) -> str:
        return link_identity_to_user(
            session,
            user,
            provider=PROXY_PROVIDER,
            subject=identity.subject,
            email=identity.email,
            confirmed=method,
            clock=state.clock,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
        )

    try:
        outcome = await run_confirmed(
            request, current, purpose="profile_link_proxy", password=confirm_password, action=link
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return await _profile_page(request, error=_t(request, BUSY), status_code=503)
    if outcome.status != "confirmed":
        message, status = _confirm_refusal(request, outcome.status)
        return await _profile_page(request, error=message, status_code=status)
    if outcome.value in {"linked", "already_linked"}:
        key = "linked" if outcome.value == "linked" else "already_linked"
        return await _profile_page(request, notice=_t(request, LINK_MESSAGES[key][1]))
    # Linked to someone else, or anything else: the generic failure (no oracle).
    return await _profile_page(
        request, error=_t(request, LINK_MESSAGES["failed"][1]), status_code=409
    )


# Unlinking


@router.post("/account/identities/{identity_id}/unlink", name="account_unlink")
async def unlink(request: Request, identity_id: str, confirm_password: FormStr = "") -> Response:
    current = _require_session(request)
    state = _state(request)
    try:
        target = UUID(identity_id)
    except ValueError:
        raise HTTPException(status_code=404) from None

    def remove(session: Session, user: User, method: ConfirmMethod) -> str:
        return unlink_identity(
            session,
            user,
            target,
            current_session_hash=current.session_hash,
            confirmed=method,
            settings=state.settings,
            clock=state.clock,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
        )

    try:
        outcome = await run_confirmed(
            request, current, purpose="profile_unlink", password=confirm_password, action=remove
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return await _profile_page(request, error=_t(request, BUSY), status_code=503)
    if outcome.status != "confirmed":
        message, status = _confirm_refusal(request, outcome.status)
        return await _profile_page(request, error=message, status_code=status)
    if outcome.value == "not_found":
        raise HTTPException(status_code=404)
    if outcome.value == "last_method":
        return await _profile_page(request, error=_t(request, UNLINK_LAST), status_code=409)
    return await _profile_page(
        request, notice=_t(request, _("The sign-in method was removed from your account."))
    )
