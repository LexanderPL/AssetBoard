"""Hatchling build hook: compile the gettext catalogs into the wheel.

Compiled ``.mo`` catalogs are build output and are not committed (they are gitignored); the
``.po`` sources are. This hook runs ``pybabel compile`` before a wheel is assembled so that a
wheel built from a git checkout or from the sdist always ships the ``.mo`` files. The wheel
target lists them as ``artifacts`` in ``pyproject.toml`` so hatchling includes them even though
they are gitignored.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

LOCALES = Path("src") / "assetboard" / "locales"


class CompileCatalogsHook(BuildHookInterface):  # type: ignore[type-arg]
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if version == "editable":
            # Editable installs serve src/ directly; `make setup|dev|test|check` compile there.
            return
        locales = Path(self.root) / LOCALES
        sources = sorted(locales.glob("*/LC_MESSAGES/messages.po"))
        if not sources:
            raise RuntimeError(f"No gettext catalogs (*.po) found under {locales}")
        # Same command as `make i18n-compile`; Babel is a build requirement.
        subprocess.run(
            [sys.executable, "-m", "babel.messages.frontend", "compile", "-d", str(locales)],
            check=True,
        )
        missing = [str(po) for po in sources if not po.with_suffix(".mo").is_file()]
        if missing:
            raise RuntimeError(f"Catalog compilation produced no .mo for: {', '.join(missing)}")
