"""Reminder channels and the daily reminder run (F3.6, ADR-0021).

Members manage only their own channels in the current workspace; viewers included, because a
reminder shows nothing the Upcoming page does not. A channel's Apprise URL is encrypted at rest
(Fernet under a subkey of the instance secret, bound to the channel id), never logged, never
put in audit data and never shown again after saving.

The run (``run_workspace``) is trusted code called by the scheduler with an explicit workspace
scope and the injected Clock. For each enabled channel whose digest time has passed today (in
the workspace time zone) and which has not run today, it claims the planned reminders as
``sending`` log rows and sets ``last_run_on`` in one immediate transaction, sends outside the
transaction, then records ``sent`` or ``failed``.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.models.notifications import NotificationChannel
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    IncomeSourceRepository,
    MembershipRepository,
    NotFound,
    NotificationChannelRepository,
    NotificationLogRepository,
    ScheduledOccurrenceRepository,
    WorkLogRepository,
)
from assetboard.db.repositories.base import WorkspaceScope
from assetboard.domain.money import Money
from assetboard.domain.notifications import (
    DEFAULT_DIGEST_TIME,
    DEFAULT_LEAD_DAYS,
    MAX_CHANNEL_NAME_LENGTH,
    MAX_LEAD_DAYS,
    ChannelRules,
    DeliveryMode,
    LogStatus,
    PendingOccurrence,
    Reminder,
    WorkSource,
    batches,
    channel_is_due,
    plan_reminders,
)
from assetboard.domain.transactions import clean_description
from assetboard.i18n.locale import normalize_locale
from assetboard.services.auth import count_limited_attempt, derive_subkey
from assetboard.services.context import Context, Role, require_role, require_role_now
from assetboard.services.notification_messages import reminder_message, test_message
from assetboard.services.notify import UNCERTAIN_ERRORS, InvalidNotificationUrl, Notifier

logger = logging.getLogger(__name__)

MAX_URL_LENGTH = 2048
MAX_CHANNELS_PER_MEMBER = 10
TEST_SEND_LIMIT = 5
TEST_SEND_WINDOW = timedelta(minutes=10)
# A ``sending`` row older than this was interrupted (restart, crash): it counts as failed.
STALE_SENDING = timedelta(minutes=10)
# Log rows that no rule needs any more are pruned after this many days (the overdue cap
# needs sent overdue rows for as long as the occurrence stays pending: those are kept).
LOG_RETENTION_DAYS = 60
RECENT_LOG_ROWS = 20


class NotificationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ChannelInput:
    name: str
    url: str  # blank on update: keep the stored URL
    enabled: bool = True
    mode: str = DeliveryMode.DIGEST.value
    digest_time: time = DEFAULT_DIGEST_TIME
    default_lead_days: int = DEFAULT_LEAD_DAYS
    notify_due_soon: bool = True
    notify_overdue: bool = True
    notify_work_logs: bool = True
    include_amounts: bool = False


@dataclass(frozen=True, slots=True)
class ChannelView:
    id: UUID
    name: str
    masked_url: str
    service_name: str
    enabled: bool
    mode: str
    digest_time: time
    default_lead_days: int
    notify_due_soon: bool
    notify_overdue: bool
    notify_work_logs: bool
    include_amounts: bool
    last_run_on: date | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class LogView:
    channel_name: str
    kind: str
    sent_on: date
    status: str
    error: str | None
    attempted_at: datetime


@dataclass(frozen=True, slots=True)
class NotificationsPage:
    channels: tuple[ChannelView, ...]
    log: tuple[LogView, ...]
    can_add: bool
    delivery_enabled: bool
    schemes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TestOutcome:
    ok: bool
    error: str | None = None


# Encryption (ADR-0019 pattern)


@lru_cache(maxsize=4)
def _cipher_for(secret_key: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_subkey(secret_key, "notification-url")))


def _cipher(settings: Settings) -> Fernet:
    return _cipher_for(settings.ensure_secret_key())


def encrypt_url(settings: Settings, channel_id: UUID, url: str, now: datetime) -> str:
    """Encrypt ``url`` bound to its channel: the plaintext is ``"<channel_id>|<url>"``."""
    plaintext = f"{channel_id}|{url}".encode()
    return _cipher(settings).encrypt_at_time(plaintext, int(now.timestamp())).decode()


def decrypt_url(settings: Settings, channel_id: UUID, token: str) -> str | None:
    """The URL, or ``None`` when it cannot be read or belongs to another channel."""
    try:
        plaintext = _cipher(settings).decrypt(token).decode()
    except InvalidToken:
        logger.warning(
            "notification_url_unreadable channel=%s; the instance secret key has probably "
            "changed. The member must enter the channel URL again.",
            channel_id,
        )
        return None
    prefix = f"{channel_id}|"
    if not plaintext.startswith(prefix):
        logger.warning("notification_url_refused channel=%s reason=foreign_row", channel_id)
        return None
    return plaintext.removeprefix(prefix)


def url_scheme(url: str) -> str:
    scheme, separator, _rest = url.partition("://")
    return scheme.lower() if separator else ""


def masked(scheme: str) -> str:
    return f"{scheme}://••••••"


def validate_url(url: str, settings: Settings, notifier: Notifier) -> tuple[str, str]:
    """``(scheme, service name)`` of a usable URL; ``NotificationError`` otherwise."""
    if not url or len(url) > MAX_URL_LENGTH or any(char.isspace() for char in url):
        raise NotificationError("invalid_url")
    scheme = url_scheme(url)
    if not scheme or not url.partition("://")[2]:
        raise NotificationError("invalid_url")
    if scheme not in settings.notification_schemes:
        raise NotificationError("scheme_not_allowed")
    try:
        info = notifier.inspect(url)
    except InvalidNotificationUrl:
        raise NotificationError("invalid_url") from None
    return scheme, info.service_name or scheme


# Channel management (every member, own channels only)


def _clean(values: ChannelInput) -> dict[str, object]:
    name = clean_description(values.name)
    if not name or len(name) > MAX_CHANNEL_NAME_LENGTH:
        raise NotificationError("invalid_name")
    try:
        mode = DeliveryMode(values.mode)
    except ValueError:
        raise NotificationError("invalid_mode") from None
    if not 0 <= values.default_lead_days <= MAX_LEAD_DAYS:
        raise NotificationError("invalid_lead_days")
    return {
        "name": name,
        "enabled": values.enabled,
        "mode": mode.value,
        "digest_time": values.digest_time.replace(second=0, microsecond=0, tzinfo=None),
        "default_lead_days": values.default_lead_days,
        "notify_due_soon": values.notify_due_soon,
        "notify_overdue": values.notify_overdue,
        "notify_work_logs": values.notify_work_logs,
        "include_amounts": values.include_amounts,
    }


def _view(channel: NotificationChannel) -> ChannelView:
    return ChannelView(
        id=channel.id,
        name=channel.name,
        masked_url=masked(channel.scheme),
        service_name=channel.service_name,
        enabled=channel.enabled,
        mode=channel.mode,
        digest_time=channel.digest_time,
        default_lead_days=channel.default_lead_days,
        notify_due_soon=channel.notify_due_soon,
        notify_overdue=channel.notify_overdue,
        notify_work_logs=channel.notify_work_logs,
        include_amounts=channel.include_amounts,
        last_run_on=channel.last_run_on,
        last_error=channel.last_error,
    )


def page(factory: sessionmaker[Session], ctx: Context, settings: Settings) -> NotificationsPage:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        channels = NotificationChannelRepository(session, ctx).for_user(ctx.user.id)
        log = NotificationLogRepository(session, ctx).recent_for_user(ctx.user.id, RECENT_LOG_ROWS)
        return NotificationsPage(
            channels=tuple(_view(channel) for channel in channels),
            log=tuple(
                LogView(name, row.kind, row.sent_on, row.status, row.error, row.attempted_at)
                for row, name in log
            ),
            can_add=len(channels) < MAX_CHANNELS_PER_MEMBER,
            delivery_enabled=settings.notifications_enabled,
            schemes=tuple(settings.notification_schemes),
        )


def channel(factory: sessionmaker[Session], ctx: Context, channel_id: UUID) -> ChannelView:
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        row = NotificationChannelRepository(session, ctx).owned(channel_id, ctx.user.id)
        if row is None:
            raise NotFound
        return _view(row)


def _audit(
    session: Session, ctx: Context, action: str, entity_id: UUID, **data: str | int | bool
) -> None:
    AuditEventRepository(session, ctx).record(
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="notification_channel",
        entity_id=str(entity_id),
        ts=ctx.clock.now(),
        data=data,
    )


def create_channel(
    factory: sessionmaker[Session],
    ctx: Context,
    settings: Settings,
    notifier: Notifier,
    values: ChannelInput,
) -> UUID:
    require_role(ctx, Role.VIEWER)
    fields = _clean(values)
    url = values.url.strip()
    scheme, service_name = validate_url(url, settings, notifier)

    def write(session: Session) -> UUID:
        require_role_now(session, ctx, Role.VIEWER)
        channels = NotificationChannelRepository(session, ctx)
        if channels.count_for_user(ctx.user.id) >= MAX_CHANNELS_PER_MEMBER:
            raise NotificationError("too_many_channels")
        channel_id = uuid4()
        channels.create(
            id=channel_id,
            user_id=ctx.user.id,
            url_encrypted=encrypt_url(settings, channel_id, url, ctx.clock.now()),
            scheme=scheme,
            service_name=service_name,
            **fields,
        )
        _audit(session, ctx, "notification_channel.created", channel_id, scheme=scheme)
        return channel_id

    return run_immediate(factory, write)


def update_channel(
    factory: sessionmaker[Session],
    ctx: Context,
    settings: Settings,
    notifier: Notifier,
    channel_id: UUID,
    values: ChannelInput,
) -> None:
    require_role(ctx, Role.VIEWER)
    fields = _clean(values)
    url = values.url.strip()
    new_url = validate_url(url, settings, notifier) if url else None

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.VIEWER)
        channels = NotificationChannelRepository(session, ctx)
        row = channels.owned(channel_id, ctx.user.id)
        if row is None:
            raise NotFound
        changes = dict(fields)
        if new_url is not None:
            changes |= {
                "url_encrypted": encrypt_url(settings, channel_id, url, ctx.clock.now()),
                "scheme": new_url[0],
                "service_name": new_url[1],
                "last_error": None,
            }
        channels.update(row.id, **changes)
        _audit(
            session,
            ctx,
            "notification_channel.updated",
            channel_id,
            scheme=row.scheme,
            url_changed=new_url is not None,
        )

    run_immediate(factory, write)


def delete_channel(factory: sessionmaker[Session], ctx: Context, channel_id: UUID) -> None:
    require_role(ctx, Role.VIEWER)

    def write(session: Session) -> None:
        require_role_now(session, ctx, Role.VIEWER)
        channels = NotificationChannelRepository(session, ctx)
        row = channels.owned(channel_id, ctx.user.id)
        if row is None:
            raise NotFound
        scheme = row.scheme
        channels.delete(row)
        _audit(session, ctx, "notification_channel.deleted", channel_id, scheme=scheme)

    run_immediate(factory, write)


def _recipient_locale(user_locale: str | None, workspace_locale: str) -> str:
    return normalize_locale(user_locale or workspace_locale)


def send_test(
    factory: sessionmaker[Session],
    ctx: Context,
    settings: Settings,
    notifier: Notifier,
    channel_id: UUID,
) -> TestOutcome:
    """Send a test message to one of the member's own channels (throttled per member)."""
    require_role(ctx, Role.VIEWER)
    if not settings.notifications_enabled:
        return TestOutcome(False, "disabled")

    def prepare(session: Session) -> str | TestOutcome:
        require_role_now(session, ctx, Role.VIEWER)
        row = NotificationChannelRepository(session, ctx).owned(channel_id, ctx.user.id)
        if row is None:
            raise NotFound
        if not count_limited_attempt(
            session,
            "notify_test",
            f"user:{ctx.user.id}",
            limit=TEST_SEND_LIMIT,
            window=TEST_SEND_WINDOW,
            now=ctx.clock.now(),
            settings=settings,
        ):
            return TestOutcome(False, "throttled")
        url = decrypt_url(settings, row.id, row.url_encrypted)
        if url is None:
            row.last_error = "decrypt_failed"
            return TestOutcome(False, "decrypt_failed")
        if url_scheme(url) not in settings.notification_schemes:
            row.last_error = "scheme_not_allowed"
            return TestOutcome(False, "scheme_not_allowed")
        return url

    prepared = run_immediate(factory, prepare)
    if isinstance(prepared, TestOutcome):
        return prepared
    title, body = test_message(
        locale=_recipient_locale(ctx.user.locale, ctx.workspace.locale),
        workspace_name=ctx.workspace.name,
    )
    result = notifier.send(prepared, title, body)
    if not result.ok:
        logger.warning(
            "notification_test_failed workspace=%s channel=%s error=%s",
            ctx.workspace.id,
            channel_id,
            result.error,
        )

    def record(session: Session) -> None:
        row = NotificationChannelRepository(session, ctx).owned(channel_id, ctx.user.id)
        if row is None:
            return
        row.last_error = None if result.ok else result.error
        _audit(session, ctx, "notification_channel.test_sent", channel_id, ok=result.ok)

    run_immediate(factory, record)
    return TestOutcome(result.ok, result.error)


# The daily run (trusted: the scheduler passes the workspace scope and the Clock)


@dataclass(slots=True)
class _Claim:
    channel_id: UUID
    url: str | None
    error: str | None
    locale: str
    workspace_name: str
    mode: DeliveryMode
    include_amounts: bool
    today: date
    reminders: list[tuple[Reminder, UUID]] = field(default_factory=list[tuple[Reminder, UUID]])


@dataclass(slots=True)
class RunStats:
    channels: int = 0
    sent: int = 0
    failed: int = 0
    deferred: int = 0  # due channels left for the next tick (time budget)


def _local_now(clock: Clock, timezone: str) -> datetime:
    return clock.now().astimezone(ZoneInfo(timezone)).replace(tzinfo=None)


def _pending(session: Session, scope: WorkspaceScope, today: date) -> list[PendingOccurrence]:
    rows = ScheduledOccurrenceRepository(session, scope).upcoming(
        today + timedelta(days=MAX_LEAD_DAYS)
    )
    return [
        PendingOccurrence(
            id=occurrence.id,
            label=occurrence.label,
            due_date=occurrence.due_date,
            amount=Money(occurrence.expected_minor, occurrence.currency),
            lead_days=item.reminder_lead_days if item is not None else None,
            income_source_id=occurrence.income_source_id,
            work_month=occurrence.work_month,
            estimated=occurrence.amount_state == "estimated"
            and occurrence.income_source_id is not None,
        )
        for occurrence, item, _category in rows
    ]


def _work_sources(session: Session, scope: WorkspaceScope) -> list[WorkSource]:
    sources = [
        source
        for source in IncomeSourceRepository(session, scope).ordered(active_only=True)
        if source.kind == "variable"
    ]
    logged = WorkLogRepository(session, scope).work_months_for_sources(
        [source.id for source in sources]
    )
    return [
        WorkSource(
            id=source.id,
            name=source.name,
            active_from=source.active_from,
            active_to=source.active_to,
            logged_months=frozenset(logged.get(source.id, set())),
        )
        for source in sources
    ]


def _recipient(session: Session, scope: WorkspaceScope, user_id: UUID) -> User | None:
    """The channel's owner while they are an active member; ``None`` otherwise."""
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        return None
    if MembershipRepository(session, scope).for_user(user_id) is None:
        return None
    return user


def _claim(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
    channel_id: UUID,
) -> _Claim | None:
    def write(session: Session) -> _Claim | None:
        workspace_repository = CurrentWorkspaceRepository(session, scope)
        # Exclusive: two workers (PostgreSQL) must not both re-claim the same failed slot.
        workspace_repository.lock(exclusive=True)
        workspace = workspace_repository.get()
        local_now = _local_now(clock, workspace.timezone)
        today = local_now.date()
        now = clock.now()
        channels = NotificationChannelRepository(session, scope)
        channel = channels.get(channel_id)
        # Re-checked under the lock: another tick may have run it meanwhile.
        if (
            channel is None
            or not channel.enabled
            or not channel_is_due(local_now, channel.digest_time, channel.last_run_on)
        ):
            return None
        channel.last_run_on = today
        logs = NotificationLogRepository(session, scope)
        for row in logs.stale_sending(now - STALE_SENDING):
            row.status, row.error = LogStatus.FAILED.value, "interrupted"
        logs.prune(today - timedelta(days=LOG_RETENTION_DAYS))
        user = _recipient(session, scope, channel.user_id)
        if user is None:
            return None
        claim = _Claim(
            channel_id=channel.id,
            url=None,
            error=None,
            locale=_recipient_locale(user.locale, workspace.locale),
            workspace_name=workspace.name,
            mode=DeliveryMode(channel.mode),
            include_amounts=channel.include_amounts,
            today=today,
        )
        logged = {
            (row.kind, row.subject_key, row.sent_on): LogStatus(row.status)
            for row in logs.since(channel.id, today - timedelta(days=LOG_RETENTION_DAYS))
        }
        reminders = plan_reminders(
            today,
            _pending(session, scope, today),
            _work_sources(session, scope),
            ChannelRules(
                default_lead_days=channel.default_lead_days,
                due_soon=channel.notify_due_soon,
                overdue=channel.notify_overdue,
                work_logs=channel.notify_work_logs,
            ),
            logged,
            logs.overdue_sent_counts(channel.id),
        )
        if not reminders:
            return claim
        url = decrypt_url(settings, channel.id, channel.url_encrypted)
        if url is None:
            claim.error = "decrypt_failed"
        elif url_scheme(url) not in settings.notification_schemes:
            claim.error = "scheme_not_allowed"
        else:
            claim.url = url
        status = LogStatus.SENDING if claim.error is None else LogStatus.FAILED
        for reminder in reminders:
            row = logs.slot(channel.id, reminder.kind.value, reminder.subject_key, reminder.slot)
            if row is None:
                row = logs.create(
                    channel_id=channel.id,
                    kind=reminder.kind.value,
                    subject_key=reminder.subject_key,
                    occurrence_id=reminder.occurrence_id,
                    sent_on=reminder.slot,
                    status=status.value,
                    error=claim.error,
                    attempted_at=now,
                )
            else:
                row.status, row.error, row.attempted_at = status.value, claim.error, now
            claim.reminders.append((reminder, row.id))
        if claim.error is not None:
            channel.last_error = claim.error
        return claim

    return run_immediate(factory, write)


def _deliver(
    claim: _Claim,
    notifier: Notifier,
    link: str | None,
    out_of_time: Callable[[], bool] = lambda: False,
) -> dict[UUID, str | None]:
    """Send the claimed reminders; the error code (or ``None``) per log row.

    Messages not sent before ``out_of_time()`` are recorded as ``deferred`` (retried next run).
    """
    if claim.url is None:
        return {}
    by_key = {reminder.key: log_id for reminder, log_id in claim.reminders}
    results: dict[UUID, str | None] = {}
    for group in batches([reminder for reminder, _ in claim.reminders], claim.mode):
        if results and out_of_time():
            for reminder in group:
                results[by_key[reminder.key]] = "deferred"
            continue
        title, body = reminder_message(
            group,
            today=claim.today,
            locale=claim.locale,
            workspace_name=claim.workspace_name,
            include_amounts=claim.include_amounts,
            link=link,
        )
        result = notifier.send(claim.url, title, body)
        for reminder in group:
            results[by_key[reminder.key]] = None if result.ok else (result.error or "failed")
    return results


def _record(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    claim: _Claim,
    results: dict[UUID, str | None],
) -> None:
    def write(session: Session) -> None:
        logs = NotificationLogRepository(session, scope)
        now = clock.now()
        last_error: str | None = None
        for log_id, error in results.items():
            row = logs.get(log_id)
            if row is None:  # the channel was deleted meanwhile
                continue
            delivered = error is None or error in UNCERTAIN_ERRORS
            row.status = LogStatus.SENT.value if delivered else LogStatus.FAILED.value
            row.error, row.attempted_at = error, now
            last_error = last_error or error
        channel = NotificationChannelRepository(session, scope).get(claim.channel_id)
        if channel is not None and results:
            channel.last_error = last_error

    run_immediate(factory, write)


def run_workspace(
    factory: sessionmaker[Session],
    scope: WorkspaceScope,
    clock: Clock,
    settings: Settings,
    notifier: Notifier,
    *,
    out_of_time: Callable[[], bool] = lambda: False,
) -> RunStats:
    """Run every due channel of one workspace; safe to call every minute.

    A failing channel is logged and does not stop the others. When ``out_of_time()`` becomes
    true, the remaining due channels are left for the next tick (they have not run today).
    """
    stats = RunStats()
    if not settings.notifications_enabled:
        return stats
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, scope).get()
        local_now = _local_now(clock, workspace.timezone)
        due = [
            channel.id
            for channel in NotificationChannelRepository(session, scope).enabled()
            if channel_is_due(local_now, channel.digest_time, channel.last_run_on)
        ]
    link = f"{settings.base_url}/upcoming" if "base_url" in settings.model_fields_set else None
    for index, channel_id in enumerate(due):
        if out_of_time():
            stats.deferred = len(due) - index
            break
        try:
            claim = _claim(factory, scope, clock, settings, channel_id)
            if claim is None:
                continue
            stats.channels += 1
            if claim.error is not None:
                stats.failed += len(claim.reminders)
                logger.warning(
                    "notification_channel_failed workspace=%s channel=%s error=%s",
                    scope.workspace.id,
                    channel_id,
                    claim.error,
                )
                continue
            if not claim.reminders:
                continue
            results = _deliver(claim, notifier, link, out_of_time)
            _record(factory, scope, clock, claim, results)
            failed = sum(error is not None for error in results.values())
            stats.sent += len(results) - failed
            stats.failed += failed
            if failed:
                logger.warning(
                    "notification_delivery_failed workspace=%s channel=%s reminders=%d; "
                    "the next daily run retries",
                    scope.workspace.id,
                    channel_id,
                    failed,
                )
        except Exception as exc:
            logger.error(
                "notification_run_failed workspace=%s channel=%s exception_type=%s",
                scope.workspace.id,
                channel_id,
                type(exc).__name__,
                exc_info=True,
            )
    return stats
