from __future__ import annotations

import re
from pathlib import Path

from assetboard.branding import ENV_PREFIX
from assetboard.config import Settings

DEPLOYMENT = Path(__file__).resolve().parents[2] / "docs" / "deployment.md"
TABLE_VARIABLE = re.compile(r"^\| `(?P<name>ASSETBOARD_[A-Z_]+)` \|", re.MULTILINE)
COMPOSE_ONLY = {
    "ASSETBOARD_BIND_ADDRESS",
    "ASSETBOARD_HOST_PORT",
    "ASSETBOARD_IMAGE",
}


def deployment_table_variables() -> set[str]:
    document = DEPLOYMENT.read_text(encoding="utf-8")
    section = document.split("## Environment variables\n", maxsplit=1)[1]
    section = section.split("\n## ", maxsplit=1)[0]
    return set(TABLE_VARIABLE.findall(section))


def test_every_settings_field_is_documented() -> None:
    documented = deployment_table_variables()
    settings_names = {f"{ENV_PREFIX}_{field.upper()}" for field in Settings.model_fields}

    assert settings_names <= documented


def test_deployment_table_has_no_unknown_environment_variables() -> None:
    settings_names = {f"{ENV_PREFIX}_{field.upper()}" for field in Settings.model_fields}

    assert deployment_table_variables() <= settings_names | COMPOSE_ONLY
