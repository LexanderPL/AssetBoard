"""F9.3 archive coverage: every workspace-owned model is archived or explained."""

from __future__ import annotations

from assetboard.db import models as _models  # noqa: F401 - registers every ORM model
from assetboard.db.base import Base
from assetboard.db.repositories.base import models_marked_workspace_owned
from assetboard.services.archive import (
    ARCHIVE_EXCLUDED_MODELS,
    ARCHIVE_INCLUDED_MODELS,
    ARCHIVE_SECRET_MODELS,
)


def test_every_workspace_owned_model_is_archived_secret_or_excluded() -> None:
    models = {model.__name__ for model in models_marked_workspace_owned(Base)}
    included = set(ARCHIVE_INCLUDED_MODELS)
    secret = set(ARCHIVE_SECRET_MODELS)
    excluded = set(ARCHIVE_EXCLUDED_MODELS)

    assert included | secret | excluded == models
    assert not included & secret
    assert not included & excluded
    assert not secret & excluded


def test_every_exclusion_has_a_reason() -> None:
    assert all(reason.strip() for reason in ARCHIVE_EXCLUDED_MODELS.values())
    assert all(reason.strip() for reason in ARCHIVE_SECRET_MODELS.values())
