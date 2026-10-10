#!/usr/bin/env python3
"""Print GitHub issue creation commands for feature headings in milestone specs."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

FEATURE_HEADING = re.compile(r"^##\s+(F\d+\.\d+)\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Feature:
    spec_id: str
    title: str
    source: Path


def find_features(spec_dir: Path) -> list[Feature]:
    """Read feature headings from M*.md files in stable order."""
    features: list[Feature] = []
    for source in sorted(spec_dir.glob("M*.md")):
        contents = source.read_text(encoding="utf-8")
        for match in FEATURE_HEADING.finditer(contents):
            features.append(Feature(match.group(1), match.group(2), source))
    return features


def command_for(feature: Feature) -> list[str]:
    title = f"{feature.spec_id} {feature.title}"
    source = feature.source
    if source.is_absolute() and source.is_relative_to(Path.cwd()):
        source = source.relative_to(Path.cwd())
    body = f"Feature specification: `{source.as_posix()}`\n\n## Acceptance checklist\n\n- [ ]"
    return ["gh", "issue", "create", "--title", title, "--body", body]


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

    features = find_features(args.spec_dir)
    for feature in features:
        command = command_for(feature)
        if args.execute:
            subprocess.run(command, check=True)
        else:
            print(shlex.join(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
