#!/usr/bin/env python3
"""Print GitHub issue creation commands for feature headings in milestone specs."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

FEATURE_HEADING = re.compile(r"^##\s+(F\d+\.\d+)\s+(.+?)\s*$", re.MULTILINE)
ACCEPTANCE = re.compile(
    r"^\*\*Acceptance\*\*\s*$([\s\S]*?)(?=^\*\*[^*]+\*\*|^## |\Z)", re.MULTILINE
)


@dataclass(frozen=True)
class Feature:
    spec_id: str
    title: str
    source: Path
    milestone: str
    acceptance: tuple[str, ...]
    stretch: bool


def find_features(spec_dir: Path) -> list[Feature]:
    """Read feature headings and acceptance bullets from M*.md files."""
    repo_root = Path(__file__).resolve().parents[1]
    features: list[Feature] = []
    for source in sorted(spec_dir.glob("M*.md")):
        contents = source.read_text(encoding="utf-8")
        matches = list(FEATURE_HEADING.finditer(contents))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(contents)
            section = contents[match.end() : end]
            acceptance_match = ACCEPTANCE.search(section)
            bullets = (
                tuple(
                    bullet.strip()
                    for bullet in re.findall(r"^\s*-\s+(.+?)\s*$", acceptance_match.group(1), re.M)
                )
                if acceptance_match
                else ()
            )
            milestone_match = re.match(r"^(M\d+)-", source.name)
            if milestone_match is None:
                continue
            try:
                relative_source = source.resolve().relative_to(repo_root)
            except ValueError:
                relative_source = source
            features.append(
                Feature(
                    spec_id=match.group(1),
                    title=match.group(2),
                    source=relative_source,
                    milestone=milestone_match.group(1),
                    acceptance=bullets,
                    stretch="(stretch)" in match.group(2).lower(),
                )
            )
    return features


def command_for(feature: Feature) -> list[str]:
    title = f"{feature.spec_id} {feature.title}"
    checklist = feature.acceptance or ("Review acceptance criteria in the feature specification.",)
    body = (
        f"Feature specification: `{feature.source.as_posix()}`\n\n"
        "## Acceptance checklist\n\n" + "\n".join(f"- [ ] {item}" for item in checklist)
    )
    command = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--milestone",
        feature.milestone,
        "--body",
        body,
    ]
    if feature.stretch:
        command.extend(["--label", "stretch"])
    return command


def existing_issue_titles() -> set[str]:
    result = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--limit", "1000", "--json", "title"],
        check=True,
        capture_output=True,
        text=True,
    )
    return {item["title"] for item in json.loads(result.stdout)}


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--spec-dir",
        type=Path,
        default=repo_root / "docs" / "specs",
        help="directory containing M*.md specs",
    )
    parser.add_argument(
        "--execute", action="store_true", help="run gh commands (default: print commands only)"
    )
    args = parser.parse_args()

    existing_titles = existing_issue_titles()
    for feature in find_features(args.spec_dir):
        command = command_for(feature)
        if command[4] in existing_titles:
            continue
        if args.execute:
            subprocess.run(command, check=True)
        else:
            print(shlex.join(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
