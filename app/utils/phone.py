"""
Phone number normalization for WhatsApp (and other channel) addressing.

Goal: produce ONE canonical key (E.164 digits, no leading "+") that is used
identically by inbound window storage, the 24-h window lookup, and outbound
delivery. If these three disagree, replies to a contact silently fail with a
false "window closed".

Country-code rule
-----------------
``default_country_code`` (e.g. "55") is only prepended to numbers given in
LOCAL / NATIONAL format — i.e. numbers that carry NO international prefix.
A number that already carries its own country code (signalled by a leading
"+" or an international "00" dialing prefix) is NEVER re-prefixed, otherwise a
Malaysian ``+60 19 9961-8128`` becomes ``55 60199618128`` and is both looked
up under the wrong window key and delivered to the wrong number.

Note: a bare number with no "+"/"00" and no matching country code is, by
definition, ambiguous (a local 11-digit number is indistinguishable from a
foreign 11-digit one). For those we trust ``default_country_code`` and prepend,
which is the correct behaviour for the common single-country case. Whenever the
caller has the number in international form, it must keep the "+" so this
function can tell the difference.
"""

from typing import Optional


def normalize_phone(raw: str, default_country_code: Optional[str] = None) -> str:
    """Return canonical E.164 digits (no leading "+") for *raw*.

    - Leading "+"        → already international, never prepend a country code.
    - Leading "00"       → international dialing prefix, strip it, never prepend.
    - Otherwise          → local/national; prepend ``default_country_code`` when
                           the number does not already start with it.
    """
    if not raw:
        return ""

    s = str(raw).strip()
    is_international = s.startswith("+")

    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return ""

    # International dialing prefix (e.g. "0060...") — strip and treat as international.
    if digits.startswith("00"):
        digits = digits[2:]
        is_international = True

    if is_international:
        return digits

    cc = "".join(c for c in str(default_country_code or "") if c.isdigit())
    if not cc:
        return digits
    if digits.startswith(cc):
        return digits
    return cc + digits
