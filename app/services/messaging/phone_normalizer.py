"""
Centralized phone number normalization.

Normalizes raw phone strings to E.164-like digit-only format
(e.g. "5511999999999") and reports a status indicating confidence.
"""

import re
from typing import Optional, Tuple

import phonenumbers

# ISO 3166-1 alpha-2 → dial prefix (covers ~50 common regions)
REGION_TO_DIAL_CODE: dict[str, str] = {
    "US": "1", "CA": "1", "GB": "44", "IE": "353",
    "BR": "55", "PT": "351", "AR": "54", "CL": "56",
    "CO": "57", "MX": "52", "PE": "51", "UY": "598",
    "VE": "58", "EC": "593", "BO": "591", "PY": "595",
    "DE": "49", "FR": "33", "ES": "34", "IT": "39",
    "NL": "31", "BE": "32", "AT": "43", "CH": "41",
    "SE": "46", "NO": "47", "DK": "45", "FI": "358",
    "PL": "48", "CZ": "420", "RO": "40", "HU": "36",
    "GR": "30", "TR": "90", "RU": "7", "UA": "380",
    "IN": "91", "PK": "92", "BD": "880",
    "CN": "86", "JP": "81", "KR": "82", "TW": "886",
    "TH": "66", "VN": "84", "ID": "62", "PH": "63",
    "MY": "60", "SG": "65",
    "AU": "61", "NZ": "64",
    "ZA": "27", "NG": "234", "KE": "254", "EG": "20",
    "MA": "212", "GH": "233",
    "AE": "971", "SA": "966", "IL": "972",
}


class PhoneNormalizer:
    """
    Normalizes a raw phone string into digits-only E.164 format.

    Priority chain (highest trust first):
      1. Starts with '+' → strip to digits → valid_e164
      2. Locale region subtag (e.g. pt-BR → BR → 55) → prepend if needed → inferred
      3. fallback_country_code (from WhatsApp instance) → prepend if needed → fallback
      4. Digits-only ≥ 11, no CC info → best guess → assumed_e164
      5. None of the above → return cleaned digits → missing

    Invalid (< 7 digits or empty) → (None, "invalid")
    """

    @staticmethod
    def normalize(
        raw_phone: str,
        locale: Optional[str] = None,
        fallback_country_code: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Returns (normalized_digits, status).

        status is one of: valid_e164, inferred, fallback, assumed_e164, missing, invalid
        """
        if not raw_phone:
            return (None, "invalid")

        had_plus = raw_phone.strip().startswith("+")

        # Strip everything except digits
        digits = re.sub(r"[^\d]", "", raw_phone)

        # E.164 permits at most 15 digits. Refuse oversized identifiers instead
        # of storing values that no provider can route reliably.
        if len(digits) < 7 or len(digits) > 15:
            return (None, "invalid")

        # 1. Had explicit '+' prefix → trust the caller
        if had_plus:
            normalized = _parse_possible(raw_phone, None)
            return (normalized, "valid_e164") if normalized else (None, "invalid")

        # 2. Try locale region subtag (SDK sends browser locale e.g. pt-BR)
        region = _extract_region(locale)
        if region:
            dial_code = REGION_TO_DIAL_CODE.get(region)
            if dial_code:
                normalized = _parse_possible(raw_phone, region)
                if normalized:
                    return (normalized, "inferred")
                if _already_has_cc(digits, dial_code):
                    normalized = _parse_possible(f"+{digits}", None)
                    if normalized:
                        return (normalized, "inferred")
                return (None, "invalid")

        # 3. Fallback from WhatsApp instance default_country_code
        if fallback_country_code:
            cc = re.sub(r"[^\d]", "", fallback_country_code)
            if cc:
                try:
                    fallback_region = phonenumbers.region_code_for_country_code(int(cc))
                except (TypeError, ValueError):
                    fallback_region = None
                normalized = _parse_possible(raw_phone, fallback_region)
                if not normalized and _already_has_cc(digits, cc):
                    normalized = _parse_possible(f"+{digits}", None)
                return (normalized, "fallback") if normalized else (None, "invalid")

        # 4. ≥ 11 digits with no CC info → best guess, likely has country code
        if len(digits) >= 11:
            normalized = _parse_possible(f"+{digits}", None)
            return (normalized, "assumed_e164") if normalized else (None, "invalid")

        # 5. Can't determine country code
        return (digits, "missing")


def _already_has_cc(digits: str, cc: str) -> bool:
    """
    Check if digits already start with the country code AND are long enough
    to be a full E.164 number (>= 11 digits).  This prevents false positives
    for short country codes (e.g. US "1") where a national number might
    coincidentally start with the same digit.
    """
    return digits.startswith(cc) and len(digits) >= 11


def _parse_possible(raw_phone: str, region: Optional[str]) -> Optional[str]:
    try:
        parsed = phonenumbers.parse(str(raw_phone), region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    value = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    digits = re.sub(r"[^\d]", "", value)
    return digits if 7 <= len(digits) <= 15 else None


def country_for_e164(value: Optional[str]) -> Optional[str]:
    """Return the ISO country inferred from an already-normalized E.164 value."""
    if not value:
        return None
    try:
        digits = re.sub(r"[^\d]", "", str(value))
        parsed = phonenumbers.parse(f"+{digits}", None)
    except phonenumbers.NumberParseException:
        return None
    region = phonenumbers.region_code_for_number(parsed)
    return region or None


def _extract_region(locale: Optional[str]) -> Optional[str]:
    """
    Extract ISO country code from a locale string.
    Handles: 'pt-BR', 'pt_BR', 'en-US', 'en_US', 'pt', etc.
    """
    if not locale:
        return None
    # Try subtag after '-' or '_'
    parts = re.split(r"[-_]", locale)
    if len(parts) >= 2:
        region = parts[1].upper()
        if len(region) == 2 and region.isalpha():
            return region
    return None
