"""Translator: `t(key, **params)` with English fallback.

Strict mode (tests, CI) raises on a key missing from English or on bad
parameters. Production mode (default) never raises: it returns a visible
marker `??key??`, so a missing string cannot break a screen or the scan path.
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from ag_shared.i18n.catalog import (
    CATALOG_DIR,
    DEFAULT_LOCALE,
    PSEUDO_LOCALE,
    Catalog,
    CatalogError,
    check_against_english,
    load_catalog,
    normalize_locale,
)
from ag_shared.i18n.pseudo import pseudo_localize

_log = logging.getLogger(__name__)

#: Signature presenters accept, so they depend on a function, not on this class.
TranslateFn = Callable[..., str]


class MissingKeyError(KeyError):
    """A key is not in the English catalog (strict mode only)."""


class Translator:
    """Looks up messages for one locale, falling back to English."""

    def __init__(
        self,
        locale: str | None = DEFAULT_LOCALE,
        *,
        catalog_dir: Path = CATALOG_DIR,
        strict: bool = False,
    ) -> None:
        self.strict = strict
        self.locale = normalize_locale(locale)
        self._english = load_catalog(catalog_dir / f"{DEFAULT_LOCALE}.json")
        self._catalog = self._load_locale(catalog_dir)

    def _load_locale(self, catalog_dir: Path) -> Catalog:
        if self.locale == DEFAULT_LOCALE:
            return self._english
        if self.locale == PSEUDO_LOCALE:
            return {k: pseudo_localize(v) for k, v in self._english.items()}
        path = catalog_dir / f"{self.locale}.json"
        if not path.exists():
            _log.warning("no catalog for locale %r; using English", self.locale)
            return self._english
        catalog = load_catalog(path)
        problems = check_against_english(catalog, self._english)
        if problems and self.strict:
            raise CatalogError(f"catalog {self.locale}: " + "; ".join(problems))
        # Keep going with English for the bad entries rather than fail a screen.
        bad = {p.split(":", 1)[0] for p in problems}
        for key in bad:
            _log.warning("catalog %s: dropping %r (see check_against_english)", self.locale, key)
        return {k: v for k, v in catalog.items() if k not in bad}

    def has(self, key: str) -> bool:
        """True when English has `key`."""
        return key in self._english

    def t(self, key: str, **params: object) -> str:
        """The message for `key` with `params` filled in."""
        template = self._catalog.get(key)
        if template is None:
            template = self._english.get(key)
        if template is None:
            if self.strict:
                raise MissingKeyError(key)
            _log.error("missing message key %r", key)
            return f"??{key}??"
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError) as exc:
            if self.strict:
                raise
            _log.error("cannot format message %r: %s", key, type(exc).__name__)
            return f"??{key}??"


_STRICT_ENV: Final = "AG_I18N_STRICT"
_default: Translator | None = None


def default_translator() -> Translator:
    """Process-wide translator (English until `set_locale`); strict if AG_I18N_STRICT=1."""
    global _default
    if _default is None:
        _default = Translator(strict=os.environ.get(_STRICT_ENV) == "1")
    return _default


def set_locale(locale: str | None, *, catalog_dir: Path = CATALOG_DIR) -> Translator:
    """Switch the process-wide translator to `locale`."""
    global _default
    _default = Translator(locale, catalog_dir=catalog_dir, strict=default_translator().strict)
    return _default


def t(key: str, **params: object) -> str:
    """Look `key` up with the process-wide translator."""
    return default_translator().t(key, **params)
