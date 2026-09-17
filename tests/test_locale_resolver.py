"""Unit tests for LocaleResolver (Phase 0, content i18n). Pure logic — no DB."""
import os
from types import SimpleNamespace

import pytest

from app.services.messaging import locale_resolver as lr
from app.services.messaging.locale_resolver import (
    LocaleResolver,
    normalize_locale,
    to_meta_language,
)


def _project(default_locale="pt-BR", supported=None, tz="America/Sao_Paulo"):
    return SimpleNamespace(
        id=1, default_locale=default_locale, supported_locales=supported, default_timezone=tz
    )


def _contact(**kw):
    base = dict(properties=None, locale=None, timezone=None, phone_e164=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("USE_LOCALE_RESOLUTION", "true")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setenv("USE_LOCALE_RESOLUTION", "false")


def test_normalize_locale():
    assert normalize_locale("pt_BR") == "pt-BR"
    assert normalize_locale("es-es") == "es-ES"
    assert normalize_locale("es") == "es-ES"      # language-only → canonical
    assert normalize_locale("xx") is None
    assert normalize_locale("") is None
    assert normalize_locale(None) is None


def test_to_meta_language():
    assert to_meta_language("pt-BR") == "pt_BR"
    assert to_meta_language("en-US") == "en_US"


def test_flag_off_returns_project_defaults(flag_off):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    loc, tz = r.resolve(_contact(phone_e164="34911111111"))  # would be es-ES if on
    assert (loc, tz) == ("pt-BR", "America/Sao_Paulo")


def test_phone_country_resolves_locale_and_tz(flag_on):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    loc, tz = r.resolve(_contact(phone_e164="34911222333"))
    assert loc == "es-ES"
    assert tz == "Europe/Madrid"


def test_brazil_number(flag_on):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    loc, tz = r.resolve(_contact(phone_e164="5511999998888"))
    assert loc == "pt-BR"
    assert tz == "America/Sao_Paulo"


def test_explicit_trait_wins_over_phone(flag_on):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    c = _contact(phone_e164="5511999998888", properties={"locale": "es-ES"})
    loc, _ = r.resolve(c)
    assert loc == "es-ES"


def test_sticky_existing_wins_over_phone(flag_on):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    c = _contact(phone_e164="34911222333", locale="pt-BR")  # already pt-BR
    loc, _ = r.resolve(c)
    assert loc == "pt-BR"


def test_unsupported_locale_falls_back_same_language(flag_on):
    # es-MX phone, but only es-ES supported → coerce to es-ES
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES"]))
    loc, _ = r.resolve(_contact(phone_e164="525511112222"))
    assert loc == "es-ES"


def test_unsupported_no_language_match_falls_back_to_default(flag_on):
    r = LocaleResolver(None, _project(default_locale="pt-BR", supported=["pt-BR"]))
    loc, _ = r.resolve(_contact(phone_e164="491511112222"))  # de-DE not supported
    assert loc == "pt-BR"


def test_pixel_hint(flag_on):
    r = LocaleResolver(None, _project(supported=["pt-BR", "es-ES", "en-US"]))
    loc, tz = r.resolve(_contact(), hint_locale="en-US", hint_timezone="America/New_York")
    assert loc == "en-US"
    assert tz == "America/New_York"
