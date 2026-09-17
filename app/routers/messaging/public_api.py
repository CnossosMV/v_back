"""
Messaging Public API Router
Public endpoints for SDK (identify, track) and backend API (send, events, users)

Security layers implemented:
- Frontend SDK (write_key): Origin/Referer validation, rate limiting per domain
- Backend API (secret_key): IP allowlist, HMAC signature, nonce deduplication, rate limiting

Backend API endpoints require HMAC signature verification for security:
- X-API-Key: The secret key (sk_live_xxx)
- X-Timestamp: Unix timestamp (seconds)
- X-Signature: HMAC-SHA256 signature of "timestamp.body"
- X-Nonce: (optional but recommended) UUID to prevent replay attacks
"""
from fastapi import APIRouter, Depends, HTTPException, Header, Request, Query
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
import ipaddress
import json
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy import text as sa_text, update as sa_update

from app.database import get_db
from app.models.messaging import (
    ContactIdentity, MessagingDomain, MessagingApiKey, MessagingUser, MessagingEvent, MessagingAnonymousProfile
)
from app.schemas.messaging import (
    IdentifyRequest, TrackRequest, PageRequest, SendRequest, SendResponse,
    VerifyInstallRequest, VerifyInstallResponse,
    BackendEventRequest, BackendUserRequest,
    MessagingUserResponse,
    SetConsentRequest, SetConsentResponse, ConsentResponse,
    AliasRequest, AliasResponse
)
from app.services.messaging import key_generator, event_processor
from app.services.messaging.schema_discovery import schema_discovery
from app.services.messaging.rate_limiter import rate_limiter
from app.services.messaging.nonce_cache import nonce_cache
from app.services.messaging.consent_manager import consent_manager
from app.services.messaging.identity_resolver import identity_resolver
from app.services.messaging.timezone_context import (
    apply_timezone_to_properties,
    apply_timezone_to_user,
    enrich_properties_with_timezone,
)
from app.services.messaging.pii_hasher import pii_hasher
from app.services.messaging.sdk_config_service import sdk_config_service
from app.services.messaging.phone_normalizer import PhoneNormalizer
from app.services.messaging.event_sources import (
    BACKEND as EVENT_SOURCE_BACKEND,
    origin_matches_domain,
    source_for_public_request,
)
from app.services.whatsapp_validation_service import WhatsAppValidationService
from app.services.messaging.destination_dispatcher import destination_dispatcher
from app.services.messaging.event_attribution import (
    merge_profile_attribution,
    normalize_attribution,
)
from app.services.messaging.attribution_resolver import record_attribution_touches
from app.services.messaging.permission_evidence_service import PermissionEvidenceService
import asyncio

router = APIRouter(prefix="/api/v1", tags=["messaging-public"])


def _record_email_permission_claim(
    db: Session,
    *,
    project_id: int,
    user: MessagingUser,
    claim,
    api_key_id: int,
    ingress: str,
) -> None:
    if claim is None:
        return
    try:
        PermissionEvidenceService(db).record_email_claim(
            project_id=project_id,
            user=user,
            claim=claim,
            api_key_id=api_key_id,
            ingress=ingress,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _ads_consent_from_payload(data, properties=None, context=None):
    if getattr(data, "ads_consent", None):
        return data.ads_consent
    properties = properties if isinstance(properties, dict) else {}
    context = context if isinstance(context, dict) else {}
    if isinstance(properties.get("ads_consent"), dict):
        return properties["ads_consent"]
    if isinstance(context.get("ads_consent"), dict):
        return context["ads_consent"]
    return None


def _record_ads_consent_from_payload(db: Session, user: Optional[MessagingUser], data, request: Request, properties=None) -> None:
    if not user:
        return
    ads_consent = _ads_consent_from_payload(data, properties=properties, context=getattr(data, "context", None))
    if not ads_consent:
        return
    ads_meta = ads_consent if isinstance(ads_consent, dict) else {}
    consent_manager.update_ads_consent(
        db=db,
        user=user,
        ads_consent=ads_consent,
        source=getattr(data, "source", None) or ads_meta.get("source"),
        policy_version=getattr(data, "policy_version", None) or ads_meta.get("policy_version"),
        evidence_id=getattr(data, "evidence_id", None) or ads_meta.get("evidence_id"),
        page_url=getattr(data, "page_url", None) or ads_meta.get("page_url"),
        ip=_request_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:500],
        metadata=getattr(data, "metadata", None) or ads_meta.get("metadata"),
        commit=False,
    )


def _record_contact_identity_if_missing(
    db: Session,
    project_id: int,
    user_id: int,
    identity_type: str,
    identity_value: Optional[str],
    source: str = "identify",
) -> None:
    if not identity_value:
        return

    value = str(identity_value)
    existing = db.query(ContactIdentity).filter(
        ContactIdentity.project_id == project_id,
        ContactIdentity.identity_type == identity_type,
        ContactIdentity.identity_value == value,
    ).first()
    if existing:
        if existing.user_id != user_id:
            logging.getLogger(__name__).warning(
                "Identity already belongs to another user: project=%s type=%s value=%s existing_user=%s new_user=%s",
                project_id,
                identity_type,
                value,
                existing.user_id,
                user_id,
            )
        return

    db.add(ContactIdentity(
        project_id=project_id,
        user_id=user_id,
        identity_type=identity_type,
        identity_value=value,
        verified=True,
        source=source,
    ))


def _request_ip(request: Request) -> Optional[str]:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    original_forwarded_for = request.headers.get("X-Original-Forwarded-For")
    if original_forwarded_for:
        return original_forwarded_for.split(",")[0].strip()
    cf_connecting_ip = request.headers.get("CF-Connecting-IP")
    if cf_connecting_ip:
        return cf_connecting_ip.strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


def _proxy_processing_notes(request: Request) -> Optional[dict]:
    tracking_host = request.headers.get("X-Versya-Tracking-Host")
    if not tracking_host:
        return None
    return {
        "tracking_proxy": {
            "hostname": tracking_host,
            "proxy_version": request.headers.get("X-Versya-Proxy-Version"),
            "original_host": request.headers.get("X-Original-Host"),
        }
    }


def _apply_attribution(
    db: Session,
    *,
    event: MessagingEvent,
    user: Optional[MessagingUser],
    anonymous_id: Optional[str],
    properties: Optional[dict],
    context: Optional[dict],
    request: Request,
) -> None:
    result = normalize_attribution(
        properties=properties,
        context=context,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:500],
        session_id=event.session_id,
    )

    attribution = result.attribution
    campaign_origin = result.campaign_origin

    # Hard-link the event to the originating send when the touch carries a
    # Versya tracked-link token (utm_source=versya&utm_content=<tracking_token>)
    if attribution:
        from app.services.messaging.event_attribution import resolve_internal_send
        send_log_id = resolve_internal_send(
            db, attribution=attribution, project_id=event.project_id,
        )
        if send_log_id:
            attribution = dict(attribution)
            attribution["send_log_id"] = send_log_id

    event.external_event_id = result.external_event_id
    event.campaign_origin = campaign_origin
    event.attribution = attribution
    merge_profile_attribution(db, event=event, user=user, anonymous_id=anonymous_id)
    record_attribution_touches(
        db, event=event, user=user, anonymous_id=anonymous_id,
    )


def _queue_destinations_safely(db: Session, event: MessagingEvent) -> None:
    try:
        destination_dispatcher.queue_for_event(db, event)
    except Exception as exc:
        db.rollback()
        logger = logging.getLogger(__name__)
        logger.error("Failed to queue destination deliveries for event %s: %s", event.id, exc)


# ==========================================
# Auth Dependencies
# ==========================================

def get_domain_from_write_key(
    request: Request,
    x_write_key: Optional[str] = Header(None, alias="X-Write-Key"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> MessagingDomain:
    """
    Authenticate using write key (pk_live_xxx) from header.
    Used by frontend SDK.

    Security checks:
    1. Validates write key format and existence
    2. Validates Origin/Referer header against allowed domains
    3. Enforces rate limits per domain
    """
    write_key = x_write_key

    # Also check Authorization header (Bearer pk_live_xxx)
    if not write_key and authorization:
        if authorization.startswith("Bearer "):
            write_key = authorization[7:]

    if not write_key:
        raise HTTPException(
            status_code=401,
            detail="Missing write key. Provide X-Write-Key header."
        )

    if not key_generator.is_write_key(write_key):
        raise HTTPException(status_code=401, detail="Invalid write key format")

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.write_key == write_key,
        MessagingDomain.is_active == True
    ).first()

    if not domain:
        raise HTTPException(status_code=401, detail="Invalid write key")

    # ==========================================
    # Origin/Referer Validation
    # ==========================================
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin and not origin_matches_domain(origin, domain):
        raise HTTPException(
            status_code=403,
            detail="Origin not allowed for this write key",
        )

    # ==========================================
    # Rate Limiting
    # ==========================================
    rate_key = f"domain:{domain.id}"
    limit_per_minute = domain.rate_limit_per_minute or 100
    limit_per_day = domain.rate_limit_per_day or 10000

    allowed_rate, error_msg = rate_limiter.check_rate_limit(
        rate_key,
        limit_per_minute,
        limit_per_day
    )
    if not allowed_rate:
        raise HTTPException(status_code=429, detail=error_msg)

    return domain


async def get_project_from_secret_key_with_signature(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_timestamp: Optional[str] = Header(None, alias="X-Timestamp"),
    x_signature: Optional[str] = Header(None, alias="X-Signature"),
    x_nonce: Optional[str] = Header(None, alias="X-Nonce"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> tuple:
    """
    Authenticate using secret key (sk_live_xxx) with HMAC signature verification.
    Used by backend API endpoints for secure server-to-server communication.

    Required headers:
    - X-API-Key: The secret key (sk_live_xxx)
    - X-Timestamp: Unix timestamp in seconds
    - X-Signature: HMAC-SHA256 signature of "timestamp.body"
    - X-Nonce: (optional but recommended) UUID to prevent replay attacks

    Security checks:
    1. Validates API key format and HMAC signature
    2. Validates nonce hasn't been used before (if provided)
    3. Validates client IP against allowlist (if configured)
    4. Enforces rate limits per API key

    The signature is computed as: HMAC-SHA256(secret_key, timestamp + "." + request_body)

    Returns (project_id, api_key, secret_key) tuple.
    """
    secret_key = x_api_key

    # Also check Authorization header (Bearer sk_live_xxx)
    if not secret_key and authorization:
        if authorization.startswith("Bearer "):
            secret_key = authorization[7:]

    if not secret_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide X-API-Key header."
        )

    if not key_generator.is_secret_key(secret_key):
        raise HTTPException(status_code=401, detail="Invalid API key format")

    # Validate signature headers
    if not x_timestamp:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Timestamp header. Include Unix timestamp in seconds."
        )

    if not x_signature:
        raise HTTPException(
            status_code=401,
            detail="Missing X-Signature header. Include HMAC-SHA256 signature."
        )

    # Read the raw request body for signature verification
    body = await request.body()

    # Verify the HMAC signature
    is_valid, error_message = key_generator.verify_hmac_signature(
        secret_key=secret_key,
        payload=body,
        timestamp=x_timestamp,
        signature=x_signature
    )

    if not is_valid:
        raise HTTPException(
            status_code=401,
            detail=f"Signature verification failed: {error_message}"
        )

    # Find API key by verifying hash
    api_keys = db.query(MessagingApiKey).filter(
        MessagingApiKey.is_active == True
    ).all()

    found_api_key = None
    for api_key in api_keys:
        if key_generator.verify_secret_key(secret_key, api_key.secret_key_hash):
            found_api_key = api_key
            break

    if not found_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    api_key = found_api_key

    # ==========================================
    # Nonce Deduplication (Replay Attack Prevention)
    # ==========================================
    if x_nonce:
        nonce_key = f"{api_key.id}:{x_nonce}"
        if nonce_cache.check_and_mark(nonce_key, ttl_seconds=300):
            raise HTTPException(
                status_code=401,
                detail="Nonce has already been used (possible replay attack)"
            )

    # ==========================================
    # IP Allowlist Validation
    # ==========================================
    if api_key.allowed_ips:
        client_ip = request.client.host if request.client else None
        if client_ip:
            try:
                client_addr = ipaddress.ip_address(client_ip)
                is_ip_allowed = False

                for ip_entry in api_key.allowed_ips:
                    try:
                        if '/' in ip_entry:
                            # CIDR notation
                            if client_addr in ipaddress.ip_network(ip_entry, strict=False):
                                is_ip_allowed = True
                                break
                        else:
                            # Single IP
                            if client_addr == ipaddress.ip_address(ip_entry):
                                is_ip_allowed = True
                                break
                    except ValueError:
                        # Invalid IP in allowlist, skip it
                        continue

                if not is_ip_allowed:
                    raise HTTPException(
                        status_code=403,
                        detail=f"IP address {client_ip} not in allowlist"
                    )
            except ValueError:
                # Invalid client IP format, skip check
                pass

    # ==========================================
    # Rate Limiting
    # ==========================================
    rate_key = f"api_key:{api_key.id}"
    allowed_rate, error_msg = rate_limiter.check_rate_limit(
        rate_key,
        api_key.rate_limit_per_minute,
        api_key.rate_limit_per_day
    )
    if not allowed_rate:
        raise HTTPException(status_code=429, detail=error_msg)

    # Update last used timestamp
    api_key.last_used_at = datetime.utcnow()
    db.commit()

    return (api_key.project_id, api_key, secret_key)


def get_project_from_secret_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db)
) -> tuple:
    """
    DEPRECATED: Use get_project_from_secret_key_with_signature instead.
    This remains for backwards compatibility but will be removed in future versions.

    Authenticate using secret key (sk_live_xxx) from header without signature verification.
    Returns (project_id, api_key) tuple.
    """
    secret_key = x_api_key

    # Also check Authorization header (Bearer sk_live_xxx)
    if not secret_key and authorization:
        if authorization.startswith("Bearer "):
            secret_key = authorization[7:]

    if not secret_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Provide X-API-Key header."
        )

    if not key_generator.is_secret_key(secret_key):
        raise HTTPException(status_code=401, detail="Invalid API key format")

    # Find API key by verifying hash
    api_keys = db.query(MessagingApiKey).filter(
        MessagingApiKey.is_active == True
    ).all()

    for api_key in api_keys:
        if key_generator.verify_secret_key(secret_key, api_key.secret_key_hash):
            # Update last used timestamp
            api_key.last_used_at = datetime.utcnow()
            db.commit()
            return (api_key.project_id, api_key)

    raise HTTPException(status_code=401, detail="Invalid API key")


def _get_project_fallback_cc(db: Session, project_id: int) -> Optional[str]:
    """Get default_country_code from the project's first active WhatsApp instance."""
    try:
        from app.models import WhatsAppInstance, Project
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return None
        instance = db.query(WhatsAppInstance).filter(
            WhatsAppInstance.project_id == project_id,
            WhatsAppInstance.is_active == True,
            WhatsAppInstance.default_country_code != None,
        ).first()
        return instance.default_country_code if instance else None
    except Exception:
        return None


def _apply_event_phone_to_user(
    db: Session,
    project_id: int,
    user: MessagingUser,
    properties: Optional[dict],
) -> bool:
    """Fill a missing contact phone from event properties."""
    raw_phone = (properties or {}).get("phone")
    if raw_phone is None:
        return False

    phone = str(raw_phone).strip()
    if not phone:
        return False

    # Events are not full identity updates, so avoid replacing an existing
    # contact phone with arbitrary event metadata.
    if user.phone and user.phone != phone:
        return False

    user.phone = phone
    fallback_cc = _get_project_fallback_cc(db, project_id)
    normalized, status = PhoneNormalizer.normalize(phone, None, fallback_cc)
    user.phone_e164 = normalized
    user.phone_norm_status = status
    if normalized:
        country = str((properties or {}).get("country") or (properties or {}).get("market_country") or "").upper()
        user.whatsapp_status = "unchecked" if country == "US" else "checking"
        return country != "US"
    return False


_TIMEZONE_KEYS = {
    "timezone",
    "timezone_source",
    "timezone_observed_at",
    "utc_offset_minutes",
}


def _timezone_snapshot(properties: Optional[dict]) -> dict:
    incoming = properties if isinstance(properties, dict) else {}
    if not incoming.get("timezone"):
        return {}
    return {key: incoming[key] for key in _TIMEZONE_KEYS if key in incoming}


def _apply_timezone_to_anonymous_profile(
    profile: MessagingAnonymousProfile,
    properties: Optional[dict],
) -> None:
    snapshot = _timezone_snapshot(properties)
    if snapshot:
        profile.properties = apply_timezone_to_properties(profile.properties, snapshot)


def _properties_with_timezone(data, *, trusted: bool = False) -> dict:
    enriched = enrich_properties_with_timezone(
        getattr(data, "properties", None) or getattr(data, "traits", None),
        getattr(data, "context", None),
        trusted_properties=trusted,
    )
    context = getattr(data, "context", None) or {}
    if context.get("locale") and not any(
        enriched.get(key) for key in ("communication_locale", "ui_language", "locale")
    ):
        enriched["locale"] = context["locale"]
        enriched["locale_source"] = "browser_context"
    return enriched


def _deep_merge_properties(current: Optional[dict], incoming: Optional[dict]) -> dict:
    """Merge profile facts recursively while preserving unrelated namespaces.

    Explicit ``False`` and ``None`` values are retained so a source can clear
    stale truth without owning the complete contact properties object.
    """
    current_map = dict(current or {})
    incoming_map = dict(incoming or {})

    def observed_at(value: object) -> Optional[datetime]:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    current_observed = observed_at(current_map.get("_observed_at"))
    incoming_observed = observed_at(incoming_map.get("_observed_at"))
    if current_observed and incoming_observed and incoming_observed < current_observed:
        return current_map

    merged = current_map
    for key, value in incoming_map.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_properties(existing, value)
        else:
            merged[key] = value
    return merged


def _backend_external_event_id(data: BackendEventRequest) -> Optional[str]:
    value = data.event_id or (data.properties or {}).get("event_id")
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _existing_backend_event(
    db: Session,
    project_id: int,
    data: BackendEventRequest,
    user: Optional[MessagingUser],
) -> Optional[MessagingEvent]:
    """Return an exact live backend event retry or reject ID reuse.

    The same stable source event ID may be retried, but it may not be reused
    for another event name or resolved contact in the same project.
    """
    external_event_id = _backend_external_event_id(data)
    if not external_event_id:
        return None
    # Serialize equal live backend IDs inside PostgreSQL so two simultaneous
    # source retries cannot both pass the lookup before either transaction
    # commits. Other test/development dialects keep the sequential behavior.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            sa_text(
                "SELECT pg_advisory_xact_lock(:project_id, hashtext(:event_id))"
            ),
            {"project_id": project_id, "event_id": external_event_id},
        )
    existing = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id,
        MessagingEvent.source == EVENT_SOURCE_BACKEND,
        MessagingEvent.external_event_id == external_event_id,
        MessagingEvent.processing_mode == "live",
    ).order_by(MessagingEvent.id).first()
    if not existing:
        return None
    expected_user_id = user.id if user else None
    if existing.event_name != data.event or existing.user_id != expected_user_id:
        raise HTTPException(
            status_code=409,
            detail="event_id is already owned by another event name or contact",
        )
    return existing


def _idempotent_event_response(db: Session, event: MessagingEvent) -> dict:
    from app.models.messaging import MessagingLog

    messages_triggered = db.query(MessagingLog.id).filter(
        MessagingLog.event_id == event.id,
    ).count()
    db.commit()
    return {
        "success": True,
        "event_id": event.id,
        "messages_triggered": messages_triggered,
        "idempotent_replay": True,
    }

def _apply_event_identity_to_user(
    db: Session,
    project_id: int,
    user: MessagingUser,
    properties: Optional[dict],
) -> None:
    """Persist backend-authoritative identity and communication routing traits."""
    incoming = properties if isinstance(properties, dict) else {}
    if incoming.get("email"):
        email = str(incoming["email"]).strip()
        user.email = email
        email_hash, _ = pii_hasher.hash_user_pii(db, project_id, email, None)
        user.email_hash = email_hash
        _record_contact_identity_if_missing(
            db, project_id, user.id, "email", email,
        )
    if incoming.get("name"):
        user.name = str(incoming["name"]).strip()

    allowed = {
        "country",
        "market_country",
        "ui_language",
        "communication_locale",
        "content_locale",
        "content_language",
        "locale",
        "currency",
    }
    merged = dict(user.properties or {})
    for key in allowed:
        if incoming.get(key) is not None:
            merged[key] = incoming[key]
    country = str(merged.get("country") or merged.get("market_country") or "").upper()
    if country:
        merged["country"] = country
        merged["market_country"] = country
    user.properties = merged
    apply_timezone_to_user(user, _timezone_snapshot(incoming))
    merged = dict(user.properties or {})

    from app.models import Project
    from app.services.messaging.locale_resolver import assign_locale, normalize_locale

    # An explicit SDK/browser locale is identity data, not heuristic country
    # inference. Persist it even while the broader locale-resolution feature
    # flag is off; the flag continues to control phone/country fallbacks.
    requested_locale = normalize_locale(
        merged.get("communication_locale")
        or merged.get("ui_language")
        or merged.get("locale")
    )
    if requested_locale:
        project = db.query(Project).filter(Project.id == project_id).first()
        supported = list(project.supported_locales or [project.default_locale]) if project else []
        selected_locale = requested_locale if requested_locale in supported else None
        if selected_locale is None:
            same_language = [
                locale for locale in supported
                if locale.split("-", 1)[0] == requested_locale.split("-", 1)[0]
            ]
            if len(same_language) == 1:
                selected_locale = same_language[0]
        if selected_locale:
            user.locale = selected_locale
            merged["locale_source"] = (
                "browser_context"
                if incoming.get("locale_source") == "browser_context"
                else "explicit_trait"
            )
            user.properties = merged

    assign_locale(
        db,
        user,
        hint_locale=(
            merged.get("communication_locale")
            or merged.get("ui_language")
            or merged.get("locale")
        ),
        hint_timezone=merged.get("timezone"),
        country_code=country or None,
        phone_e164=user.phone_e164,
    )
    from app.services.messaging.contact_profile_service import ContactProfileService
    ContactProfileService(db).sync_user(
        user,
        source=getattr(user, "created_via", None) or "api",
        context={"locale_source": merged.get("locale_source")},
    )


# ==========================================
# Frontend SDK Endpoints (Write Key Auth)
# ==========================================

@router.post("/identify")
async def identify_user(
    data: IdentifyRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Identify a user - used by frontend SDK.
    Creates or updates a messaging user with their traits.
    """
    project_id = domain.project_id
    data.traits = _properties_with_timezone(data)

    # Rate limit: 5 calls per anonymous_id per 60s
    if data.anonymous_id:
        rate_key = f"identify:{data.anonymous_id}"
        allowed, msg = rate_limiter.check_rate_limit(rate_key, 5, 1000)
        if not allowed:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for this anonymous_id")

    # Find or create user
    user = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.external_id == data.user_id
    ).first()

    if user:
        # Update existing user
        if data.traits:
            if data.traits.get('email'):
                user.email = data.traits['email']
            if data.traits.get('phone'):
                user.phone = data.traits['phone']
            if data.traits.get('name'):
                user.name = data.traits['name']

            # Merge properties
            existing_props = dict(user.properties or {})
            existing_props.update(data.traits)
            user.properties = existing_props

        user.last_seen_at = datetime.utcnow()
    else:
        # Create new user
        user = MessagingUser(
            project_id=project_id,
            external_id=data.user_id,
            email=data.traits.get('email') if data.traits else None,
            phone=data.traits.get('phone') if data.traits else None,
            name=data.traits.get('name') if data.traits else None,
            properties=data.traits
        )
        db.add(user)

    _apply_event_identity_to_user(db, project_id, user, data.traits)
    # Normalize phone at ingestion time
    raw_phone = data.traits.get('phone') if data.traits else None
    wa_check_needed = False
    if raw_phone:
        locale = (data.context or {}).get('locale')
        fallback_cc = _get_project_fallback_cc(db, project_id)
        normalized, status = PhoneNormalizer.normalize(raw_phone, locale, fallback_cc)
        user.phone_e164 = normalized
        user.phone_norm_status = status
        # Mark as checking so UI can show loading state
        if normalized:
            user.whatsapp_status = "checking"
            wa_check_needed = True

    db.commit()
    db.refresh(user)

    _record_contact_identity_if_missing(db, project_id, user.id, "contact_id", data.user_id)
    if data.traits:
        _record_contact_identity_if_missing(db, project_id, user.id, "email", data.traits.get("email"))
        _record_contact_identity_if_missing(db, project_id, user.id, "phone", data.traits.get("phone"))
    if data.anonymous_id:
        _record_contact_identity_if_missing(db, project_id, user.id, "anonymous_id", data.anonymous_id)
    db.commit()
    db.refresh(user)

    _record_ads_consent_from_payload(db, user, data, request, properties=data.traits)
    db.commit()
    db.refresh(user)

    # Fire async WhatsApp validation (non-blocking, own DB session)
    if wa_check_needed and user.phone_e164:
        try:
            asyncio.create_task(
                WhatsAppValidationService().validate_number(
                    project_id, user.id, user.phone_e164,
                )
            )
        except RuntimeError:
            pass  # No running event loop (e.g. during tests)

    # Link anonymous_id to this user if provided
    if data.anonymous_id:
        anon_profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id
        ).first()

        if anon_profile:
            _apply_timezone_to_anonymous_profile(anon_profile, data.traits)
            apply_timezone_to_user(user, _timezone_snapshot(anon_profile.properties))
            apply_timezone_to_user(user, _timezone_snapshot(data.traits))

            if not anon_profile.merged_to_user_id:
                anon_profile.merged_to_user_id = user.id
                anon_profile.merged_at = datetime.utcnow()
            anon_profile.last_seen_at = datetime.utcnow()
            if data.device_fingerprint:
                anon_profile.device_fingerprint = data.device_fingerprint
        else:
            anon_profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=data.anonymous_id,
                merged_to_user_id=user.id,
                merged_at=datetime.utcnow(),
                device_fingerprint=data.device_fingerprint,
                properties=_timezone_snapshot(data.traits),
            )
            db.add(anon_profile)
        db.commit()

        # Backfill user_id on all prior anonymous events for instant match
        db.execute(
            sa_update(MessagingEvent)
            .where(
                MessagingEvent.project_id == project_id,
                MessagingEvent.anonymous_id == data.anonymous_id,
                MessagingEvent.user_id.is_(None),
            )
            .values(user_id=user.id)
        )
        db.commit()

    # Account association
    if data.account_id and user:
        identity_resolver._associate_account(db, project_id, user.id, data.account_id)

    response = {
        "success": True,
        "user_id": user.external_id,
        "internal_id": user.id,
    }
    if user.phone_norm_status in ("missing", "invalid"):
        response["warnings"] = [{
            "code": "phone_normalization_incomplete",
            "message": "Phone could not be normalized. Provide with country code (e.g. +5511999999999).",
            "field": "phone",
        }]
    return response


@router.post("/track")
async def track_event(
    data: TrackRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Track an event - used by frontend SDK.
    Records the event and triggers any matching templates.
    """
    project_id = domain.project_id

    # Reject debug sessions from production tracking
    debug_session = request.headers.get("X-Debug-Session")
    if debug_session:
        raise HTTPException(
            status_code=400,
            detail="Debug sessions cannot use the production track endpoint"
        )

    # Find user if user_id provided
    event_properties = _properties_with_timezone(data)
    if getattr(data, "event_id", None):
        event_properties.setdefault("event_id", data.event_id)

    user = None
    if data.user_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == data.user_id
        ).first()

    # Fallback: resolve user from event properties (handles race with identify)
    user_auto_created = False
    if not user and event_properties:
        fallback_uid = event_properties.get("user_id") or event_properties.get("external_id")
        if fallback_uid:
            user = db.query(MessagingUser).filter(
                MessagingUser.project_id == project_id,
                MessagingUser.external_id == str(fallback_uid)
            ).first()
        if not user:
            fallback_email = event_properties.get("email")
            if fallback_email:
                user = db.query(MessagingUser).filter(
                    MessagingUser.project_id == project_id,
                    MessagingUser.email == fallback_email
                ).first()

    # Auto-create user if properties contain identifying data (fixes identify/track race)
    if not user and event_properties:
        auto_ext_id = (
            data.user_id
            or str(event_properties.get("user_id") or "")
            or str(event_properties.get("external_id") or "")
        ) or None
        auto_email = event_properties.get("email")

        if auto_ext_id or auto_email:
            ext_id = auto_ext_id or f"auto_email:{auto_email}"
            try:
                user = MessagingUser(
                    project_id=project_id,
                    external_id=ext_id,
                    email=auto_email,
                    name=event_properties.get("name"),
                    phone=event_properties.get("phone"),
                    properties={"_created_via": "track_auto"}
                )
                db.add(user)
                db.flush()
                user_auto_created = True
            except IntegrityError:
                db.rollback()
                user = db.query(MessagingUser).filter(
                    MessagingUser.project_id == project_id,
                    MessagingUser.external_id == ext_id
                ).first()

    # Tombstone resolution
    if user and user.status == 'merged' and user.merged_into:
        from app.services.messaging.contact_merge_service import ContactMergeService
        merge_svc = ContactMergeService(db)
        resolved_id = merge_svc.resolve_tombstone(project_id, user.id)
        if resolved_id != user.id:
            user = db.query(MessagingUser).filter(MessagingUser.id == resolved_id).first()

    # A track call can win the race against identify(). Keep the anonymous
    # profile attached to the explicit current user so later events inherit the
    # complete pre-login journey.
    if user and data.anonymous_id:
        anonymous_profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id,
        ).first()
        if not anonymous_profile or anonymous_profile.merged_to_user_id != user.id:
            identity_resolver.merge_anonymous_to_user(
                db, project_id, data.anonymous_id, user.id,
            )

    wa_check_needed = False
    if user and event_properties:
        wa_check_needed = _apply_event_phone_to_user(db, project_id, user, event_properties)
        _apply_event_identity_to_user(db, project_id, user, event_properties)

    # Get request metadata
    ip_address = _request_ip(request)
    user_agent = request.headers.get("User-Agent", "")[:500]
    _record_ads_consent_from_payload(db, user, data, request, properties=event_properties)

    # Parse client timestamp
    _client_ts = None
    if getattr(data, 'client_ts', None):
        try:
            _client_ts = datetime.fromisoformat(data.client_ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    # Create event
    event = MessagingEvent(
        project_id=project_id,
        user_id=user.id if user else None,
        anonymous_id=data.anonymous_id,
        event_name=data.event,
        properties=event_properties,
        source=source_for_public_request(request, domain),
        ip_address=ip_address,
        user_agent=user_agent,
        session_id=getattr(data, 'session_id', None),
        client_ts=_client_ts,
        processing_notes=_proxy_processing_notes(request),
    )
    db.add(event)
    db.flush()
    _apply_attribution(
        db,
        event=event,
        user=user,
        anonymous_id=data.anonymous_id,
        properties=event_properties,
        context=data.context,
        request=request,
    )
    db.commit()
    db.refresh(event)

    if wa_check_needed and user and user.phone_e164:
        try:
            asyncio.create_task(
                WhatsAppValidationService().validate_number(
                    project_id, user.id, user.phone_e164,
                )
            )
        except RuntimeError:
            pass

    # Create/update anonymous profile if anonymous_id present and no user
    if data.anonymous_id and not user:
        anon_profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id
        ).first()
        if anon_profile:
            _apply_timezone_to_anonymous_profile(anon_profile, event_properties)
            anon_profile.last_seen_at = datetime.utcnow()
            if data.device_fingerprint:
                anon_profile.device_fingerprint = data.device_fingerprint
        else:
            anon_profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=data.anonymous_id,
                device_fingerprint=data.device_fingerprint,
                properties=_timezone_snapshot(event_properties),
            )
            db.add(anon_profile)
        db.commit()
        merge_profile_attribution(db, event=event, user=None, anonymous_id=data.anonymous_id)
        db.commit()

    # Auto-discover schema from event
    schema_discovery.discover_from_event(db, project_id, data.event, event_properties)

    _queue_destinations_safely(db, event)

    # Process event asynchronously (triggers templates)
    logs = await event_processor.process_event(db, event)

    response = {
        "success": True,
        "event_id": event.id,
        "messages_triggered": len(logs)
    }
    if user_auto_created:
        response["user_auto_created"] = True
    return response


@router.post("/page")
async def track_page(
    data: PageRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Track a page view - used by frontend SDK.
    This is a special event type for page views.
    """
    project_id = domain.project_id

    # Get request metadata
    ip_address = _request_ip(request)
    user_agent = request.headers.get("User-Agent", "")[:500]

    # Build properties
    properties = _properties_with_timezone(data)
    if getattr(data, "event_id", None):
        properties.setdefault("event_id", data.event_id)
    if data.name:
        properties['page_name'] = data.name

    # Parse client timestamp
    _page_client_ts = None
    if getattr(data, 'client_ts', None):
        try:
            _page_client_ts = datetime.fromisoformat(data.client_ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    # Create page view event
    event = MessagingEvent(
        project_id=project_id,
        anonymous_id=data.anonymous_id,
        event_name="page_view",
        properties=properties,
        source=source_for_public_request(request, domain),
        ip_address=ip_address,
        user_agent=user_agent,
        session_id=getattr(data, 'session_id', None),
        client_ts=_page_client_ts,
        processing_notes=_proxy_processing_notes(request),
    )
    db.add(event)
    db.flush()
    _apply_attribution(
        db,
        event=event,
        user=None,
        anonymous_id=data.anonymous_id,
        properties=properties,
        context=data.context,
        request=request,
    )
    db.commit()

    # Create/update anonymous profile if anonymous_id present
    if data.anonymous_id:
        anon_profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id
        ).first()
        if anon_profile:
            anon_profile.last_seen_at = datetime.utcnow()
            _apply_timezone_to_anonymous_profile(anon_profile, properties)
            if data.device_fingerprint:
                anon_profile.device_fingerprint = data.device_fingerprint
            # Tombstone resolution for page events via anonymous profile
            if anon_profile.merged_to_user_id:
                from app.services.messaging.contact_merge_service import ContactMergeService
                merge_svc = ContactMergeService(db)
                resolved_id = merge_svc.resolve_tombstone(project_id, anon_profile.merged_to_user_id)
                event.user_id = resolved_id
        else:
            anon_profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=data.anonymous_id,
                device_fingerprint=data.device_fingerprint,
                properties=_timezone_snapshot(properties),
            )
            db.add(anon_profile)
        db.commit()
        merge_profile_attribution(db, event=event, user=None, anonymous_id=data.anonymous_id)
        db.commit()

    # /page bypasses EventProcessor, so invalidate MES explicitly. Direct
    # attribution covers anonymous page views whose identity is still unknown.
    try:
        from app.services.scoring.mes_engine import MESEngine
        MESEngine(db).mark_stale_for_event(event)
        db.commit()
    except Exception as exc:
        db.rollback()
        logging.getLogger(__name__).warning(
            "Could not mark MES stale for page event %s: %s", event.id, exc,
        )

    schema_discovery.discover_from_event(db, project_id, "page_view", properties)
    _queue_destinations_safely(db, event)

    return {
        "success": True,
        "event_id": event.id
    }


@router.post("/verify-install", response_model=VerifyInstallResponse)
async def verify_install(
    data: VerifyInstallRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Verify SDK installation - called by the SDK on initialization.
    Records when the snippet was first successfully installed.
    """
    # Record installation time if not already recorded
    if not domain.snippet_installed_at:
        domain.snippet_installed_at = datetime.utcnow()
        db.commit()
        message = "SDK installation verified and recorded"
    else:
        message = "SDK successfully installed and authenticated"

    return VerifyInstallResponse(
        verified=True,
        domain=domain.domain,
        project_id=domain.project_id,
        message=message
    )


# ==========================================
# Consent Endpoints (GDPR/LGPD)
# ==========================================

@router.post("/consent", response_model=SetConsentResponse)
async def set_consent(
    data: SetConsentRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Set consent preferences - used by frontend SDK.
    Creates or updates user consent state for GDPR/LGPD compliance.
    """
    project_id = domain.project_id
    ip_address = request.client.host if request.client else None

    # Find user if user_id provided
    user = None
    if data.user_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == data.user_id
        ).first()

        if not user:
            # Create minimal user for consent tracking
            user = MessagingUser(
                project_id=project_id,
                external_id=data.user_id
            )
            db.add(user)
            db.commit()
            db.refresh(user)

    if user:
        # Update consent
        user = consent_manager.update_consent(
            db=db,
            user=user,
            marketing=data.marketing,
            analytics=data.analytics,
            ip=ip_address
        )
        if data.ads_consent:
            user = consent_manager.update_ads_consent(
                db=db,
                user=user,
                ads_consent=data.ads_consent,
                source=data.source or "sdk",
                policy_version=data.policy_version,
                evidence_id=data.evidence_id,
                page_url=data.page_url,
                ip=ip_address,
                user_agent=request.headers.get("User-Agent", "")[:500],
                metadata=data.metadata,
            )

        consent_state = consent_manager.get_consent_state(user)
        return SetConsentResponse(
            success=True,
            consent=ConsentResponse(
                marketing=consent_state.marketing,
                analytics=consent_state.analytics,
                ad_user_data=consent_state.ad_user_data,
                ad_personalization=consent_state.ad_personalization,
                given_at=consent_state.given_at,
                version=consent_state.version
            )
        )

    # No user to update - return default consent (for anonymous users)
    return SetConsentResponse(
        success=True,
        consent=ConsentResponse(
            marketing=data.marketing or False,
            analytics=data.analytics or False,
            ad_user_data=bool((data.ads_consent or {}).get("ad_user_data")) if isinstance(data.ads_consent, dict) else False,
            ad_personalization=bool((data.ads_consent or {}).get("ad_personalization")) if isinstance(data.ads_consent, dict) else False,
            given_at=datetime.utcnow() if (data.marketing or data.analytics) else None,
            version=consent_manager.CURRENT_VERSION
        )
    )


@router.get("/consent", response_model=ConsentResponse)
async def get_consent(
    request: Request,
    user_id: Optional[str] = None,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Get current consent state - used by frontend SDK.
    Returns the user's current consent preferences.
    """
    project_id = domain.project_id

    if user_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == user_id
        ).first()

        if user:
            consent_state = consent_manager.get_consent_state(user)
            return ConsentResponse(
                marketing=consent_state.marketing,
                analytics=consent_state.analytics,
                ad_user_data=consent_state.ad_user_data,
                ad_personalization=consent_state.ad_personalization,
                given_at=consent_state.given_at,
                version=consent_state.version
            )

    # Default consent for unknown users
    return ConsentResponse(
        marketing=False,
        analytics=False,
        ad_user_data=False,
        ad_personalization=False,
        given_at=None,
        version=None
    )


# ==========================================
# Alias/Identity Resolution Endpoints
# ==========================================

@router.post("/alias", response_model=AliasResponse)
async def alias_user(
    data: AliasRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Merge anonymous profile to known user - used by frontend SDK.
    Call this after identify() to merge anonymous activity.
    """
    project_id = domain.project_id

    # Find the user by external_id
    user = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.external_id == data.user_id
    ).first()

    if not user:
        return AliasResponse(
            success=False,
            merged=False,
            events_reassigned=0
        )

    # Check if anonymous profile exists
    profile = db.query(MessagingAnonymousProfile).filter(
        MessagingAnonymousProfile.project_id == project_id,
        MessagingAnonymousProfile.anonymous_id == data.previous_id
    ).first()

    if not profile:
        # No anonymous profile to merge, but that's OK
        return AliasResponse(
            success=True,
            merged=False,
            events_reassigned=0
        )

    if profile.merged_to_user_id:
        # Already merged
        return AliasResponse(
            success=True,
            merged=True,
            events_reassigned=0
        )

    # Perform the merge
    identity_resolver.merge_anonymous_to_user(
        db=db,
        project_id=project_id,
        anonymous_id=data.previous_id,
        user_id=user.id
    )

    # Count reassigned events
    events_count = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id,
        MessagingEvent.user_id == user.id,
        MessagingEvent.anonymous_id == data.previous_id
    ).count()

    return AliasResponse(
        success=True,
        merged=True,
        events_reassigned=events_count
    )


# ==========================================
# Visitor Data Endpoint (Cross-Subdomain Personalization)
# ==========================================

@router.get("/visitor-data")
async def get_visitor_data(
    anonymous_id: str = Query(..., max_length=100),
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db),
):
    """
    Get personalized visitor data by anonymous_id - used by frontend SDK.
    Returns configured traits and scores for the resolved user.
    Requires personalization to be enabled for the project.
    """
    from app.services.messaging.visitor_data_service import VisitorDataService
    from fastapi.responses import JSONResponse

    service = VisitorDataService(db)
    config = service.get_config(domain.project_id)

    if not config or not config.enabled:
        raise HTTPException(status_code=404, detail="Personalization not enabled")

    result = service.resolve_visitor(domain.project_id, anonymous_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Visitor not found or consent required")

    ttl = config.cache_ttl_seconds or 300
    return JSONResponse(
        content=result,
        headers={
            "Cache-Control": f"private, max-age={ttl}",
        }
    )


# ==========================================
# Fingerprint Recovery Endpoint (Tier 5 hint)
# ==========================================

@router.get("/recover-identity")
async def recover_identity(
    device_fingerprint: str = Query(..., max_length=20),
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db),
):
    """
    Recover anonymous_id by device fingerprint - used by frontend SDK.
    Returns the most recent unmerged anonymous_id matching the fingerprint.
    Tier 5 hint only - never used for identity merge.
    """
    rate_key = f"recover_fp:{device_fingerprint}"
    allowed, msg = rate_limiter.check_rate_limit(rate_key, 3, 1000)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    anon_profile = db.query(MessagingAnonymousProfile).filter(
        MessagingAnonymousProfile.project_id == domain.project_id,
        MessagingAnonymousProfile.device_fingerprint == device_fingerprint,
        MessagingAnonymousProfile.merged_to_user_id.is_(None)
    ).order_by(MessagingAnonymousProfile.last_seen_at.desc()).first()

    return {
        "anonymous_id": anon_profile.anonymous_id if anon_profile else None
    }


# ==========================================
# SDK Runtime Config Endpoint
# ==========================================

@router.get("/config")
async def get_sdk_config(
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Get SDK runtime configuration - used by frontend SDK.
    Returns event schemas, no-code mappings, dataLayer settings, etc.

    Response is cacheable (5 min TTL, stale-while-revalidate).
    """
    config = await sdk_config_service.get_sdk_config(
        db=db,
        project_id=domain.project_id,
        domain_id=domain.id
    )

    # Add cache headers
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=config,
        headers={
            "Cache-Control": "public, max-age=60, stale-while-revalidate=300",
            "ETag": f'"{config["version"]}"'
        }
    )


@router.post("/config/reload")
async def reload_config(
    domain: MessagingDomain = Depends(get_domain_from_write_key),
):
    """Trigger SDK config reload. Stub for Phase 2 Redis pub/sub."""
    return {"status": "ok", "message": "Config reload triggered"}


# ==========================================
# Backend API Endpoints (Secret Key Auth)
# ==========================================

@router.post("/send", response_model=SendResponse)
async def send_message(
    request: Request,
    auth: tuple = Depends(get_project_from_secret_key_with_signature),
    db: Session = Depends(get_db)
):
    """
    Send a message directly - used by backend API.
    Renders template and dispatches to configured channel.

    Required headers:
    - X-API-Key: Your secret key (sk_live_xxx)
    - X-Timestamp: Unix timestamp in seconds
    - X-Signature: HMAC-SHA256(secret_key, timestamp + "." + body)
    """
    project_id, api_key, _ = auth

    # Check permissions
    if "send" not in (api_key.permissions or []):
        raise HTTPException(status_code=403, detail="API key lacks 'send' permission")

    # Parse the cached body
    try:
        body = await request.body()
        body_data = json.loads(body)
        data = SendRequest(**body_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")

    try:
        log = await event_processor.send_direct(
            db=db,
            project_id=project_id,
            template_slug=data.template_slug,
            recipient=data.recipient,
            variables=data.variables or {},
            user_id=data.user_id,
            channel_slug=data.channel_slug
        )

        return SendResponse(
            success=True,
            message_id=log.id,
            status=log.status
        )

    except ValueError as e:
        return SendResponse(
            success=False,
            status="failed",
            error=str(e)
        )
    except Exception as e:
        return SendResponse(
            success=False,
            status="failed",
            error=f"Internal error: {str(e)}"
        )


def _prepare_backend_event_identity(
    db: Session,
    project_id: int,
    data: BackendEventRequest,
    request: Request,
    api_key_id: int,
    ingress: str,
):
    data.properties = _properties_with_timezone(data, trusted=True)
    transition_user = None
    if data.user_id and data.anonymous_id:
        linked_profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id,
        ).first()
        if linked_profile and linked_profile.merged_to_user_id:
            transition_user = db.query(MessagingUser).filter(
                MessagingUser.id == linked_profile.merged_to_user_id,
                MessagingUser.status == "active",
            ).first()
    if data.user_id and transition_user is None and data.properties.get("email"):
        email = str(data.properties["email"]).strip().lower()
        email_hash, _ = pii_hasher.hash_user_pii(db, project_id, email, None)
        transition_user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
            MessagingUser.email_hash == email_hash,
        ).first()

    user = None
    if data.user_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == data.user_id,
        ).first()
        if not user and transition_user:
            user = transition_user
            previous_external_id = user.external_id
            if previous_external_id and previous_external_id != data.user_id:
                _record_contact_identity_if_missing(
                    db, project_id, user.id, "contact_id", previous_external_id,
                )
            user.external_id = data.user_id
        if not user:
            user = MessagingUser(project_id=project_id, external_id=data.user_id)
            db.add(user)
            db.flush()

        _record_contact_identity_if_missing(
            db, project_id, user.id, "contact_id", data.user_id,
        )
    if user and data.anonymous_id:
        identity_resolver.merge_anonymous_to_user(
            db, project_id, data.anonymous_id, user.id,
        )

    if data.anonymous_id:
        profile = db.query(MessagingAnonymousProfile).filter(
            MessagingAnonymousProfile.project_id == project_id,
            MessagingAnonymousProfile.anonymous_id == data.anonymous_id,
        ).first()
        if not profile:
            profile = MessagingAnonymousProfile(
                project_id=project_id,
                anonymous_id=data.anonymous_id,
                merged_to_user_id=user.id if user else None,
                merged_at=datetime.utcnow() if user else None,
            )
            db.add(profile)
        profile.last_seen_at = datetime.utcnow()
        _apply_timezone_to_anonymous_profile(profile, data.properties)

    wa_check_needed = False
    if user:
        wa_check_needed = _apply_event_phone_to_user(db, project_id, user, data.properties)
        _apply_event_identity_to_user(db, project_id, user, data.properties)
        if data.contact_properties:
            user.properties = _deep_merge_properties(
                user.properties,
                data.contact_properties,
            )
        _record_ads_consent_from_payload(
            db, user, data, request, properties=data.properties,
        )
        _record_email_permission_claim(
            db,
            project_id=project_id,
            user=user,
            claim=data.email_permission,
            api_key_id=api_key_id,
            ingress=ingress,
        )
    return user, data.properties, wa_check_needed

@router.post("/events/simple")
async def create_backend_event_simple(
    data: BackendEventRequest,
    request: Request,
    auth: tuple = Depends(get_project_from_secret_key),
    db: Session = Depends(get_db)
):
    """
    Track an event from backend - simplified version without HMAC.
    Use this for testing or internal services. For production, use /events with HMAC.

    Required headers:
    - X-API-Key: Your secret key (sk_live_xxx)
    """
    project_id, api_key = auth

    # Check permissions
    if "track" not in (api_key.permissions or []):
        raise HTTPException(status_code=403, detail="API key lacks 'track' permission")

    user, event_properties, wa_check_needed = _prepare_backend_event_identity(
        db, project_id, data, request, api_key.id, "backend_events_api_key",
    )

    existing_event = _existing_backend_event(db, project_id, data, user)
    if existing_event:
        return _idempotent_event_response(db, existing_event)

    # Parse client timestamp
    _be_client_ts = None
    if getattr(data, 'client_ts', None):
        try:
            _be_client_ts = datetime.fromisoformat(data.client_ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    # Create event
    event = MessagingEvent(
        project_id=project_id,
        user_id=user.id if user else None,
        event_name=data.event,
        anonymous_id=data.anonymous_id,
        properties=event_properties,
        source=EVENT_SOURCE_BACKEND,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:500],
        session_id=getattr(data, 'session_id', None),
        client_ts=_be_client_ts,
        external_event_id=_backend_external_event_id(data),
        occurred_at=data.timestamp or _be_client_ts or datetime.utcnow(),
    )
    db.add(event)
    db.flush()
    _apply_attribution(
        db,
        event=event,
        user=user,
        anonymous_id=data.anonymous_id,
        properties=event_properties,
        context=data.context,
        request=request,
    )
    db.commit()
    db.refresh(event)

    if wa_check_needed and user and user.phone_e164:
        try:
            asyncio.create_task(
                WhatsAppValidationService().validate_number(
                    project_id, user.id, user.phone_e164,
                )
            )
        except RuntimeError:
            pass

    # Auto-discover schema from event
    schema_discovery.discover_from_event(db, project_id, data.event, event_properties)

    _queue_destinations_safely(db, event)

    # Process event
    logs = await event_processor.process_event(db, event)

    return {
        "success": True,
        "event_id": event.id,
        "messages_triggered": len(logs)
    }


@router.post("/events")
async def create_backend_event(
    request: Request,
    auth: tuple = Depends(get_project_from_secret_key_with_signature),
    db: Session = Depends(get_db)
):
    """
    Track an event from backend - used by backend API.
    Similar to /track but with secret key auth and HMAC signature verification.

    Required headers:
    - X-API-Key: Your secret key (sk_live_xxx)
    - X-Timestamp: Unix timestamp in seconds
    - X-Signature: HMAC-SHA256(secret_key, timestamp + "." + body)
    """
    project_id, api_key, _ = auth

    # Check permissions
    if "track" not in (api_key.permissions or []):
        raise HTTPException(status_code=403, detail="API key lacks 'track' permission")

    # Parse the cached body
    try:
        body = await request.body()
        body_data = json.loads(body)
        data = BackendEventRequest(**body_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")

    user, event_properties, wa_check_needed = _prepare_backend_event_identity(
        db, project_id, data, request, api_key.id, "backend_events_hmac",
    )

    existing_event = _existing_backend_event(db, project_id, data, user)
    if existing_event:
        return _idempotent_event_response(db, existing_event)

    # Parse client timestamp
    _be_client_ts = None
    if getattr(data, 'client_ts', None):
        try:
            _be_client_ts = datetime.fromisoformat(data.client_ts.replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            pass

    # Create event
    event = MessagingEvent(
        project_id=project_id,
        user_id=user.id if user else None,
        event_name=data.event,
        anonymous_id=data.anonymous_id,
        properties=event_properties,
        source=EVENT_SOURCE_BACKEND,
        ip_address=_request_ip(request),
        user_agent=request.headers.get("User-Agent", "")[:500],
        session_id=getattr(data, 'session_id', None),
        client_ts=_be_client_ts,
        external_event_id=_backend_external_event_id(data),
        occurred_at=data.timestamp or _be_client_ts or datetime.utcnow(),
    )
    db.add(event)
    db.flush()
    _apply_attribution(
        db,
        event=event,
        user=user,
        anonymous_id=data.anonymous_id,
        properties=event_properties,
        context=data.context,
        request=request,
    )
    db.commit()
    db.refresh(event)

    if wa_check_needed and user and user.phone_e164:
        try:
            asyncio.create_task(
                WhatsAppValidationService().validate_number(
                    project_id, user.id, user.phone_e164,
                )
            )
        except RuntimeError:
            pass

    # Auto-discover schema from event
    schema_discovery.discover_from_event(db, project_id, data.event, event_properties)

    _queue_destinations_safely(db, event)

    # Process event
    logs = await event_processor.process_event(db, event)

    return {
        "success": True,
        "event_id": event.id,
        "messages_triggered": len(logs)
    }


@router.post("/users", response_model=MessagingUserResponse)
async def create_or_update_user(
    request: Request,
    auth: tuple = Depends(get_project_from_secret_key_with_signature),
    db: Session = Depends(get_db)
):
    """
    Create or update a user from backend - used by backend API.

    Required headers:
    - X-API-Key: Your secret key (sk_live_xxx)
    - X-Timestamp: Unix timestamp in seconds
    - X-Signature: HMAC-SHA256(secret_key, timestamp + "." + body)
    """
    project_id, api_key, _ = auth

    # Check permissions
    if "identify" not in (api_key.permissions or []):
        raise HTTPException(status_code=403, detail="API key lacks 'identify' permission")

    # Parse the cached body
    try:
        body = await request.body()
        body_data = json.loads(body)
        data = BackendUserRequest(**body_data)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")

    # Find or create user
    user = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.external_id == data.user_id
    ).first()

    if user:
        # Update existing user
        if data.email is not None:
            user.email = data.email
        if data.phone is not None:
            user.phone = data.phone
        if data.name is not None:
            user.name = data.name
        if data.properties:
            user.properties = _deep_merge_properties(user.properties, data.properties)
        user.last_seen_at = datetime.utcnow()
    else:
        # Create new user
        user = MessagingUser(
            project_id=project_id,
            external_id=data.user_id,
            email=data.email,
            phone=data.phone,
            name=data.name,
            properties=data.properties
        )
        db.add(user)

    db.flush()
    identity_properties = dict(data.properties or {})
    if data.email is not None:
        identity_properties["email"] = str(data.email)
    if data.phone is not None:
        identity_properties["phone"] = data.phone
    if data.name is not None:
        identity_properties["name"] = data.name
    _apply_event_identity_to_user(db, project_id, user, identity_properties)
    _record_email_permission_claim(
        db,
        project_id=project_id,
        user=user,
        claim=data.email_permission,
        api_key_id=api_key.id,
        ingress="backend_users_hmac",
    )

    # Normalize phone and trigger WhatsApp validation
    wa_check_needed = False
    if data.phone:
        from app.services.messaging.phone_normalizer import PhoneNormalizer
        fallback_cc = _get_project_fallback_cc(db, project_id)
        normalized, norm_status = PhoneNormalizer.normalize(data.phone, None, fallback_cc)
        user.phone_e164 = normalized
        user.phone_norm_status = norm_status
        if normalized:
            user.whatsapp_status = "checking"
            wa_check_needed = True

    db.commit()
    db.refresh(user)

    # Fire async WhatsApp validation (non-blocking, own DB session)
    if wa_check_needed and user.phone_e164:
        try:
            asyncio.create_task(
                WhatsAppValidationService().validate_number(
                    project_id, user.id, user.phone_e164,
                )
            )
        except RuntimeError:
            pass  # No running event loop (e.g. during tests)

    return user
