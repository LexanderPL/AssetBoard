"""The link resolver registry and its batch contract (F5.4)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from assetboard.domain.money import Money
from assetboard.domain.networth import Side
from assetboard.domain.networth_history import ItemHistory
from assetboard.services.networth_links import (
    UNAVAILABLE,
    LinkTarget,
    registry,
)
from tests.fake_links import FakeLinks, FakeTarget
from tests.tenant.harness import context_for
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path):
    yield from workspace_db(tmp_path)


def _history(
    *, link_id, currency: str = "EUR", link_type: str = "portfolio_asset_class"
) -> ItemHistory:
    return ItemHistory(
        id=uuid4(),
        side=Side.ASSET,
        category_id=uuid4(),
        currency=currency,
        include_in_net_worth=True,
        archived_at=None,
        valuations=(),
        valuation_mode="linked",
        link_type=link_type,
        link_id=link_id,
    )


def test_register_reset_and_duplicate(fake_links: FakeLinks) -> None:
    assert registry.labels() == {}
    fake_links.register(label="Fake source")
    assert registry.label("portfolio_asset_class") == "Fake source"
    with pytest.raises(ValueError, match="already registered"):
        fake_links.register()
    with pytest.raises(ValueError, match="unsupported"):
        fake_links.register(link_type="not_a_link_type")
    fake_links.reset()
    assert registry.labels() == {}


def test_choices_and_describe_are_workspace_scoped(db: WorkspaceDb, fake_links: FakeLinks) -> None:
    other_workspace = uuid4()
    own = FakeTarget(
        workspace_id=db.workspace_id,
        link_id=uuid4(),
        label="Own target",
        href="/fake/own",
    )
    foreign = FakeTarget(
        workspace_id=other_workspace,
        link_id=uuid4(),
        label="Foreign target",
    )
    fake_links.register(targets=[own, foreign])
    ctx = context_for(db.workspace_id, db.clock)
    with db.factory() as session:
        options = registry.options_for_side(session, ctx, Side.ASSET)
        assert [option.choice.label for option in options] == ["Own target"]
        assert isinstance(
            registry.describe(session, ctx, "portfolio_asset_class", own.link_id), LinkTarget
        )
        assert registry.describe(session, ctx, "portfolio_asset_class", foreign.link_id) is None
        assert registry.describe(session, ctx, "portfolio_asset_class", uuid4()) is None


def test_unknown_type_and_side_options_are_empty(db: WorkspaceDb, fake_links: FakeLinks) -> None:
    target = FakeTarget(workspace_id=db.workspace_id, link_id=uuid4(), label="Liability target")
    fake_links.register(side=Side.LIABILITY, targets=[target])
    ctx = context_for(db.workspace_id, db.clock)
    with db.factory() as session:
        assert registry.options_for_side(session, ctx, Side.ASSET) == ()
        assert registry.options_for_side(session, ctx, Side.LIABILITY) != ()
        assert registry.describe(session, ctx, "missing", uuid4()) is UNAVAILABLE
        history = _history(link_id=uuid4(), link_type="loan_balance")
        day = date(2026, 1, 1)
        assert registry.resolve_many(session, ctx, [history], (day,)) == {
            (history.id, day): UNAVAILABLE
        }


def test_resolve_many_is_one_call_per_link_type_and_checks_currency(
    db: WorkspaceDb, fake_links: FakeLinks
) -> None:
    first_id, second_id = uuid4(), uuid4()
    day_one, day_two = date(2026, 1, 31), date(2026, 2, 28)
    spec = fake_links.register(
        targets=[
            FakeTarget(
                workspace_id=db.workspace_id,
                link_id=first_id,
                label="First",
                values={day_one: 100, day_two: 200},
            ),
            FakeTarget(
                workspace_id=db.workspace_id,
                link_id=second_id,
                label="Second",
                values={day_one: 300, day_two: 400},
                resolution_currency="USD",
            ),
        ]
    )
    histories = [_history(link_id=first_id), _history(link_id=second_id)]
    ctx = context_for(db.workspace_id, db.clock)
    with db.factory() as session:
        resolved = registry.resolve_many(session, ctx, histories, (day_one, day_two))
    assert spec.calls == [(db.workspace_id, frozenset({first_id, second_id}), (day_one, day_two))]
    assert resolved[(histories[0].id, day_one)] == Money(100, "EUR")
    assert resolved[(histories[0].id, day_two)] == Money(200, "EUR")
    assert resolved[(histories[1].id, day_one)] == UNAVAILABLE
    assert resolved[(histories[1].id, day_two)] == UNAVAILABLE


def test_resolve_one_wraps_resolve_many(db: WorkspaceDb, fake_links: FakeLinks) -> None:
    target_id = uuid4()
    day = date(2026, 3, 31)
    spec = fake_links.register(
        targets=[
            FakeTarget(
                workspace_id=db.workspace_id,
                link_id=target_id,
                label="Target",
                values={day: 123},
            )
        ]
    )
    ctx = context_for(db.workspace_id, db.clock)
    with db.factory() as session:
        assert registry.resolve_one(
            session,
            ctx,
            link_type="portfolio_asset_class",
            link_id=target_id,
            currency="EUR",
            as_of=day,
        ) == Money(123, "EUR")
        assert (
            registry.resolve_one(
                session,
                ctx,
                link_type="portfolio_asset_class",
                link_id=target_id,
                currency="USD",
                as_of=day,
            )
            == UNAVAILABLE
        )
    assert spec.calls == [
        (db.workspace_id, frozenset({target_id}), (day,)),
        (db.workspace_id, frozenset({target_id}), (day,)),
    ]
