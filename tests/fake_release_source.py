"""A ``ReleaseSource`` for tests: returns a canned body or fails, never uses the network."""

from __future__ import annotations

import json
from dataclasses import dataclass

from assetboard.services.update_check import UpdateCheckError


def release_body(tag: str, *, draft: bool = False, prerelease: bool = False) -> bytes:
    return json.dumps(
        {"tag_name": tag, "name": tag, "draft": draft, "prerelease": prerelease}
    ).encode()


@dataclass
class FakeReleaseSource:
    # The body ``fetch_latest_release`` returns; ``fail_with`` raises that reason instead.
    body: bytes = b""
    fail_with: str | None = "offline"
    calls: int = 0

    def fetch_latest_release(self) -> bytes:
        self.calls += 1
        if self.fail_with is not None:
            raise UpdateCheckError(self.fail_with)
        return self.body
