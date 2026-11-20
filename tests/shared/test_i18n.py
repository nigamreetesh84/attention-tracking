"""String catalog: lookup, fallback, strict mode, pseudo-locale, placeholders."""

import json
from pathlib import Path

import pytest
from ag_shared.i18n import (
    CATALOG_DIR,
    CatalogError,
    MissingKeyError,
    Translator,
    available_locales,
    check_against_english,
    install_jinja,
    load_catalog,
    normalize_locale,
    placeholders,
    pseudo_localize,
    set_locale,
    t,
)


def _write(dir_: Path, locale: str, data: dict[str, str]) -> None:
    (dir_ / f"{locale}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def cat_dir(tmp_path: Path) -> Path:
    _write(tmp_path, "en", {"a.hello": "Hello, {name}.", "a.bye": "Bye.", "a.n": "{n} left"})
    return tmp_path


@pytest.mark.parametrize(
    ("raw", "want"),
    [("hi", "hi"), ("hi-IN", "hi"), ("hi_IN", "hi"), ("TE", "te"), ("", "en"), (None, "en"),
     ("qps-ploc", "qps")],
)  # fmt: skip
def test_normalize_locale(raw: str | None, want: str) -> None:
    assert normalize_locale(raw) == want


def test_lookup_and_params(cat_dir: Path) -> None:
    tr = Translator("en", catalog_dir=cat_dir, strict=True)
    assert tr.t("a.hello", name="Asha") == "Hello, Asha."
    assert tr.t("a.bye") == "Bye."
    assert tr.has("a.bye")
    assert not tr.has("zzz")


def test_adding_a_language_is_adding_a_file(cat_dir: Path) -> None:
    assert Translator("hi", catalog_dir=cat_dir).t("a.bye") == "Bye."  # no file yet
    _write(cat_dir, "hi", {"a.hello": "नमस्ते, {name}।"})
    assert "hi" in available_locales(cat_dir)
    tr = Translator("hi-IN", catalog_dir=cat_dir, strict=True)
    assert tr.t("a.hello", name="Asha") == "नमस्ते, Asha।"
    assert tr.t("a.bye") == "Bye."  # missing key falls back to English


def test_missing_key_strict_raises_and_production_marks(cat_dir: Path) -> None:
    with pytest.raises(MissingKeyError):
        Translator("en", catalog_dir=cat_dir, strict=True).t("nope.key")
    assert Translator("en", catalog_dir=cat_dir).t("nope.key") == "??nope.key??"


def test_missing_param(cat_dir: Path) -> None:
    with pytest.raises(KeyError):
        Translator("en", catalog_dir=cat_dir, strict=True).t("a.hello")
    assert Translator("en", catalog_dir=cat_dir).t("a.hello") == "??a.hello??"


def test_placeholder_mismatch(cat_dir: Path) -> None:
    _write(cat_dir, "te", {"a.hello": "హలో, {nome}", "a.bye": "వీడ్కోలు", "x.extra": "x"})
    with pytest.raises(CatalogError):
        Translator("te", catalog_dir=cat_dir, strict=True)
    tr = Translator("te", catalog_dir=cat_dir)  # production: bad entries fall back
    assert tr.t("a.hello", name="A") == "Hello, A."
    assert tr.t("a.bye") == "వీడ్కోలు"
    assert tr.t("x.extra") == "??x.extra??"


def test_check_against_english_and_placeholders() -> None:
    assert placeholders("{a} and {b:>3} {{literal}}") == {"a", "b"}
    with pytest.raises(CatalogError):
        placeholders("broken {")
    en = {"k": "Hi {name}"}
    assert check_against_english({"k": "Namaste {name}"}, en) == []
    assert len(check_against_english({"k": "Namaste"}, en)) == 1
    assert len(check_against_english({"other": "x"}, en)) == 1
    assert len(check_against_english({"k": "{"}, en)) == 1


def test_pseudo_locale_preserves_placeholders() -> None:
    out = pseudo_localize("Try again in {minutes} min. {{x}}")
    assert out.startswith("[") and out.endswith("]")
    assert "{minutes}" in out
    assert out != "Try again in {minutes} min."
    assert out.format(minutes=5).count("5") == 1
    assert "{x}" in out.format(minutes=5)


def test_pseudo_translator_covers_every_english_key() -> None:
    en = Translator("en", strict=True)
    ps = Translator("qps", strict=True)
    for key, template in load_catalog(CATALOG_DIR / "en.json").items():
        params = dict.fromkeys(placeholders(template), "7")
        assert ps.t(key, **params) != en.t(key, **params)
        assert ps.t(key, **params).startswith("[")


def test_bad_catalog_files(tmp_path: Path) -> None:
    (tmp_path / "en.json").write_text("[1]", encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "en.json")
    (tmp_path / "en.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "en.json")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path / "absent.json")


def test_default_translator_and_set_locale() -> None:
    try:
        assert t("scan.ok") == "Scan recorded."
        set_locale("qps")
        assert t("scan.ok").startswith("[")
    finally:
        set_locale("en")
    assert t("scan.ok") == "Scan recorded."


def test_jinja_hook_without_importing_jinja() -> None:
    class Env:
        globals: dict[str, object] = {}  # noqa: RUF012
        filters: dict[str, object] = {}  # noqa: RUF012

    env = Env()
    install_jinja(env, Translator("en", strict=True))
    assert callable(env.globals["t"])
    assert env.filters["t"]("login.locked", minutes=3) == "Too many attempts. Try again in 3 min."  # type: ignore[operator]


def test_jinja_hook_real_environment() -> None:
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(autoescape=True)
    install_jinja(env, Translator("en", strict=True))
    assert env.from_string('{{ t("login.ok", name="A") }}').render() == "Welcome, A."
    assert env.from_string('{{ "scan.ok"|t }}').render() == "Scan recorded."
