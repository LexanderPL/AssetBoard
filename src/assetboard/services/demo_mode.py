"""Public demo mode (F9.11b): the demo user/workspace, its login and its resets.

The demo is one user and one workspace flagged ``is_demo``. Only the operator can turn it on,
through the environment (``ASSETBOARD_DEMO_ENABLED``); there is no in-app switch. This module
owns the lifecycle:

* ``ensure_demo_instance`` creates or aligns the pair at startup, under the instance-settings
  row lock, and builds the F9.11a dataset when the workspace is empty.
* ``reset_demo_instance`` wipes the workspace's rows (not its membership or the workspace row,
  so ids and sessions stay valid), runs the builder in the same transaction, records the reset
  on the instance settings and as the instance-level audit event ``demo.reset``. Stored import
  files are removed only after the commit, never on failure.
* ``disable_demo_instance`` ends demo sessions and deactivates the user when the operator turns
  demo mode off; the rows stay until an instance administrator deletes them.
* ``delete_demo_instance`` is the administrator's explicit deletion of the demo data.
* ``demo_login`` counts successful demo sign-ins per client address and creates the session.
"""

# The upload-file helpers are shared with the import service on purpose; the demo owns the
# files it creates and must remove exactly them, before or after its own transaction.
# pyright: reportPrivateUsage=false

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.branding import CLI_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import (
    InstanceSettings,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.repositories import (
    ImportBatchRepository,
    MembershipRepository,
    create_owned_workspace,
)
from assetboard.db.repositories.workspace import CurrentWorkspaceRepository, workspace_references
from assetboard.domain.demo import DEMO_DATASET_VERSION
from assetboard.i18n import gettext
from assetboard.services import archive, imports
from assetboard.services import demo as demo_service
from assetboard.services.admin_users import require_instance_admin
from assetboard.services.auth import (
    count_limited_attempt,
    create_user_session,
    ip_throttle_key,
    record_auth_event,
    revoke_all_sessions,
    session_id_hash,
)
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef

__all__ = [
    "DEMO_EMAIL",
    "DemoEmailTaken",
    "DemoInstanceMissing",
    "DemoLoginOutcome",
    "DemoResetResult",
    "DemoStatus",
    "delete_demo_instance",
    "demo_login",
    "disable_demo_instance",
    "ensure_demo_instance",
    "next_reset_at",
    "read_demo_status",
    "record_demo_error",
    "reset_demo_instance",
]

logger = logging.getLogger(__name__)
# The reserved ``.invalid`` domain can never receive mail, so no address is ever contacted.
DEMO_EMAIL = f"demo@{CLI_NAME}.invalid"
DEMO_DISPLAY_NAME = "Demo"
DEMO_RESET_WINDOW = timedelta(hours=1)
DEMO_STALE_AFTER = timedelta(hours=24)
ResetReason = Literal["startup", "scheduled", "manual"]


class DemoEmailTaken(RuntimeError):
    """A real account already has the demo email address; never take it over."""


class DemoInstanceMissing(RuntimeError):
    """The demo user or workspace is not there (demo mode is off or bootstrap failed)."""


@dataclass(frozen=True, slots=True)
class DemoStatus:
    user_id: UUID | None
    workspace_id: UUID | None
    created: bool
    last_reset_at: datetime | None
    last_reset_error: str | None
    dataset_version: int | None
    reset_due: bool


@dataclass(frozen=True, slots=True)
class DemoResetResult:
    ok: bool
    version: int | None = None
    reference_date: date | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DemoLoginOutcome:
    session_id: str | None = None
    expires_at: datetime | None = None
    throttled: bool = False


@dataclass(frozen=True, slots=True)
class _WorkspaceId:
    id: UUID


@dataclass(frozen=True, slots=True)
class _WorkspaceScope:
    workspace: _WorkspaceId


def _instance_row(session: Session, *, lock: bool) -> InstanceSettings:
    row = session.get(InstanceSettings, 1, with_for_update=True if lock else None)
    if row is None:
        row = InstanceSettings(id=1)
        session.add(row)
        session.flush()
    return row


def _demo_user(session: Session) -> User | None:
    return session.scalars(select(User).where(User.is_demo.is_(True))).first()


def _demo_workspace(session: Session) -> Workspace | None:
    return session.scalars(select(Workspace).where(Workspace.is_demo.is_(True))).first()


def _context(user: User, workspace: Workspace, clock: Clock) -> Context:
    return Context(
        user=UserRef(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            locale=user.locale,
            is_instance_admin=False,
            is_demo=True,
        ),
        workspace=WorkspaceRef(
            id=workspace.id,
            name=workspace.name,
            base_currency=workspace.base_currency,
            locale=workspace.locale,
            timezone=workspace.timezone,
            country_code=workspace.country_code,
            require_two_factor=workspace.require_two_factor,
            taxes_enabled=workspace.taxes_enabled,
            upcoming_days=workspace.upcoming_days,
            is_demo=True,
        ),
        role=Role.OWNER,
        clock=clock,
        auth_method="demo",
    )


def _reset_due(row: InstanceSettings, now: datetime) -> bool:
    if row.demo_dataset_version != DEMO_DATASET_VERSION:
        return True
    if row.demo_last_reset_at is None:
        return True
    return now - row.demo_last_reset_at > DEMO_STALE_AFTER


def _status(
    row: InstanceSettings,
    *,
    now: datetime,
    user: User | None,
    workspace: Workspace | None,
    created: bool = False,
) -> DemoStatus:
    return DemoStatus(
        user_id=user.id if user is not None else None,
        workspace_id=workspace.id if workspace is not None else None,
        created=created,
        last_reset_at=row.demo_last_reset_at,
        last_reset_error=row.demo_last_reset_error,
        dataset_version=row.demo_dataset_version,
        reset_due=_reset_due(row, now),
    )


def _discard_uploads(paths: list[Path]) -> None:
    """Best-effort removal of the demo's stored upload files (never fails the reset)."""
    for path in paths:
        try:
            imports._delete_upload(path)
        except OSError:
            logger.warning("demo_upload_cleanup_failed path=%s", path)


def _ensure_membership(session: Session, workspace: Workspace, user: User) -> None:
    """The demo workspace has exactly the demo user as its owner."""
    scope = _WorkspaceScope(_WorkspaceId(workspace.id))
    repository = MembershipRepository(session, scope)
    members = repository.members()
    demo_membership = None
    for member in members:
        if member.user_id == user.id:
            demo_membership = member
        else:
            repository.delete(member.membership_id)
    if demo_membership is None:
        repository.add_member(user_id=user.id, role="owner")
    elif demo_membership.role != "owner":
        repository.update(demo_membership.membership_id, role="owner")
    session.flush()


def _record_reset(
    session: Session,
    clock: Clock,
    row: InstanceSettings,
    *,
    reason: ResetReason,
    status: Literal["ok", "failed"],
    version: int | None,
) -> None:
    if status == "ok":
        row.demo_last_reset_at = clock.now()
        row.demo_dataset_version = version
        row.demo_last_reset_error = None
    data: dict[str, str | int | bool] = {"reason": reason, "status": status}
    if version is not None:
        data["version"] = version
    record_auth_event(
        session,
        clock,
        action="demo.reset",
        entity_type="instance",
        entity_id=None,
        data=data,
    )
    session.flush()


def ensure_demo_instance(
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
    data_dir: Path,
) -> DemoStatus:
    """Create or align the demo user/workspace; builds the dataset when it is empty.

    Runs under the instance-settings row lock, so concurrent workers cannot create two demo
    users or workspaces. The unique partial indexes are the last line of defence.
    """
    today = clock.today(settings.demo_timezone)
    workspace_name = gettext("Demo", settings.default_locale)
    created = False
    upload_paths: list[Path] = []

    def ensure(session: Session) -> DemoStatus:
        nonlocal created
        row = _instance_row(session, lock=True)
        email_owner = session.scalars(select(User).where(User.email == DEMO_EMAIL)).first()
        if email_owner is not None and not email_owner.is_demo:
            raise DemoEmailTaken(
                f"{DEMO_EMAIL} belongs to a real account; refusing to turn it into the demo user"
            )
        user = _demo_user(session) or email_owner
        workspace = _demo_workspace(session)
        if user is None:
            user = User(
                email=DEMO_EMAIL,
                display_name=DEMO_DISPLAY_NAME,
                password_hash=None,
                locale=None,
                theme="system",
                is_active=True,
                is_instance_admin=False,
                must_change_password=False,
                is_demo=True,
                birth_year=today.year - 38,
                life_expectancy=88,
            )
            session.add(user)
            session.flush()
            created = True
        else:
            # Turning demo mode back on reactivates the same account; it never keeps a
            # password or administrator rights.
            user.email = DEMO_EMAIL
            user.display_name = DEMO_DISPLAY_NAME
            user.password_hash = None
            user.is_active = True
            user.is_instance_admin = False
            user.is_demo = True
            user.must_change_password = False
            if user.birth_year is None:
                user.birth_year = today.year - 38
            if user.life_expectancy == 90:
                user.life_expectancy = 88
            session.flush()

        data_changed = False
        if workspace is None:
            workspace, _scope = create_owned_workspace(
                session,
                name=workspace_name,
                base_currency=settings.default_currency,
                country_code=None,
                timezone=settings.demo_timezone,
                locale=settings.default_locale,
                owner_user_id=user.id,
                is_demo=True,
            )
            created = True
        else:
            workspace.is_demo = True
            data_changed = (
                workspace.base_currency != settings.default_currency
                or workspace.locale != settings.default_locale
                or workspace.timezone != settings.demo_timezone
            )
            workspace.name = workspace_name
            if archive.workspace_is_empty(session, _context(user, workspace, clock)):
                # No data to preserve: align the current defaults before the initial build.
                workspace.base_currency = settings.default_currency
                workspace.locale = settings.default_locale
                workspace.timezone = settings.demo_timezone
                data_changed = False
            session.flush()

        _ensure_membership(session, workspace, user)
        ctx = _context(user, workspace, clock)
        if archive.workspace_is_empty(session, ctx):
            plan = demo_service.plan_for_workspace(ctx, today)
            session.info["clock"] = clock
            result, upload_path = demo_service.build_demo_workspace_in_session(
                session,
                ctx,
                plan,
                data_dir=data_dir,
                horizon_months=settings.recurrence_horizon_months,
            )
            # The builder moves ``Session.info`` to the historical clock; timestamps of the
            # instance row and the audit event must come from now again.
            session.info["clock"] = clock
            if upload_path is not None:
                upload_paths.append(upload_path)
            _record_reset(
                session,
                clock,
                row,
                reason="startup",
                status="ok",
                version=result.version,
            )
        elif data_changed:
            # The next startup/reset rebuilds the rows in the new locale/currency/time zone.
            row.demo_last_reset_at = None
            session.flush()
        return _status(
            row,
            now=clock.now(),
            user=user,
            workspace=workspace,
            created=created,
        )

    try:
        status = run_immediate(factory, ensure)
    except BaseException:
        _discard_uploads(upload_paths)
        raise
    _discard_uploads(upload_paths)
    return status


def record_demo_error(factory: sessionmaker[Session], error: BaseException) -> None:
    """Remember the bootstrap exception class on the instance settings (best effort)."""
    name = type(error).__name__[:200]

    def record(session: Session) -> None:
        row = _instance_row(session, lock=True)
        row.demo_last_reset_error = name
        session.flush()

    try:
        run_immediate(factory, record)
    except Exception:  # noqa: BLE001 - the error is already logged by the caller
        logger.error("demo_error_record_failed exception_type=%s", type(error).__name__)


def read_demo_status(factory: sessionmaker[Session], *, clock: Clock) -> DemoStatus | None:
    """The demo pair and reset state for the administrator panel; ``None`` when absent."""
    with factory() as session:
        user = _demo_user(session)
        workspace = _demo_workspace(session)
        row = session.get(InstanceSettings, 1)
        if row is None:
            row = InstanceSettings(id=1)
        if user is None and workspace is None and row.demo_last_reset_error is None:
            return None
        return _status(row, now=clock.now(), user=user, workspace=workspace)


def next_reset_at(settings: Settings, now: datetime) -> datetime:
    """The next configured reset time in the demo time zone, as a UTC datetime."""
    zone = ZoneInfo(settings.demo_timezone)
    local = now.astimezone(zone)
    hour, minute = settings.demo_reset_hour_minute
    candidate = datetime.combine(local.date(), time(hour, minute), tzinfo=zone)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), time(hour, minute), tzinfo=zone
        )
    return candidate.astimezone(UTC)


def _reset_import_files(session: Session, workspace_id: UUID, data_dir: Path) -> list[Path]:
    scope = _WorkspaceScope(_WorkspaceId(workspace_id))
    batches = ImportBatchRepository(session, scope).list()
    return [imports._upload_path(data_dir, workspace_id, batch.id) for batch in batches]


def _wipe_workspace_data(session: Session, workspace_id: UUID) -> None:
    """Delete every workspace-owned row except the workspace and its memberships."""
    owned, _references = workspace_references()
    for table in owned:
        if table.name == "memberships":
            continue
        session.execute(delete(table).where(table.c.workspace_id == workspace_id))
    session.flush()


def reset_demo_instance(
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
    data_dir: Path,
    *,
    reason: ResetReason,
) -> DemoResetResult:
    """Replace the demo dataset atomically; never raises for a failed reset.

    The wipe, the builder, the instance settings and the ``demo.reset`` audit event commit
    together. Old upload files are deleted only after that commit; on failure the transaction
    rolls back and nothing is deleted. The batch created by the new build is never part of the
    old-file cleanup: it becomes "old" on the next reset.
    """
    old_paths: list[Path] = []

    def reset(session: Session) -> DemoResetResult:
        row = _instance_row(session, lock=True)
        workspace = session.scalars(
            select(Workspace).where(Workspace.is_demo.is_(True)).with_for_update()
        ).first()
        if workspace is None:
            raise DemoInstanceMissing("there is no demo workspace")
        user = _demo_user(session)
        if user is None:
            raise DemoInstanceMissing("there is no demo user")
        old_paths.extend(_reset_import_files(session, workspace.id, data_dir))
        _wipe_workspace_data(session, workspace.id)
        # The rows are gone, so the workspace can safely move to the current instance defaults;
        # changing the base currency with live rows would be wrong.
        workspace.name = gettext("Demo", settings.default_locale)
        workspace.base_currency = settings.default_currency
        workspace.locale = settings.default_locale
        workspace.timezone = settings.demo_timezone
        session.flush()
        _ensure_membership(session, workspace, user)
        ctx = _context(user, workspace, clock)
        today = clock.today(settings.demo_timezone)
        plan = demo_service.plan_for_workspace(ctx, today)
        session.info["clock"] = clock
        # Keep the new build's upload file: it belongs to the new batch, not to the old ones.
        result, _upload_path = demo_service.build_demo_workspace_in_session(
            session,
            ctx,
            plan,
            data_dir=data_dir,
            horizon_months=settings.recurrence_horizon_months,
        )
        session.info["clock"] = clock
        _record_reset(
            session,
            clock,
            row,
            reason=reason,
            status="ok",
            version=result.version,
        )
        return DemoResetResult(ok=True, version=result.version, reference_date=today)

    try:
        result = run_immediate(factory, reset)
    except Exception as error:  # noqa: BLE001 - a failed reset must not stop startup/jobs
        logger.error(
            "demo_reset_failed reason=%s exception_type=%s",
            reason,
            type(error).__name__,
        )
        # A separate transaction: the failed one was rolled back, old data and files stay.
        name = type(error).__name__[:200]

        def record(session: Session) -> None:
            row = _instance_row(session, lock=True)
            row.demo_last_reset_error = name
            _record_reset(
                session,
                clock,
                row,
                reason=reason,
                status="failed",
                version=row.demo_dataset_version,
            )

        try:
            run_immediate(factory, record)
        except Exception:  # noqa: BLE001 - report through the return value and the first log
            logger.error("demo_reset_error_record_failed exception_type=%s", type(error).__name__)
        return DemoResetResult(ok=False, error=name)

    _discard_uploads(old_paths)
    return result


def disable_demo_instance(factory: sessionmaker[Session]) -> bool:
    """Turning demo mode off: end demo sessions and deactivate the user, keeping the rows."""

    def disable(session: Session) -> bool:
        user = _demo_user(session)
        if user is None:
            return False
        revoke_all_sessions(session, user.id)
        if user.is_active:
            user.is_active = False
            session.flush()
        return True

    return run_immediate(factory, disable)


def delete_demo_instance(
    factory: sessionmaker[Session],
    clock: Clock,
    *,
    actor_id: UUID,
    host: str | None = None,
    user_agent: str | None = None,
) -> bool:
    """Remove the demo workspace and user on an instance administrator's explicit request."""
    now = clock.now()

    def remove(session: Session) -> bool:
        require_instance_admin(session, actor_id)
        workspace = _demo_workspace(session)
        if workspace is None:
            return False
        user = _demo_user(session)
        if user is not None:
            revoke_all_sessions(session, user.id)
        scope = _WorkspaceScope(_WorkspaceId(workspace.id))
        CurrentWorkspaceRepository(session, scope).lock(exclusive=True)
        record_auth_event(
            session,
            clock,
            action="demo.deleted",
            entity_type="workspace",
            entity_id=str(workspace.id),
            actor_user_id=actor_id,
            host=host,
            user_agent=user_agent,
        )
        CurrentWorkspaceRepository(session, scope).delete_with_all_data(now=now)
        if user is not None:
            session.delete(user)
        # The panel disappears with the data; a later demo mode start begins clean.
        row = session.get(InstanceSettings, 1)
        if row is not None:
            row.demo_last_reset_at = None
            row.demo_last_reset_error = None
            row.demo_dataset_version = None
        session.flush()
        return True

    return run_immediate(factory, remove)


def demo_login(
    factory: sessionmaker[Session],
    settings: Settings,
    clock: Clock,
    *,
    host: str | None,
    user_agent: str | None,
) -> DemoLoginOutcome | None:
    """Create a demo session, if this address has not exhausted its hourly demo sign-ins."""
    now = clock.now()

    def login(session: Session) -> DemoLoginOutcome | None:
        user = _demo_user(session)
        workspace = _demo_workspace(session)
        if user is None or workspace is None or not user.is_active:
            return None
        if not count_limited_attempt(
            session,
            "demo",
            ip_throttle_key(host),
            limit=settings.demo_ip_limit,
            window=DEMO_RESET_WINDOW,
            now=now,
            settings=settings,
        ):
            return DemoLoginOutcome(throttled=True)
        session_id = create_user_session(
            session,
            user,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            auth_method="demo",
        )
        row = session.get(UserSession, session_id_hash(session_id))
        if row is None:
            raise RuntimeError("Demo session was not created")
        row.current_workspace_id = workspace.id
        user.last_login_at = now
        record_auth_event(
            session,
            clock,
            action="auth.login_success",
            entity_type="user",
            entity_id=str(user.id),
            actor_user_id=user.id,
            host=host,
            user_agent=user_agent,
            data={"method": "demo"},
        )
        session.flush()
        return DemoLoginOutcome(session_id=session_id, expires_at=row.expires_at)

    return run_immediate(factory, login)
