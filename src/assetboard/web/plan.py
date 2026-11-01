"""The month summary block (F2.3 part B, F2.4): view models and messages.

The block is ``#month-summary`` (``months/_summary.html``): the in/out flow (``InOutView``: where
the money came from above how it was used), the Out and In planned-vs-actual bars and one row per
category in the summary's groups (expenses, saved / invested, income, withdrawn from savings, and
the plan-only "Transfers (not counted)" while such lines exist), each with its planned value
(F2.3: editable inline for editors and owners while the month is open), actual and difference
(F2.4), and a totals row per group. The groups sit in two tables, money out (expenses, saved /
invested) and money in (income, withdrawn), each with its total (Out and In). A category without
a planned value has no difference, tint or "over plan" hint, and a group's total difference counts
only its planned categories. Home keeps the older flow bar of earnings (``KpiView``).

Shared by ``web.month_routes`` (the Month page), ``web.routes`` (Home shows the flow bar) and
``web.plan_routes`` (the planned-value writes). Everything here
is read-only; writes and their authorization live in ``services.budget``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.categories import Treatment
from assetboard.domain.money import Money
from assetboard.domain.summary import (
    BLOCK_ORDER,
    FLOW_SCALE,
    FlowBar,
    InOutFlow,
    SortOrder,
    SummaryBlock,
    SummaryRow,
    flow_bar,
    in_out_flow,
)
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_month_after_from
from assetboard.i18n.numbers import format_amount_input, format_money, format_rate_tenths
from assetboard.services.budget import CopyResult, PlanCategory, get_plan
from assetboard.services.context import Context, Role, has_role
from assetboard.services.months import MonthView
from assetboard.services.summary import SummaryData, month_summary
from assetboard.web.security import summary_sort_cookie_name


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


GROUP_HEADINGS = {
    "expense": _("Expenses"),
    "saved": _("Saved / invested"),
    "income": _("Income"),
    "withdrawn": _("Withdrawn from savings"),
    "transfer": _("Transfers (not counted)"),
}
# The two tables of the summary (owner, 2026-10-31 and 2026-11-01): their groups.
MONEY_BLOCKS = (
    ("out", ("expense", "saved")),
    ("in", ("income", "withdrawn")),
)
# One message per ``services.budget.InvalidPlannedValue`` code.
PLAN_ERRORS = {
    "invalid_amount": _("Enter a valid amount."),
    "amount_negative": _("A planned value cannot be negative."),
    "amount_too_large": _("The amount is too large."),
    "too_many_decimals": _("Too many decimal places for %(currency)s."),
    "category_archived": _("This category is archived: it takes no new planned values."),
    "category_transfer": _("Transfers are not counted in the plan: this category takes none."),
}
PLAN_STATUS = {
    "created": _("Planned value saved."),
    "updated": _("Planned value saved."),
    "unchanged": _("Planned value saved."),
    "removed": _("Planned value removed."),
}


@dataclass(frozen=True, slots=True)
class CellState:
    """What a request adds to one planned cell: the typed value and its message."""

    category_id: UUID
    value: str
    error: str | None = None
    focus: bool = False


@dataclass(frozen=True, slots=True)
class CopiedNotice:
    count: int
    skipped: int
    source: str  # the source month after "from"
    automatic: bool


@dataclass(frozen=True, slots=True)
class PlanRowView:
    category_id: str
    name: str
    color: str
    archived: bool
    editable: bool  # the planned cell is an input (writers; plannable or with a line)
    value: str  # the input's value (locale decimal sign, no grouping); "" without a line
    saved_value: str  # the stored value in the same form (Esc restores it)
    display: str  # the formatted planned amount for read-only cells ("" without a line)
    error: str | None
    focus: bool
    actual: str = ""  # "" for the plan-only transfer group
    pending: str = ""  # expected but not confirmed yet ("" when nothing is pending)
    forecast: str = ""  # actual + pending: "If everything pending arrives: ..."
    # "" without a planned value (owner, 2026-11-01: nothing to compare with).
    difference: str = ""
    tone: str = ""  # "favourable", "unfavourable" or "even" (for the difference); "" without
    has_plan: bool = False  # a planned value above 0
    # Actual above plan: "danger" for an expense category, "success" for an income one (the
    # row's background tint); "" otherwise, and for the other groups.
    over: str = ""
    # Not above plan yet, but actual + pending is: the same tone as ``over`` (a softer hint
    # under the pending line, no tint); "" otherwise.
    pending_over: str = ""


@dataclass(frozen=True, slots=True)
class TotalsView:
    planned: str
    actual: str
    pending: str  # "" when nothing is pending
    forecast: str
    difference: str  # "" when no category of the group has a planned value
    tone: str


@dataclass(frozen=True, slots=True)
class PlanGroupView:
    key: str
    heading: str
    rows: tuple[PlanRowView, ...]
    totals: TotalsView | None = None  # None for the plan-only transfer group


@dataclass(frozen=True, slots=True)
class MoneyBlockView:
    """A table of the summary: money out (expenses, saved / invested) or money in (income,
    withdrawn from savings), with its total (planned, actual and pending)."""

    key: str  # "out" or "in"
    groups: tuple[PlanGroupView, ...]
    totals: TotalsView


@dataclass(frozen=True, slots=True)
class KpiView:
    earned: str
    spent: str
    forecast_earned: str  # Home's forecast line
    forecast_spent: str
    saved: str
    net: str
    net_tone: str
    savings_rate: str | None  # None: nothing earned (no caption)
    overspend: str | None = None  # spending and saving above earnings (-net), else None
    flow: FlowBar = FlowBar(0, 0, 0)  # positions on 0..flow_scale (domain.summary.flow_bar)
    forecast_flow: FlowBar = FlowBar(0, 0, 0)  # Home's second, thinner bar
    flow_scale: int = FLOW_SCALE


@dataclass(frozen=True, slots=True)
class InOutView:
    """The Month page's flow (owner, 2026-10-31): In = earned + withdrawn from savings, Out =
    spent + saved / invested, left = In - Out ("short by" when negative). Amounts are formatted;
    a pending part is "" when nothing is pending."""

    total_in: str
    earned: str
    withdrawn: str
    total_out: str
    spent: str
    saved: str
    left: str  # the absolute value: ``short`` says the sign
    short: bool  # Out above In (confirmed)
    in_pending: str
    earned_pending: str
    withdrawn_pending: str
    out_pending: str
    spent_pending: str
    saved_pending: str
    forecast_left: str  # left (absolute) if everything pending arrives
    forecast_short: bool
    has_withdrawn: bool  # something withdrawn, confirmed or pending (the strip shows it)
    has_pending: bool
    zero: str  # 0 in the currency (the screen-reader text's empty parts)
    flow: InOutFlow  # with the pending parts
    confirmed_flow: InOutFlow  # without them (the "Show pending" toggle off)
    savings_rate: str | None  # saved / earned (None: nothing earned)


@dataclass(frozen=True, slots=True)
class BarView:
    key: str  # "expense" or "income" (Home); "out" or "in" (the Month page)
    planned: str
    actual: str
    planned_width: int  # 0-100, of max(planned, actual + pending)
    actual_width: int
    over: bool  # actual above planned
    has_plan: bool = True  # a planned amount above 0 (the bar shows its marker)
    pending: str = ""  # "" when nothing is pending
    pending_end: int = 0  # 0-100: the end of the striped pending part (it starts at actual)
    forecast: str = ""  # actual + pending
    pending_over: bool = False  # not over plan yet, but actual + pending is


@dataclass(frozen=True, slots=True)
class SummaryView:
    month: str
    can_write: bool  # editor or owner, and the month is open
    groups: tuple[PlanGroupView, ...]  # only groups with rows
    banner_source: str | None  # "Copy plan from <month>" is offered (month after "from")
    copied: CopiedNotice | None
    kpis: KpiView | None = None
    bars: tuple[BarView, ...] = ()  # Home: spent and earned against their plans
    in_out: InOutView | None = None  # the Month page's flow
    in_out_bars: tuple[BarView, ...] = ()  # the Month page: Out and In against their plans
    money_blocks: tuple[MoneyBlockView, ...] = ()  # the money out and money in tables
    sort: str = "actual"
    filter_id: str = ""  # the category the transaction tables are filtered to (aria-current)
    future: bool = False  # a future month: the flow bar is a projection ("Expected spent ...")
    source: SummaryData | None = field(default=None, compare=False, repr=False)  # the donuts

    def row(self, category_id: UUID) -> PlanRowView | None:
        wanted = str(category_id)
        return next(
            (r for g in self.groups for r in g.rows if r.category_id == wanted),
            None,
        )

    def group_of(self, category_id: UUID) -> PlanGroupView | None:
        wanted = str(category_id)
        return next((g for g in self.groups if any(r.category_id == wanted for r in g.rows)), None)


def _tone(amount: Money) -> str:
    if amount.amount_minor > 0:
        return "favourable"
    return "unfavourable" if amount.amount_minor < 0 else "even"


def _signed(amount: Money, locale: str) -> str:
    """A difference with its sign: ``+€400.00``, ``-€20.00``, ``€0.00``."""
    text = format_money(amount, locale)
    return f"+{text}" if amount.amount_minor > 0 else text


def _width(part: int, whole: int) -> int:
    return part * 100 // whole if whole > 0 else 0


def _pending(amount: Money, locale: str) -> str:
    """A pending amount for display; "" when nothing is pending."""
    return format_money(amount, locale) if amount.amount_minor > 0 else ""


def _bar(key: str, planned: Money, actual: Money, expected: Money, locale: str) -> BarView:
    pending = max(expected.amount_minor, 0)
    forecast = actual.amount_minor + pending
    scale = max(planned.amount_minor, actual.amount_minor, forecast)
    over = actual.amount_minor > planned.amount_minor
    return BarView(
        key=key,
        planned=format_money(planned, locale),
        actual=format_money(actual, locale),
        planned_width=_width(planned.amount_minor, scale),
        actual_width=_width(actual.amount_minor, scale),
        over=over,
        has_plan=planned.amount_minor > 0,
        pending=_pending(expected, locale),
        pending_end=_width(forecast, scale),
        forecast=format_money(Money(forecast, actual.currency), locale),
        pending_over=not over and pending > 0 and forecast > planned.amount_minor,
    )


def _sum_blocks(blocks: tuple[SummaryBlock, ...]) -> tuple[Money, Money, Money]:
    """Planned, actual and pending of several summary blocks together (Out or In)."""
    currency = blocks[0].planned.currency
    return (
        Money(sum(b.planned.amount_minor for b in blocks), currency),
        Money(sum(b.actual.amount_minor for b in blocks), currency),
        Money(sum(max(b.expected.amount_minor, 0) for b in blocks), currency),
    )


def _block_totals(planned: Money, actual: Money, expected: Money, locale: str) -> TotalsView:
    """A money out or money in table's total. It has no difference: its groups' differences have
    different signs (saving more is favourable, spending more is not)."""
    forecast = Money(actual.amount_minor + expected.amount_minor, actual.currency)
    return TotalsView(
        planned=format_money(planned, locale),
        actual=format_money(actual, locale),
        pending=_pending(expected, locale),
        forecast=format_money(forecast, locale),
        difference="",
        tone="",
    )


def _in_out(
    currency: str,
    expense: SummaryBlock,
    saved: SummaryBlock,
    income: SummaryBlock,
    withdrawn: SummaryBlock,
    savings_rate: str | None,
    locale: str,
) -> InOutView:
    """The Month page's flow from the four summary blocks (see ``InOutView``)."""

    def money(minor: int) -> str:
        return format_money(Money(minor, currency), locale)

    def maybe(minor: int) -> str:
        return money(minor) if minor > 0 else ""

    amounts = {
        "earned": income.actual.amount_minor,
        "withdrawn": withdrawn.actual.amount_minor,
        "spent": expense.actual.amount_minor,
        "saved": saved.actual.amount_minor,
    }
    pendings = {
        "earned_pending": max(income.expected.amount_minor, 0),
        "withdrawn_pending": max(withdrawn.expected.amount_minor, 0),
        "spent_pending": max(expense.expected.amount_minor, 0),
        "saved_pending": max(saved.expected.amount_minor, 0),
    }
    total_in = amounts["earned"] + amounts["withdrawn"]
    total_out = amounts["spent"] + amounts["saved"]
    in_pending = pendings["earned_pending"] + pendings["withdrawn_pending"]
    out_pending = pendings["spent_pending"] + pendings["saved_pending"]
    left = total_in - total_out
    forecast_left = left + in_pending - out_pending
    return InOutView(
        total_in=money(total_in),
        earned=money(amounts["earned"]),
        withdrawn=money(amounts["withdrawn"]),
        total_out=money(total_out),
        spent=money(amounts["spent"]),
        saved=money(amounts["saved"]),
        left=money(abs(left)),
        short=left < 0,
        in_pending=maybe(in_pending),
        earned_pending=maybe(pendings["earned_pending"]),
        withdrawn_pending=maybe(pendings["withdrawn_pending"]),
        out_pending=maybe(out_pending),
        spent_pending=maybe(pendings["spent_pending"]),
        saved_pending=maybe(pendings["saved_pending"]),
        forecast_left=money(abs(forecast_left)),
        forecast_short=forecast_left < 0,
        has_withdrawn=amounts["withdrawn"] != 0 or pendings["withdrawn_pending"] > 0,
        has_pending=in_pending > 0 or out_pending > 0,
        zero=money(0),
        flow=in_out_flow(**amounts, **pendings),
        confirmed_flow=in_out_flow(**amounts),
        savings_rate=savings_rate,
    )


# The blocks whose rows are tinted when their actual is above plan, and the tint.
OVER_PLAN_TINT = {"expense": "danger", "income": "success"}


def build_summary(
    factory: sessionmaker[Session],
    ctx: Context,
    request: Request,
    view: MonthView,
    *,
    cell: CellState | None = None,
    copied: CopyResult | None = None,
    automatic: bool = False,
    focus_first: bool = False,
    sort: SortOrder | None = None,
    filter_id: UUID | None = None,
) -> SummaryView:
    """The ``#month-summary`` block of ``view.month`` (blocking: run in a thread).

    The rows, totals and KPIs come from ``services.summary.month_summary`` (sorted as the
    browser's ``summary_sort`` cookie says); the plan (``services.budget.get_plan``) adds the
    plan-only transfer group, which categories take new planned values, and the copy banner.
    """
    locale = request.state.locale
    sort = sort or summary_sort(request)
    plan = get_plan(factory, ctx, view.month)
    data = month_summary(factory, ctx, view.month, sort=sort)
    summary = data.summary
    can_write = has_role(ctx, Role.EDITOR) and not view.closed
    first = focus_first and can_write

    def row_view(category: PlanCategory, planned: Money | None, **extra: str) -> PlanRowView:
        nonlocal first
        has_line = planned is not None and planned.amount_minor > 0
        plannable = not category.archived and category.treatment != Treatment.TRANSFER
        editable = can_write and (plannable or has_line)
        saved = format_amount_input(planned, locale) if planned is not None and has_line else ""
        value, error, focus = saved, None, False
        if cell is not None and cell.category_id == category.id and editable:
            value, error, focus = cell.value, cell.error, cell.focus
        if first and editable:
            focus, first = True, False
        return PlanRowView(
            has_plan=has_line,
            category_id=str(category.id),
            name=category.name,
            color=category.color,
            archived=category.archived,
            editable=editable,
            value=value,
            saved_value=saved,
            display=format_money(planned, locale) if planned is not None and has_line else "",
            error=error,
            focus=focus,
            **extra,
        )

    def summary_row(key: str, r: SummaryRow) -> PlanRowView:
        category = data.categories[r.category_id]
        actual = format_money(r.actual, locale)
        pending = _pending(r.expected, locale)
        forecast = format_money(r.forecast, locale)
        if r.planned.amount_minor <= 0:
            # No planned value (owner, 2026-11-01): no difference, tint or "over plan" hint.
            return row_view(category, r.planned, actual=actual, pending=pending, forecast=forecast)
        tint = OVER_PLAN_TINT.get(key, "")
        over = r.actual.amount_minor > r.planned.amount_minor
        pending_over = (
            not over
            and r.expected.amount_minor > 0
            and r.forecast.amount_minor > r.planned.amount_minor
        )
        return row_view(
            category,
            r.planned,
            actual=actual,
            pending=pending,
            forecast=forecast,
            difference=_signed(r.difference, locale),
            tone=_tone(r.difference),
            over=tint if over else "",
            pending_over=tint if pending_over else "",
        )

    groups: list[PlanGroupView] = []
    for block in summary.blocks:
        rows = tuple(summary_row(block.key, r) for r in block.rows)
        if not rows:
            continue
        # The total difference counts only the planned categories (owner, 2026-11-01).
        planned_rows = [r for r in block.rows if r.planned.amount_minor > 0]
        difference = Money(sum(r.difference.amount_minor for r in planned_rows), summary.currency)
        totals = TotalsView(
            planned=format_money(block.planned, locale),
            actual=format_money(block.actual, locale),
            pending=_pending(block.expected, locale),
            forecast=format_money(block.forecast, locale),
            difference=_signed(difference, locale) if planned_rows else "",
            tone=_tone(difference) if planned_rows else "",
        )
        heading = gettext(GROUP_HEADINGS[block.key], locale)
        groups.append(PlanGroupView(block.key, heading, rows, totals))
    transfers = plan.group("transfer")
    if transfers is not None and transfers.rows:
        rows = tuple(row_view(r.category, r.planned) for r in transfers.rows)
        groups.append(PlanGroupView("transfer", gettext(GROUP_HEADINGS["transfer"], locale), rows))
    kpis = summary.kpis
    expense, saved, income, withdrawn = (summary.block(key) for key in BLOCK_ORDER)
    rate = kpis.savings_rate_tenths
    savings_rate = format_rate_tenths(rate, locale) if rate is not None else None
    kpi_view = KpiView(
        earned=format_money(kpis.earned, locale),
        spent=format_money(kpis.spent, locale),
        forecast_earned=format_money(kpis.forecast_earned, locale),
        forecast_spent=format_money(kpis.forecast_spent, locale),
        saved=format_money(kpis.saved, locale),
        net=format_money(kpis.net, locale),
        net_tone=_tone(kpis.net),
        savings_rate=savings_rate,
        overspend=format_money(Money(-kpis.net.amount_minor, kpis.net.currency), locale)
        if kpis.net.amount_minor < 0
        else None,
        flow=flow_bar(kpis.earned.amount_minor, kpis.spent.amount_minor, kpis.saved.amount_minor),
        forecast_flow=flow_bar(
            kpis.forecast_earned.amount_minor,
            kpis.forecast_spent.amount_minor,
            kpis.forecast_saved.amount_minor,
        ),
    )
    bars = (
        _bar("expense", expense.planned, expense.actual, expense.expected, locale),
        _bar("income", income.planned, income.actual, income.expected, locale),
    )
    in_out = _in_out(summary.currency, expense, saved, income, withdrawn, savings_rate, locale)
    sums = {"out": _sum_blocks((expense, saved)), "in": _sum_blocks((income, withdrawn))}
    in_out_bars = tuple(_bar(key, *sums[key], locale) for key in ("out", "in"))
    by_key = {g.key: g for g in groups}
    money_blocks = tuple(
        MoneyBlockView(
            key=key,
            groups=tuple(by_key[k] for k in keys if k in by_key),
            totals=_block_totals(*sums[key], locale),
        )
        for key, keys in MONEY_BLOCKS
        if any(k in by_key for k in keys)
    )
    banner = None
    if can_write and not plan.has_lines and plan.copyable_from_previous and plan.previous:
        banner = format_month_after_from(plan.previous, locale)
    notice = None
    if copied is not None and can_write:
        notice = CopiedNotice(
            count=copied.copied,
            skipped=copied.skipped,
            source=format_month_after_from(copied.source, locale),
            automatic=automatic,
        )
    return SummaryView(
        month=str(view.month),
        can_write=can_write,
        groups=tuple(groups),
        banner_source=banner,
        copied=notice,
        kpis=kpi_view,
        bars=bars,
        in_out=in_out,
        in_out_bars=in_out_bars,
        money_blocks=money_blocks,
        sort=sort,
        filter_id=str(filter_id) if filter_id is not None else "",
        future=view.is_future,
        source=data,
    )


def summary_sort(request: Request) -> SortOrder:
    """The browser's chosen order of the summary rows (cookie ``summary_sort``; owner, P4)."""
    value = request.cookies.get(summary_sort_cookie_name(request.app.state.settings), "")
    return "manual" if value == "manual" else "actual"


def summary_context(summary: SummaryView) -> dict[str, object]:
    """The ``#month-summary`` block's context (``months/_summary.html``)."""
    return {"summary": summary}


def plan_error(request: Request, code: str, currency: str) -> str:
    """The translated message for an ``InvalidPlannedValue`` code."""
    text = gettext(PLAN_ERRORS.get(code, PLAN_ERRORS["invalid_amount"]), request.state.locale)
    return text % {"currency": currency} if code == "too_many_decimals" else text
