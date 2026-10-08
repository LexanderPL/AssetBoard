from __future__ import annotations

import logging
import secrets
from ipaddress import ip_network
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from assetboard.branding import ENV_PREFIX


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=f"{ENV_PREFIX}_",
        case_sensitive=False,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: str = Field(default="dev")
    data_dir: Path = Field(default=Path("./data"))
    database_url: str | None = None
    secret_key: str | None = None
    base_url: str = "http://localhost:8000"
    log_level: str = "INFO"
    log_format: str = "text"
    trusted_proxies: list[str] = Field(default_factory=list)
    default_locale: str = "en"
    default_currency: str = "EUR"
    auto_migrate: bool = True

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: str) -> str:
        if value not in {"dev", "prod"}:
            raise ValueError("ENV must be 'dev' or 'prod'")
        return value

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if value.upper() not in valid:
            raise ValueError("LOG_LEVEL must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL")
        return value.upper()

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        if value not in {"text", "json"}:
            raise ValueError("LOG_FORMAT must be 'text' or 'json'")
        return value

    @field_validator("default_locale")
    @classmethod
    def validate_locale(cls, value: str) -> str:
        if len(value) != 2 or not value.islower():
            raise ValueError("DEFAULT_LOCALE must be a two-letter lowercase language code")
        return value

    @field_validator("default_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("DEFAULT_CURRENCY must be a three-letter ISO currency code")
        return normalized

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        for proxy in value:
            try:
                ip_network(proxy, strict=False)
            except ValueError as exc:
                raise ValueError(
                    "TRUSTED_PROXIES must contain valid IPv4 or IPv6 CIDR networks"
                ) from exc
        return value

    @model_validator(mode="after")
    def finalize_settings(self) -> Settings:
        self.data_dir = self.data_dir.expanduser()

        if self.database_url is None:
            database_filename = self.data_dir / "assetboard.db"
            self.database_url = f"sqlite:///{database_filename}"
        elif not self.database_url.startswith("sqlite://"):
            raise ValueError("DATABASE_URL must use the SQLite scheme for this foundation")

        if self.secret_key is None:
            secret_file = self.data_dir / "secret_key"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.secret_key = (
                secret_file.read_text(encoding="utf-8") if secret_file.exists() else None
            )
            if self.secret_key is None:
                self.secret_key = secrets.token_urlsafe(32)
                secret_file.write_text(self.secret_key, encoding="utf-8")
                logging.warning("Generated a new secret key and saved it to %s", secret_file)

        return self
