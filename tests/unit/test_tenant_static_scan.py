from __future__ import annotations

from pathlib import Path

import pytest

import assetboard
from assetboard.db.base import Base
from assetboard.db.repositories.base import models_with_workspace_column
from tests.tenant.static_scan import (
    BOOTSTRAP_ALLOWLIST,
    scan_source,
    scan_tree,
    split_allowed,
)

SRC = Path(assetboard.__file__).parent.parent
MODELS = models_with_workspace_column(Base)


def test_no_unscoped_queries_outside_repositories() -> None:
    remaining, used = split_allowed(scan_tree(SRC, MODELS), BOOTSTRAP_ALLOWLIST)
    assert [str(violation) for violation in remaining] == [], (
        "workspace-owned models must be used through a repository in assetboard.db.repositories"
    )
    stale = set(BOOTSTRAP_ALLOWLIST) - used
    assert not stale, f"stale bootstrap allowlist entries: {sorted(stale)}"
    assert all(reason.strip() for reason in BOOTSTRAP_ALLOWLIST.values())


def test_stale_allowlist_entries_are_detected() -> None:
    allowlist = {**BOOTSTRAP_ALLOWLIST, ("assetboard.services.auth", "gone"): "removed code"}
    _remaining, used = split_allowed(scan_tree(SRC, MODELS), allowlist)
    assert set(allowlist) - used == {("assetboard.services.auth", "gone")}


# One seeded violation per detection kind.

SEEDED = {
    "select_query": (
        "from sqlalchemy import select\n"
        "from assetboard.db.models.identity import Membership\n"
        "def leak(session):\n"
        "    return session.scalars(select(Membership)).all()\n",
        "query",
    ),
    "session_get": (
        "from assetboard.db.models.identity import AuditEvent\n"
        "def leak(session, event_id):\n"
        "    return session.get(AuditEvent, event_id)\n",
        "query",
    ),
    "aliased_import": (
        "from assetboard.db.models.identity import Membership as M\n"
        "def leak(session, select):\n"
        "    return session.execute(select(M))\n",
        "query",
    ),
    "module_alias": (
        "from assetboard.db import models as m\n"
        "def leak(session, select):\n"
        "    return session.execute(select(m.Membership))\n",
        "query",
    ),
    "import_as_module": (
        "import assetboard.db.models.identity as identity\n"
        "def leak(session, select):\n"
        "    return session.execute(select(identity.AuditEvent))\n",
        "query",
    ),
    "attribute_access": (
        "from sqlalchemy import select\n"
        "from assetboard.db.models.identity import Membership, User\n"
        "def leak(session, workspace_id):\n"
        "    return select(User).where(Membership.workspace_id == workspace_id)\n",
        "attribute",
    ),
    "instantiation": (
        "from assetboard.db.models import Membership\n"
        "def leak(session, workspace_id, user_id):\n"
        "    session.add(Membership(workspace_id=workspace_id, user_id=user_id, role='owner'))\n",
        "instantiation",
    ),
}


@pytest.mark.parametrize("case", sorted(SEEDED))
def test_scan_detects_seeded_violation(case: str) -> None:
    source, kind = SEEDED[case]
    violations = scan_source(source, "assetboard.services.seeded", MODELS)
    assert [(violation.kind, violation.function) for violation in violations] == [(kind, "leak")]


def test_scan_ignores_imports_annotations_and_other_models() -> None:
    source = (
        "from __future__ import annotations\n"
        "from sqlalchemy import select\n"
        "from assetboard.db.models.identity import Membership, User\n"
        "def fine(session, row: Membership) -> list[Membership]:\n"
        "    value: Membership | None = None\n"
        "    return session.scalars(select(User)).all()\n"
    )
    assert scan_source(source, "assetboard.services.seeded", MODELS) == []


def test_allowlisted_bootstrap_is_keyed_by_module_and_function() -> None:
    source, _kind = SEEDED["instantiation"]
    violations = scan_source(source, "assetboard.services.auth", MODELS)
    remaining, used = split_allowed(violations, {("assetboard.services.auth", "other"): "x"})
    assert remaining == violations and used == set()
