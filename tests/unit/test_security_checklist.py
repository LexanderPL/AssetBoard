"""The security checklist must stay complete and honest (F9.6)."""

from __future__ import annotations

import re
from pathlib import Path

CHECKLIST = Path(__file__).resolve().parents[2] / "docs" / "security-checklist.md"
ROW = re.compile(
    r"^\| (?P<id>\d+\.\d+\.\d+) \| (?P<requirement>.*?) \| "
    r"(?P<status>Done|N/A|Gap) \| (?P<evidence>.*?) \|$"
)


def rows() -> list[re.Match[str]]:
    return [
        match
        for line in CHECKLIST.read_text(encoding="utf-8").splitlines()
        if (match := ROW.match(line))
    ]


def test_checklist_covers_every_asvs_40_level_1_requirement_once() -> None:
    found = [match["id"] for match in rows()]
    # ASVS 4.0.3 Level 1 has 127 requirements across V2-V14; V1 has none.
    assert len(found) == 127
    assert len(set(found)) == len(found)
    assert found[0] == "2.1.1" and found[-1] == "14.5.3"


def test_every_row_has_a_status_and_a_link_or_an_na_reason() -> None:
    for match in rows():
        assert match["status"] in {"Done", "N/A", "Gap"}
        evidence = match["evidence"].strip()
        assert evidence, match["id"]
        if match["status"] in {"Done", "Gap"}:
            assert "](" in evidence, f"{match['id']} has no code or test link"


def test_every_gap_is_repeated_in_the_summary() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    summary = text.split("### Genuine gaps and follow-ups", 1)[1].split("\n## ", 1)[0]
    gap_ids = [match["id"] for match in rows() if match["status"] == "Gap"]
    for gap_id in gap_ids:
        assert f"**{gap_id}**" in summary, gap_id


def test_checklist_names_the_ci_and_license_artefacts() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    for required in (
        "security.yml",
        "docker.yml",
        "THIRD_PARTY_LICENSES",
        "Trivy",
        "pip-audit",
        "SBOM",
    ):
        assert required in text, required


def test_summary_counts_match_the_rows() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    counts = {
        status: sum(1 for row in rows() if row["status"] == status)
        for status in ("Done", "N/A", "Gap")
    }
    for status, count in counts.items():
        assert re.search(rf"\| {re.escape(status)} \| {count} \|", text), (status, count)


def test_every_local_link_target_exists() -> None:
    text = CHECKLIST.read_text(encoding="utf-8")
    for target in re.findall(r"\]\((\.\./[^)#]+)", text):
        assert (CHECKLIST.parent / target).resolve().exists(), target
