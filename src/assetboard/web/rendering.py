from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from assetboard.domain.money import Money
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.i18n import translations_for
from assetboard.i18n.numbers import format_money
from assetboard.web.security import apply_new_csrf_cookie, csrf_token, web_session

_TEMPLATE_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=2)
def templates_for(locale: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=_TEMPLATE_DIR)
    templates.env.add_extension("jinja2.ext.i18n")
    translations = translations_for(locale)
    cast(Any, templates.env).install_gettext_translations(translations, newstyle=True)
    templates.env.globals.update(Money=Money, format_money=format_money)
    templates.env.globals.update(
        static_url=static_url,
        scoped_url=scoped_url,
        uuid4=uuid4,
        csrf_token=csrf_token,
        month_nav_links=month_nav_links,
    )
    return templates


def scoped_url(request: Request, route_name: str, **path_params: str) -> str:
    route_path = request.app.url_path_for(route_name, **path_params)
    root_path = request.scope.get("root_path", "").rstrip("/")
    return f"{root_path}{route_path}"


# The pages that show one month (``/m/YYYY-MM`` and ``/transactions/YYYY-MM``): while one of
# them is shown, the sidebar's Month and Transactions links keep that month.
_MONTH_PAGE_ROUTES = frozenset({"month", "transactions_month"})


def viewed_month(request: Request) -> YearMonth | None:
    """The month of the Month or Transactions page being shown; ``None`` on any other page."""
    route = request.scope.get("route")
    if getattr(route, "name", None) not in _MONTH_PAGE_ROUTES:
        return None
    try:
        return YearMonth.parse(str(request.path_params.get("month", "")))
    except InvalidMonth:
        return None


def month_nav_links(request: Request) -> tuple[str, str]:
    """The sidebar's Month and Transactions hrefs: the viewed month's pages while the Month or
    Transactions page of a month is shown, otherwise the current-month redirects (``/m``,
    ``/transactions``)."""
    month = viewed_month(request)
    if month is None:
        return scoped_url(request, "month_current"), scoped_url(request, "transactions")
    return (
        scoped_url(request, "month", month=str(month)),
        scoped_url(request, "transactions_month", month=str(month)),
    )


def static_url(request: Request, path: str) -> str:
    return f"{scoped_url(request, 'static', path=path)}?v={request.app.version}"


def render_template(
    request: Request,
    name: str,
    *,
    context: dict[str, object] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    locale = request.state.locale
    settings = request.app.state.settings
    template_context: dict[str, object] = {
        "locale": locale,
        "app_name": request.app.state.app_name,
        "default_currency": settings.default_currency,
        "web_session": web_session(request),
        # Set for every signed-in page with a workspace; see load_workspace_nav in web/context.py.
        "workspace_nav": getattr(request.state, "workspace_nav", None),
    }
    if context:
        template_context.update(context)
    response = templates_for(locale).TemplateResponse(
        request=request,
        name=name,
        context=template_context,
        status_code=status_code,
    )
    # A form rendered for a signed-out visitor may have created the anonymous CSRF secret.
    apply_new_csrf_cookie(request, response)
    return response
