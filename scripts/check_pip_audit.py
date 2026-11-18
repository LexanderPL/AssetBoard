#!/usr/bin/env python3
"""Fail when ``pip-audit`` found a high or critical Python vulnerability.

``pip-audit``'s JSON output names the affected package and advisory but does not include a
severity. The advisory's OSV record does: GitHub advisories carry ``database_specific.severity``
and some ecosystems carry it under ``affected[].ecosystem_specific``. A record with no severity
is treated as high: the gate is fail-closed, and an operator can still read the non-blocking
report for the details.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

OSV_URL = "https://api.osv.dev/v1/vulns/"
RANK = {"LOW": 1, "MODERATE": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


def advisories(report: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    """(package, version, advisory) for every vulnerability in a pip-audit JSON report."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for dependency in report.get("dependencies", []):
        for vulnerability in dependency.get("vulns", []) or []:
            found.append(
                (
                    str(dependency.get("name", "?")),
                    str(dependency.get("version", "?")),
                    vulnerability,
                )
            )
    return found


def _from_record(record: dict[str, Any]) -> str | None:
    for candidate in (
        record.get("database_specific", {}).get("severity"),
        *(
            affected.get("ecosystem_specific", {}).get("severity")
            for affected in record.get("affected", [])
        ),
    ):
        if isinstance(candidate, str) and candidate.upper() in RANK:
            return candidate.upper()
    return None


def severity(advisory: dict[str, Any], fetch: Callable[[str], dict[str, Any]]) -> str:
    """The advisory's severity from its embedded data or its OSV record; UNKNOWN if absent."""
    direct = advisory.get("severity")
    if isinstance(direct, str) and direct.upper() in RANK:
        return direct.upper()
    if isinstance(direct, list):
        for item in direct:
            if isinstance(item, str) and item.upper() in RANK:
                return item.upper()
    advisory_id = advisory.get("id")
    if not isinstance(advisory_id, str) or not advisory_id:
        return "UNKNOWN"
    try:
        record = fetch(advisory_id)
    except (OSError, urllib.error.URLError, ValueError, KeyError):
        return "UNKNOWN"
    return _from_record(record) or "UNKNOWN"


def fetch_osv(advisory_id: str) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - fixed HTTPS endpoint and an ID from pip-audit
        OSV_URL + advisory_id,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def blocking(
    report: dict[str, Any], fetch: Callable[[str], dict[str, Any]] = fetch_osv
) -> list[tuple[str, str, str, str]]:
    """(package, version, advisory id, severity) for every high/critical or unknown advisory."""
    result: list[tuple[str, str, str, str]] = []
    for package, version, advisory in advisories(report):
        level = severity(advisory, fetch)
        if level == "UNKNOWN" or RANK[level] >= RANK["HIGH"]:
            result.append((package, version, str(advisory.get("id", "?")), level))
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="pip-audit JSON report")
    args = parser.parse_args(list(argv) if argv is not None else None)
    with open(args.report, encoding="utf-8") as report_file:
        report = json.load(report_file)
    findings = blocking(report)
    if findings:
        print("High or critical Python vulnerabilities:", file=sys.stderr)
        for package, version, advisory_id, level in findings:
            print(f"  {package} {version}: {advisory_id} ({level})", file=sys.stderr)
        return 1
    print("No high or critical Python vulnerabilities.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
