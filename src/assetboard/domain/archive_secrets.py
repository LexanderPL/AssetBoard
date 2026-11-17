"""Passphrase envelope for the optional workspace secrets member (F9.3)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import cast

SECRETS_FILE = "secrets.json.enc"
SECRETS_FORMAT = "assetboard-workspace-secrets"
SECRETS_VERSION = 1
SECRETS_CIPHER = "fernet"
SECRETS_KDF = "pbkdf2-sha256"
MIN_PASSPHRASE_LENGTH = 12
DEFAULT_ITERATIONS = 600_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 10_000_000
SALT_BYTES = 16


@dataclass(frozen=True, slots=True)
class SecretsEnvelope:
    """The unencrypted envelope around a Fernet token; parsing never needs the passphrase."""

    salt: bytes
    token: str
    iterations: int = DEFAULT_ITERATIONS


def parse_secrets_envelope(data: bytes) -> SecretsEnvelope:
    """Validate the member's JSON envelope; raise ``ValueError`` for anything unexpected."""
    try:
        loaded: object = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError("invalid secrets envelope") from None
    if not isinstance(loaded, dict):
        raise ValueError("invalid secrets envelope")
    value = cast("dict[object, object]", loaded)
    if value.get("format") != SECRETS_FORMAT or value.get("version") != SECRETS_VERSION:
        raise ValueError("unsupported secrets format")
    if value.get("cipher") != SECRETS_CIPHER:
        raise ValueError("unsupported secrets cipher")
    kdf = value.get("kdf")
    if not isinstance(kdf, dict):
        raise ValueError("invalid secrets KDF")
    kdf_value = cast("dict[object, object]", kdf)
    if kdf_value.get("name") != SECRETS_KDF:
        raise ValueError("unsupported secrets KDF")
    iterations = kdf_value.get("iterations")
    if type(iterations) is not int or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError("invalid secrets KDF iterations")
    salt_text = kdf_value.get("salt")
    if not isinstance(salt_text, str):
        raise ValueError("invalid secrets salt")
    try:
        salt = base64.b64decode(salt_text, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid secrets salt") from None
    if len(salt) != SALT_BYTES:
        raise ValueError("invalid secrets salt")
    token = value.get("data")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,}={0,2}", token):
        raise ValueError("invalid secrets token")
    return SecretsEnvelope(salt=salt, token=token, iterations=iterations)


def secrets_envelope_bytes(*, salt: bytes, token: str, iterations: int) -> bytes:
    """The canonical envelope written beside the encrypted payload."""
    return (
        json.dumps(
            {
                "format": SECRETS_FORMAT,
                "version": SECRETS_VERSION,
                "cipher": SECRETS_CIPHER,
                "kdf": {
                    "name": SECRETS_KDF,
                    "iterations": iterations,
                    "salt": base64.b64encode(salt).decode("ascii"),
                },
                "data": token,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
