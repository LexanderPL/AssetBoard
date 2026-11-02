"""The net-worth summary (F5.2): golden example, exclusions, shares and property tests.

The independent oracle is the hand-computed table in ``test_golden_example``: every total and
share below was calculated by hand from the item values, not by calling the function under test.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.networth import Side
from assetboard.domain.networth_summary import SummaryItem, summarize_net_worth


def _eur(minor: int) -> Money:
    return Money(minor, "EUR")


def _item(
    category_id: UUID,
    value: int | None,
    *,
    side: Side = Side.ASSET,
    include: bool = True,
    archived: bool = False,
    currency: str = "EUR",
) -> SummaryItem:
    return SummaryItem(
        id=uuid4(),
        side=side,
        category_id=category_id,
        value=Money(value, currency) if value is not None else None,
        include_in_net_worth=include,
        archived=archived,
    )


def test_golden_example() -> None:
    # Oracle (minor units):
    #   assets      = 30,000,000 + 2,000,000 + 500,000 = 32,500,000
    #   liabilities = 20,000,000 + 500,000             = 20,500,000
    #   net         = 12,000,000
    #   shares      = round(total / side total * 1000), half up
    #   house 923, car 62, cash 15; mortgage 976, card 24.
    house, car, cash = uuid4(), uuid4(), uuid4()
    mortgage, card = uuid4(), uuid4()
    summary = summarize_net_worth(
        [
            _item(house, 30_000_000),
            _item(car, 2_000_000),
            _item(cash, 500_000),
            _item(mortgage, 20_000_000, side=Side.LIABILITY),
            _item(card, 500_000, side=Side.LIABILITY),
        ],
        currency="EUR",
    )
    assert (summary.assets, summary.liabilities, summary.net) == (
        _eur(32_500_000),
        _eur(20_500_000),
        _eur(12_000_000),
    )
    asset_rows = [
        (row.category_id, row.total.amount_minor, row.share_tenths)
        for row in summary.asset_categories
    ]
    assert asset_rows == [
        (house, 30_000_000, 923),
        (car, 2_000_000, 62),
        (cash, 500_000, 15),
    ]
    liability_rows = [
        (row.category_id, row.total.amount_minor, row.share_tenths)
        for row in summary.liability_categories
    ]
    assert liability_rows == [
        (mortgage, 20_000_000, 976),
        (card, 500_000, 24),
    ]
    assert summary.non_base_item_ids == ()
    assert summary.no_value_item_ids == ()
    assert summary.excluded_item_ids == ()


def test_liabilities_only_has_negative_net_and_no_asset_shares() -> None:
    mortgage = uuid4()
    summary = summarize_net_worth([_item(mortgage, 10_000, side=Side.LIABILITY)], currency="EUR")
    assert summary.assets == _eur(0)
    assert summary.liabilities == _eur(10_000)
    assert summary.net == _eur(-10_000)
    assert summary.asset_categories == ()
    assert summary.liability_categories[0].share_tenths == 1000


def test_zero_total_categories_are_left_out() -> None:
    zero = uuid4()
    counted = uuid4()
    summary = summarize_net_worth([_item(zero, 0)], currency="EUR")
    assert summary.assets == _eur(0)
    assert summary.net == _eur(0)
    assert summary.asset_categories == ()
    # A zero category next to a real one disappears without changing the real total/share.
    summary = summarize_net_worth([_item(zero, 0), _item(counted, 100)], currency="EUR")
    assert summary.assets == _eur(100)
    assert [(row.category_id, row.total, row.share_tenths) for row in summary.asset_categories] == [
        (counted, _eur(100), 1000)
    ]


def test_exclusions_follow_the_f5_1_order() -> None:
    counted = uuid4()
    excluded_category = uuid4()
    no_value_category = uuid4()
    non_base_category = uuid4()
    archived_category = uuid4()
    excluded = _item(excluded_category, 200, include=False)
    no_value = _item(no_value_category, None)
    non_base = _item(non_base_category, 300, currency="USD")
    archived = _item(archived_category, 400, archived=True)
    summary = summarize_net_worth(
        [
            _item(counted, 100),
            excluded,
            no_value,
            non_base,
            archived,
        ],
        currency="EUR",
    )
    assert summary.assets == _eur(100)
    assert [row.category_id for row in summary.asset_categories] == [counted]
    assert summary.excluded_item_ids == (excluded.id,)
    assert summary.no_value_item_ids == (no_value.id,)
    assert summary.non_base_item_ids == (non_base.id,)
    assert archived.id not in {
        *summary.excluded_item_ids,
        *summary.no_value_item_ids,
        *summary.non_base_item_ids,
    }


def test_categories_sort_by_total_with_input_order_as_tie_break() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    summary = summarize_net_worth(
        [
            _item(first, 100),
            _item(second, 300),
            _item(third, 100),
        ],
        currency="EUR",
    )
    assert [row.category_id for row in summary.asset_categories] == [second, first, third]


def test_categories_are_calculated_per_side_and_currency() -> None:
    assets = uuid4()
    liabilities = uuid4()
    summary = summarize_net_worth(
        [
            _item(assets, 500),
            _item(liabilities, 200, side=Side.LIABILITY),
            _item(uuid4(), 900, currency="PLN"),
        ],
        currency="EUR",
    )
    assert summary.assets == _eur(500)
    assert summary.liabilities == _eur(200)
    assert summary.asset_categories[0].share_tenths == 1000
    assert summary.liability_categories[0].share_tenths == 1000
    assert len(summary.non_base_item_ids) == 1


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=10**6),
            st.booleans(),
            st.booleans(),
            st.sampled_from([None, "EUR", "USD"]),
            st.sampled_from([Side.ASSET, Side.LIABILITY]),
        ),
        max_size=20,
    )
)
def test_property_totals_and_exclusions(
    rows: list[tuple[int, bool, bool, str | None, Side]],
) -> None:
    category_ids = [uuid4() for _ in range(4)]
    items = [
        SummaryItem(
            id=uuid4(),
            side=side,
            category_id=category_ids[index % len(category_ids)],
            value=None if currency is None else Money(amount, currency),
            include_in_net_worth=include,
            archived=archived,
        )
        for index, (amount, include, archived, currency, side) in enumerate(rows)
    ]
    summary = summarize_net_worth(items, currency="EUR")
    assert summary.net == summary.assets - summary.liabilities
    for side, total, categories in (
        (Side.ASSET, summary.assets, summary.asset_categories),
        (Side.LIABILITY, summary.liabilities, summary.liability_categories),
    ):
        counted = [
            item
            for item in items
            if item.side is side
            and not item.archived
            and item.include_in_net_worth
            and item.value is not None
            and item.value.currency == "EUR"
        ]
        assert sum(row.total.amount_minor for row in categories) == total.amount_minor
        assert total.amount_minor == sum(
            item.value.amount_minor for item in counted if item.value is not None
        )
        for row in categories:
            if total.amount_minor == 0:
                assert row.share_tenths is None
            else:
                assert row.share_tenths is not None
                assert 0 <= row.share_tenths <= 1000
