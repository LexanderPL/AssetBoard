"""Provider settings per workspace (F6.4).

Only owners change provider settings. Keys and other provider options are encrypted under the
instance secret and are never returned to a template, an audit event or a log.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.prices import PriceSourceConfig
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InstrumentRepository,
    PriceSourceConfigRepository,
)
from assetboard.domain.providers import PROVIDERS, ProviderDefinition
from assetboard.services.context import (
    Context,
    Role,
    require_not_demo,
    require_role,
    require_role_now,
)
from assetboard.services.secret_box import decrypt_secret, encrypt_secret

SECRET_PURPOSE = "price-source-settings"


@dataclass(frozen=True, slots=True)
class ProviderCard:
    definition: ProviderDefinition
    enabled: bool
    settings_configured: bool
    last_success_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    instruments_using: int
    daily_call_budget: int
    quota_used: int
    quota_day: date | None


@dataclass(frozen=True, slots=True)
class ProviderPage:
    cards: tuple[ProviderCard, ...]


@dataclass(frozen=True, slots=True)
class ProviderSaveResult:
    enabled: bool
    newly_enabled: bool


def provider_page(factory: sessionmaker[Session], ctx: Context) -> ProviderPage:
    require_role(ctx, Role.OWNER)
    with factory() as session:
        configs = {
            row.provider_key: row for row in PriceSourceConfigRepository(session, ctx).list()
        }
        instruments = InstrumentRepository(session, ctx).ordered()
        using: dict[str, int] = {}
        for instrument in instruments:
            if instrument.archived_at is None and instrument.provider_key:
                using[instrument.provider_key] = using.get(instrument.provider_key, 0) + 1
        today_utc = ctx.clock.today("UTC")
        cards = tuple(
            _card(
                definition,
                configs.get(definition.key),
                using.get(definition.key, 0),
                today_utc,
            )
            for definition in PROVIDERS.values()
        )
    return ProviderPage(cards=cards)


def save_provider_config(
    factory: sessionmaker[Session],
    ctx: Context,
    settings: Settings,
    provider_key: str,
    *,
    enabled: bool,
    settings_values: Mapping[str, str] | None = None,
    clear_settings: bool = False,
    daily_call_budget: int | None = None,
) -> ProviderSaveResult:
    """Save one provider's opt-in state and report a disabled-to-enabled transition."""
    require_role(ctx, Role.OWNER)
    require_not_demo(ctx)
    definition = PROVIDERS.get(provider_key)
    if definition is None:
        raise UnknownProvider(provider_key)
    if daily_call_budget is not None and not 1 <= daily_call_budget <= 100_000:
        raise InvalidProviderSetting

    def write(session: Session) -> ProviderSaveResult:
        require_role_now(session, ctx, Role.OWNER)
        CurrentWorkspaceRepository(session, ctx).lock(exclusive=False)
        repository = PriceSourceConfigRepository(session, ctx)
        row = repository.for_provider(provider_key)
        was_enabled = bool(row is not None and row.enabled)
        changed_settings = clear_settings or settings_values is not None
        if row is None:
            row = repository.create(
                provider_key=provider_key,
                enabled=enabled,
                settings_encrypted=None,
                daily_call_budget=definition.default_daily_budget or 20,
            )
        if clear_settings:
            row.settings_encrypted = None
        elif settings_values is not None:
            row.settings_encrypted = _encrypt_settings(
                settings,
                row.id,
                provider_key,
                settings_values,
                ctx.clock.now(),
            )
        if daily_call_budget is not None:
            row.daily_call_budget = daily_call_budget
        if enabled and definition.requires_key:
            existing = provider_settings(settings, row) if row.settings_encrypted else {}
            submitted = "" if clear_settings else (settings_values or {}).get("api_key", "").strip()
            if not submitted and (clear_settings or not existing or not existing.get("api_key")):
                raise MissingProviderKey
        row.enabled = enabled
        session.flush()
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="price_source.updated",
            entity_type="price_source",
            entity_id=str(row.id),
            ts=ctx.clock.now(),
            data={
                "provider": provider_key,
                "enabled": enabled,
                "settings_changed": changed_settings,
                "name": definition.label,
            },
        )
        return ProviderSaveResult(
            enabled=enabled,
            newly_enabled=enabled and not was_enabled,
        )

    return run_immediate(factory, write)


def provider_settings(settings: Settings, row: PriceSourceConfig) -> dict[str, str] | None:
    """Decrypt a config's settings for provider use; ``None`` if absent or unreadable."""
    if row.settings_encrypted is None:
        return {}
    plaintext = decrypt_secret(
        settings,
        purpose=SECRET_PURPOSE,
        binding=_binding(row.id, row.provider_key),
        token=row.settings_encrypted,
    )
    if plaintext is None:
        return None
    try:
        parsed = json.loads(plaintext)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    data = cast(dict[object, object], parsed)
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(key, str) and isinstance(value, str)
    }


class UnknownProvider(Exception):
    def __init__(self, provider_key: str) -> None:
        super().__init__(provider_key)
        self.provider_key = provider_key


class MissingProviderKey(Exception):
    """A key-required provider cannot be enabled without a key."""


class InvalidProviderSetting(Exception):
    """A provider option is outside its accepted range."""


def _card(
    definition: ProviderDefinition,
    config: PriceSourceConfig | None,
    instruments_using: int,
    today_utc: date,
) -> ProviderCard:
    quota_current = bool(config and config.quota_day == today_utc)
    return ProviderCard(
        definition=definition,
        enabled=bool(config and config.enabled),
        settings_configured=bool(config and config.settings_encrypted),
        last_success_at=config.last_success_at if config else None,
        last_error_at=config.last_error_at if config else None,
        last_error_code=config.last_error_code if config else None,
        instruments_using=instruments_using,
        daily_call_budget=config.daily_call_budget
        if config
        else (definition.default_daily_budget or 20),
        quota_used=config.quota_used if quota_current and config else 0,
        quota_day=config.quota_day if config else None,
    )


def provider_enabled(factory: sessionmaker[Session], ctx: Context, provider_key: str) -> bool:
    with factory() as session:
        row = PriceSourceConfigRepository(session, ctx).for_provider(provider_key)
        return bool(row and row.enabled)


def _binding(config_id: UUID, provider_key: str) -> str:
    return f"{config_id}|{provider_key}"


def _encrypt_settings(
    settings: Settings,
    config_id: UUID,
    provider_key: str,
    values: Mapping[str, str],
    now: datetime,
) -> str:
    payload = json.dumps(dict(values), sort_keys=True, separators=(",", ":"))
    return encrypt_secret(
        settings,
        purpose=SECRET_PURPOSE,
        binding=_binding(config_id, provider_key),
        plaintext=payload,
        now=now,
    )
