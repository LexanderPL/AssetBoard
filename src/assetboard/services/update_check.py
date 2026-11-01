"""Opt-in update check (ADR-0023).

When the instance setting ``check_for_updates`` is on (it is off by default), a scheduler job asks
GitHub once a day for the latest published release of this project. The request carries only a
``User-Agent`` with the product name: no cookies, no instance or user data. ``releases/latest``
already excludes drafts and prereleases; a tag that is not a plain version number is refused.

The last successful result (the release tag and when it was read) is stored on the instance
settings row. Instance administrators see "Update available" in the sidebar when that release is
newer than the running version. A failed check changes nothing and is retried the next day; it
is logged without details and never affects a page.
"""

from __future__ import annotations

import json
import logging
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, cast

from sqlalchemy.orm import Session, sessionmaker

from assetboard.branding import LATEST_RELEASE_API_URL, RELEASE_PAGE_URL, USER_AGENT
from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import InstanceSettings

ROW_ID = 1
HTTP_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 512 * 1024
CHECK_INTERVAL = timedelta(days=1)

# A plain version number, optionally with a leading "v". A release tag must be exactly that
# ("v0.3.6"); the running version only starts with it ("0.3.7" of "0.3.7.dev2+gabc").
_TAG = re.compile(r"v?(\d{1,6}(?:\.\d{1,6}){0,3})")


class UpdateCheckError(Exception):
    """The latest release could not be read; ``reason`` is a short code, safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReleaseSource(Protocol):
    def fetch_latest_release(self) -> bytes:
        """The JSON body of the latest release; raises ``UpdateCheckError``."""
        ...


class GitHubReleaseSource:
    """Stdlib HTTPS client for the GitHub releases API: one GET, 5 s timeout, no cookies."""

    def __init__(self, url: str = LATEST_RELEASE_API_URL) -> None:
        self.url = url
        # No cookie processor: the default opener neither stores nor sends cookies.
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )

    def fetch_latest_release(self) -> bytes:
        request = urllib.request.Request(
            self.url,
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                body: bytes = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise UpdateCheckError(f"http_{error.code}") from None
        except (OSError, ValueError) as error:
            raise UpdateCheckError(type(error).__name__) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise UpdateCheckError("response_too_large")
        return body


def parse_release(body: bytes) -> str:
    """The release tag from a ``releases/latest`` body; refuses drafts, prereleases, odd tags."""
    try:
        parsed: object = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise UpdateCheckError("malformed_json") from None
    if not isinstance(parsed, dict):
        raise UpdateCheckError("malformed_release")
    data = cast(dict[str, object], parsed)
    tag = data.get("tag_name")
    if not isinstance(tag, str) or _TAG.fullmatch(tag) is None:
        raise UpdateCheckError("malformed_tag")
    if data.get("draft") is True or data.get("prerelease") is True:
        raise UpdateCheckError("not_a_release")
    return tag


def version_key(text: str) -> tuple[int, ...] | None:
    """The numeric parts of a version, or ``None`` when it does not start with one."""
    match = _TAG.match(text.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(latest: str, current: str) -> bool:
    """Whether release ``latest`` is newer than the running version ``current``."""
    latest_key, current_key = version_key(latest), version_key(current)
    if latest_key is None or current_key is None:
        return False
    width = max(len(latest_key), len(current_key))
    pad = (0,) * width
    return (latest_key + pad)[:width] > (current_key + pad)[:width]


def display_version(tag: str) -> str:
    return tag.removeprefix("v")


@dataclass(frozen=True, slots=True)
class UpdateNotice:
    version: str
    url: str


def read_update_notice(session: Session, current_version: str) -> UpdateNotice | None:
    """The newer release to show to administrators, if the check is on and found one."""
    row = session.get(InstanceSettings, ROW_ID)
    if row is None or not row.check_for_updates or row.latest_release_tag is None:
        return None
    tag = row.latest_release_tag
    if _TAG.fullmatch(tag) is None or not is_newer(tag, current_version):
        return None
    return UpdateNotice(version=display_version(tag), url=RELEASE_PAGE_URL.format(tag=tag))


def update_notice(factory: sessionmaker[Session], current_version: str) -> UpdateNotice | None:
    with factory() as session:
        return read_update_notice(session, current_version)


def check_due(last_checked: datetime | None, now: datetime) -> bool:
    """A day has passed since the last successful check (or there was none)."""
    return last_checked is None or now - last_checked >= CHECK_INTERVAL


def check_enabled(factory: sessionmaker[Session]) -> tuple[bool, datetime | None]:
    """Whether the check is on, and when it last succeeded."""
    with factory() as session:
        row = session.get(InstanceSettings, ROW_ID)
        if row is None:
            return False, None
        return row.check_for_updates, row.update_checked_at


def run_update_check(
    factory: sessionmaker[Session],
    source: ReleaseSource,
    clock: Clock,
    logger: logging.Logger,
) -> str:
    """One check: ``disabled`` (setting off, nothing sent), ``checked`` or ``failed``."""
    enabled, _ = check_enabled(factory)
    if not enabled:
        return "disabled"
    try:
        tag = parse_release(source.fetch_latest_release())
    except UpdateCheckError as error:
        logger.info("update_check_failed reason=%s; next check tomorrow", error.reason)
        return "failed"

    def store(session: Session) -> bool:
        row = session.get(InstanceSettings, ROW_ID, with_for_update=True)
        if row is None or not row.check_for_updates:
            return False  # turned off while the request ran: keep nothing
        row.latest_release_tag = tag
        row.update_checked_at = clock.now()
        session.flush()
        return True

    if not run_immediate(factory, store):
        return "disabled"
    logger.debug("update_check_done latest=%s", display_version(tag))
    return "checked"
