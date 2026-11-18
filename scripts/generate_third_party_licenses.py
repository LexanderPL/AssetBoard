#!/usr/bin/env python3
"""Generate ``THIRD_PARTY_LICENSES`` from the locked runtime dependency set.

The file is a review artefact and a distribution notice: it lists every Python distribution
installed with AssetBoard, the license metadata that distribution publishes, and the license
and notice files shipped inside its wheel. Vendored JavaScript assets have their own licenses
under ``src/assetboard/static/vendor/``; ``vendor/README.md`` records their versions and
SHA-256 checksums.

Run ``make licenses`` after changing ``pyproject.toml`` or ``uv.lock``. Run
``python scripts/generate_third_party_licenses.py --check`` in CI to catch a stale file.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from importlib.metadata import Distribution, distributions
from pathlib import Path

from packaging.markers import Marker, default_environment

from assetboard.branding import APP_NAME

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "THIRD_PARTY_LICENSES"
_LICENSE_FILENAMES = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE)([._-].*)?$", re.IGNORECASE)
_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^ ;\\]+)")


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_requirements(requirements: Path | None = None) -> dict[str, str]:
    """Normalized name -> locked version for the runtime closure (project itself excluded)."""
    if requirements is not None:
        text = requirements.read_text(encoding="utf-8")
    else:
        result = subprocess.run(
            [
                "uv",
                "export",
                "--frozen",
                "--no-default-groups",
                "--no-emit-project",
                "--no-hashes",
                "--format",
                "requirements-txt",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        text = result.stdout
    found: dict[str, str] = {}
    environment = default_environment()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        requirement, _, marker = stripped.partition(";")
        if marker:
            try:
                if not Marker(marker.strip()).evaluate(environment):
                    continue
            except Exception:  # a marker newer than the local packaging fallback
                continue
        match = _REQUIREMENT.match(requirement.strip())
        if match is not None:
            found[normalize(match.group("name"))] = match.group("version")
    return found


def _license_expression(metadata: object) -> str:
    get = metadata.get  # type: ignore[attr-defined]
    get_all = metadata.get_all  # type: ignore[attr-defined]
    expression = (get("License-Expression") or "").strip()
    if expression:
        return expression
    declared = (get("License") or "").strip()
    if declared and "\n" not in declared and len(declared) <= 120:
        return declared
    classifiers = [
        value.removeprefix("License ::").strip()
        for value in get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    return "; ".join(dict.fromkeys(classifiers)) or "unknown (not declared in metadata)"


def _project_url(metadata: object) -> str:
    get = metadata.get  # type: ignore[attr-defined]
    get_all = metadata.get_all  # type: ignore[attr-defined]
    home = (get("Home-page") or "").strip()
    if home:
        return home
    for value in get_all("Project-URL") or []:
        _label, _, url = value.partition(",")
        if url.strip().startswith(("https://", "http://")):
            return url.strip()
    return ""


def _license_files(dist: Distribution) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for item in dist.files or []:
        name = str(item)
        if not _LICENSE_FILENAMES.search(name):
            continue
        try:
            path = item.locate()
        except (OSError, ValueError):
            continue
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.append((name, "\n".join(line.rstrip() for line in text.splitlines()).rstrip()))
    return sorted(found)


def generate(requirements: dict[str, str]) -> str:
    installed = {
        normalize(dist.metadata["Name"]): dist
        for dist in distributions()
        if dist.metadata.get("Name")
    }
    missing = sorted(set(requirements) - set(installed))
    if missing:
        raise SystemExit(
            "runtime dependencies are not installed in this environment: "
            + ", ".join(missing)
            + "\nRun `uv sync --all-groups` first."
        )

    lines = [
        f"{APP_NAME} third-party notices",
        "=" * (len(APP_NAME) + len(" third-party notices")),
        "",
        "Generated by scripts/generate_third_party_licenses.py from the locked runtime",
        "dependencies. Do not edit by hand; run `make licenses` instead.",
        "",
        "The Python packages below are installed with AssetBoard. Their license metadata and",
        "the license or notice files shipped in their wheels follow. Vendored JavaScript",
        "assets are listed in src/assetboard/static/vendor/README.md.",
        "",
    ]
    for name in sorted(requirements):
        dist = installed[name]
        metadata = dist.metadata
        locked_version = requirements[name]
        if dist.version != locked_version:
            raise SystemExit(
                f"{metadata['Name']} {dist.version} is installed but uv.lock pins "
                f"{locked_version}; run `uv sync --all-groups` first."
            )
        lines.extend(
            [
                "-" * 78,
                f"Package: {metadata['Name']}",
                f"Version: {locked_version}",
                f"License: {_license_expression(metadata)}",
            ]
        )
        url = _project_url(metadata)
        if url:
            lines.append(f"Project: {url}")
        license_files = _license_files(dist)
        if not license_files:
            lines.append("License text: not installed in the wheel's metadata.")
        for filename, text in license_files:
            lines.extend(["", f"--- {filename} ---", text])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--requirements",
        type=Path,
        help="read a requirements file instead of running `uv export`",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the output file is stale",
    )
    args = parser.parse_args(argv)
    text = generate(runtime_requirements(args.requirements))
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.exists() else ""
        if current != text:
            print(
                f"{args.output} is stale; run `make licenses` and commit the result.",
                file=sys.stderr,
            )
            return 1
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
