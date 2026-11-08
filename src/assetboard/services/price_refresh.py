"""Provider refresh, first history backfill and provider status (F6.4).

Network calls never run inside a database transaction. Reads collect small detached target
objects, the provider is called outside any session, then each batch is written by a short
``run_immediate`` transaction under the shared workspace lock. A unique clash means another
writer inserted the same observation first; the batch is retried once as an update.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.engine import run_immediate
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    FxRateRepository,
    InstrumentRepository,
    InvestmentTransactionRepository,
    PricePointRepository,
    PriceSourceConfigRepository,
    ProviderErrorRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.domain.holdings import HoldingTransaction, compute_holdings
from assetboard.domain.prices import (
    FxProvider,
    PriceProvider,
    ProviderInstrument,
    ProviderQuote,
    ProviderRateQuote,
)
from assetboard.domain.providers import (
    InvalidProviderRate,
    ProviderDefinition,
    UnsupportedUnit,
    convert_to_workspace_base,
    nbp_gold_to_unit,
    normalize_instrument_symbol,
    normalize_provider_decimal,
    provider_definition,
)
from assetboard.services.context import Context, Role, require_role
from assetboard.services.providers.transport import (
    ProviderPayloadError,
    ProviderRuntime,
    ProviderTransportError,
    RateLimited,
)

ERROR_RETENTION_DAYS = 90


@dataclass(frozen=True, slots=True)
class RefreshStats:
    provider_key: str
    status: str
    prices: int = 0
    rates: int = 0
    errors: int = 0
    skipped: int = 0
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    provider_key: str
    provider_label: str
    enabled: bool
    last_price_date: date | None
    last_error_code: str | None
    last_error_at: datetime | None
    error: bool
    rate_limited: bool


@dataclass(frozen=True, slots=True)
class _InstrumentTarget:
    id: UUID
    type: str
    quote_currency: str
    unit_label: str
    provider_key: str | None
    provider_symbol: str | None
    provider_backfilled_at: datetime | None


@dataclass(slots=True)
class _Targets:
    base_currency: str
    timezone: str
    price_targets: list[_InstrumentTarget]
    fx_currencies: set[str]
    first_transaction_dates: dict[UUID, date]
    currency_first_dates: dict[str, date]


@dataclass(slots=True)
class _Run:
    prices: int = 0
    rates: int = 0
    errors: int = 0
    skipped: int = 0
    status: str = "ok"
    retry_after: int | None = None


@dataclass(frozen=True, slots=True)
class _Workspace:
    id: UUID


@dataclass(frozen=True, slots=True)
class _Scope:
    workspace: _Workspace


def refresh_provider(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    runtime: ProviderRuntime,
    *,
    mode: str = "latest",
    wait: bool = True,
) -> RefreshStats:
    definition = provider_definition(provider_key)
    if definition is None:
        return RefreshStats(provider_key=provider_key, status="unknown_provider")
    with factory() as session:
        config = PriceSourceConfigRepository(session, scope).for_provider(provider_key)
        if config is None or not config.enabled:
            return RefreshStats(provider_key=provider_key, status="disabled")
        targets = _targets(session, scope, provider_key)
    if not targets.price_targets and not targets.fx_currencies:
        _prune_errors(factory, scope, runtime.clock.now())
        return RefreshStats(provider_key=provider_key, status="no_targets")

    provider = runtime.providers.get(provider_key)
    if provider is None:
        return RefreshStats(provider_key=provider_key, status="unknown_provider")
    run = _Run()
    today = runtime.clock.today(targets.timezone)
    try:
        if targets.price_targets and hasattr(provider, "fetch_latest"):
            _refresh_latest_prices(
                factory,
                scope,
                provider_key,
                definition,
                cast(PriceProvider, provider),
                targets,
                runtime,
                today,
                wait,
                run,
            )
        if targets.fx_currencies and hasattr(provider, "fetch_latest_fx"):
            _refresh_latest_fx(
                factory,
                scope,
                provider_key,
                definition,
                cast(FxProvider, provider),
                targets,
                runtime,
                today,
                wait,
                run,
            )
        if mode == "full":
            if targets.price_targets and hasattr(provider, "fetch_history"):
                _backfill_prices(
                    factory,
                    scope,
                    provider_key,
                    definition,
                    cast(PriceProvider, provider),
                    targets,
                    runtime,
                    today,
                    run,
                )
            if targets.fx_currencies and hasattr(provider, "fetch_history_fx"):
                _backfill_fx(
                    factory,
                    scope,
                    provider_key,
                    definition,
                    cast(FxProvider, provider),
                    targets,
                    runtime,
                    today,
                    run,
                )
    finally:
        _prune_errors(factory, scope, runtime.clock.now())
    return RefreshStats(
        provider_key=provider_key,
        status=_status(run),
        prices=run.prices,
        rates=run.rates,
        errors=run.errors,
        skipped=run.skipped,
        retry_after=run.retry_after,
    )


def refresh_now(
    factory: sessionmaker[Session],
    ctx: Context,
    provider_key: str,
    runtime: ProviderRuntime,
) -> RefreshStats:
    """Owner-triggered latest refresh; never waits behind a long rate limit."""
    require_role(ctx, Role.OWNER)
    stats = refresh_provider(factory, ctx, provider_key, runtime, mode="latest", wait=False)

    def audit(session: Session) -> None:
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="price_source.refreshed",
            entity_type="price_source",
            entity_id=provider_key,
            ts=ctx.clock.now(),
            data={
                "provider": provider_key,
                "status": stats.status,
                "prices": stats.prices,
                "rates": stats.rates,
                "errors": stats.errors,
                **({"retry_after": stats.retry_after} if stats.retry_after else {}),
            },
        )

    run_immediate(factory, audit)
    return stats


def refresh_all(
    factory: sessionmaker[Session],
    provider_key: str,
    runtime: ProviderRuntime,
    logger: logging.Logger,
) -> list[RefreshStats]:
    """Scheduler entry point: refresh every workspace that enabled the provider."""
    with factory() as session:
        workspace_ids = [row[0] for row in RecurringWorkspaceReader(session).workspaces()]
    results: list[RefreshStats] = []
    for workspace_id in workspace_ids:
        try:
            results.append(
                refresh_provider(
                    factory,
                    _Scope(_Workspace(workspace_id)),
                    provider_key,
                    runtime,
                    mode="full",
                    wait=True,
                )
            )
        except Exception as exc:
            logger.error(
                "price_refresh_failed workspace=%s provider=%s exception_type=%s",
                workspace_id,
                provider_key,
                type(exc).__name__,
            )
    return results


def provider_status(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    instrument_id: UUID,
) -> ProviderStatus | None:
    with factory() as session:
        instrument = InstrumentRepository(session, scope).get_or_raise(instrument_id)
        provider_key = instrument.provider_key
        if not provider_key:
            return None
        definition = provider_definition(provider_key)
        config = PriceSourceConfigRepository(session, scope).for_provider(provider_key)
        latest = PricePointRepository(session, scope).latest_provider_for_instrument(
            instrument_id, provider_key
        )
        error = ProviderErrorRepository(session, scope).latest_for_instrument(
            provider_key, instrument_id, exclude=("history_limited",)
        )
        last_success_at = latest.updated_at if latest is not None else None
        current = error is not None and (
            last_success_at is None or error.occurred_at > last_success_at
        )
        rate_limited = current and error is not None and error.code == "rate_limited"
        error_is_current = current and not rate_limited
        return ProviderStatus(
            provider_key=provider_key,
            provider_label=definition.label if definition is not None else provider_key,
            enabled=bool(config and config.enabled),
            last_price_date=latest.date if latest is not None else None,
            last_error_code=error.code if current and error is not None else None,
            last_error_at=error.occurred_at if current and error is not None else None,
            error=error_is_current,
            rate_limited=rate_limited,
        )


def _targets(session: Session, scope: WorkspaceScope, provider_key: str) -> _Targets:
    workspace = CurrentWorkspaceRepository(session, scope).get()
    instruments = [
        row for row in InstrumentRepository(session, scope).ordered() if row.archived_at is None
    ]
    transactions = InvestmentTransactionRepository(session, scope).live()
    holding_rows = [
        HoldingTransaction(
            id=row.id,
            instrument_id=row.instrument_id,
            date=row.date,
            type=row.type,
            quantity=row.quantity,
            unit_price=row.unit_price,
            fee_minor=row.fee_minor,
            quote_currency=row.quote_currency,
            fx_to_base=row.fx_to_base if row.fx_to_base is not None else Decimal(1),
            created_at=row.created_at,
        )
        for row in transactions
    ]
    holdings = compute_holdings(
        holding_rows,
        getattr(workspace, "investment_cost_basis", "average"),
        None,
        base_currency=workspace.base_currency,
    )
    held = {holding.instrument_id for holding in holdings if holding.quantity > 0}
    base = workspace.base_currency
    first_dates: dict[UUID, date] = {}
    for row in transactions:
        if row.date is None:
            continue
        previous = first_dates.get(row.instrument_id)
        if previous is None or row.date < previous:
            first_dates[row.instrument_id] = row.date
    fx_currencies = {
        row.quote_currency for row in instruments if row.id in held and row.quote_currency != base
    }
    currency_first_dates: dict[str, date] = {}
    for row in instruments:
        if row.id not in held or row.quote_currency not in fx_currencies:
            continue
        first = first_dates.get(row.id)
        if first is None:
            continue
        previous = currency_first_dates.get(row.quote_currency)
        if previous is None or first < previous:
            currency_first_dates[row.quote_currency] = first
    price_targets = [
        _InstrumentTarget(
            id=row.id,
            type=row.type,
            quote_currency=row.quote_currency,
            unit_label=row.unit_label,
            provider_key=row.provider_key,
            provider_symbol=row.provider_symbol,
            provider_backfilled_at=row.provider_backfilled_at,
        )
        for row in instruments
        if row.id in held
        and row.provider_key == provider_key
        and row.valuation_source == "provider"
        and row.type != "currency"
    ]
    return _Targets(
        base_currency=base,
        timezone=workspace.timezone,
        price_targets=price_targets,
        fx_currencies=fx_currencies,
        first_transaction_dates=first_dates,
        currency_first_dates=currency_first_dates,
    )


def _refresh_latest_prices(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    definition: ProviderDefinition,
    provider: PriceProvider,
    targets: _Targets,
    runtime: ProviderRuntime,
    today: date,
    wait: bool,
    run: _Run,
) -> None:
    by_quote: dict[str, list[tuple[_InstrumentTarget, str]]] = {}
    for target in targets.price_targets:
        symbol = normalize_instrument_symbol(definition, target.provider_symbol)
        instrument = ProviderInstrument(
            id=target.id,
            type=target.type,
            quote_currency=target.quote_currency,
            provider_key=target.provider_key,
            provider_symbol=target.provider_symbol,
        )
        if symbol is None or not provider.supports(instrument):
            _record_error(
                factory,
                scope,
                provider_key,
                target.id,
                "latest",
                "unsupported_instrument",
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        by_quote.setdefault(target.quote_currency, []).append((target, symbol))
    for quote_currency, rows in by_quote.items():
        symbols = sorted({symbol for _target, symbol in rows})
        try:
            quotes = provider.fetch_latest(symbols, quote_currency, wait=wait)
        except RateLimited as error:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "latest",
                "rate_limited",
                runtime.clock.now(),
            )
            run.status = "rate_limited"
            run.retry_after = error.retry_after_seconds
            run.errors += 1
            return
        except (ProviderTransportError, ProviderPayloadError) as error:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "latest",
                error.code,
                runtime.clock.now(),
            )
            run.status = "error"
            run.errors += 1
            return
        points: list[tuple[UUID, date, Decimal, str]] = []
        for target, symbol in rows:
            quote = quotes.get(symbol)
            if quote is None or quote.date > today:
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    target.id,
                    "latest",
                    "no_data",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            try:
                price = _price_for_target(definition, target, quote)
            except (UnsupportedUnit, InvalidProviderRate):
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    target.id,
                    "latest",
                    "unsupported_unit",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            points.append((target.id, quote.date, price, provider_key))
        if points:
            try:
                _upsert_prices(factory, scope, points)
            except (IntegrityError, ProviderTransportError):
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    None,
                    "latest",
                    "write_conflict",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            run.prices += len(points)
    if run.prices:
        _record_success(factory, scope, provider_key, runtime.clock.now())


def _refresh_latest_fx(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    definition: ProviderDefinition,
    provider: FxProvider,
    targets: _Targets,
    runtime: ProviderRuntime,
    today: date,
    wait: bool,
    run: _Run,
) -> None:
    needed = set(targets.fx_currencies)
    if targets.base_currency != provider.natural_base:
        needed.add(targets.base_currency)
    try:
        rates = provider.fetch_latest_fx(sorted(needed), wait=wait)
    except RateLimited as error:
        _record_error(
            factory,
            scope,
            provider_key,
            None,
            "fx",
            "rate_limited",
            runtime.clock.now(),
        )
        run.status = "rate_limited"
        run.retry_after = error.retry_after_seconds
        run.errors += 1
        return
    except (ProviderTransportError, ProviderPayloadError) as error:
        _record_error(
            factory,
            scope,
            provider_key,
            None,
            "fx",
            error.code,
            runtime.clock.now(),
        )
        run.status = "error"
        run.errors += 1
        return
    base_rate = _base_rate(rates, targets.base_currency, provider.natural_base)
    if base_rate is None:
        _record_error(
            factory,
            scope,
            provider_key,
            None,
            "fx",
            "base_not_published",
            runtime.clock.now(),
        )
        run.errors += 1
        run.skipped += len(targets.fx_currencies)
        return
    base_quote = rates.get(targets.base_currency)
    rows: list[tuple[str, str, date, Decimal, str]] = []
    for currency in sorted(targets.fx_currencies):
        quote = rates.get(currency)
        if quote is None or quote.date > today:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "no_data",
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        if targets.base_currency != provider.natural_base and (
            base_quote is None or base_quote.date != quote.date
        ):
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "date_mismatch",
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        try:
            rate = normalize_provider_decimal(
                convert_to_workspace_base(quote.rate, base_rate), positive=True
            )
        except InvalidProviderRate:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "invalid_rate",
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        rows.append((targets.base_currency, currency, quote.date, rate, provider_key))
    if rows:
        try:
            _upsert_rates(factory, scope, rows)
        except (IntegrityError, ProviderTransportError):
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "write_conflict",
                runtime.clock.now(),
            )
            run.errors += 1
            return
        run.rates += len(rows)
        _record_success(factory, scope, provider_key, runtime.clock.now())


def _backfill_prices(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    definition: ProviderDefinition,
    provider: PriceProvider,
    targets: _Targets,
    runtime: ProviderRuntime,
    today: date,
    run: _Run,
) -> None:
    for target in targets.price_targets:
        if target.provider_backfilled_at is not None:
            continue
        first = targets.first_transaction_dates.get(target.id)
        if first is None:
            run.skipped += 1
            continue
        symbol = normalize_instrument_symbol(definition, target.provider_symbol)
        instrument = ProviderInstrument(
            id=target.id,
            type=target.type,
            quote_currency=target.quote_currency,
            provider_key=target.provider_key,
            provider_symbol=target.provider_symbol,
        )
        if symbol is None or not provider.supports(instrument):
            _record_error(
                factory,
                scope,
                provider_key,
                target.id,
                "history",
                "unsupported_instrument",
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        try:
            quotes = provider.fetch_history(symbol, first, today, target.quote_currency, wait=True)
        except RateLimited as error:
            _record_error(
                factory,
                scope,
                provider_key,
                target.id,
                "history",
                "rate_limited",
                runtime.clock.now(),
            )
            run.status = "rate_limited"
            run.retry_after = error.retry_after_seconds
            run.errors += 1
            return
        except (ProviderTransportError, ProviderPayloadError) as error:
            _record_error(
                factory,
                scope,
                provider_key,
                target.id,
                "history",
                error.code,
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        rows: list[tuple[UUID, date, Decimal, str]] = []
        for quote in quotes:
            if quote.date < first or quote.date > today:
                continue
            try:
                price = _price_for_target(definition, target, quote)
            except (UnsupportedUnit, InvalidProviderRate):
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    target.id,
                    "history",
                    "unsupported_unit",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            rows.append((target.id, quote.date, price, provider_key))
        if rows:
            try:
                _upsert_prices(factory, scope, rows)
            except (IntegrityError, ProviderTransportError):
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    target.id,
                    "history",
                    "write_conflict",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            run.prices += len(rows)
        earliest = min((quote.date for quote in quotes), default=None)
        if earliest is None or earliest > first:
            _record_error(
                factory,
                scope,
                provider_key,
                target.id,
                "history",
                "history_limited",
                runtime.clock.now(),
                informational=True,
            )
        _mark_backfilled(factory, scope, target.id, runtime.clock.now())


def _backfill_fx(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    definition: ProviderDefinition,
    provider: FxProvider,
    targets: _Targets,
    runtime: ProviderRuntime,
    today: date,
    run: _Run,
) -> None:
    with factory() as session:
        stored = FxRateRepository(session, scope).for_base_source_on_or_before(
            targets.base_currency, provider_key, today
        )
    coverage: dict[str, date] = {}
    for row in stored:
        previous = coverage.get(row.quote_currency)
        if previous is None or row.date < previous:
            coverage[row.quote_currency] = row.date
    for currency in sorted(targets.fx_currencies):
        first = targets.currency_first_dates.get(currency)
        if first is None:
            run.skipped += 1
            continue
        if currency in coverage and coverage[currency] <= first:
            continue
        try:
            quotes = provider.fetch_history_fx(currency, first, today, wait=True)
            base_quotes: Sequence[ProviderRateQuote] = ()
            if targets.base_currency != provider.natural_base:
                base_quotes = provider.fetch_history_fx(
                    targets.base_currency, first, today, wait=True
                )
        except RateLimited as error:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "rate_limited",
                runtime.clock.now(),
            )
            run.status = "rate_limited"
            run.retry_after = error.retry_after_seconds
            run.errors += 1
            return
        except (ProviderTransportError, ProviderPayloadError) as error:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                error.code,
                runtime.clock.now(),
            )
            run.errors += 1
            continue
        base_by_date = {quote.date: quote.rate for quote in base_quotes}
        if targets.base_currency == provider.natural_base:
            base_by_date = {quote.date: Decimal(1) for quote in quotes}
        rows: list[tuple[str, str, date, Decimal, str]] = []
        for quote in quotes:
            if quote.date < first or quote.date > today:
                continue
            base_rate = base_by_date.get(quote.date)
            if base_rate is None:
                continue
            try:
                rate = normalize_provider_decimal(
                    convert_to_workspace_base(quote.rate, base_rate), positive=True
                )
            except InvalidProviderRate:
                run.errors += 1
                continue
            rows.append((targets.base_currency, currency, quote.date, rate, provider_key))
        if rows:
            try:
                _upsert_rates(factory, scope, rows)
            except (IntegrityError, ProviderTransportError):
                _record_error(
                    factory,
                    scope,
                    provider_key,
                    None,
                    "fx",
                    "write_conflict",
                    runtime.clock.now(),
                )
                run.errors += 1
                continue
            run.rates += len(rows)
        earliest = min((quote.date for quote in quotes if quote.date in base_by_date), default=None)
        if earliest is None or earliest > first:
            _record_error(
                factory,
                scope,
                provider_key,
                None,
                "fx",
                "history_limited",
                runtime.clock.now(),
                informational=True,
            )
    if run.rates:
        _record_success(factory, scope, provider_key, runtime.clock.now())


def _price_for_target(
    definition: ProviderDefinition, target: _InstrumentTarget, quote: ProviderQuote
) -> Decimal:
    if definition.key == "nbp":
        price = nbp_gold_to_unit(quote.price, target.unit_label)
    else:
        price = quote.price
    return normalize_provider_decimal(price, positive=True)


def _base_rate(
    rates: Mapping[str, ProviderRateQuote],
    base_currency: str,
    natural_base: str,
) -> Decimal | None:
    if base_currency == natural_base:
        return Decimal(1)
    quote = rates.get(base_currency)
    if quote is None or not quote.rate.is_finite() or quote.rate <= 0:
        return None
    return quote.rate


def _upsert_prices(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    rows: Sequence[tuple[UUID, date, Decimal, str]],
) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        repository = PricePointRepository(session, scope)
        for instrument_id, day, price, provider_key in rows:
            existing = repository.provider_on(instrument_id, day)
            if existing is None:
                repository.create(
                    instrument_id=instrument_id,
                    date=day,
                    price=price,
                    source="provider",
                    provider_key=provider_key,
                )
            else:
                existing.price = price
                existing.provider_key = provider_key
        session.flush()

    try:
        run_immediate(factory, write)
    except IntegrityError:
        run_immediate(factory, write)


def _upsert_rates(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    rows: Sequence[tuple[str, str, date, Decimal, str]],
) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        repository = FxRateRepository(session, scope)
        for base, quote, day, rate, source in rows:
            existing = repository.rate_on(base, quote, day, source)
            if existing is None:
                repository.create(
                    base_currency=base,
                    quote_currency=quote,
                    date=day,
                    rate=rate,
                    source=source,
                )
            else:
                existing.rate = rate
        session.flush()

    try:
        run_immediate(factory, write)
    except IntegrityError:
        run_immediate(factory, write)


def _mark_backfilled(
    factory: sessionmaker[Session], scope: WorkspaceScope, instrument_id: UUID, now: datetime
) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        InstrumentRepository(session, scope).update(instrument_id, provider_backfilled_at=now)

    run_immediate(factory, write)


def _record_success(
    factory: sessionmaker[Session], scope: WorkspaceScope, provider_key: str, now: datetime
) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        row = PriceSourceConfigRepository(session, scope).for_provider(provider_key)
        if row is not None:
            row.last_success_at = now
            session.flush()

    run_immediate(factory, write)


def _record_error(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    provider_key: str,
    instrument_id: UUID | None,
    error_scope: str,
    code: str,
    now: datetime,
    *,
    informational: bool = False,
) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        ProviderErrorRepository(session, scope).create(
            provider_key=provider_key,
            instrument_id=instrument_id,
            scope=error_scope,
            code=code,
            occurred_at=now,
            attempts=1,
        )
        if not informational:
            row = PriceSourceConfigRepository(session, scope).for_provider(provider_key)
            if row is not None:
                row.last_error_at = now
                row.last_error_code = code
                session.flush()

    run_immediate(factory, write)


def _prune_errors(factory: sessionmaker[Session], scope: WorkspaceScope, now: datetime) -> None:
    def write(session: Session) -> None:
        CurrentWorkspaceRepository(session, scope).lock(exclusive=False)
        ProviderErrorRepository(session, scope).prune(now - timedelta(days=ERROR_RETENTION_DAYS))

    run_immediate(factory, write)


def _status(run: _Run) -> str:
    if run.status == "rate_limited":
        return "rate_limited"
    if run.errors == 0:
        return "ok"
    return "partial" if run.prices or run.rates else "error"
