"""Regression tests for WhatsApp phone normalization (E.164 window-key bug).

Prod incident 2026-06-29: outbound replies to non-Brazil contacts on the Meta
Cloud API instance (default_country_code="55") prepended "55" to a Malaysian
+60 number, producing 5560199618128 — which missed the inbound window key
(60199618128) and would deliver to the wrong number. See app.utils.phone.
"""
from types import SimpleNamespace

from app.utils.phone import normalize_phone
from app.services.messaging.phone_normalizer import PhoneNormalizer, country_for_e164
from app.services.whatsapp_sender import WhatsAppSender


def _instance(cc="55"):
    return SimpleNamespace(default_country_code=cc)


# ── local/national numbers get the default country code ──────────────────────

def test_local_number_gets_default_cc():
    assert normalize_phone("11987654321", "55") == "5511987654321"


def test_local_number_already_has_cc_unchanged():
    assert normalize_phone("5511987654321", "55") == "5511987654321"


# ── foreign numbers keep their own country code (the bug) ────────────────────

def test_foreign_number_with_plus_not_reprefixed():
    # Malaysia +60 — must NOT become 5560199618128
    assert normalize_phone("+60199618128", "55") == "60199618128"


def test_foreign_number_formatted_with_plus():
    assert normalize_phone("+60 19-9618-128", "55") == "60199618128"


def test_foreign_number_double_zero_prefix():
    assert normalize_phone("0060199618128", "55") == "60199618128"


def test_inbound_and_outbound_agree_on_window_key():
    """The Meta inbound wa_id is stored RAW as the window key (it is already
    canonical E.164 without '+'). Outbound, given the contact's phone in '+'
    form, must normalize to that same key so the window lookup hits."""
    inbound_wa_id = "60199618128"                        # stored raw by webhook
    outbound = normalize_phone("+60199618128", "55")     # contact phone_e164 form
    assert inbound_wa_id == outbound == "60199618128"


def test_brazil_inbound_and_outbound_agree_on_window_key():
    """Brazil wa_id carries the 55 prefix; an outbound local number must too."""
    inbound_wa_id = "5511987654321"                      # Meta wa_id for a BR contact
    outbound = normalize_phone("11987654321", "55")      # user typed local form
    assert inbound_wa_id == outbound == "5511987654321"


# ── edge cases ───────────────────────────────────────────────────────────────

def test_no_default_cc_returns_digits():
    assert normalize_phone("60199618128", None) == "60199618128"


def test_empty_input():
    assert normalize_phone("", "55") == ""
    assert normalize_phone(None, "55") == ""


# ── sender delegates to the shared util ──────────────────────────────────────

def test_sender_normalize_foreign_number():
    assert WhatsAppSender._normalize_phone("+60199618128", _instance("55")) == "60199618128"


def test_sender_normalize_local_number():
    assert WhatsAppSender._normalize_phone("11987654321", _instance("55")) == "5511987654321"


# ── contact data-quality normalization ─────────────────────────────────────

def test_contact_normalizer_validates_explicit_e164_and_country():
    value, status = PhoneNormalizer.normalize("+55 11 98765-4321")
    assert (value, status) == ("5511987654321", "valid_e164")
    assert country_for_e164(value) == "BR"


def test_contact_normalizer_infers_brazil_only_from_explicit_locale():
    value, status = PhoneNormalizer.normalize("11 98765-4321", locale="pt-BR")
    assert (value, status) == ("5511987654321", "inferred")


def test_contact_normalizer_rejects_oversized_identifier():
    assert PhoneNormalizer.normalize("+551198765432112345") == (None, "invalid")
