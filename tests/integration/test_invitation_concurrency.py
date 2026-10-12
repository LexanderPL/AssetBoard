"""Two people accepting the same invitation at the same time: exactly one membership."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from assetboard.services import invitations
from assetboard.services.invitations import InvitationInvalid, accept_invitation
from assetboard.services.workspaces import create_invitation
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def test_concurrent_accepts_create_one_membership(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("olga", "owner")
    for key in ("amy", "ben"):
        db.add_member(key, "owner", workspace_id=db.add_workspace(f"{key}-own"))
    token = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="editor", email=None, expires_in_days=7
    ).token

    def accept(key: str) -> object:
        return accept_invitation(
            db.factory,
            token,
            user_id=db.users[key],
            user_email=f"{key}@example.com",
            clock=db.clock,
        )

    outcomes = serialize_at_checkpoint(
        db.engine, monkeypatch, invitations, lambda: accept("amy"), lambda: accept("ben")
    )
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], InvitationInvalid)
    assert db.roles() == {"olga": "owner", "amy": "editor"}
