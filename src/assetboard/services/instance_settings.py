"""Instance settings an administrator changes in the UI (pre-release A).

One row, ``instance_settings.id = 1`` (migration 0013); a missing row reads as the defaults.

* ``allow_workspace_creation`` (default on): any signed-in user may create a workspace and
  becomes its owner. Instance administrators always may. It covers only that self-service
  creation: open registration and OIDC or proxy auto-create follow their own settings.
* ``allow_registration`` (default off, pre-release B, ADR-0017): open self-registration with a
  verified email. It can be turned on only with SMTP configured and without ``OIDC_ONLY``, and
  is effective only while both still hold (``registration_open``).

Changes run inside the acting administrator's confirmation (``web.confirm.run_confirmed``) and
are recorded as the instance-level event ``instance.settings_updated`` with the changed values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.models.identity import InstanceSettings, User
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import record_auth_event

ROW_ID = 1
DEFAULT_ALLOW_WORKSPACE_CREATION = True
DEFAULT_ALLOW_REGISTRATION = False


@dataclass(frozen=True, slots=True)
class InstanceSettingsView:
    allow_workspace_creation: bool = DEFAULT_ALLOW_WORKSPACE_CREATION
    allow_registration: bool = DEFAULT_ALLOW_REGISTRATION


def read_settings(session: Session, *, lock: bool = False) -> InstanceSettingsView:
    """The current settings. ``lock`` takes a shared row lock (PostgreSQL) for a write that
    depends on them; SQLite serialises writers at ``BEGIN IMMEDIATE`` instead."""
    row = session.get(InstanceSettings, ROW_ID, with_for_update={"read": True} if lock else None)
    if row is None:
        return InstanceSettingsView()
    return InstanceSettingsView(
        allow_workspace_creation=row.allow_workspace_creation,
        allow_registration=row.allow_registration,
    )


def get_settings(factory: sessionmaker[Session]) -> InstanceSettingsView:
    with factory() as session:
        return read_settings(session)


def workspace_creation_allowed(session: Session, *, is_instance_admin: bool) -> bool:
    """Whether this account may create a workspace now."""
    return is_instance_admin or read_settings(session).allow_workspace_creation


RegistrationBlocker = Literal["registration_needs_mail", "registration_oidc_only"]


def registration_blocker(settings: Settings) -> RegistrationBlocker | None:
    """Why open registration cannot be on with this configuration, or ``None``."""
    if settings.oidc_only:
        return "registration_oidc_only"
    if not settings.mail_enabled:
        return "registration_needs_mail"
    return None


def registration_open(session: Session, settings: Settings) -> bool:
    """Open registration is on and effective: the setting, SMTP, and no ``OIDC_ONLY``."""
    return registration_blocker(settings) is None and read_settings(session).allow_registration


UpdateStatus = Literal["done", "unchanged", "registration_needs_mail", "registration_oidc_only"]


def update_settings(
    session: Session,
    actor: User,
    *,
    allow_workspace_creation: bool,
    allow_registration: bool,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> UpdateStatus:
    """Change the settings, inside the confirming transaction (``actor`` confirmed).

    Migration 0013 creates the row, so it always exists; the insert below only covers a
    database where it was deleted by hand. Two administrators inserting it at once could then
    collide on PostgreSQL (a primary-key error, nothing lost); that race is theoretical.
    """
    if not actor.is_active or not actor.is_instance_admin:
        raise NotInstanceAdmin
    row = session.get(InstanceSettings, ROW_ID, with_for_update=True)
    if row is None:
        row = InstanceSettings(
            id=ROW_ID,
            allow_workspace_creation=DEFAULT_ALLOW_WORKSPACE_CREATION,
            allow_registration=DEFAULT_ALLOW_REGISTRATION,
        )
        session.add(row)
    # Turning registration on needs email; turning it off is always allowed.
    if allow_registration and not row.allow_registration:
        blocker = registration_blocker(settings)
        if blocker is not None:
            return blocker
    changed: dict[str, str | int | bool] = {}
    if row.allow_workspace_creation is not allow_workspace_creation:
        changed["allow_workspace_creation"] = allow_workspace_creation
    if row.allow_registration is not allow_registration:
        changed["allow_registration"] = allow_registration
    if not changed:
        return "unchanged"
    row.allow_workspace_creation = allow_workspace_creation
    row.allow_registration = allow_registration
    row.updated_at = clock.now()
    row.updated_by_user_id = actor.id
    record_auth_event(
        session,
        clock,
        action="instance.settings_updated",
        entity_type="instance",
        entity_id=None,
        actor_user_id=actor.id,
        host=host,
        user_agent=user_agent,
        data=changed,
    )
    session.flush()
    return "done"
