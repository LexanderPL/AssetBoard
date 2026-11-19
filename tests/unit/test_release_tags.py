"""Release image tag rules for v1.0 (F9.8)."""

from __future__ import annotations

import pytest
from scripts import verify_release_tags
from scripts.verify_release_tags import (
    TagError,
    check_remote,
    expected_tags,
    tag_from_reference,
    validate_metadata,
)


def test_stable_v1_publishes_version_minor_major_and_latest() -> None:
    assert expected_tags("v1.0.0") == ("1.0.0", "1.0", "1", "latest")
    assert expected_tags("v1.2.3") == ("1.2.3", "1.2", "1", "latest")


def test_zero_x_omits_the_major_only_tag() -> None:
    assert expected_tags("v0.8.3") == ("0.8.3", "0.8", "latest")


def test_prerelease_publishes_only_its_version_and_does_not_move_latest() -> None:
    assert expected_tags("v1.0.0-rc.1") == ("1.0.0-rc.1",)
    assert expected_tags("v1.2.3-beta.2") == ("1.2.3-beta.2",)


def test_the_leading_v_is_optional() -> None:
    assert expected_tags("1.0.0") == expected_tags("v1.0.0")


@pytest.mark.parametrize("tag", ["1.0", "v1", "latest", "v1.0.0.0", "release-1.0.0"])
def test_a_non_release_tag_is_refused(tag: str) -> None:
    with pytest.raises(TagError):
        expected_tags(tag)


def test_tag_extraction_handles_a_registry_port() -> None:
    assert tag_from_reference("ghcr.io:443/owner/assetboard:1.0.0") == "1.0.0"
    assert tag_from_reference("assetboard:latest") == "latest"


def test_metadata_must_match_exactly() -> None:
    references = [
        "ghcr.io/owner/assetboard:1.0.0",
        "ghcr.io/owner/assetboard:1.0",
        "ghcr.io/owner/assetboard:1",
        "ghcr.io/owner/assetboard:latest",
    ]
    assert validate_metadata("v1.0.0", references) == ("1", "1.0", "1.0.0", "latest")


def test_missing_or_extra_metadata_tags_fail() -> None:
    without_latest = [
        "ghcr.io/owner/assetboard:1.0.0",
        "ghcr.io/owner/assetboard:1.0",
        "ghcr.io/owner/assetboard:1",
    ]
    with pytest.raises(TagError, match="missing latest"):
        validate_metadata("v1.0.0", without_latest)

    with_extra = [
        *without_latest,
        "ghcr.io/owner/assetboard:latest",
        "ghcr.io/owner/assetboard:next",
    ]
    with pytest.raises(TagError, match="unexpected next"):
        validate_metadata("v1.0.0", with_extra)


def test_duplicate_metadata_tags_fail() -> None:
    references = [
        "ghcr.io/owner/assetboard:1.0.0",
        "ghcr.io/owner/assetboard:1.0.0",
        "ghcr.io/owner/assetboard:1.0",
        "ghcr.io/owner/assetboard:1",
        "ghcr.io/owner/assetboard:latest",
    ]
    with pytest.raises(TagError, match="duplicate"):
        validate_metadata("v1.0.0", references)


def test_metadata_changes_the_zero_x_major_tag() -> None:
    # A 0.x build never publishes `0`, even if metadata-action starts doing so.
    references = [
        "ghcr.io/owner/assetboard:0.8.3",
        "ghcr.io/owner/assetboard:0.8",
        "ghcr.io/owner/assetboard:0",
        "ghcr.io/owner/assetboard:latest",
    ]
    with pytest.raises(TagError, match="unexpected 0"):
        validate_metadata("v0.8.3", references)


def _manifest(*platforms: tuple[str, str]) -> dict[str, object]:
    return {
        "manifests": [
            {"platform": {"os": os_name, "architecture": architecture}}
            for os_name, architecture in platforms
        ]
    }


def test_remote_check_accepts_linux_amd64_and_arm64(monkeypatch: pytest.MonkeyPatch) -> None:
    inspected: list[str] = []

    def fake(reference: str) -> dict[str, object]:
        inspected.append(reference)
        return _manifest(("linux", "amd64"), ("linux", "arm64"))

    monkeypatch.setattr(verify_release_tags, "_inspect", fake)
    digests = check_remote("ghcr.io/owner/assetboard", "v1.0.0")
    assert len(set(digests.values())) == 1
    assert inspected == [f"ghcr.io/owner/assetboard:{name}" for name in expected_tags("v1.0.0")]


def test_remote_check_refuses_a_windows_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(reference: str) -> dict[str, object]:
        if reference.endswith(":latest"):
            return _manifest(("linux", "amd64"), ("windows", "amd64"))
        return _manifest(("linux", "amd64"), ("linux", "arm64"))

    monkeypatch.setattr(verify_release_tags, "_inspect", fake)
    with pytest.raises(TagError, match="missing platforms: linux/arm64"):
        check_remote("ghcr.io/owner/assetboard", "v1.0.0")


def test_remote_check_refuses_a_malformed_tag_before_inspecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(reference: str) -> dict[str, object]:
        raise AssertionError(f"must not inspect {reference}")

    monkeypatch.setattr(verify_release_tags, "_inspect", fake)
    with pytest.raises(TagError):
        check_remote("ghcr.io/owner/assetboard", "not-a-tag")
