"""Catalog files: loading, locale normalization and placeholder checks.

A catalog is a flat JSON object of dotted keys to message templates using
`{name}` placeholders. Adding a language is adding `<locale>.json` to the
catalog directory; nothing in code changes (CLAUDE.md rule 13).
"""

import json
import string
from pathlib import Path
from typing import Final

DEFAULT_LOCALE: Final = "en"
PSEUDO_LOCALE: Final = "qps"
CATALOG_DIR: Final = Path(__file__).parent / "catalogs"

Catalog = dict[str, str]


class CatalogError(ValueError):
    """A catalog file is malformed or disagrees with English."""


def normalize_locale(locale: str | None) -> str:
    """Primary language subtag, lower case: `hi-IN` and `hi_IN` become `hi`.

    Empty or missing input gives the default locale. The pseudo-locale `qps`
    (and `qps-ploc`) normalizes to `qps`.
    """
    if not locale or not locale.strip():
        return DEFAULT_LOCALE
    return locale.strip().replace("_", "-").split("-", 1)[0].lower()


def placeholders(template: str) -> frozenset[str]:
    """Names of the `{placeholders}` in `template`.

    Raises CatalogError for a malformed template (unbalanced braces).
    """
    try:
        return frozenset(
            name for _, name, _, _ in string.Formatter().parse(template) if name is not None
        )
    except ValueError as exc:
        raise CatalogError(f"malformed template {template!r}: {exc}") from exc


def load_catalog(path: Path) -> Catalog:
    """Read one catalog file; every key and value must be a string."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read catalog {path.name}: {exc}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise CatalogError(f"catalog {path.name} must be a flat object of strings")
    return dict(raw)


def available_locales(catalog_dir: Path = CATALOG_DIR) -> list[str]:
    """Locales that have a catalog file in `catalog_dir`."""
    return sorted(p.stem for p in catalog_dir.glob("*.json"))


def check_against_english(catalog: Catalog, english: Catalog) -> list[str]:
    """Problems in `catalog` relative to English (empty list means fine).

    Reports keys English does not have and keys whose placeholders differ.
    Missing keys are not a problem: they fall back to English.
    """
    problems: list[str] = []
    for key, template in sorted(catalog.items()):
        if key not in english:
            problems.append(f"{key}: not in the English catalog")
            continue
        try:
            got = placeholders(template)
        except CatalogError as exc:
            problems.append(f"{key}: {exc}")
            continue
        want = placeholders(english[key])
        if got != want:
            problems.append(f"{key}: placeholders {sorted(got)} differ from English {sorted(want)}")
    return problems
