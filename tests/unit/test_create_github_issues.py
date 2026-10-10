from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_github_issues.py"
SPEC = importlib.util.spec_from_file_location("create_github_issues", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


def test_find_features_reads_milestone_headings_in_order(tmp_path: Path) -> None:
    (tmp_path / "M1-identity.md").write_text("## F1.2 Workspaces\n", encoding="utf-8")
    (tmp_path / "M0-foundations.md").write_text("## F0.1 Scaffold\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("## F9.9 Ignored\n", encoding="utf-8")

    features = generator.find_features(tmp_path)

    assert [(feature.spec_id, feature.title) for feature in features] == [
        ("F0.1", "Scaffold"),
        ("F1.2", "Workspaces"),
    ]
    assert features[0].source.name == "M0-foundations.md"


def test_command_for_includes_spec_and_acceptance_checklist() -> None:
    feature = generator.Feature("F0.1", "Repository scaffold", Path("docs/specs/M0-foundations.md"))

    command = generator.command_for(feature)

    assert command[:5] == ["gh", "issue", "create", "--title", "F0.1 Repository scaffold"]
    assert command[5] == "--body"
    assert "docs/specs/M0-foundations.md" in command[6]
    assert "## Acceptance checklist" in command[6]
