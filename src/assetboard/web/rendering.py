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


# The stable labels and icons of the sidebar destinations (F9.12); the profile customiser uses
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
# Heroicons v2.2.0 outline paths (MIT), 24x24 viewBox, rendered at 20x20 with currentColor.
# Source: https://github.com/tailwindlabs/heroicons/tree/v2.2.0/src/24/outline
# Commit: 0435d4ca364a608cc75e2f8683d374e55abbae26. The path data is copied at development
# time; nothing is fetched at runtime. See static/vendor/README.md for the licence record.
SIDEBAR_ICONS: dict[str, tuple[str, ...]] = {
    "home": (
        "M2.25 12L11.2045 3.04549C11.6438 2.60615 12.3562 2.60615 12.7955 3.04549L21.75 12M4.5 "
        "9.75V19.875C4.5 20.4963 5.00368 21 5.625 21H9.75V16.125C9.75 15.5037 10.2537 15 10.875"
        " 15H13.125C13.7463 15 14.25 15.5037 14.25 16.125V21H18.375C18.9963 21 19.5 20.4963 19."
        "5 19.875V9.75M8.25 21H16.5",
    ),
    "month": (
        "M6.75 3V5.25M17.25 3V5.25M3 18.75V7.5C3 6.25736 4.00736 5.25 5.25 5.25H18.75C19.9926 5"
        ".25 21 6.25736 21 7.5V18.75M3 18.75C3 19.9926 4.00736 21 5.25 21H18.75C19.9926 21 21 1"
        "9.9926 21 18.75M3 18.75V11.25C3 10.0074 4.00736 9 5.25 9H18.75C19.9926 9 21 10.0074 21"
        " 11.25V18.75M12 12.75H12.0075V12.7575H12V12.75ZM12 15H12.0075V15.0075H12V15ZM12 17.25H"
        "12.0075V17.2575H12V17.25ZM9.75 15H9.7575V15.0075H9.75V15ZM9.75 17.25H9.7575V17.2575H9."
        "75V17.25ZM7.5 15H7.5075V15.0075H7.5V15ZM7.5 17.25H7.5075V17.2575H7.5V17.25ZM14.25 12.7"
        "5H14.2575V12.7575H14.25V12.75ZM14.25 15H14.2575V15.0075H14.25V15ZM14.25 17.25H14.2575V"
        "17.2575H14.25V17.25ZM16.5 12.75H16.5075V12.7575H16.5V12.75ZM16.5 15H16.5075V15.0075H16"
        ".5V15Z",
    ),
    "transactions": (
        "M8.25 6.75H20.25M8.25 12H20.25M8.25 17.25H20.25M3.75 6.75H3.7575V6.7575H3.75V6.75ZM4.1"
        "25 6.75C4.125 6.95711 3.95711 7.125 3.75 7.125C3.54289 7.125 3.375 6.95711 3.375 6.75C"
        "3.375 6.54289 3.54289 6.375 3.75 6.375C3.95711 6.375 4.125 6.54289 4.125 6.75ZM3.75 12"
        "H3.7575V12.0075H3.75V12ZM4.125 12C4.125 12.2071 3.95711 12.375 3.75 12.375C3.54289 12."
        "375 3.375 12.2071 3.375 12C3.375 11.7929 3.54289 11.625 3.75 11.625C3.95711 11.625 4.1"
        "25 11.7929 4.125 12ZM3.75 17.25H3.7575V17.2575H3.75V17.25ZM4.125 17.25C4.125 17.4571 3"
        ".95711 17.625 3.75 17.625C3.54289 17.625 3.375 17.4571 3.375 17.25C3.375 17.0429 3.542"
        "89 16.875 3.75 16.875C3.95711 16.875 4.125 17.0429 4.125 17.25Z",
    ),
    "history": (
        "M21 21L15.8033 15.8033M15.8033 15.8033C17.1605 14.4461 18 12.5711 18 10.5C18 6.35786 1"
        "4.6421 3 10.5 3C6.35786 3 3 6.35786 3 10.5C3 14.6421 6.35786 18 10.5 18C12.5711 18 14."
        "4461 17.1605 15.8033 15.8033Z",
    ),
    "reports": (
        "M3 13.125C3 12.5037 3.50368 12 4.125 12H6.375C6.99632 12 7.5 12.5037 7.5 13.125V19.875"
        "C7.5 20.4963 6.99632 21 6.375 21H4.125C3.50368 21 3 20.4963 3 19.875V13.125Z",
        "M9.75 8.625C9.75 8.00368 10.2537 7.5 10.875 7.5H13.125C13.7463 7.5 14.25 8.00368 14.25"
        " 8.625V19.875C14.25 20.4963 13.7463 21 13.125 21H10.875C10.2537 21 9.75 20.4963 9.75 1"
        "9.875V8.625Z",
        "M16.5 4.125C16.5 3.50368 17.0037 3 17.625 3H19.875C20.4963 3 21 3.50368 21 4.125V19.87"
        "5C21 20.4963 20.4963 21 19.875 21H17.625C17.0037 21 16.5 20.4963 16.5 19.875V4.125Z",
    ),
    "upcoming": (
        "M12 6V12H16.5M21 12C21 16.9706 16.9706 21 12 21C7.02944 21 3 16.9706 3 12C3 7.02944 7."
        "02944 3 12 3C16.9706 3 21 7.02944 21 12Z",
    ),
    "recurring": (
        "M16.0228 9.34841H21.0154V9.34663M2.98413 19.6444V14.6517M2.98413 14.6517L7.97677 14.65"
        "17M2.98413 14.6517L6.16502 17.8347C7.15555 18.8271 8.41261 19.58 9.86436 19.969C14.265"
        "4 21.1483 18.7892 18.5364 19.9685 14.1353M4.03073 9.86484C5.21 5.46374 9.73377 2.85194"
        " 14.1349 4.03121C15.5866 4.4202 16.8437 5.17312 17.8342 6.1655L21.0154 9.34663M21.0154"
        " 4.3558V9.34663",
    ),
    "income": (
        "M2.25 18.75C7.71719 18.75 13.0136 19.4812 18.0468 20.8512C18.7738 21.0491 19.5 20.5086"
        " 19.5 19.7551V18.75M3.75 4.5V5.25C3.75 5.66421 3.41421 6 3 6H2.25M2.25 6V5.625C2.25 5."
        "00368 2.75368 4.5 3.375 4.5H20.25M2.25 6V15M20.25 4.5V5.25C20.25 5.66421 20.5858 6 21 "
        "6H21.75M20.25 4.5H20.625C21.2463 4.5 21.75 5.00368 21.75 5.625V15.375C21.75 15.9963 21"
        ".2463 16.5 20.625 16.5H20.25M21.75 15H21C20.5858 15 20.25 15.3358 20.25 15.75V16.5M20."
        "25 16.5H3.75M3.75 16.5H3.375C2.75368 16.5 2.25 15.9963 2.25 15.375V15M3.75 16.5V15.75C"
        "3.75 15.3358 3.41421 15 3 15H2.25M15 10.5C15 12.1569 13.6569 13.5 12 13.5C10.3431 13.5"
        " 9 12.1569 9 10.5C9 8.84315 10.3431 7.5 12 7.5C13.6569 7.5 15 8.84315 15 10.5ZM18 10.5"
        "H18.0075V10.5075H18V10.5ZM6 10.5H6.0075V10.5075H6V10.5Z",
    ),
    "taxes": (
        "M9 14.25L15 8.25M19.5 4.75699V21.75L15.75 20.25L12 21.75L8.25 20.25L4.5 21.75V4.75699C"
        "4.5 3.649 5.30608 2.70014 6.40668 2.57241C8.24156 2.35947 10.108 2.25 12 2.25C13.892 2"
        ".25 15.7584 2.35947 17.5933 2.57241C18.6939 2.70014 19.5 3.649 19.5 4.75699ZM9.75 9H9."
        "7575V9.0075H9.75V9ZM10.125 9C10.125 9.20711 9.95711 9.375 9.75 9.375C9.54289 9.375 9.3"
        "75 9.20711 9.375 9C9.375 8.79289 9.54289 8.625 9.75 8.625C9.95711 8.625 10.125 8.79289"
        " 10.125 9ZM14.25 13.5H14.2575V13.5075H14.25V13.5ZM14.625 13.5C14.625 13.7071 14.4571 1"
        "3.875 14.25 13.875C14.0429 13.875 13.875 13.7071 13.875 13.5C13.875 13.2929 14.0429 13"
        ".125 14.25 13.125C14.4571 13.125 14.625 13.2929 14.625 13.5Z",
    ),
    "net_worth": (
        "M12 3V20.25M12 20.25C10.528 20.25 9.1179 20.515 7.81483 21M12 20.25C13.472 20.25 14.88"
        "21 20.515 16.1852 21M18.75 4.97089C16.5446 4.66051 14.291 4.5 12 4.5C9.70897 4.5 7.455"
        "42 4.66051 5.25 4.97089M18.75 4.97089C19.7604 5.1131 20.7608 5.28677 21.75 5.49087M18."
        "75 4.97089L21.3704 15.6961C21.4922 16.1948 21.2642 16.7237 20.7811 16.8975C20.1468 17."
        "1257 19.4629 17.25 18.75 17.25C18.0371 17.25 17.3532 17.1257 16.7189 16.8975C16.2358 1"
        "6.7237 16.0078 16.1948 16.1296 15.6961L18.75 4.97089ZM2.25 5.49087C3.23922 5.28677 4.2"
        "3956 5.1131 5.25 4.97089M5.25 4.97089L7.87036 15.6961C7.9922 16.1948 7.76419 16.7237 7"
        ".28114 16.8975C6.6468 17.1257 5.96292 17.25 5.25 17.25C4.53708 17.25 3.8532 17.1257 3."
        "21886 16.8975C2.73581 16.7237 2.5078 16.1948 2.62964 15.6961L5.25 4.97089Z",
    ),
    "investments": (
        "M2.25 17.9999L9 11.2499L13.3064 15.5564C14.5101 13.188 16.5042 11.2022 19.1203 10.0375"
        "L21.8609 8.81726M21.8609 8.81726L15.9196 6.53662M21.8609 8.81726L19.5802 14.7585",
    ),
    "loans": (
        "M2.25 8.25H21.75M2.25 9H21.75M5.25 14.25H11.25M5.25 16.5H8.25M4.5 19.5H19.5C20.7426 19"
        ".5 21.75 18.4926 21.75 17.25V6.75C21.75 5.50736 20.7426 4.5 19.5 4.5H4.5C3.25736 4.5 2"
        ".25 5.50736 2.25 6.75V17.25C2.25 18.4926 3.25736 19.5 4.5 19.5Z",
    ),
    "fire": (
        "M15.3622 5.21361C18.2427 6.50069 20.25 9.39075 20.25 12.7497C20.25 17.306 16.5563 20.9"
        "997 12 20.9997C7.44365 20.9997 3.75 17.306 3.75 12.7497C3.75 10.5376 4.62058 8.52889 6"
        ".03781 7.04746C6.8043 8.11787 7.82048 8.99731 9.00121 9.60064C9.04632 6.82497 10.348 4"
        ".35478 12.3621 2.73413C13.1255 3.75788 14.1379 4.61821 15.3622 5.21361Z",
        "M12 18C14.0711 18 15.75 16.3211 15.75 14.25C15.75 12.3467 14.3321 10.7746 12.4949 10.5"
        "324C11.4866 11.437 10.7862 12.6779 10.5703 14.0787C9.78769 13.8874 9.06529 13.5425 8.4"
        "3682 13.0779C8.31559 13.4467 8.25 13.8407 8.25 14.25C8.25 16.3211 9.92893 18 12 18Z",
    ),
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
        sidebar_icons=SIDEBAR_ICONS,
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
