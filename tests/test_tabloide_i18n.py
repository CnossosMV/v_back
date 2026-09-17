"""Tabloide market/locale delivery regression tests."""
import asyncio
from types import SimpleNamespace

from app.services.event_actions.executor import ActionContext, ActionExecutor
from app.services.messaging.locale_resolver import LocaleResolver, contact_locale_tz
from app.services.messaging import template_selector
from app.services.messaging.text_quality import find_mojibake, has_mojibake


class _RowsQuery:
    def __init__(self, rows):
        self.rows = rows

    def order_by(self, *args):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows
def _project():
    return SimpleNamespace(
        id=1,
        default_locale="pt-BR",
        supported_locales=["pt-BR", "en-US", "es-US"],
        default_timezone="America/Sao_Paulo",
    )


def _contact(**kwargs):
    values = {
        "properties": {},
        "locale": None,
        "timezone": None,
        "phone_e164": None,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def test_us_country_defaults_to_english(monkeypatch):
    monkeypatch.setenv("USE_LOCALE_RESOLUTION", "true")
    locale, _ = LocaleResolver(None, _project()).resolve(
        _contact(), country_code="US"
    )
    assert locale == "en-US"


def test_us_explicit_spanish_wins_and_contact_helper_agrees(monkeypatch):
    monkeypatch.setenv("USE_LOCALE_RESOLUTION", "true")
    contact = _contact(
        properties={"country": "US", "communication_locale": "es-US"}
    )
    locale, _ = LocaleResolver(None, _project()).resolve(contact, country_code="US")
    helper_locale, _ = contact_locale_tz(_project(), contact=contact)
    assert locale == "es-US"
    assert helper_locale == "es-US"



def test_event_locale_overrides_stale_contact_locale():
    context = ActionContext(
        db=None,
        project_id=1,
        user_id=1,
        event_id=1,
        event_data={
            "properties": {
                "country": "US",
                "market_country": "US",
                "communication_locale": "es-US",
            }
        },
        user_data={
            "properties": {
                "country": "US",
                "communication_locale": "en-US",
            }
        },
        variables={},
    )

    routing_data = ActionExecutor._locale_routing_user_data(context)
    locale, _ = contact_locale_tz(_project(), user_data=routing_data)

    assert locale == "es-US"

def _variant(locale, template_id):
    return SimpleNamespace(locale=locale, id=template_id, is_active=True)


def test_template_selector_uses_exact_us_spanish(monkeypatch):
    monkeypatch.setattr(template_selector, "_flag_on", lambda db, project_id: True)
    rows = [_variant("pt-BR", 1), _variant("en-US", 2), _variant("es-US", 3)]
    monkeypatch.setattr(
        template_selector, "_base_query", lambda db, project_id, slug: _RowsQuery(rows)
    )

    selected = template_selector.resolve_template(object(), 1, "welcome-email", "es-US")

    assert selected.id == 3


def test_template_selector_never_falls_back_to_portuguese_for_us(monkeypatch):
    monkeypatch.setattr(template_selector, "_flag_on", lambda db, project_id: True)
    rows = [_variant("pt-BR", 1), _variant("en-US", 2)]
    monkeypatch.setattr(
        template_selector, "_base_query", lambda db, project_id, slug: _RowsQuery(rows)
    )

    assert template_selector.resolve_template(object(), 1, "welcome-email", "fr-US").id == 2

    only_portuguese = [_variant("pt-BR", 1)]
    monkeypatch.setattr(
        template_selector,
        "_base_query",
        lambda db, project_id, slug: _RowsQuery(only_portuguese),
    )
    assert template_selector.resolve_template(object(), 1, "welcome-email", "fr-US") is None


def test_mojibake_validator_allows_accents_and_blocks_corruption():
    assert not has_mojibake("Voce pode usar acentos: voce nao.")
    assert not has_mojibake("Voc\u00ea pode usar acentos: condi\u00e7\u00e3o.")
    assert find_mojibake(("Condi\u00c3\u00a7\u00c3\u00a3o",)) == "\u00c3"


def test_whatsapp_is_skipped_for_us_before_provider_lookup():
    context = ActionContext(
        db=None,
        project_id=1,
        user_id=1,
        event_id=1,
        event_data={"properties": {"country": "US"}},
        user_data={"country": "US", "phone": "+15551234567"},
        variables={},
    )

    result = asyncio.run(ActionExecutor()._send_whatsapp_message({}, context))

    assert result.success is True
    assert result.data == {
        "skipped": True,
        "reason": "channel_not_enabled_for_market",
    }
