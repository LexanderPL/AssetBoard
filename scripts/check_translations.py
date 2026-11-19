"""Report and gate translation completeness for a gettext catalog.

The v1.0 gate requires Polish user-facing strings to be at least 95% translated, and the project
rules forbid shipping missing or fuzzy entries in a supported catalog. The check counts every
current (non-obsolete) message with a non-empty id. Plural messages count once and must have all
of their plural forms translated. The source catalog is not checked: its msgstr fields are empty
by design.

Usage:
    python scripts/check_translations.py [--locale pl] [--threshold 95]

Exit status is 1 when the catalog is below the threshold or contains missing/fuzzy entries.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from babel.messages.catalog import Message
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parents[1]
LOCALES = ROOT / "src" / "assetboard" / "locales"


def translated(message: Message) -> bool:
    value = message.string
    if isinstance(value, tuple):
        return bool(value) and all(part and part.strip() for part in value)
    return bool(value and value.strip())


def scan(locale: str) -> tuple[int, int, int, int]:
    path = LOCALES / locale / "LC_MESSAGES" / "messages.po"
    if not path.is_file():
        raise SystemExit(f"Missing catalog: {path}")
    with path.open("rb") as handle:
        catalog = read_po(handle, locale=locale)

    total = translated_count = missing = fuzzy = 0
    for message in catalog:
        if not message.id:
            continue
        total += 1
        if "fuzzy" in message.flags:
            fuzzy += 1
        if translated(message):
            translated_count += 1
        else:
            missing += 1
    return total, translated_count, missing, fuzzy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locale", default="pl", help="catalog locale to check (default: pl)")
    parser.add_argument(
        "--threshold",
        type=float,
        default=95.0,
        help="minimum translated percentage for v1.0 (default: 95)",
    )
    args = parser.parse_args(argv)

    total, translated_count, missing, fuzzy = scan(args.locale)
    percentage = 100.0 if total == 0 else translated_count * 100.0 / total
    print(
        f"{args.locale}: {translated_count}/{total} translated "
        f"({percentage:.1f}%), {missing} missing, {fuzzy} fuzzy"
    )

    failed = False
    if percentage < args.threshold:
        print(
            f"Translation completeness {percentage:.1f}% is below the "
            f"{args.threshold:.1f}% threshold.",
            file=sys.stderr,
        )
        failed = True
    if missing:
        print(f"{missing} user-facing string(s) are missing a translation.", file=sys.stderr)
        failed = True
    if fuzzy:
        print(f"{fuzzy} user-facing string(s) are marked fuzzy.", file=sys.stderr)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
