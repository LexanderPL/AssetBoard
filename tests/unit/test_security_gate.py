"""The pip-audit severity gate (F9.6): high/critical block, everything else is reported."""

from __future__ import annotations

from typing import Any

from scripts.check_pip_audit import advisories, blocking, severity


def _report(*vulns: dict[str, Any]) -> dict[str, Any]:
    return {"dependencies": [{"name": "example", "version": "1.0", "vulns": list(vulns)}]}


def test_advisories_flattens_the_pip_audit_shape() -> None:
    report = _report({"id": "GHSA-1"}, {"id": "GHSA-2"})
    assert [(name, version, item["id"]) for name, version, item in advisories(report)] == [
        ("example", "1.0", "GHSA-1"),
        ("example", "1.0", "GHSA-2"),
    ]
    assert advisories({}) == []


def test_severity_prefers_embedded_data_then_the_osv_record() -> None:
    assert severity({"severity": "HIGH"}, fetch=lambda _id: {}) == "HIGH"
    seen: list[str] = []

    def fetch(advisory_id: str) -> dict[str, Any]:
        seen.append(advisory_id)
        return {"database_specific": {"severity": "CRITICAL"}}

    assert severity({"id": "GHSA-x"}, fetch=fetch) == "CRITICAL"
    assert seen == ["GHSA-x"]


def test_severity_uses_the_affected_ecosystem_record() -> None:
    record = {"affected": [{"ecosystem_specific": {"severity": "MODERATE"}}]}
    assert severity({"id": "PYSEC-x"}, fetch=lambda _id: record) == "MODERATE"


def test_unknown_severity_is_fail_closed() -> None:
    assert severity({"id": "GHSA-x"}, fetch=lambda _id: {}) == "UNKNOWN"
    assert (
        severity({"id": "GHSA-x"}, fetch=lambda _id: (_ for _ in ()).throw(OSError())) == "UNKNOWN"
    )


def test_the_gate_blocks_high_critical_and_unknown_but_not_low_or_medium() -> None:
    report = _report(
        {"id": "GHSA-low", "severity": "LOW"},
        {"id": "GHSA-medium", "severity": "MEDIUM"},
        {"id": "GHSA-high", "severity": "HIGH"},
        {"id": "GHSA-critical", "severity": "CRITICAL"},
    )
    levels = {item[2]: item[3] for item in blocking(report, fetch=lambda _id: {})}
    assert levels == {
        "GHSA-high": "HIGH",
        "GHSA-critical": "CRITICAL",
    }
    assert [item[2] for item in blocking(report, fetch=lambda _id: {})] == [
        "GHSA-high",
        "GHSA-critical",
    ]
    assert blocking(_report(), fetch=lambda _id: {}) == []


def test_an_unknown_advisory_blocks() -> None:
    report = _report({"id": "GHSA-unrated"})
    assert blocking(report, fetch=lambda _id: {}) == [("example", "1.0", "GHSA-unrated", "UNKNOWN")]
