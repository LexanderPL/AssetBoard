import subprocess
import tempfile
from importlib import import_module
from pathlib import Path


def test_assetboard_branding_exports_required_names() -> None:
    branding = import_module("AssetBoard.branding")

    assert branding.APP_NAME == "AssetBoard"
    assert branding.ENV_PREFIX == "ASSETBOARD"
    assert branding.CLI_NAME == "assetboard"


def test_required_repository_layout_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        root / "src" / "AssetBoard" / "domain",
        root / "src" / "AssetBoard" / "services",
        root / "src" / "AssetBoard" / "db",
        root / "src" / "AssetBoard" / "web",
        root / "src" / "AssetBoard" / "api",
        root / "src" / "AssetBoard" / "jobs",
        root / "src" / "AssetBoard" / "i18n",
        root / "src" / "AssetBoard" / "branding.py",
        root / "tests" / "unit",
        root / "tests" / "integration",
        root / "docs" / "adr",
        root / "docker",
        root / "scripts",
    ]

    missing = [path for path in required if not path.exists()]
    assert not missing, f"Missing required paths: {missing}"


def test_project_tooling_configuration_files_exist() -> None:
    root = Path(__file__).resolve().parents[1]
    required = [
        root / "pyproject.toml",
        root / ".python-version",
        root / "Makefile",
        root / ".pre-commit-config.yaml",
        root / ".github" / "workflows" / "ci.yml",
        root / ".gitignore",
    ]

    missing = [path for path in required if not path.exists()]
    assert not missing, f"Missing toolchain files: {missing}"


def test_pre_commit_blocks_database_and_spreadsheet_files() -> None:
    root = Path(__file__).resolve().parents[1]

    with tempfile.TemporaryDirectory() as temp_dir:
        repo = Path(temp_dir)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=repo, check=True)
        config_path = root / ".pre-commit-config.yaml"
        subprocess.run(
            ["cp", "-R", str(config_path), str(repo / ".pre-commit-config.yaml")],
            check=True,
        )

        for extension in ("db", "xlsx"):
            forbidden = repo / f"data.test.{extension}"
            forbidden.write_text("test", encoding="utf-8")
            subprocess.run(["git", "add", str(forbidden)], cwd=repo, check=True)
            result = subprocess.run(
                ["pre-commit", "run", "--all-files"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
            assert result.returncode != 0
            assert "Database or spreadsheet files are not allowed." in result.stdout
            subprocess.run(
                ["git", "rm", "--cached", "--force", str(forbidden)],
                cwd=repo,
                check=True,
            )
            forbidden.unlink()
