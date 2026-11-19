#!/usr/bin/env python3
"""Verify the image tags a release build produces (F9.8).

``docker/metadata-action`` in ``.github/workflows/docker.yml`` is the source of truth for the
published tags. This script checks its output against the release policy, so a tag-rule change
cannot silently drop ``1``, ``1.0`` or ``latest`` (or publish a major tag for 0.x). It can also
inspect the published manifest list to confirm every expected tag exists, resolves to the same
digest and carries both release platforms.

Stable ``v1.0.0`` -> ``1.0.0``, ``1.0``, ``1``, ``latest``.
Stable ``v0.8.3`` -> ``0.8.3``, ``0.8``, ``latest`` (no major-only tag before 1.0).
Prerelease ``v1.0.0-rc.1`` -> ``1.0.0-rc.1`` only; it does not move ``latest``.

Usage:
  # docker/metadata-action output on stdin
  printf '%s\\n' "$IMAGE_TAGS" | scripts/verify_release_tags.py --tag v1.0.0

  # what the policy expects, one tag name per line
  scripts/verify_release_tags.py --tag v1.0.0 --print-expected

  # inspect an already-published multi-arch image (needs docker and a registry login)
  scripts/verify_release_tags.py --tag v1.0.0 --check-remote ghcr.io/owner/assetboard
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence

RELEASE_TAG = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?P<prerelease>-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
RELEASE_PLATFORMS = frozenset({"linux/amd64", "linux/arm64"})


class TagError(ValueError):
    """A tag or a metadata output that does not follow the release policy."""


def expected_tags(tag: str) -> tuple[str, ...]:
    """The image tag suffixes ``docker/metadata-action`` must produce for ``tag``."""
    match = RELEASE_TAG.match(tag.strip())
    if match is None:
        raise TagError(f"{tag!r} is not a release tag such as v1.0.0 or v1.0.0-rc.1")
    version = tag.strip().removeprefix("v")
    if match["prerelease"]:
        return (version,)
    major = int(match["major"])
    minor = int(match["minor"])
    tags = [version, f"{major}.{minor}"]
    if major > 0:
        tags.append(str(major))
    tags.append("latest")
    return tuple(tags)


def tag_from_reference(reference: str) -> str:
    """The tag part of ``ghcr.io/owner/app:1.0.0`` (registries may carry a port)."""
    name = reference.strip().rsplit("/", 1)[-1]
    _, separator, tag = name.rpartition(":")
    if not separator or not tag:
        raise TagError(f"{reference!r} has no image tag")
    return tag


def validate_metadata(tag: str, references: Iterable[str]) -> tuple[str, ...]:
    """Check metadata-action output exactly matches the policy; return the actual tags."""
    expected = set(expected_tags(tag))
    lines = [line.strip() for line in references if line.strip()]
    actual = [tag_from_reference(line) for line in lines]
    duplicates = {name for name in actual if actual.count(name) > 1}
    if duplicates:
        raise TagError(f"duplicate image tags: {', '.join(sorted(duplicates))}")
    found = set(actual)
    missing = sorted(expected - found)
    unexpected = sorted(found - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected {', '.join(unexpected)}")
        raise TagError(f"metadata for {tag} is wrong: {'; '.join(details)}")
    return tuple(sorted(found))


def _inspect(reference: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["docker", "manifest", "inspect", reference],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise TagError("docker is required for --check-remote") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit code {exc.returncode}"
        raise TagError(f"cannot inspect {reference}: {reason}") from exc
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise TagError(f"{reference} returned no readable manifest") from exc
    if not isinstance(document, dict):
        raise TagError(f"{reference} returned an unexpected manifest")
    return document


def _platforms(manifest: dict[str, object]) -> set[str]:
    entries = manifest.get("manifests")
    if not isinstance(entries, list):
        return set()
    platforms: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        platform = entry.get("platform")
        if not isinstance(platform, dict):
            continue
        os_name = platform.get("os")
        architecture = platform.get("architecture")
        if isinstance(os_name, str) and isinstance(architecture, str):
            platforms.add(f"{os_name}/{architecture}")
    return platforms


def check_remote(base_image: str, tag: str) -> dict[str, str]:
    """Inspect every expected tag; all must be multi-arch and share one manifest digest."""
    base = base_image.rstrip("/:")
    if ":" in base.rsplit("/", 1)[-1]:
        raise TagError("pass the image repository without a tag to --check-remote")
    digests: dict[str, str] = {}
    for name in expected_tags(tag):
        reference = f"{base}:{name}"
        manifest = _inspect(reference)
        missing = RELEASE_PLATFORMS - _platforms(manifest)
        if missing:
            raise TagError(f"{reference} is missing platforms: {', '.join(sorted(missing))}")
        digests[name] = hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode("utf-8")
        ).hexdigest()
    unique = set(digests.values())
    if len(unique) != 1:
        rendered = ", ".join(f"{name}={digest[:12]}" for name, digest in digests.items())
        raise TagError(f"release tags point to different manifests: {rendered}")
    return digests


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag, for example v1.0.0")
    parser.add_argument(
        "--print-expected",
        action="store_true",
        help="print the expected image tag suffixes and exit",
    )
    parser.add_argument(
        "--check-remote",
        metavar="IMAGE",
        help="inspect an already-published image repository (no tag) and verify its manifests",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.print_expected:
            for name in expected_tags(args.tag):
                print(name)
            return 0
        if args.check_remote:
            digests = check_remote(args.check_remote, args.tag)
            digest = next(iter(digests.values()))
            print(f"All release tags share manifest sha256:{digest}:")
            for name in expected_tags(args.tag):
                print(f"  {args.check_remote.rstrip('/:')}:{name}")
            return 0
        references = sys.stdin.read().splitlines()
        if not references:
            raise TagError("no image references on stdin; pipe docker/metadata-action tags")
        tags = validate_metadata(args.tag, references)
        print(f"{args.tag} publishes: {', '.join(tags)}")
        return 0
    except TagError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
