"""
LocaleResolver — resolves a contact's (locale, timezone) for content i18n.

Locale is a content-variant axis (see docs/versya-i18n-multimarket-PLAN.md), NOT a
Base matrix dimension. Resolution order (highest first):
    explicit trait → sticky existing value → pixel hint (navigator.language)
    → phone/number country → channel default → project default.

Gated by USE_LOCALE_RESOLUTION. When off, everyone resolves to the project default
(so behaviour is identical to before the i18n work).
"""
import logging

logger = logging.getLogger(__name__)


def _flag_on(db=None, project_id: int | None = None) -> bool:
    from app.services.engine_rollout_service import effective_mode
    return effective_mode(db, project_id, "locale_resolution") == "enforce"


# Dialing code (E.164 prefix) → (locale, IANA timezone). Longest prefix wins.
# Representative tz only; per-contact tz hint (browser) overrides when present.
_DIAL: dict[str, tuple[str, str]] = {
    "55": ("pt-BR", "America/Sao_Paulo"),
    "351": ("pt-PT", "Europe/Lisbon"),
    "34": ("es-ES", "Europe/Madrid"),
    "52": ("es-MX", "America/Mexico_City"),
    "54": ("es-AR", "America/Argentina/Buenos_Aires"),
    "1": ("en-US", "America/New_York"),
    "44": ("en-GB", "Europe/London"),
    "49": ("de-DE", "Europe/Berlin"),
    "33": ("fr-FR", "Europe/Paris"),
    "39": ("it-IT", "Europe/Rome"),
}

# Language-only fallback (e.g. navigator.language == "es") → a canonical region.
_LANG_DEFAULT = {
    "pt": "pt-BR",
    "es": "es-ES",
    "en": "en-US",
    "de": "de-DE",
    "fr": "fr-FR",
    "it": "it-IT",
}


_COUNTRY_DEFAULT = {
    "BR": "pt-BR",
    "US": "en-US",
}

def normalize_locale(raw) -> str | None:
    """Canonicalise a raw tag to BCP-47 'xx-YY'. Returns None if unrecognisable."""
    if not raw or not isinstance(raw, str):
        return None
    tag = raw.strip().replace("_", "-")
    if not tag:
        return None
    parts = tag.split("-")
    lang = parts[0].lower()
    if len(parts) >= 2 and parts[1]:
        return f"{lang}-{parts[1].upper()}"
    return _LANG_DEFAULT.get(lang)


def to_meta_language(locale: str) -> str:
    """BCP-47 'pt-BR' → Meta WhatsApp language code 'pt_BR'."""
    return (locale or "").replace("-", "_")


def _locale_from_phone(phone_e164) -> str | None:
    if not phone_e164:
        return None
    digits = "".join(ch for ch in str(phone_e164) if ch.isdigit())
    for length in (3, 2, 1):  # longest dial prefix wins
        if _DIAL.get(digits[:length]):
            return _DIAL[digits[:length]][0]
    return None


def _tz_from_phone(phone_e164) -> str | None:
    if not phone_e164:
        return None
    digits = "".join(ch for ch in str(phone_e164) if ch.isdigit())
    for length in (3, 2, 1):
        if _DIAL.get(digits[:length]):
            return _DIAL[digits[:length]][1]
    return None


class LocaleResolver:
    def __init__(self, db, project):
        self.db = db
        self.project = project

    def supported(self) -> list[str]:
        sl = getattr(self.project, "supported_locales", None)
        return sl if sl else [self.project.default_locale]

    def _coerce_supported(self, locale: str | None) -> str | None:
        if not locale:
            return None
        sup = self.supported()
        if locale in sup:
            return locale
        # try same-language fallback within supported set (es-MX → es-ES)
        lang = locale.split("-")[0]
        for s in sup:
            if s.split("-")[0] == lang:
                return s
        return None

    def resolve(
        self,
        contact=None,
        *,
        hint_locale=None,
        hint_timezone=None,
        country_code=None,
        phone_e164=None,
    ) -> tuple[str, str]:
        """Return (locale, timezone). Off-flag ⇒ project defaults."""
        if not _flag_on(self.db, self.project.id):
            return self.project.default_locale, self.project.default_timezone

        phone = phone_e164 or (getattr(contact, "phone_e164", None) if contact else None)
        locale = self._resolve_locale(contact, hint_locale, country_code, phone)
        tz = self._resolve_timezone(contact, hint_timezone, phone)
        return locale, tz

    def _resolve_locale(self, contact, hint_locale, country_code, phone) -> str:
        props = (getattr(contact, "properties", None) or {}) if contact else {}
        country_locale = _COUNTRY_DEFAULT.get(str(country_code or "").upper())
        candidates = [
            normalize_locale(props.get("communication_locale")),
            normalize_locale(props.get("locale") or props.get("lang")),
            getattr(contact, "locale", None) if contact else None,
            normalize_locale(hint_locale),
            country_locale,
            _locale_from_phone(phone),
        ]
        for cand in candidates:
            coerced = self._coerce_supported(cand)
            if coerced:
                return coerced
        return self.project.default_locale

    def _resolve_timezone(self, contact, hint_timezone, phone) -> str:
        props = (getattr(contact, "properties", None) or {}) if contact else {}
        for cand in (
            props.get("timezone") or props.get("tz"),   # explicit trait
            getattr(contact, "timezone", None) if contact else None,  # sticky existing
            hint_timezone,                              # pixel hint (Intl tz)
            _tz_from_phone(phone),                      # number country
        ):
            if cand and isinstance(cand, str) and cand.strip():
                return cand.strip()
        return self.project.default_timezone


def contact_locale_tz(project, *, contact=None, user_data=None) -> tuple[str, str]:
    """Resolve send locale/timezone from explicit communication traits and market."""
    props = {}
    if contact is not None:
        props.update(getattr(contact, "properties", None) or {})
    if user_data:
        props.update(user_data.get("properties") or {})
        props.update({key: value for key, value in user_data.items() if key != "properties"})

    country = str(props.get("country") or props.get("market_country") or "").upper()
    requested = normalize_locale(
        props.get("communication_locale")
        or props.get("locale")
        or props.get("ui_language")
    )
    locale = requested or (getattr(contact, "locale", None) if contact is not None else None)
    timezone = getattr(contact, "timezone", None) if contact is not None else None
    timezone = timezone or props.get("timezone")

    supported = getattr(project, "supported_locales", None) or [project.default_locale]
    if locale not in supported:
        language = (locale or "").split("-", 1)[0]
        locale = next(
            (item for item in supported if item.split("-", 1)[0] == language),
            None,
        )
    if country == "US" and locale not in {"en-US", "es-US"}:
        locale = "en-US"
    elif country == "BR":
        locale = "pt-BR"
    return locale or project.default_locale, timezone or project.default_timezone


def assign_locale(
    db,
    user,
    *,
    hint_locale=None,
    hint_timezone=None,
    country_code=None,
    phone_e164=None,
) -> None:
    """Resolve and persist user.locale/timezone at ingestion. No-op when the flag is
    off. Sticky: an existing value is preserved unless an explicit trait overrides it
    (handled inside LocaleResolver.resolve). Commits only when something changed."""
    if user is None or not _flag_on(db, user.project_id):
        return
    from app.models import Project
    project = db.query(Project).filter(Project.id == user.project_id).first()
    if not project:
        return
    locale, tz = LocaleResolver(db, project).resolve(
        user,
        hint_locale=hint_locale,
        hint_timezone=hint_timezone,
        country_code=country_code,
        phone_e164=phone_e164 or getattr(user, "phone_e164", None),
    )
    changed = False
    if locale and user.locale != locale:
        user.locale = locale
        changed = True
    if tz and user.timezone != tz:
        user.timezone = tz
        changed = True
    if changed:
        db.commit()
