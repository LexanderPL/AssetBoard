from datetime import date
from uuid import uuid4

import pytest

from assetboard.domain.import_keys import (
    classify_budget_line,
    classify_import_match,
    lineage_import_key,
    matches_lineage,
    transaction_content_key,
)


def test_identical_transaction_rows_get_distinct_occurrence_keys() -> None:
    fields = {
        "day": date(2026, 10, 1),
        "direction": "expense",
        "amount_minor": 425,
        "currency": "EUR",
        "source_category_name_key": "food",
        "description_key": "coffee",
    }

    first = transaction_content_key(**fields, occurrence=1)
    second = transaction_content_key(**fields, occurrence=2)

    assert first.startswith("c1:")
    assert len(first) == 67
    assert first != second
    assert first == transaction_content_key(**fields, occurrence=1)


def test_content_key_requires_a_positive_occurrence() -> None:
    with pytest.raises(ValueError, match="positive"):
        transaction_content_key(
            day=date(2026, 10, 1),
            direction="expense",
            amount_minor=425,
            currency="EUR",
            source_category_name_key="food",
            description_key="coffee",
            occurrence=0,
        )


def test_lineage_matches_local_ids_and_preserved_import_keys() -> None:
    source_id = uuid4()

    assert matches_lineage(source_id=source_id, local_id=source_id, import_key=None)
    assert matches_lineage(
        source_id=source_id,
        local_id=uuid4(),
        import_key=lineage_import_key(source_id),
    )
    assert not matches_lineage(source_id=source_id, local_id=uuid4(), import_key=None)


@pytest.mark.parametrize(
    ("identity_found", "values_match", "soft_deleted", "action", "reason"),
    [
        (False, False, False, "create", None),
        (True, True, False, "duplicate", "already imported"),
        (True, True, True, "duplicate", "imported before and deleted"),
        (True, False, False, "conflict", "changed since the export"),
    ],
)
def test_import_match_classification(
    identity_found: bool,
    values_match: bool,
    soft_deleted: bool,
    action: str,
    reason: str | None,
) -> None:
    decision = classify_import_match(
        identity_found=identity_found,
        values_match=values_match,
        soft_deleted=soft_deleted,
    )
    assert (decision.action, decision.reason) == (action, reason)


@pytest.mark.parametrize(
    ("existing", "incoming", "action"),
    [(None, 100, "create"), (100, 100, "duplicate"), (100, 200, "conflict")],
)
def test_budget_line_deduplication(existing: int | None, incoming: int, action: str) -> None:
    assert classify_budget_line(existing, incoming).action == action
