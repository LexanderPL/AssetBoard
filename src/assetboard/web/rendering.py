from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from assetboard.domain.money import Money
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.domain.navigation import DEFAULT_SIDEBAR, HOME_KEY
from assetboard.i18n import gettext, translations_for
from assetboard.i18n.dates import format_day, format_time
from assetboard.i18n.numbers import format_money, format_price
from assetboard.web.pwa import DARK_THEME_COLOR, LIGHT_THEME_COLOR
from assetboard.web.security import apply_new_csrf_cookie, csrf_token, web_session

if TYPE_CHECKING:
    from assetboard.services.context import WorkspaceNav

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _(message: str) -> str:
    """Marks a message for extraction; the templates translate it at render time."""
    return message


# The stable labels and glyphs of the sidebar destinations (F9.12); the profile customiser uses
# the same labels, so a destination is named identically in both places.
SIDEBAR_LABELS: dict[str, str] = {
    "home": _("Home"),
    "month": _("Month"),
    "transactions": _("Transactions"),
    "history": _("History"),
    "reports": _("Reports"),
    "upcoming": _("Upcoming"),
    "recurring": _("Recurring"),
    "income": _("Income"),
    "taxes": _("Taxes"),
    "net_worth": _("Net worth"),
    "investments": _("Investments"),
    "loans": _("Loans"),
    "fire": _("FIRE projection"),
}
SIDEBAR_GLYPHS: dict[str, str] = {
    "home": "⌂",
    "month": "◷",
    "transactions": "≡",
    "history": "⌕",
    "reports": "▦",
    "upcoming": "◔",
    "recurring": "↻",
    "income": "¤",
    "taxes": "%",
    "net_worth": "◇",
    "investments": "↗",
    "loans": "▤",
    "fire": "✦",
}


def sidebar_labels(locale: str) -> dict[str, str]:
    """The sidebar labels in ``locale`` (the profile customiser shows the same names)."""
    return {key: gettext(message, locale) for key, message in SIDEBAR_LABELS.items()}


@dataclass(frozen=True, slots=True)
class SidebarLink:
    key: str
    href: str
    current: bool


def format_moment_utc(moment: datetime, locale: str) -> str:
    """An instant as the locale writes a day and time, marked UTC (instance-wide events)."""
    local = moment.astimezone(UTC)
    return f"{format_day(local.date(), locale)} {format_time(local.strftime('%H:%M'), locale)} UTC"


@lru_cache(maxsize=2)
def templates_for(locale: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=_TEMPLATE_DIR)
    templates.env.add_extension("jinja2.ext.i18n")
    translations = translations_for(locale)
    cast(Any, templates.env).install_gettext_translations(translations, newstyle=True)
    templates.env.globals.update(Money=Money, format_money=format_money, format_price=format_price)
    templates.env.globals.update(
        static_url=static_url,
        scoped_url=scoped_url,
        uuid4=uuid4,
        csrf_token=csrf_token,
        format_moment_utc=format_moment_utc,
        month_nav_links=month_nav_links,
        sidebar_links=sidebar_links,
        sidebar_labels=sidebar_labels(locale),
        sidebar_glyphs=SIDEBAR_GLYPHS,
        theme_color_light=LIGHT_THEME_COLOR,
        theme_color_dark=DARK_THEME_COLOR,
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


def sidebar_links(request: Request, nav: WorkspaceNav | None) -> tuple[SidebarLink, ...]:
    """The visible destinations in the user's saved order, with their href and current state.

    Home is always present. Without a workspace context only Home is shown (the account, admin
    and audit pages of a user with no membership). Taxes appears only when the workspace enables
    the feature. Hiding a destination in the sidebar never removes access, but a disabled feature
    is different: while the Taxes switch is off, its pages and forms answer 404; the legacy
    calculator URL redirects to the Taxes page, which then answers 404.
    """
    path = request.url.path
    items = nav.sidebar if nav is not None else DEFAULT_SIDEBAR
    month_href, transactions_href = ("", "")
    if nav is not None:
        month_href, transactions_href = month_nav_links(request)
    taxes_enabled = nav is not None and nav.current.taxes_enabled
    links: list[SidebarLink] = []
    for item in items:
        key = item.key
        if key == HOME_KEY:
            href = scoped_url(request, "home")
            links.append(SidebarLink(key, href, path == href))
            continue
        if nav is None or not item.visible:
            continue
        if key == "taxes" and not taxes_enabled:
            continue
        if key == "month":
            base = scoped_url(request, "month_current")
            current = path == base or path.startswith(base + "/")
            href = month_href
        elif key == "transactions":
            base = scoped_url(request, "transactions")
            current = path == base or path.startswith(base + "/")
            href = transactions_href
        elif key == "history":
            href = scoped_url(request, "history")
            current = path == href or path.startswith(href + "/")
        elif key == "reports":
            href = scoped_url(request, "reports")
            current = path == href or path.startswith(href + "/")
        elif key == "upcoming":
            href = scoped_url(request, "scheduled_upcoming")
            current = path.endswith("/upcoming")
        elif key == "recurring":
            href = scoped_url(request, "scheduled_items")
            current = "/recurring" in path
        elif key == "income":
            href = scoped_url(request, "income_sources")
            current = path == href or path.startswith(href + "/")
        elif key == "taxes":
            href = scoped_url(request, "tax_rules")
            current = "/taxes" in path
        elif key == "net_worth":
            href = scoped_url(request, "networth_summary")
            base = scoped_url(request, "networth")
            current = path == base or path.startswith(base + "/")
        elif key == "investments":
            href = scoped_url(request, "investments_portfolio")
            base = scoped_url(request, "investments")
            current = path == base or path.startswith(base + "/")
        elif key == "loans":
            href = scoped_url(request, "loans")
            current = path == href or path.startswith(href + "/")
        elif key == "fire":
            href = scoped_url(request, "fire_scenarios")
            current = path == href or path.startswith(href + "/")
        else:
            # Stored unknown keys never reach this loop (normalize_sidebar drops them); a new
            # canonical key without an href/current rule fails loudly instead of disappearing.
            raise KeyError(f"sidebar destination {key!r} has no link rule")
        links.append(SidebarLink(key, href, current))
    return tuple(links)


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
    current_session = web_session(request)
    template_context: dict[str, object] = {
        "locale": locale,
        "app_name": request.app.state.app_name,
        "default_currency": settings.default_currency,
        "web_session": current_session,
        # Every page of a demo session shows the non-dismissable banner (F9.11b).
        "demo_banner": {
            "time": format_time(settings.demo_reset_time, locale),
            "timezone": settings.demo_timezone,
        }
        if current_session is not None and current_session.is_demo
        else None,
        # Templates hide account, workspace and provider controls for the public demo.
        "demo_session": current_session is not None and current_session.is_demo,
        # Set for every signed-in page with a workspace; see load_workspace_nav in web/context.py.
        "workspace_nav": getattr(request.state, "workspace_nav", None),
        # The running version (sidebar footer), and for administrators a newer release.
        "app_version": request.app.version,
        "update_notice": getattr(request.state, "update_notice", None),
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
