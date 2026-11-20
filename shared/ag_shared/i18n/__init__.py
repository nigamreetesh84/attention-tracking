"""String catalog and translation (PLAN 2.7).

Shared by the PySide6 workstation (through presenters) and the Jinja2 console.
"""

from ag_shared.i18n.catalog import (
    CATALOG_DIR,
    DEFAULT_LOCALE,
    PSEUDO_LOCALE,
    CatalogError,
    available_locales,
    check_against_english,
    load_catalog,
    normalize_locale,
    placeholders,
)
from ag_shared.i18n.jinja import install_jinja
from ag_shared.i18n.pseudo import pseudo_localize
from ag_shared.i18n.translator import (
    MissingKeyError,
    TranslateFn,
    Translator,
    default_translator,
    set_locale,
    t,
)

__all__ = [
    "CATALOG_DIR",
    "DEFAULT_LOCALE",
    "PSEUDO_LOCALE",
    "CatalogError",
    "MissingKeyError",
    "TranslateFn",
    "Translator",
    "available_locales",
    "check_against_english",
    "default_translator",
    "install_jinja",
    "load_catalog",
    "normalize_locale",
    "placeholders",
    "pseudo_localize",
    "set_locale",
    "t",
]
