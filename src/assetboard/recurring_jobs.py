from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.repositories import NotFound
from assetboard.db.repositories.scheduled import RecurringWorkspaceReader
from assetboard.services.notifications import run_workspace as run_notifications
from assetboard.services.notify import Notifier
from assetboard.services.occurrences import auto_post_workspace
from assetboard.services.scheduled import generate_workspace


@dataclass(frozen=True, slots=True)
class _Workspace:
    id: UUID


@dataclass(frozen=True, slots=True)
class _Scope:
    workspace: _Workspace


class RecurringJobs:
    def __init__(
        self,
        factory: sessionmaker[Session],
        settings: Settings,
        clock: Callable[[], Clock],
        logger: logging.Logger,
        notifier: Callable[[], Notifier] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory, self.settings, self.clock, self.logger = factory, settings, clock, logger
        self.notifier, self.monotonic = notifier, monotonic
        self._notify_offset = 0
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.zones: dict[UUID, str] = {}
        self._reconciling = threading.Lock()
        self.caught_up = threading.Event()  # set when the startup catch-up has finished

    def run(self, workspace_id: UUID) -> None:
        try:
            generate_workspace(
                self.factory, _Scope(_Workspace(workspace_id)), self.clock(), self.settings
            )
        except NotFound:
            # The workspace was deleted after the last refresh (ADR-0020); the next refresh
            # removes its job. Expected, so no error and no traceback.
            self.logger.debug(
                "recurring_generation_skipped workspace=%s reason=deleted", workspace_id
            )
        except Exception as exc:
            self.logger.error(
                "recurring_generation_failed workspace=%s exception_type=%s; next run will retry",
                workspace_id,
                type(exc).__name__,
                exc_info=True,
            )

    def auto_post(self, workspace_id: UUID) -> None:
        try:
            auto_post_workspace(self.factory, _Scope(_Workspace(workspace_id)), self.clock())
        except Exception as exc:
            self.logger.error(
                "recurring_auto_post_failed workspace=%s exception_type=%s; next run will retry",
                workspace_id,
                type(exc).__name__,
                exc_info=True,
            )

    def notify(self) -> None:
        """One reminder tick (F3.6): run the due channels of every workspace.

        Bounded by ``NOTIFICATION_TICK_SECONDS``: no new channel starts after it, and the
        channels not reached run on the next tick. The starting workspace rotates so that a
        slow workspace cannot starve the others.
        """
        if self.notifier is None or not self.settings.notifications_enabled:
            return
        deadline = self.monotonic() + self.settings.notification_tick_seconds

        def out_of_time() -> bool:
            return self.monotonic() >= deadline

        workspace_ids = list(self.zones)
        if not workspace_ids:
            return
        start = self._notify_offset % len(workspace_ids)
        self._notify_offset += 1
        for workspace_id in workspace_ids[start:] + workspace_ids[:start]:
            if out_of_time():
                break
            try:
                run_notifications(
                    self.factory,
                    _Scope(_Workspace(workspace_id)),
                    self.clock(),
                    self.settings,
                    self.notifier(),
                    out_of_time=out_of_time,
                )
            except NotFound:
                self.logger.debug(
                    "notification_run_skipped workspace=%s reason=deleted", workspace_id
                )
            except Exception as exc:
                self.logger.error(
                    "notification_run_failed workspace=%s exception_type=%s; next tick retries",
                    workspace_id,
                    type(exc).__name__,
                    exc_info=True,
                )

    def reconcile(self) -> None:
        with self._reconciling:
            self._reconcile()

    def catch_up(self) -> None:
        """Startup catch-up: rebuild the daily jobs and generate every workspace's horizon."""
        try:
            self.reconcile()
            for workspace_id in tuple(self.zones):
                self.run(workspace_id)
                self.auto_post(workspace_id)
        finally:
            self.caught_up.set()

    def _reconcile(self) -> None:
        with self.factory() as session:
            workspaces = dict(RecurringWorkspaceReader(session).workspaces())
        for workspace_id, timezone in workspaces.items():
            if self.zones.get(workspace_id) != timezone:
                self.scheduler.add_job(
                    self.run,
                    CronTrigger(hour=3, minute=0, timezone=timezone),
                    args=[workspace_id],
                    id=str(workspace_id),
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=None,
                )
                self.scheduler.add_job(
                    self.auto_post,
                    CronTrigger(hour=0, minute=5, timezone=timezone),
                    args=[workspace_id],
                    id=f"auto-post-{workspace_id}",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=None,
                )
        for workspace_id in self.zones.keys() - workspaces.keys():
            self.scheduler.remove_job(str(workspace_id))
            self.scheduler.remove_job(f"auto-post-{workspace_id}")
        self.zones = workspaces

    def start(self) -> None:
        """Start without touching the database: the catch-up runs as an immediate job."""
        self.scheduler.add_job(self.catch_up, id="startup-catch-up", misfire_grace_time=None)
        self.scheduler.add_job(self.reconcile, "interval", minutes=1, id="refresh-workspaces")
        self.scheduler.add_job(
            self.notify,
            "interval",
            minutes=1,
            id="notifications",
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=True)
