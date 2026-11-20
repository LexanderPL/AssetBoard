"""F9.3 encrypted archive secrets and the optional member."""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from assetboard.config import Settings
from assetboard.db.repositories import (
    NotificationChannelRepository,
    PriceSourceConfigRepository,
)
from assetboard.domain.archive import parse_archive
from assetboard.domain.archive_secrets import SECRETS_FILE, SecretsEnvelope, parse_secrets_envelope
from assetboard.services.archive import export_workspace
from assetboard.services.archive_secrets import (
    InvalidPassphrase,
    decrypt_archive_secrets,
    encrypt_archive_secrets,
)
from assetboard.services.import_csv import default_import_adapters
from assetboard.services.imports import (
    ImportAdapterRegistry,
    ImportSecretsPassphraseInvalid,
    ImportSecretsPassphraseRequired,
    apply_import,
    plan_import,
    preview_rollback_import,
    rollback_import,
    upload_import,
)
from assetboard.services.notifications import decrypt_url, encrypt_url
from assetboard.services.price_sources import encrypt_provider_settings, provider_settings
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

PASSPHRASE = "correct horse battery staple"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        secret_key="test-key-1ce63290dee8aded1f6448b",
    )


def _source_with_secrets(db: WorkspaceDb, settings: Settings) -> None:
    ctx = db.ctx("own", "owner")
    now = db.clock.now()
    with db.factory.begin() as session:
        channel_id = uuid4()
        NotificationChannelRepository(session, ctx).create(
            id=channel_id,
            user_id=db.users["own"],
            name="Phone",
            url_encrypted=encrypt_url(settings, channel_id, "discord://webhook/token", now),
            scheme="discord",
            service_name="Discord",
            enabled=True,
            mode="digest",
            digest_time=time(8, 0),
            default_lead_days=3,
        )
        config_id = uuid4()
        config_lineage = uuid4()
        PriceSourceConfigRepository(session, ctx).create(
            id=config_id,
            provider_key="eodhd",
            enabled=True,
            settings_encrypted=encrypt_provider_settings(
                settings, config_id, "eodhd", {"api_key": "provider-secret"}, now
            ),
            daily_call_budget=20,
            import_key=f"id:{config_lineage}",
        )


def _target(db: WorkspaceDb) -> UUID:
    target = db.add_workspace("Restored")
    db.add_member("own", "owner", workspace_id=target)
    return target


def _registry() -> ImportAdapterRegistry:
    return ImportAdapterRegistry(default_import_adapters())


def test_passphrase_crypto_round_trip_and_wrong_passphrase() -> None:
    now = datetime(2026, 3, 1, tzinfo=UTC)
    member = encrypt_archive_secrets(
        PASSPHRASE,
        {"notification_channels": [], "provider_settings": []},
        now=now,
    )
    envelope = parse_secrets_envelope(member)
    assert isinstance(envelope, SecretsEnvelope)
    assert decrypt_archive_secrets(PASSPHRASE, envelope) == {
        "notification_channels": [],
        "provider_settings": [],
    }
    with pytest.raises(InvalidPassphrase):
        decrypt_archive_secrets("wrong passphrase!", envelope)
    with pytest.raises(InvalidPassphrase):
        encrypt_archive_secrets("short", {}, now=now)


def test_secrets_envelope_is_strict() -> None:
    import json

    with pytest.raises(ValueError):
        parse_secrets_envelope(b"not json")
    with pytest.raises(ValueError):
        parse_secrets_envelope(json.dumps({"format": "other"}).encode())
    with pytest.raises(ValueError):
        parse_secrets_envelope(
            json.dumps(
                {
                    "format": "assetboard-workspace-secrets",
                    "version": 1,
                    "cipher": "fernet",
                    "kdf": {
                        "name": "pbkdf2-sha256",
                        "iterations": 1000,
                        "salt": "AAAA",
                    },
                    "data": "x" * 40,
                }
            ).encode()
        )


def test_export_include_secrets_encrypts_the_member(tmp_path: Path) -> None:
    for db in workspace_db(tmp_path):
        db.add_member("own", "owner")
        settings = _settings(tmp_path)
        _source_with_secrets(db, settings)
        archive = export_workspace(
            db.factory,
            db.ctx("own", "owner"),
            include_secrets=True,
            passphrase=PASSPHRASE,
            settings=settings,
        )
        assert b"discord://webhook/token" not in archive.content
        assert b"provider-secret" not in archive.content
        parsed, errors = parse_archive(archive.content)
        assert errors == () and parsed is not None
        assert parsed.secrets is not None
        assert SECRETS_FILE in parsed.files
        manifest = parsed.manifest["files"]
        assert isinstance(manifest, list)
        assert any(
            isinstance(entry, dict) and entry.get("path") == SECRETS_FILE for entry in manifest
        )
        for data in parsed.files.values():
            assert b"discord://webhook/token" not in data
            assert b"provider-secret" not in data
        payload = decrypt_archive_secrets(PASSPHRASE, parsed.secrets)
        assert payload["notification_channels"][0]["url"] == "discord://webhook/token"  # type: ignore[index]
        assert payload["provider_settings"][0]["settings"]["api_key"] == "provider-secret"  # type: ignore[index]
        with db.factory() as session:
            config = PriceSourceConfigRepository(session, db.ctx("own", "owner")).for_provider(
                "eodhd"
            )
            assert config is not None and config.import_key is not None
            expected_id = config.import_key.removeprefix("id:")
        assert payload["provider_settings"][0]["id"] == expected_id  # type: ignore[index]


def test_restore_requires_and_uses_the_passphrase(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    for source in workspace_db(tmp_path / "source"):
        source.add_member("own", "owner")
        settings = _settings(tmp_path)
        _source_with_secrets(source, settings)
        archive = export_workspace(
            source.factory,
            source.ctx("own", "owner"),
            include_secrets=True,
            passphrase=PASSPHRASE,
            settings=settings,
        ).content
        (tmp_path / "target").mkdir(exist_ok=True)
        for db in workspace_db(tmp_path / "target"):
            db.add_member("own", "owner")
            target = _target(db)
            ctx = db.ctx("own", "owner", workspace_id=target)
            target_settings = Settings(
                _env_file=None,
                data_dir=tmp_path / "target-data",
                secret_key="test-key-8881905e1d1af007f25fe2e",
            )
            batch_id = upload_import(
                db.factory,
                ctx,
                data_dir=tmp_path,
                filename="archive.zip",
                content=archive,
                mode="restore",
            )
            report = plan_import(db.factory, ctx, tmp_path, batch_id, _registry())
            assert report["counts"]["invalid"] == 0, report
            assert report["secrets_present"] is True
            assert report["counts"]["secrets_included"] == 1
            assert not any(
                warning["code"] == "provider_key_required" for warning in report["warnings"]
            )
            with pytest.raises(ImportSecretsPassphraseRequired):
                apply_import(
                    db.factory,
                    ctx,
                    tmp_path,
                    batch_id,
                    _registry(),
                    instance_settings=target_settings,
                )
            with pytest.raises(ImportSecretsPassphraseInvalid):
                apply_import(
                    db.factory,
                    ctx,
                    tmp_path,
                    batch_id,
                    _registry(),
                    secrets_passphrase="wrong passphrase!",
                    instance_settings=target_settings,
                )
            apply_import(
                db.factory,
                ctx,
                tmp_path,
                batch_id,
                _registry(),
                secrets_passphrase=PASSPHRASE,
                instance_settings=target_settings,
            )
            with db.factory() as session:
                channels = NotificationChannelRepository(session, ctx).list()
                assert [row.name for row in channels] == ["Phone"]
                assert (
                    decrypt_url(target_settings, channels[0].id, channels[0].url_encrypted)
                    == "discord://webhook/token"
                )
                config = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
                assert config is not None
                assert config.enabled is True
                assert provider_settings(target_settings, config) == {"api_key": "provider-secret"}
                assert provider_settings(settings, config) is None
            second = export_workspace(
                db.factory,
                ctx,
                include_secrets=True,
                passphrase=PASSPHRASE,
                settings=target_settings,
            ).content
            second_parsed, second_errors = parse_archive(second)
            assert second_errors == () and second_parsed is not None
            assert second_parsed.secrets is not None
            second_payload = decrypt_archive_secrets(PASSPHRASE, second_parsed.secrets)
            with db.factory() as session:
                config = PriceSourceConfigRepository(session, ctx).for_provider("eodhd")
                assert config is not None and config.import_key is not None
                expected_id = config.import_key.removeprefix("id:")
            assert second_payload["provider_settings"][0]["id"] == expected_id  # type: ignore[index]
            preview = preview_rollback_import(db.factory, ctx, batch_id)
            assert preview.counts["notification_channels"] == 1
            assert preview.blocked_reasons == ()
            rollback_import(db.factory, ctx, batch_id)
            with db.factory() as session:
                assert NotificationChannelRepository(session, ctx).list() == []


def test_merge_ignores_secrets_with_a_warning(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()
    for source in workspace_db(tmp_path / "source"):
        source.add_member("own", "owner")
        settings = _settings(tmp_path)
        _source_with_secrets(source, settings)
        archive = export_workspace(
            source.factory,
            source.ctx("own", "owner"),
            include_secrets=True,
            passphrase=PASSPHRASE,
            settings=settings,
        ).content
        (tmp_path / "target").mkdir(exist_ok=True)
        for db in workspace_db(tmp_path / "target"):
            db.add_member("own", "owner")
            target = _target(db)
            ctx = db.ctx("own", "owner", workspace_id=target)
            batch_id = upload_import(
                db.factory,
                ctx,
                data_dir=tmp_path,
                filename="archive.zip",
                content=archive,
                mode="merge",
            )
            report = plan_import(db.factory, ctx, tmp_path, batch_id, _registry())
            assert any(warning["code"] == "secrets_restore_only" for warning in report["warnings"])
            apply_import(
                db.factory,
                ctx,
                tmp_path,
                batch_id,
                _registry(),
            )
            with db.factory() as session:
                assert NotificationChannelRepository(session, ctx).list() == []


def test_export_without_secrets_omits_channels_and_keys(tmp_path: Path) -> None:
    for db in workspace_db(tmp_path):
        db.add_member("own", "owner")
        settings = _settings(tmp_path)
        _source_with_secrets(db, settings)
        content = export_workspace(db.factory, db.ctx("own", "owner")).content
        parsed, errors = parse_archive(content)
        assert errors == () and parsed is not None
        assert parsed.secrets is None
        assert SECRETS_FILE not in parsed.files
        for data in parsed.files.values():
            assert b"discord://webhook/token" not in data
            assert b"provider-secret" not in data
        with db.factory() as session:
            from sqlalchemy import select

            from assetboard.db.models.identity import AuditEvent

            event = session.scalars(
                select(AuditEvent).where(AuditEvent.action == "workspace.exported")
            ).one()
            assert event.data["secrets_included"] == 0
            assert "discord://webhook/token" not in str(event.data)
            assert "provider-secret" not in str(event.data)


def test_only_an_owner_can_export_secrets(tmp_path: Path) -> None:
    from assetboard.services.context import PermissionDenied

    for db in workspace_db(tmp_path):
        db.add_member("own", "owner")
        db.add_member("ed", "editor")
        settings = _settings(tmp_path)
        with pytest.raises(PermissionDenied):
            export_workspace(
                db.factory,
                db.ctx("ed", "editor"),
                include_secrets=True,
                passphrase=PASSPHRASE,
                settings=settings,
            )
