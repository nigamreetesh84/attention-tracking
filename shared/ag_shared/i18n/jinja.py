"""Jinja2 hook: registers `t` as a global and a filter.

Duck-typed on purpose: jinja2 is only in the server extra, so this module
never imports it and the workstation does not depend on it.

    {{ t("login.locked", minutes=5) }}   or   {{ "login.failed"|t }}
"""

from typing import Protocol

from ag_shared.i18n.translator import Translator


class _JinjaEnvironment(Protocol):
    globals: dict[str, object]
    filters: dict[str, object]


def install_jinja(env: _JinjaEnvironment, translator: Translator) -> None:
    """Make `t(key, **params)` and `key|t(**params)` available in templates."""
    env.globals["t"] = translator.t
    env.filters["t"] = translator.t
