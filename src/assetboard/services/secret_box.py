"""Bound secret encryption shared by notifications and provider settings (ADR-0019 pattern).

A secret is encrypted with Fernet under a purpose-specific subkey of the instance secret and
bound to the owning row. The binding is part of the plaintext, so a token from one row cannot be
decrypted as another row's secret. Callers choose a stable purpose string; changing it makes
existing tokens unreadable, exactly like a secret-key change.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from assetboard.config import Settings
from assetboard.services.auth import derive_subkey


class SecretFailure(StrEnum):
    INVALID = "invalid"
    FOREIGN = "foreign"


@dataclass(frozen=True, slots=True)
class SecretDecryptResult:
    plaintext: str | None
    failure: SecretFailure | None


@lru_cache(maxsize=8)
def _cipher_for(secret_key: str, purpose: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_subkey(secret_key, purpose)))


def encrypt_secret(
    settings: Settings,
    *,
    purpose: str,
    binding: str,
    plaintext: str,
    now: datetime,
) -> str:
    """Encrypt ``plaintext`` bound to ``binding`` under an instance subkey for ``purpose``."""
    if not binding:
        raise ValueError("A secret binding must not be empty")
    cipher = _cipher_for(settings.ensure_secret_key(), purpose)
    payload = f"{binding}|{plaintext}".encode()
    return cipher.encrypt_at_time(payload, int(now.timestamp())).decode()


def decrypt_secret_result(
    settings: Settings,
    *,
    purpose: str,
    binding: str,
    token: str,
) -> SecretDecryptResult:
    """The plaintext and, when it fails, whether the token was invalid or foreign."""
    if not binding:
        return SecretDecryptResult(None, SecretFailure.INVALID)
    try:
        plaintext = _cipher_for(settings.ensure_secret_key(), purpose).decrypt(token).decode()
    except InvalidToken:
        return SecretDecryptResult(None, SecretFailure.INVALID)
    prefix = f"{binding}|"
    if not plaintext.startswith(prefix):
        return SecretDecryptResult(None, SecretFailure.FOREIGN)
    return SecretDecryptResult(plaintext.removeprefix(prefix), None)


def decrypt_secret(
    settings: Settings,
    *,
    purpose: str,
    binding: str,
    token: str,
) -> str | None:
    """The plaintext, or ``None`` when it is unreadable or belongs to another binding."""
    return decrypt_secret_result(settings, purpose=purpose, binding=binding, token=token).plaintext
