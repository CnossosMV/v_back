"""Contact endpoints, permission evidence, data quality and operational alerts."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User
from app.models.campaigns import ContactEndpoint, ContactPermissionEvidence, OperationalAlert
from app.models.messaging import MessagingUser
from app.routers.auth import get_current_user
from app.schemas.campaigns import ContactEndpointCreate, ContactEndpointUpdate, ContactPermissionEvidenceCreate
from app.services.messaging.contact_profile_service import ContactProfileService
from app.services.messaging.phone_normalizer import PhoneNormalizer, country_for_e164
from app.services.messaging.pii_hasher import pii_hasher
from app.services.operational_alert_service import OperationalAlertService

router = APIRouter(prefix="/projects/{project_id}", tags=["contact-profiles"])


def _project_or_404(db: Session, current_user: User, project_id: int) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True,  # noqa: E712
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _contact_or_404(db: Session, project_id: int, contact_id: int) -> MessagingUser:
    contact = db.query(MessagingUser).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id,
        MessagingUser.status != "deleted",
    ).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    return contact


def _endpoint_payload(endpoint: ContactEndpoint) -> dict[str, Any]:
    return {
        "id": endpoint.id,
        "project_id": endpoint.project_id,
        "user_id": endpoint.user_id,
        "endpoint_type": endpoint.endpoint_type,
        "value": endpoint.value,
        "normalized_value": endpoint.normalized_value,
        "value_hash": endpoint.value_hash,
        "is_primary": endpoint.is_primary,
        "status": endpoint.status,
        "source": endpoint.source,
        "metadata": endpoint.endpoint_metadata,
        "first_seen_at": endpoint.first_seen_at,
        "last_seen_at": endpoint.last_seen_at,
        "created_at": endpoint.created_at,
        "updated_at": endpoint.updated_at,
    }


@router.get("/contacts/{contact_id}/endpoints")
def list_contact_endpoints(
    project_id: int,
    contact_id: int,
    _role=Depends(require_project_role("viewer")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    _contact_or_404(db, project_id, contact_id)
    return [
        _endpoint_payload(endpoint)
        for endpoint in ContactProfileService(db).list_endpoints(project_id, contact_id)
    ]


@router.post("/contacts/{contact_id}/endpoints", status_code=201)
def create_contact_endpoint(
    project_id: int,
    contact_id: int,
    payload: ContactEndpointCreate,
    _role=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _project_or_404(db, current_user, project_id)
    contact = _contact_or_404(db, project_id, contact_id)
    if payload.user_id != contact_id:
        raise HTTPException(status_code=422, detail="Endpoint user_id must match contact_id")
    endpoint_type = payload.endpoint_type.strip().lower()
    value = payload.value.strip()
    salt = project.pii_salt or pii_hasher.get_or_create_project_salt(db, project_id)
    if endpoint_type == "email":
        normalized = pii_hasher.normalize_email(value)
        if not normalized or "@" not in normalized:
            raise HTTPException(status_code=422, detail="Invalid email endpoint")
        value_hash = pii_hasher.hash_email(normalized, salt)
    elif endpoint_type == "phone":
        normalized, status = PhoneNormalizer.normalize(value, locale=contact.locale)
        if not normalized:
            raise HTTPException(status_code=422, detail="Phone is not a possible E.164 number")
        value_hash = pii_hasher.hash_phone(normalized, salt)
    else:
        normalized = payload.normalized_value or value
        value_hash = hashlib.sha256(
            f"{salt}{endpoint_type}:{normalized}".encode("utf-8")
        ).hexdigest()
    service = ContactProfileService(db)
    endpoint, _created, _reactivated = service._upsert_endpoint(
        user=contact,
        endpoint_type=endpoint_type,
        value=normalized,
        normalized_value=normalized,
        value_hash=value_hash,
        source=payload.source or "manual",
        metadata=payload.metadata or {},
        make_primary=payload.is_primary,
    )
    if payload.is_primary:
        service.set_primary(project_id, contact_id, endpoint.id)
    else:
        db.commit()
        db.refresh(endpoint)
    return _endpoint_payload(endpoint)


@router.put("/contacts/{contact_id}/endpoints/{endpoint_id}")
def update_contact_endpoint(
    project_id: int,
    contact_id: int,
    endpoint_id: int,
    payload: ContactEndpointUpdate,
    _role=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    _contact_or_404(db, project_id, contact_id)
    endpoint = db.query(ContactEndpoint).filter(
        ContactEndpoint.id == endpoint_id,
        ContactEndpoint.project_id == project_id,
        ContactEndpoint.user_id == contact_id,
    ).first()
    if not endpoint:
        raise HTTPException(status_code=404, detail="Contact endpoint not found")
    values = payload.model_dump(exclude_unset=True)
    if "status" in values:
        endpoint.status = values["status"]
    if "metadata" in values:
        endpoint.endpoint_metadata = values["metadata"]
    if values.get("is_primary"):
        return _endpoint_payload(ContactProfileService(db).set_primary(project_id, contact_id, endpoint_id))
    db.commit()
    db.refresh(endpoint)
    return _endpoint_payload(endpoint)


@router.post("/contacts/{contact_id}/endpoints/{endpoint_id}/primary")
def set_primary_endpoint(
    project_id: int,
    contact_id: int,
    endpoint_id: int,
    _role=Depends(require_project_role("editor")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    _contact_or_404(db, project_id, contact_id)
    try:
        endpoint = ContactProfileService(db).set_primary(project_id, contact_id, endpoint_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _endpoint_payload(endpoint)


@router.get("/contacts/{contact_id}/permission-evidence")
def list_permission_evidence(
    project_id: int,
    contact_id: int,
    channel: Optional[str] = None,
    _role=Depends(require_project_role("viewer")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    _contact_or_404(db, project_id, contact_id)
    query = db.query(ContactPermissionEvidence).filter(
        ContactPermissionEvidence.project_id == project_id,
        ContactPermissionEvidence.user_id == contact_id,
    )
    if channel:
        query = query.filter(ContactPermissionEvidence.channel == channel)
    rows = query.order_by(ContactPermissionEvidence.captured_at.desc()).all()
    return [{
        "id": row.id,
        "project_id": row.project_id,
        "user_id": row.user_id,
        "endpoint_id": row.endpoint_id,
        "channel": row.channel,
        "permission_type": row.permission_type,
        "status": row.status,
        "source": row.source,
        "policy_version": row.policy_version,
        "evidence_ref": row.evidence_ref,
        "captured_at": row.captured_at,
        "expires_at": row.expires_at,
        "metadata": row.evidence_metadata,
        "created_at": row.created_at,
    } for row in rows]


@router.post("/contacts/{contact_id}/permission-evidence", status_code=201)
def create_permission_evidence(
    project_id: int,
    contact_id: int,
    payload: ContactPermissionEvidenceCreate,
    _role=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    contact = _contact_or_404(db, project_id, contact_id)
    if payload.user_id != contact_id:
        raise HTTPException(status_code=422, detail="Evidence user_id must match contact_id")
    if payload.endpoint_id:
        endpoint = db.query(ContactEndpoint.id).filter(
            ContactEndpoint.id == payload.endpoint_id,
            ContactEndpoint.project_id == project_id,
            ContactEndpoint.user_id == contact_id,
        ).first()
        if not endpoint:
            raise HTTPException(status_code=422, detail="Endpoint does not belong to this contact")
    if payload.status not in {"granted", "denied", "withdrawn", "unknown"}:
        raise HTTPException(status_code=422, detail="Invalid permission evidence status")
    captured_at = payload.captured_at or datetime.utcnow()
    expires_at = payload.expires_at
    if captured_at.tzinfo is not None:
        captured_at = captured_at.astimezone(timezone.utc).replace(tzinfo=None)
    if expires_at is not None and expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
    if captured_at > datetime.utcnow() + timedelta(minutes=5):
        raise HTTPException(status_code=422, detail="Permission evidence cannot be captured in the future")
    if expires_at is not None and expires_at <= captured_at:
        raise HTTPException(status_code=422, detail="Permission evidence expiry must be after capture")

    row = ContactPermissionEvidence(
        project_id=project_id,
        user_id=contact_id,
        endpoint_id=payload.endpoint_id,
        channel=payload.channel,
        permission_type=payload.permission_type,
        status=payload.status,
        source=payload.source,
        policy_version=payload.policy_version,
        evidence_ref=payload.evidence_ref,
        captured_at=captured_at,
        expires_at=expires_at,
        evidence_metadata=payload.metadata,
    )
    db.add(row)
    # Keep legacy consent fields as a current-state cache only.
    if payload.channel == "email" and payload.permission_type == "marketing":
        channels = dict(contact.consent_channels or {})
        channels["email"] = {
            "granted": payload.status == "granted",
            "source": payload.source,
            "captured_at": captured_at.isoformat(),
        }
        contact.consent_channels = channels
        contact.consent_marketing = payload.status == "granted"
    db.commit()
    db.refresh(row)
    return {"id": row.id, "status": row.status, "captured_at": row.captured_at}


def _quality_snapshot(users: list[MessagingUser], endpoints_by_user: dict[int, list[ContactEndpoint]]) -> dict[str, Any]:
    counts = {
        "active_contacts": len(users),
        "missing_locale": 0,
        "missing_country": 0,
        "missing_phone_e164": 0,
        "ambiguous_phone": 0,
        "invalid_phone": 0,
        "country_conflict": 0,
        "safe_country_backfill": 0,
        "email_contacts": 0,
        "phone_contacts": 0,
        "canonical_email_endpoints": 0,
        "canonical_phone_endpoints": 0,
    }
    issues: list[dict[str, Any]] = []
    locale_distribution: Counter[str] = Counter()
    country_distribution: Counter[str] = Counter()
    phone_country_distribution: Counter[str] = Counter()
    phone_status_distribution: Counter[str] = Counter()
    for user in users:
        props = user.properties or {}
        explicit_country = str(props.get("country") or props.get("market_country") or "").upper() or None
        locale_distribution[str(user.locale or "unknown")] += 1
        country_distribution[str(explicit_country or "unknown")] += 1
        if user.email:
            counts["email_contacts"] += 1
        has_phone = bool(user.phone_e164 or user.phone)
        if has_phone:
            counts["phone_contacts"] += 1
            phone_status_distribution[str(user.phone_norm_status or "unknown")] += 1
        endpoints = endpoints_by_user.get(user.id, [])
        counts["canonical_email_endpoints"] += sum(
            1 for endpoint in endpoints
            if endpoint.endpoint_type == "email" and endpoint.status == "active"
        )
        counts["canonical_phone_endpoints"] += sum(
            1 for endpoint in endpoints
            if endpoint.endpoint_type == "phone" and endpoint.status == "active"
        )
        if not user.locale:
            counts["missing_locale"] += 1
        if not explicit_country:
            counts["missing_country"] += 1
        if has_phone and not user.phone_e164:
            counts["missing_phone_e164"] += 1
        if has_phone and user.phone_norm_status in {"missing", "assumed_e164", "fallback"}:
            counts["ambiguous_phone"] += 1
        if has_phone and (
            user.phone_norm_status == "invalid" or (user.phone_e164 and len(user.phone_e164) > 15)
        ):
            counts["invalid_phone"] += 1
        phone_country = country_for_e164(user.phone_e164)
        if has_phone:
            phone_country_distribution[str(phone_country or "unknown")] += 1
        if explicit_country and phone_country and explicit_country != phone_country:
            counts["country_conflict"] += 1
            issues.append({"contact_id": user.id, "reason": "country_conflict"})
        if not explicit_country and phone_country and user.phone_norm_status in {"valid_e164", "inferred"}:
            counts["safe_country_backfill"] += 1
        if not user.locale:
            issues.append({"contact_id": user.id, "reason": "missing_locale"})
        if has_phone and user.phone_norm_status in {"missing", "assumed_e164", "fallback", "invalid"}:
            issues.append({"contact_id": user.id, "reason": f"phone_{user.phone_norm_status}"})
    def ranked(values: Counter[str]) -> list[dict[str, Any]]:
        return [
            {"value": value, "count": count}
            for value, count in sorted(values.items(), key=lambda item: (-item[1], item[0]))
        ]

    return {
        "counts": counts,
        "distributions": {
            "locale": ranked(locale_distribution),
            "country": ranked(country_distribution),
            "phone_country": ranked(phone_country_distribution),
            "phone_status": ranked(phone_status_distribution),
        },
        "issues": issues[:100],
        "issues_truncated": len(issues) > 100,
    }


@router.get("/contact-quality/overview")
def contact_quality_overview(
    project_id: int,
    _role=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    users = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox == False,  # noqa: E712
    ).all()
    endpoints = db.query(ContactEndpoint).filter(ContactEndpoint.project_id == project_id).all()
    by_user: dict[int, list[ContactEndpoint]] = {}
    for endpoint in endpoints:
        by_user.setdefault(endpoint.user_id, []).append(endpoint)
    return _quality_snapshot(users, by_user)


@router.post("/contact-quality/apply-safe")
def apply_safe_contact_quality_backfill(
    project_id: int,
    _role=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _project_or_404(db, current_user, project_id)
    supported = sorted(set(project.supported_locales or [project.default_locale]))
    users = db.query(MessagingUser).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox == False,  # noqa: E712
    ).all()
    updated = 0
    for user in users:
        if not user.phone_e164:
            continue
        changed = False
        validated_phone, validation_status = PhoneNormalizer.normalize(f"+{user.phone_e164}")
        if not validated_phone:
            if user.phone_norm_status != "invalid":
                user.phone_norm_status = "invalid"
                updated += 1
            continue
        if user.phone_norm_status not in {"valid_e164", "inferred"}:
            # The identifier already includes an international prefix and has
            # now been independently validated. Keep inferred provenance when
            # present, but upgrade ambiguous legacy rows to a conclusive state.
            user.phone_norm_status = validation_status
            changed = True
        country = country_for_e164(validated_phone)
        if not country:
            if changed:
                updated += 1
            continue
        props = dict(user.properties or {})
        explicit_country = str(props.get("country") or props.get("market_country") or "").upper()
        if explicit_country and explicit_country != country:
            if changed:
                updated += 1
            continue
        if not explicit_country:
            props["country"] = country
            props["market_country"] = country
            props["country_source"] = "phone_e164"
            props["country_confidence"] = "high" if user.phone_norm_status == "valid_e164" else "medium"
            changed = True
        if not user.locale:
            # A country is not language evidence.  Only infer a locale when
            # the project has exactly one locale for that country; countries
            # with multiple supported languages remain queued for review.
            locale_candidates = [
                locale for locale in supported
                if locale.upper().endswith(f"-{country}")
            ]
            if len(locale_candidates) == 1:
                user.locale = locale_candidates[0]
                props["locale_source"] = "phone_country"
                props["locale_confidence"] = "medium"
                changed = True
        if changed:
            user.properties = props
            updated += 1
    db.commit()
    return {"updated": updated, "review_required": True}


@router.get("/operational-alerts")
def list_operational_alerts(
    project_id: int,
    status: Optional[str] = Query(None),
    _role=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = _project_or_404(db, current_user, project_id)
    query = db.query(OperationalAlert).filter(
        OperationalAlert.workspace_id == project.workspace_id,
        (OperationalAlert.project_id == project_id) | (OperationalAlert.project_id.is_(None)),
    )
    if status:
        query = query.filter(OperationalAlert.status == status)
    return query.order_by(OperationalAlert.last_seen_at.desc()).limit(100).all()


@router.post("/operational-alerts/{alert_id}/acknowledge")
def acknowledge_operational_alert(
    project_id: int,
    alert_id: int,
    _role=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _project_or_404(db, current_user, project_id)
    try:
        return OperationalAlertService(db).acknowledge(project_id, alert_id, current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
