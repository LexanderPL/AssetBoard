from __future__ import annotations

from datetime import date
from time import perf_counter

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from assetboard.domain.loans import (
    BalanceCheckpoint,
    CostPeriod,
    DayCount,
    LoanKind,
    LoanTerms,
    LoanValidationError,
    OneOffOverpayment,
    OverpaymentMode,
    OverpaymentStrategy,
    RatePeriod,
    Schedule,
    ScheduleAnchor,
    ScheduleError,
    build_schedule,
    select_anchor,
    summarize_schedule,
)
from assetboard.domain.money import Money
from assetboard.domain.percent_or_fixed import PercentOrFixed


def _money(minor: int, currency: str = "EUR") -> Money:
    return Money(minor, currency)


def _terms(**overrides: object) -> LoanTerms:
    values: dict[str, object] = {
        "kind": LoanKind.ANNUITY,
        "currency": "EUR",
        "principal_original": _money(100_000),
        "origination_date": date(2026, 1, 1),
        "first_payment_date": date(2026, 2, 1),
        "term_months": 12,
        "payment_day": 1,
        "installment_override": None,
        "overpayment_mode": OverpaymentMode.NONE,
        "overpayment_amount": None,
    }
    values.update(overrides)
    return LoanTerms(**values)  # type: ignore[arg-type]


def _build(
    terms: LoanTerms,
    *,
    rates: list[RatePeriod] | None = None,
    costs: list[CostPeriod] | None = None,
    overpayments: list[OneOffOverpayment] | None = None,
    anchor: ScheduleAnchor | None = None,
    as_of: date | None = None,
    strategy: OverpaymentStrategy = OverpaymentStrategy.REDUCE_TERM,
    day_count: DayCount = DayCount.MONTHLY,
) -> Schedule:
    chosen_anchor = anchor or select_anchor(terms, [], as_of or terms.first_payment_date)
    chosen_as_of = as_of or chosen_anchor.date
    return build_schedule(
        terms,
        rates if rates is not None else [RatePeriod(date(2026, 1, 1), 600)],
        costs or [],
        overpayments or [],
        chosen_anchor,
        chosen_as_of,
        strategy,
        day_count,
    )


# 1. Payment date generation


def test_payment_dates_start_at_the_first_payment_and_clamp_month_end() -> None:
    terms = _terms(
        term_months=4,
        first_payment_date=date(2026, 1, 31),
        payment_day=31,
    )
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        anchor=ScheduleAnchor(date(2026, 1, 1), _money(100_000)),
        as_of=date(2026, 1, 1),
    )
    assert [row.date for row in schedule.rows] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_a_mid_month_checkpoint_starts_at_the_next_nominal_payment_day() -> None:
    terms = _terms(
        term_months=3,
        first_payment_date=date(2026, 2, 1),
        payment_day=31,
    )
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        anchor=ScheduleAnchor(date(2026, 2, 15), _money(100_000)),
        as_of=date(2026, 2, 15),
    )
    assert [row.date for row in schedule.rows] == [
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]


def test_a_future_first_payment_after_a_mid_month_anchor_wins() -> None:
    terms = _terms(term_months=2, first_payment_date=date(2026, 2, 28), payment_day=28)
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        anchor=ScheduleAnchor(date(2026, 1, 15), _money(100_000)),
        as_of=date(2026, 1, 15),
    )
    assert [row.date for row in schedule.rows] == [date(2026, 2, 28), date(2026, 3, 28)]


# 2. select_anchor


def test_select_anchor_uses_the_latest_checkpoint_at_or_before_as_of() -> None:
    terms = _terms(origination_date=date(2026, 1, 1), principal_original=_money(300_000))
    checkpoints = [
        BalanceCheckpoint(date(2026, 6, 1), _money(250_000)),
        BalanceCheckpoint(date(2026, 1, 1), _money(290_000)),
    ]
    anchor = select_anchor(terms, checkpoints, date(2026, 7, 1))
    assert anchor == ScheduleAnchor(date(2026, 6, 1), _money(250_000))


def test_select_anchor_falls_back_to_origination_and_ignores_future_checkpoints() -> None:
    terms = _terms(origination_date=date(2026, 1, 1), principal_original=_money(300_000))
    checkpoints = [BalanceCheckpoint(date(2026, 6, 1), _money(250_000))]
    anchor = select_anchor(terms, checkpoints, date(2026, 5, 1))
    assert anchor == ScheduleAnchor(date(2026, 1, 1), _money(300_000))


def test_select_anchor_requires_a_checkpoint_or_origination() -> None:
    terms = _terms(origination_date=None, principal_original=None)
    with pytest.raises(ScheduleError) as error:
        select_anchor(terms, [], date(2026, 1, 1))
    assert error.value.code == "no_anchor"


def test_select_anchor_refuses_a_wrong_currency() -> None:
    terms = _terms(origination_date=date(2026, 1, 1), principal_original=_money(300_000))
    with pytest.raises(LoanValidationError):
        select_anchor(
            terms,
            [BalanceCheckpoint(date(2026, 6, 1), _money(250_000, "USD"))],
            date(2026, 7, 1),
        )


# 4. Equal principal


def test_equal_principal_regular_principal_and_final_residual() -> None:
    terms = _terms(kind=LoanKind.EQUAL_PRINCIPAL, principal_original=_money(100_000))
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)])
    assert [row.principal.amount_minor for row in schedule.rows[:-1]] == [8333] * 11
    assert schedule.rows[-1].principal.amount_minor == 8337
    assert schedule.rows[-1].closing.amount_minor == 0
    assert sum(row.principal.amount_minor for row in schedule.rows) == 100_000


def test_equal_principal_zero_rate_and_interest() -> None:
    terms = _terms(kind=LoanKind.EQUAL_PRINCIPAL, principal_original=_money(100_000))
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 550)])
    assert schedule.rows[0].principal.amount_minor == 8333
    assert schedule.rows[0].interest.amount_minor == 458
    assert schedule.rows[-1].closing.amount_minor == 0


def test_equal_principal_ignores_reduce_installment() -> None:
    base = _terms(kind=LoanKind.EQUAL_PRINCIPAL, principal_original=_money(100_000))
    overpayment = OneOffOverpayment(date(2026, 6, 1), _money(40_000))
    term = _build(
        base,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        overpayments=[overpayment],
        strategy=OverpaymentStrategy.REDUCE_TERM,
    )
    installment = _build(
        base,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        overpayments=[overpayment],
        strategy=OverpaymentStrategy.REDUCE_INSTALLMENT,
    )
    assert [row.principal.amount_minor for row in term.rows] == [
        row.principal.amount_minor for row in installment.rows
    ]
    assert term.rows[-1].closing.amount_minor == 0


# 5. Day counts


def test_monthly_and_actual_365_interest() -> None:
    terms = _terms(
        kind=LoanKind.EQUAL_PRINCIPAL,
        principal_original=_money(100_000),
        term_months=2,
    )
    monthly = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 1200)],
        day_count=DayCount.MONTHLY,
    )
    actual = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 1200)],
        day_count=DayCount.ACTUAL_365,
    )
    assert [row.interest.amount_minor for row in monthly.rows] == [1000, 500]
    assert [row.interest.amount_minor for row in actual.rows] == [1019, 460]


def test_actual_365_counts_leap_year_days() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        first_payment_date=date(2024, 3, 1),
        term_months=1,
    )
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2024, 1, 1), 1200)],
        anchor=ScheduleAnchor(date(2024, 2, 1), _money(100_000)),
        as_of=date(2024, 2, 1),
        day_count=DayCount.ACTUAL_365,
    )
    assert schedule.rows[0].date == date(2024, 3, 1)
    assert schedule.rows[0].interest.amount_minor == 953  # 29 days


# 6. Rate changes


def test_rate_change_recomputes_the_annuity_and_applies_on_the_row_date() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    rates = [
        RatePeriod(date(2026, 1, 1), 1200),
        RatePeriod(date(2026, 7, 1), 600),
    ]
    schedule = _build(terms, rates=rates)
    assert schedule.rows[5].date == date(2026, 7, 1)
    assert schedule.rows[5].annual_rate_bp == 600
    opening = schedule.rows[5].opening.amount_minor
    expected_interest = (opening * 600 * 2 + 120_000) // 240_000
    assert schedule.rows[5].interest.amount_minor == expected_interest
    assert schedule.rows[5].principal.amount_minor != schedule.rows[4].principal.amount_minor
    assert schedule.rows[-1].closing.amount_minor == 0


def test_rate_change_before_the_anchor_is_used() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    schedule = _build(
        terms,
        rates=[
            RatePeriod(date(2025, 1, 1), 1200),
            RatePeriod(date(2026, 1, 1), 600),
        ],
    )
    assert schedule.rows[0].annual_rate_bp == 600


def test_a_missing_rate_is_an_error() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    with pytest.raises(ScheduleError) as error:
        _build(terms, rates=[])
    assert error.value.code == "missing_rate"


def test_duplicate_rate_dates_are_an_error() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    with pytest.raises(ScheduleError) as error:
        _build(
            terms,
            rates=[
                RatePeriod(date(2026, 1, 1), 500),
                RatePeriod(date(2026, 1, 1), 600),
            ],
        )
    assert error.value.code == "duplicate_rate"


# 7. Costs


def test_costs_are_inclusive_and_overlapping_components_are_summed() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=2)
    costs = [
        CostPeriod(PercentOrFixed.percent(100), date(2026, 2, 1), date(2026, 2, 1)),
        CostPeriod(PercentOrFixed.fixed_amount(_money(500)), date(2026, 2, 1)),
    ]
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)], costs=costs)
    assert [row.costs.amount_minor for row in schedule.rows] == [1500, 500]


def test_percent_costs_round_half_up_per_component() -> None:
    terms = _terms(principal_original=_money(100_100), term_months=1)
    costs = [CostPeriod(PercentOrFixed.percent(50), date(2026, 1, 1))]
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)], costs=costs)
    assert schedule.rows[0].opening.amount_minor == 100_100
    assert schedule.rows[0].costs.amount_minor == 501


# 8. Overpayments


def test_fixed_extra_shortens_the_term_and_clears_exactly() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=12,
        overpayment_mode=OverpaymentMode.FIXED_EXTRA,
        overpayment_amount=_money(10_000),
    )
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)])
    assert len(schedule.rows) < 12
    assert schedule.rows
    assert schedule.rows[-1].closing.amount_minor == 0
    assert (
        sum(row.principal.amount_minor for row in schedule.rows)
        + sum(row.overpayment.amount_minor for row in schedule.rows)
        == 100_000
    )


def test_target_total_includes_principal_interest_and_costs() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=12,
        overpayment_mode=OverpaymentMode.TARGET_TOTAL,
        overpayment_amount=_money(20_000),
    )
    costs = [CostPeriod(PercentOrFixed.fixed_amount(_money(500)), date(2026, 1, 1))]
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)], costs=costs)
    first = schedule.rows[0]
    assert first.principal.amount_minor == 8333
    assert first.costs.amount_minor == 500
    assert first.overpayment.amount_minor == 11_167
    assert first.installment_total.amount_minor == 20_000
    assert schedule.rows[-1].closing.amount_minor == 0


def test_reduce_term_and_reduce_installment_after_a_one_off_overpayment() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    overpayment = [OneOffOverpayment(date(2026, 6, 1), _money(30_000))]
    term = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        overpayments=overpayment,
        strategy=OverpaymentStrategy.REDUCE_TERM,
    )
    installment = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        overpayments=overpayment,
        strategy=OverpaymentStrategy.REDUCE_INSTALLMENT,
    )
    assert term.rows[4].overpayment.amount_minor == 30_000
    assert term.rows[5].principal.amount_minor == 8334
    assert installment.rows[5].principal.amount_minor < term.rows[5].principal.amount_minor
    assert term.rows[-1].closing.amount_minor == 0
    assert installment.rows[-1].closing.amount_minor == 0


def test_one_off_overpayment_timing() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        overpayments=[
            OneOffOverpayment(date(2026, 1, 15), _money(1_000)),  # before first payment
            OneOffOverpayment(date(2026, 2, 15), _money(2_000)),  # between payments
            OneOffOverpayment(date(2026, 2, 1), _money(3_000)),  # exactly on a row
            OneOffOverpayment(date(2026, 1, 1), _money(9_000)),  # on the anchor: ignored
            OneOffOverpayment(date(2030, 1, 1), _money(9_000)),  # after payoff: ignored
        ],
    )
    assert schedule.rows[0].overpayment.amount_minor == 4_000
    assert schedule.rows[1].overpayment.amount_minor == 2_000
    assert sum(row.overpayment.amount_minor for row in schedule.rows) == 6_000


def test_a_target_total_cannot_make_closing_negative() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=2,
        overpayment_mode=OverpaymentMode.TARGET_TOTAL,
        overpayment_amount=_money(200_000),
    )
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)])
    assert all(row.closing.amount_minor >= 0 for row in schedule.rows)
    assert schedule.rows[-1].closing.amount_minor == 0
    assert (
        sum(row.principal.amount_minor for row in schedule.rows)
        + sum(row.overpayment.amount_minor for row in schedule.rows)
        == 100_000
    )


# 9. Checkpoint start


def test_checkpoint_start_has_no_invented_past_rows_and_summary_uses_as_of() -> None:
    terms = _terms(
        origination_date=date(2020, 1, 1),
        principal_original=_money(300_000),
        first_payment_date=date(2020, 2, 1),
        term_months=240,
    )
    checkpoints = [BalanceCheckpoint(date(2026, 1, 15), _money(250_000))]
    anchor = select_anchor(terms, checkpoints, date(2026, 2, 10))
    assert anchor == ScheduleAnchor(date(2026, 1, 15), _money(250_000))
    schedule = build_schedule(
        terms,
        [RatePeriod(date(2026, 1, 1), 500)],
        [],
        [],
        anchor,
        date(2026, 2, 10),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    assert schedule.rows[0].date == date(2026, 2, 1)
    assert schedule.rows[0].opening == _money(250_000)
    assert all(row.date >= date(2026, 2, 1) for row in schedule.rows)
    summary = summarize_schedule(schedule, date(2026, 2, 10))
    assert summary.remaining_principal == schedule.rows[0].closing
    assert summary.next_payment_date == date(2026, 3, 1)
    before = summarize_schedule(schedule, date(2026, 1, 20))
    assert before.remaining_principal == _money(250_000)
    assert before.next_payment_date == date(2026, 2, 1)


# 10. Summary


def test_summary_remaining_next_and_total_values() -> None:
    terms = _terms(principal_original=_money(100_000), term_months=12)
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 600)])
    before = summarize_schedule(schedule, date(2026, 1, 1))
    assert before.remaining_principal == _money(100_000)
    assert before.next_payment_date == date(2026, 2, 1)
    assert before.months_remaining == 12
    assert before.percent_repaid_bp == 0
    assert before.payoff_date == schedule.rows[-1].date
    during = summarize_schedule(schedule, date(2026, 7, 1))
    assert during.remaining_principal == schedule.rows[5].closing
    assert during.next_payment_date == date(2026, 8, 1)
    assert during.months_remaining == 6
    assert during.remaining_interest.amount_minor == sum(
        row.interest.amount_minor for row in schedule.rows[6:]
    )
    after = summarize_schedule(schedule, date(2027, 2, 1))
    assert after.remaining_principal.amount_minor == 0
    assert after.next_payment_date is None and after.next_installment is None
    assert after.months_remaining == 0 and after.percent_repaid_bp == 10_000
    assert after.total_paid.amount_minor == sum(
        row.installment_total.amount_minor for row in schedule.rows
    )
    assert after.total_interest.amount_minor == sum(
        row.interest.amount_minor for row in schedule.rows
    )


def test_summary_uses_the_anchor_balance_when_original_principal_is_absent() -> None:
    terms = _terms(principal_original=None, origination_date=None, term_months=12)
    schedule = build_schedule(
        terms,
        [RatePeriod(date(2026, 1, 1), 600)],
        [],
        [],
        ScheduleAnchor(date(2026, 1, 1), _money(100_000)),
        date(2026, 1, 1),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    summary = summarize_schedule(schedule, date(2026, 7, 1))
    expected = (100_000 - schedule.rows[5].closing.amount_minor) * 10_000 // 100_000
    assert summary.percent_repaid_bp == expected


def test_zero_balance_returns_an_empty_schedule_and_zero_summary() -> None:
    terms = _terms(principal_original=None, origination_date=None)
    schedule = build_schedule(
        terms,
        [],
        [],
        [],
        ScheduleAnchor(date(2026, 1, 1), _money(0)),
        date(2026, 6, 1),
        OverpaymentStrategy.REDUCE_TERM,
        DayCount.MONTHLY,
    )
    assert schedule.rows == ()
    summary = summarize_schedule(schedule, date(2026, 6, 1))
    assert summary.remaining_principal.amount_minor == 0
    assert summary.next_payment_date is None and summary.next_installment is None
    assert summary.payoff_date is None and summary.months_remaining == 0
    assert summary.percent_repaid_bp == 0
    assert summary.total_paid.amount_minor == 0


# 12. Property tests


@settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    principal=st.integers(min_value=100, max_value=5_000_000),
    rate=st.integers(min_value=0, max_value=2_000),
    term=st.integers(min_value=1, max_value=60),
    extra=st.integers(min_value=0, max_value=20_000),
)
def test_schedule_invariants(principal: int, rate: int, term: int, extra: int) -> None:
    terms = _terms(
        principal_original=_money(principal),
        term_months=term,
        overpayment_mode=OverpaymentMode.FIXED_EXTRA if extra else OverpaymentMode.NONE,
        overpayment_amount=_money(extra) if extra else None,
    )
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), rate)])
    assert all(row.closing.amount_minor >= 0 for row in schedule.rows)
    assert (
        sum(row.principal.amount_minor for row in schedule.rows)
        + sum(row.overpayment.amount_minor for row in schedule.rows)
        == principal
    )
    assert sum(row.installment_total.amount_minor for row in schedule.rows) == sum(
        row.principal.amount_minor
        + row.overpayment.amount_minor
        + row.interest.amount_minor
        + row.costs.amount_minor
        for row in schedule.rows
    )
    assert schedule.rows[-1].closing.amount_minor == 0


@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    principal=st.integers(min_value=1_000, max_value=2_000_000),
    rate=st.integers(min_value=0, max_value=1_500),
    term=st.integers(min_value=2, max_value=36),
    extra=st.integers(min_value=0, max_value=5_000),
    more=st.integers(min_value=0, max_value=5_000),
)
def test_more_fixed_extra_never_increases_total_interest(
    principal: int, rate: int, term: int, extra: int, more: int
) -> None:
    def build(amount: int):
        terms = _terms(
            principal_original=_money(principal),
            term_months=term,
            overpayment_mode=(OverpaymentMode.FIXED_EXTRA if amount else OverpaymentMode.NONE),
            overpayment_amount=_money(amount) if amount else None,
        )
        return _build(terms, rates=[RatePeriod(date(2026, 1, 1), rate)])

    base = summarize_schedule(build(extra), date(2026, 1, 1))
    larger = summarize_schedule(build(extra + more), date(2026, 1, 1))
    assert larger.total_interest.amount_minor <= base.total_interest.amount_minor


# 13. Performance


def test_a_360_month_schedule_builds_in_under_ten_ms() -> None:
    terms = _terms(
        principal_original=_money(30_000_000),
        term_months=360,
    )
    anchor = ScheduleAnchor(date(2026, 1, 1), _money(30_000_000))
    _build(terms, rates=[RatePeriod(date(2026, 1, 1), 550)], anchor=anchor)
    timings = []
    for _ in range(3):
        started = perf_counter()
        _build(terms, rates=[RatePeriod(date(2026, 1, 1), 550)], anchor=anchor)
        timings.append(perf_counter() - started)
    assert min(timings) < 0.010


# 14. Input validation


def test_input_validation() -> None:
    terms = _terms(principal_original=_money(100_000))
    with pytest.raises(LoanValidationError):
        build_schedule(
            terms,
            [RatePeriod(date(2026, 1, 1), 600)],
            [],
            [],
            ScheduleAnchor(date(2026, 1, 1), _money(100_000, "USD")),
            date(2026, 1, 1),
            OverpaymentStrategy.REDUCE_TERM,
            DayCount.MONTHLY,
        )
    with pytest.raises(LoanValidationError):
        _build(_terms(term_months=0))
    with pytest.raises(LoanValidationError):
        _build(_terms(principal_original=_money(-1)))
    with pytest.raises(LoanValidationError):
        _build(terms, rates=[RatePeriod(date(2026, 1, 1), 10_001)])
    with pytest.raises(LoanValidationError):
        build_schedule(
            terms,
            [RatePeriod(date(2026, 1, 1), 600)],
            [],
            [],
            ScheduleAnchor(date(2026, 1, 1), _money(100_000)),
            date(2025, 12, 31),
            OverpaymentStrategy.REDUCE_TERM,
            DayCount.MONTHLY,
        )
    with pytest.raises(ScheduleError) as below:
        build_schedule(
            _terms(
                principal_original=_money(100_000),
                installment_override=_money(500),
            ),
            [RatePeriod(date(2026, 1, 1), 1200)],
            [],
            [],
            ScheduleAnchor(date(2026, 1, 1), _money(100_000)),
            date(2026, 1, 1),
            OverpaymentStrategy.REDUCE_TERM,
            DayCount.MONTHLY,
        )
    assert below.value.code == "negative_amortization"
    with pytest.raises(LoanValidationError):
        _build(_terms(kind=LoanKind.EQUAL_PRINCIPAL, installment_override=_money(500)))
    with pytest.raises(LoanValidationError):
        _build(terms, rates=[RatePeriod(date(2026, 1, 1), 600)], day_count="weekly")  # type: ignore[arg-type]


def test_fixed_extra_with_reduce_installment_lowers_the_next_principal() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=12,
        overpayment_mode=OverpaymentMode.FIXED_EXTRA,
        overpayment_amount=_money(10_000),
    )
    schedule = _build(
        terms,
        rates=[RatePeriod(date(2026, 1, 1), 0)],
        strategy=OverpaymentStrategy.REDUCE_INSTALLMENT,
    )
    assert schedule.rows[0].principal.amount_minor == 8333
    assert schedule.rows[1].principal.amount_minor < schedule.rows[0].principal.amount_minor
    assert schedule.rows[-1].closing.amount_minor == 0


def test_target_total_with_interest_includes_interest_in_the_target() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=12,
        overpayment_mode=OverpaymentMode.TARGET_TOTAL,
        overpayment_amount=_money(20_000),
    )
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 1200)])
    first = schedule.rows[0]
    assert first.interest.amount_minor > 0
    assert first.installment_total.amount_minor == 20_000
    assert first.overpayment.amount_minor == (
        20_000 - first.principal.amount_minor - first.interest.amount_minor
    )


def test_installment_override_is_used_for_an_annuity() -> None:
    terms = _terms(
        principal_original=_money(100_000),
        term_months=12,
        installment_override=_money(2_000),
    )
    schedule = _build(terms, rates=[RatePeriod(date(2026, 1, 1), 0)])
    assert schedule.rows[0].principal.amount_minor == 2_000
    assert schedule.rows[0].installment_total.amount_minor == 2_000
    assert schedule.rows[-1].closing.amount_minor == 0
