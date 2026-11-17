"""Passphrase encryption and collection of the archive's optional secrets member (F9.3).

Fernet is already a runtime dependency for notification URLs and provider settings, and
PBKDF2-HMAC-SHA256 derives its key from the user's passphrase. No new dependency is needed.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from datetime import datetime
from typing import cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from sqlalchemy.orm import Session

from assetboard.config import Settings
from assetboard.db.repositories import (
    NotificationChannelRepository,
    PriceSourceConfigRepository,
)
from assetboard.domain.archive import lineage_id
from assetboard.domain.archive_secrets import (
    DEFAULT_ITERATIONS,
    MIN_PASSPHRASE_LENGTH,
    SecretsEnvelope,
    secrets_envelope_bytes,
)
from assetboard.services.context import Context
from assetboard.services.notifications import decrypt_url
from assetboard.services.price_sources import provider_settings


class InvalidPassphrase(ValueError):
    """The passphrase is missing, too short or does not open the member."""


def validate_passphrase(passphrase: str | None) -> str:
    value = (passphrase or "").strip()
    if len(value) < MIN_PASSPHRASE_LENGTH:
        raise InvalidPassphrase("a passphrase of at least 12 characters is required")
    return value


def _key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def encrypt_archive_secrets(
    passphrase: str, payload: Mapping[str, object], *, now: datetime
) -> bytes:
    """Encrypt ``payload`` and return the complete ``secrets.json.enc`` member."""
    value = validate_passphrase(passphrase)
    salt = os.urandom(16)
    plaintext = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    token = (
        Fernet(_key(value, salt, DEFAULT_ITERATIONS))
        .encrypt_at_time(plaintext, int(now.timestamp()))
        .decode("ascii")
    )
    return secrets_envelope_bytes(salt=salt, token=token, iterations=DEFAULT_ITERATIONS)


def decrypt_archive_secrets(passphrase: str, envelope: SecretsEnvelope) -> dict[str, object]:
    """Decrypt and parse the payload; raise ``InvalidPassphrase`` on a wrong passphrase."""
    value = validate_passphrase(passphrase)
    try:
        plaintext = (
            Fernet(_key(value, envelope.salt, envelope.iterations))
            .decrypt(envelope.token.encode("ascii"))
            .decode("utf-8")
        )
    except (InvalidToken, ValueError, UnicodeDecodeError):
        raise InvalidPassphrase("the passphrase does not open the secrets member") from None
    try:
        loaded: object = json.loads(plaintext)
    except (ValueError, RecursionError):
        raise InvalidPassphrase("the secrets payload is invalid") from None
    if not isinstance(loaded, dict):
        raise InvalidPassphrase("the secrets payload is invalid")
    return {str(key): item for key, item in cast("dict[object, object]", loaded).items()}


def collect_archive_secrets(
    session: Session, ctx: Context, settings: Settings
) -> dict[str, object]:
    """The plaintext secrets the owner chose to include in the archive.

    Unreadable stored secrets are omitted rather than failing the whole export; the export
    audit event records how many were skipped.
    """
    channels: list[dict[str, object]] = []
    skipped = 0
    for channel in NotificationChannelRepository(session, ctx).for_user(ctx.user.id):
        url = decrypt_url(settings, channel.id, channel.url_encrypted)
        if url is None:
            skipped += 1
            continue
        channels.append(
            {
                "id": str(channel.id),
                "name": channel.name,
                "url": url,
                "scheme": channel.scheme,
                "service_name": channel.service_name,
                "enabled": channel.enabled,
                "mode": channel.mode,
                "digest_time": channel.digest_time.strftime("%H:%M:%S"),
                "default_lead_days": channel.default_lead_days,
                "notify_due_soon": channel.notify_due_soon,
                "notify_overdue": channel.notify_overdue,
                "notify_work_logs": channel.notify_work_logs,
                "include_amounts": channel.include_amounts,
            }
        )
    providers: list[dict[str, object]] = []
    for config in PriceSourceConfigRepository(session, ctx).list():
        values = provider_settings(settings, config)
        if values is None:
            skipped += 1
            continue
        if not values:
            continue
        providers.append(
            {
                "id": str(lineage_id(config.id, config.import_key)),
                "provider_key": config.provider_key,
                "settings": values,
            }
        )
    return {
        "notification_channels": channels,
        "provider_settings": providers,
        "skipped": skipped,
    }
