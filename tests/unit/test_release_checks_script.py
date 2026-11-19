"""The shell release checks must not report success when they verified nothing (F9.8)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_checks.sh"

MANIFEST_LINUX = (
    '{"manifests":[{"platform":{"os":"linux","architecture":"amd64"}},'
    '{"platform":{"os":"linux","architecture":"arm64"}}]}'
)
MANIFEST_WINDOWS = (
    '{"manifests":[{"platform":{"os":"linux","architecture":"amd64"}},'
    '{"platform":{"os":"windows","architecture":"amd64"}}]}'
)


def _stub_docker(tmp_path: Path, *, windows_latest: bool = False) -> Path:
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ $1 == manifest && $2 == inspect ]]; then\n"
        '  if [[ "${3##*:}" == latest && "${STUB_WINDOWS:-0}" == 1 ]]; then\n'
        f"    printf '%s\\n' '{MANIFEST_WINDOWS}'\n"
        "  else\n"
        f"    printf '%s\\n' '{MANIFEST_LINUX}'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run(
    tmp_path: Path, *arguments: str, windows_latest: bool = False
) -> subprocess.CompletedProcess[str]:
    path = _stub_docker(tmp_path, windows_latest=windows_latest)
    env = {
        **os.environ,
        "PATH": f"{path}{os.pathsep}{os.environ['PATH']}",
        "TMPDIR": str(tmp_path),
        "STUB_WINDOWS": "1" if windows_latest else "0",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "verify-tags", "ghcr.io/owner/assetboard", *arguments],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def test_a_malformed_tag_fails_instead_of_checking_nothing(tmp_path: Path) -> None:
    completed = _run(tmp_path, "not-a-tag")
    assert completed.returncode != 0
    assert "All expected tags" not in completed.stderr


def test_a_windows_manifest_fails(tmp_path: Path) -> None:
    completed = _run(tmp_path, "v1.0.0", windows_latest=True)
    assert completed.returncode != 0
    assert "missing platforms: linux/arm64" in completed.stderr


def test_two_linux_platforms_pass(tmp_path: Path) -> None:
    completed = _run(tmp_path, "v1.0.0")
    assert completed.returncode == 0, completed.stderr
    assert "linux/amd64 and linux/arm64" in completed.stdout + completed.stderr
