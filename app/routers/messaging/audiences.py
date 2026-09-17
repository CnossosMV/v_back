"""
Messaging audiences router.

Audience sync is separate from event delivery/CAPI. It builds and syncs
consent-eligible customer lists for ads platforms.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
import csv
import io
import json
import uuid
from datetime import datetime
from fastapi import File, Form, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import Any, Dict, List, Optional, Tuple

from app.database import get_db
from app.models import Project, User
from app.models.messaging import (
    ContactIdentity,
    MessagingAudience,
    MessagingAudienceDestination,
    MessagingAudienceMembership,
    MessagingAudienceSyncJob,
    MessagingAudienceWebhookDelivery,
    MessagingAudienceWebhookEndpoint,
    MessagingDestination,
    MessagingUser,
)
from app.routers.auth import get_current_user
from app.schemas.messaging import (
    AudienceCsvImportPreviewResponse,
    AudienceImportResultResponse,
    AudienceProjectImportPreviewRequest,
    AudienceProjectImportPreviewResponse,
    AudienceProjectImportRequest,
    MessagingAudienceCreate,
    MessagingAudienceDestinationCreate,
    MessagingAudienceDestinationResponse,
    MessagingAudienceDestinationUpdate,
    MessagingAudienceMembershipResponse,
    MessagingAudiencePreviewResponse,
    MessagingAudienceResponse,
    MessagingAudienceSyncJobResponse,
    MessagingAudienceSyncRequest,
    MessagingAudienceUpdate,
    MessagingAudienceWebhookCreate,
    MessagingAudienceWebhookDeliveryResponse,
    MessagingAudienceWebhookResponse,
    MessagingAudienceWebhookUpdate,
)
from app.services.messaging.audience_sync import audience_sync_service
from app.services.messaging.consent_manager import consent_manager
from app.services.messaging.contact_lifecycle_events import lifecycle_emitter
from app.services.messaging.phone_normalizer import PhoneNormalizer
from app.services.messaging.pii_hasher import pii_hasher


router = APIRouter(prefix="/projects/{project_id}/messaging/audiences", tags=["messaging-audiences"])


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _audience_or_404(db: Session, project_id: int, audience_id: int) -> MessagingAudience:
    audience = db.query(MessagingAudience).filter(
        MessagingAudience.project_id == project_id,
        MessagingAudience.id == audience_id,
    ).first()
    if not audience:
        raise HTTPException(status_code=404, detail="Audience not found")
    return audience


def _audience_destination_or_404(
    db: Session,
    project_id: int,
    audience_id: int,
    audience_destination_id: int,
) -> MessagingAudienceDestination:
    row = db.query(MessagingAudienceDestination).filter(
        MessagingAudienceDestination.project_id == project_id,
        MessagingAudienceDestination.audience_id == audience_id,
        MessagingAudienceDestination.id == audience_destination_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audience destination not found")
    return row


def _audience_destination_response(row: MessagingAudienceDestination) -> MessagingAudienceDestinationResponse:
    response = MessagingAudienceDestinationResponse.model_validate(row)
    response.provider_ready = bool(row.external_audience_id)
    return response


def _webhook_response(row: MessagingAudienceWebhookEndpoint) -> MessagingAudienceWebhookResponse:
    return MessagingAudienceWebhookResponse(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        url=row.url,
        events=row.events,
        is_active=row.is_active,
        has_secret=bool(row.secret),
        last_delivery_status=row.last_delivery_status,
        last_delivered_at=row.last_delivered_at,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


CSV_COLUMN_ALIASES = {
    "email": ["email", "e-mail", "email_address", "emailaddress"],
    "phone": ["phone", "telephone", "tel", "mobile", "phone_number", "phonenumber", "celular", "whatsapp"],
    "name": ["name", "full_name", "fullname", "nome", "contact_name"],
    "first_name": ["first_name", "firstname", "first", "given_name", "nome"],
    "last_name": ["last_name", "lastname", "last", "family_name", "sobrenome"],
    "external_id": ["external_id", "externalid", "id", "user_id", "userid", "customer_id", "cliente_id"],
    "tags": ["tags", "tag", "labels", "segmentos"],
    "lifecycle_stage": ["lifecycle_stage", "stage", "lifecycle", "status", "state"],
}


def _suggest_column_mapping(headers: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for header in headers:
        normalized = header.lower().strip().replace(" ", "_")
        for field, aliases in CSV_COLUMN_ALIASES.items():
            if normalized in aliases:
                mapping[header] = field
                break
    return mapping


async def _read_csv(file: UploadFile) -> Tuple[List[str], List[Dict[str, str]], List[List[str]]]:
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a .csv file")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    if not headers:
        raise HTTPException(status_code=400, detail="CSV file has no headers")
    rows = list(reader)
    if len(rows) > 50000:
        raise HTTPException(status_code=400, detail="CSV file too large (max 50,000 rows)")
    sample_rows = [[row.get(header, "") for header in headers] for row in rows[:5]]
    return headers, rows, sample_rows


def _parse_mapping(column_mapping: Optional[str], headers: List[str]) -> Dict[str, str]:
    if not column_mapping:
        return _suggest_column_mapping(headers)
    try:
        raw = json.loads(column_mapping)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="column_mapping must be valid JSON")
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="column_mapping must be an object")
    return {str(k): str(v) for k, v in raw.items() if v}


def _mapped_value(row: Dict[str, str], mapping: Dict[str, str], field: str) -> Optional[str]:
    for header, mapped_field in mapping.items():
        if mapped_field == field:
            value = row.get(header)
            if value is not None and str(value).strip():
                return str(value).strip()
    return None


def _mapped_name(row: Dict[str, str], mapping: Dict[str, str]) -> Optional[str]:
    explicit_name = _mapped_value(row, mapping, "name")
    if explicit_name:
        return explicit_name
    parts = [
        _mapped_value(row, mapping, "first_name"),
        _mapped_value(row, mapping, "last_name"),
    ]
    combined = " ".join(part for part in parts if part)
    return combined or None


def _split_tags(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    normalized = str(raw).replace(";", ",").replace("|", ",")
    return [tag.strip() for tag in normalized.split(",") if tag.strip()]


def _find_user_for_import(
    db: Session,
    project_id: int,
    external_id: Optional[str],
    email: Optional[str],
    phone: Optional[str],
) -> Optional[MessagingUser]:
    def find_by_identity(identity_types: List[str], identity_values: List[str]) -> Optional[MessagingUser]:
        clean_values = [value for value in identity_values if value]
        if not clean_values:
            return None
        identity = db.query(ContactIdentity).filter(
            ContactIdentity.project_id == project_id,
            ContactIdentity.identity_type.in_(identity_types),
            ContactIdentity.identity_value.in_(clean_values),
        ).order_by(ContactIdentity.verified.desc(), ContactIdentity.created_at.asc()).first()
        if not identity:
            return None
        return db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.id == identity.user_id,
            MessagingUser.status == "active",
        ).first()

    if external_id:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.external_id == external_id,
            MessagingUser.status == "active",
        ).order_by(MessagingUser.created_at.asc()).first()
        if user:
            return user

        user = find_by_identity(["contact_id", "external_id", "user_id"], [external_id])
        if user:
            return user

    normalized_email = pii_hasher.normalize_email(email) if email else None
    if normalized_email:
        user = find_by_identity(["email"], [normalized_email])
        if user:
            return user

    phone_candidates: List[str] = []
    if phone:
        phone_e164, _phone_status = PhoneNormalizer.normalize(phone, locale=None, fallback_country_code=None)
        normalized_phone_digits = pii_hasher.normalize_phone(phone)
        phone_candidates = [candidate for candidate in {phone_e164, phone.strip(), normalized_phone_digits} if candidate]
        user = find_by_identity(["phone"], phone_candidates)
        if user:
            return user

    email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, email, phone)
    if email_hash:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.email_hash == email_hash,
            MessagingUser.status == "active",
        ).order_by(MessagingUser.created_at.asc()).first()
        if user:
            return user
    if normalized_email:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            func.lower(func.trim(MessagingUser.email)) == normalized_email,
            MessagingUser.status == "active",
        ).order_by(MessagingUser.created_at.asc()).first()
        if user:
            return user
    if phone_hash:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.phone_hash == phone_hash,
            MessagingUser.status == "active",
        ).order_by(MessagingUser.created_at.asc()).first()
        if user:
            return user
    if phone_candidates:
        user = db.query(MessagingUser).filter(
            MessagingUser.project_id == project_id,
            MessagingUser.status == "active",
            or_(MessagingUser.phone_e164.in_(phone_candidates), MessagingUser.phone.in_(phone_candidates)),
        ).order_by(MessagingUser.created_at.asc()).first()
        if user:
            return user
    return None


def _create_identity_if_missing(
    db: Session,
    project_id: int,
    user_id: int,
    identity_type: str,
    identity_value: Optional[str],
    source: str,
) -> None:
    if not identity_value:
        return
    existing = db.query(ContactIdentity).filter(
        ContactIdentity.project_id == project_id,
        ContactIdentity.identity_type == identity_type,
        ContactIdentity.identity_value == identity_value,
    ).first()
    if not existing:
        db.add(ContactIdentity(
            project_id=project_id,
            user_id=user_id,
            identity_type=identity_type,
            identity_value=identity_value,
            verified=False,
            source=source,
        ))


def _apply_user_fields(
    db: Session,
    project_id: int,
    user: MessagingUser,
    *,
    email: Optional[str],
    phone: Optional[str],
    name: Optional[str],
    tags: List[str],
    lifecycle_stage: Optional[str],
    properties: Optional[Dict[str, Any]] = None,
    overwrite_existing: bool = False,
) -> bool:
    changed = False
    fallback_cc = None
    if phone:
        phone_e164, phone_status = PhoneNormalizer.normalize(phone, locale=None, fallback_country_code=fallback_cc)
    else:
        phone_e164, phone_status = None, None
    email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, email, phone)
    updates = {
        "email": email,
        "phone": phone,
        "phone_e164": phone_e164,
        "phone_norm_status": phone_status,
        "name": name,
        "lifecycle_stage": lifecycle_stage,
        "email_hash": email_hash,
        "phone_hash": phone_hash,
    }
    for field, value in updates.items():
        if value is None:
            continue
        if overwrite_existing or not getattr(user, field, None):
            if getattr(user, field, None) != value:
                setattr(user, field, value)
                changed = True
    if tags:
        current_tags = list(user.tags or [])
        merged = current_tags[:]
        for tag in tags:
            if tag not in merged:
                merged.append(tag)
        if merged != current_tags:
            user.tags = merged
            changed = True
    if properties:
        current_props = dict(user.properties or {})
        for key, value in properties.items():
            if overwrite_existing or key not in current_props or current_props.get(key) in (None, ""):
                current_props[key] = value
        if current_props != (user.properties or {}):
            user.properties = current_props
            changed = True
    return changed


def _set_audience_membership(db: Session, audience: MessagingAudience, user: MessagingUser) -> MessagingAudienceMembership:
    result = audience_sync_service.evaluate_user(db, audience, user)
    membership = db.query(MessagingAudienceMembership).filter(
        MessagingAudienceMembership.project_id == audience.project_id,
        MessagingAudienceMembership.audience_id == audience.id,
        MessagingAudienceMembership.user_id == user.id,
    ).first()
    if not membership:
        membership = MessagingAudienceMembership(
            project_id=audience.project_id,
            audience_id=audience.id,
            user_id=user.id,
        )
        db.add(membership)
    membership.state = result.state
    membership.eligibility_reason = result.reason
    membership.eligibility_details = result.details or None
    membership.identifiers_present = result.identifiers_present
    membership.last_evaluated_at = datetime.utcnow()
    return membership


def _grant_import_ads_consent(
    db: Session,
    user: MessagingUser,
    *,
    source: str,
    policy_version: str,
    evidence_id: Optional[str],
    evidence_url: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    consent_manager.update_ads_consent(
        db,
        user,
        {"ad_user_data": True, "ad_personalization": True},
        source=source,
        policy_version=policy_version,
        evidence_id=evidence_id,
        page_url=evidence_url,
        metadata=metadata,
        commit=False,
    )


@router.get("", response_model=List[MessagingAudienceResponse])
def list_audiences(
    project_id: int,
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    query = db.query(MessagingAudience).filter(MessagingAudience.project_id == project_id)
    if not include_archived:
        query = query.filter(MessagingAudience.status != "archived")
    return query.order_by(MessagingAudience.created_at.desc()).all()


@router.post("", response_model=MessagingAudienceResponse)
def create_audience(
    project_id: int,
    data: MessagingAudienceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = MessagingAudience(
        project_id=project_id,
        name=data.name,
        description=data.description,
        source_type=data.source_type or "dynamic_rule",
        source_config=data.source_config,
        rule_config=data.rule_config or {"match": "all", "filters": []},
        status=data.status or "active",
        refresh_mode=data.refresh_mode or "manual",
        created_by_user_id=current_user.id,
    )
    db.add(audience)
    db.commit()
    db.refresh(audience)
    return audience


@router.post("/preview", response_model=MessagingAudiencePreviewResponse)
def preview_rule_config(
    project_id: int,
    data: MessagingAudienceCreate,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    return audience_sync_service.preview(db, project_id, data.rule_config, limit=limit)


@router.get("/users/{user_id}/memberships", response_model=List[MessagingAudienceMembershipResponse])
def list_user_audience_memberships(
    project_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    return db.query(MessagingAudienceMembership).filter(
        MessagingAudienceMembership.project_id == project_id,
        MessagingAudienceMembership.user_id == user_id,
    ).order_by(MessagingAudienceMembership.updated_at.desc()).all()


@router.get("/webhooks", response_model=List[MessagingAudienceWebhookResponse])
def list_audience_webhooks(
    project_id: int,
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    query = db.query(MessagingAudienceWebhookEndpoint).filter(
        MessagingAudienceWebhookEndpoint.project_id == project_id,
    )
    if not include_inactive:
        query = query.filter(MessagingAudienceWebhookEndpoint.is_active == True)
    return [_webhook_response(row) for row in query.order_by(MessagingAudienceWebhookEndpoint.created_at.desc()).all()]


@router.post("/webhooks", response_model=MessagingAudienceWebhookResponse)
def create_audience_webhook(
    project_id: int,
    data: MessagingAudienceWebhookCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    row = MessagingAudienceWebhookEndpoint(
        project_id=project_id,
        name=data.name,
        url=data.url,
        secret=data.secret,
        events=data.events or [
            "audience.sync.succeeded",
            "audience.sync.partial_failed",
            "audience.sync.failed",
            "audience.provider_status.updated",
        ],
        is_active=data.is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _webhook_response(row)


@router.put("/webhooks/{webhook_id}", response_model=MessagingAudienceWebhookResponse)
def update_audience_webhook(
    project_id: int,
    webhook_id: int,
    data: MessagingAudienceWebhookUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    row = db.query(MessagingAudienceWebhookEndpoint).filter(
        MessagingAudienceWebhookEndpoint.project_id == project_id,
        MessagingAudienceWebhookEndpoint.id == webhook_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audience webhook not found")
    for field in ("name", "url", "events", "is_active"):
        value = getattr(data, field)
        if value is not None:
            setattr(row, field, value)
    if data.secret is not None:
        row.secret = data.secret
    db.commit()
    db.refresh(row)
    return _webhook_response(row)


@router.delete("/webhooks/{webhook_id}")
def disable_audience_webhook(
    project_id: int,
    webhook_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    row = db.query(MessagingAudienceWebhookEndpoint).filter(
        MessagingAudienceWebhookEndpoint.project_id == project_id,
        MessagingAudienceWebhookEndpoint.id == webhook_id,
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Audience webhook not found")
    row.is_active = False
    db.commit()
    return {"success": True}


@router.get("/webhooks/{webhook_id}/deliveries", response_model=List[MessagingAudienceWebhookDeliveryResponse])
def list_audience_webhook_deliveries(
    project_id: int,
    webhook_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    return db.query(MessagingAudienceWebhookDelivery).filter(
        MessagingAudienceWebhookDelivery.project_id == project_id,
        MessagingAudienceWebhookDelivery.webhook_endpoint_id == webhook_id,
    ).order_by(MessagingAudienceWebhookDelivery.created_at.desc()).limit(limit).all()


@router.post("/import-csv/preview", response_model=AudienceCsvImportPreviewResponse)
async def preview_audience_csv_import(
    project_id: int,
    file: UploadFile = File(...),
    column_mapping: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    headers, rows, sample_rows = await _read_csv(file)
    mapping = _parse_mapping(column_mapping, headers)
    matched_existing = 0
    will_create = 0
    missing_identifier = 0
    seen_existing_ids: set[int] = set()
    for row in rows:
        external_id = _mapped_value(row, mapping, "external_id")
        email = _mapped_value(row, mapping, "email")
        phone = _mapped_value(row, mapping, "phone")
        if not external_id and not email and not phone:
            missing_identifier += 1
            continue
        existing = _find_user_for_import(db, project_id, external_id, email, phone)
        if existing:
            if existing.id not in seen_existing_ids:
                matched_existing += 1
                seen_existing_ids.add(existing.id)
        else:
            will_create += 1
    return AudienceCsvImportPreviewResponse(
        headers=headers,
        sample_rows=sample_rows,
        total_rows=len(rows),
        suggested_mapping=mapping or _suggest_column_mapping(headers),
        matched_existing=matched_existing,
        will_create=will_create,
        missing_identifier=missing_identifier,
    )


@router.post("/import-csv", response_model=AudienceImportResultResponse)
async def import_audience_csv(
    project_id: int,
    name: str = Form(...),
    description: Optional[str] = Form(None),
    file: UploadFile = File(...),
    column_mapping: Optional[str] = Form(None),
    consent_source: str = Form(...),
    policy_version: str = Form(...),
    evidence_id: Optional[str] = Form(None),
    evidence_url: Optional[str] = Form(None),
    confirm_ads_consent: bool = Form(False),
    overwrite_existing: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    headers, rows, _sample_rows = await _read_csv(file)
    mapping = _parse_mapping(column_mapping, headers)
    import_id = f"aud_csv_{uuid.uuid4().hex[:12]}"
    audience = MessagingAudience(
        project_id=project_id,
        name=name,
        description=description,
        source_type="csv_import",
        source_config={
            "import_id": import_id,
            "file_name": file.filename,
            "row_count": len(rows),
            "mapping": mapping,
            "overwrite_existing": overwrite_existing,
            "consent_evidence": {
                "source": consent_source,
                "policy_version": policy_version,
                "evidence_id": evidence_id,
                "evidence_url": evidence_url,
                "confirmed": confirm_ads_consent,
            },
        },
        rule_config={"match": "static", "filters": []},
        status="active",
        refresh_mode="manual",
        created_by_user_id=current_user.id,
    )
    db.add(audience)
    db.flush()

    created = updated = skipped = matched_existing = 0
    eligible = excluded = missing_consent = missing_identifier = 0
    errors: List[Dict[str, Any]] = []
    imported_user_ids: set[int] = set()

    for row_num, row in enumerate(rows, start=2):
        try:
            external_id = _mapped_value(row, mapping, "external_id")
            email = _mapped_value(row, mapping, "email")
            phone = _mapped_value(row, mapping, "phone")
            name_value = _mapped_name(row, mapping)
            lifecycle_stage = _mapped_value(row, mapping, "lifecycle_stage")
            tags = _split_tags(_mapped_value(row, mapping, "tags"))
            if not external_id and not email and not phone:
                skipped += 1
                missing_identifier += 1
                errors.append({"row": row_num, "error": "Missing external_id, email, or phone"})
                continue

            user = _find_user_for_import(db, project_id, external_id, email, phone)
            if user:
                matched_existing += 1
                if _apply_user_fields(
                    db,
                    project_id,
                    user,
                    email=email,
                    phone=phone,
                    name=name_value,
                    tags=tags,
                    lifecycle_stage=lifecycle_stage,
                    overwrite_existing=overwrite_existing,
                ):
                    updated += 1
            else:
                generated_external_id = external_id or f"{import_id}_{uuid.uuid4().hex[:12]}"
                phone_e164, phone_status = (PhoneNormalizer.normalize(phone, locale=None, fallback_country_code=None) if phone else (None, None))
                email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, email, phone)
                user = MessagingUser(
                    project_id=project_id,
                    external_id=generated_external_id,
                    email=email,
                    phone=phone,
                    phone_e164=phone_e164,
                    phone_norm_status=phone_status,
                    name=name_value,
                    tags=tags or None,
                    lifecycle_stage=lifecycle_stage,
                    email_hash=email_hash,
                    phone_hash=phone_hash,
                    created_via="audience_csv_import",
                    status="active",
                    is_subscribed=True,
                )
                db.add(user)
                db.flush()
                created += 1
                _create_identity_if_missing(db, project_id, user.id, "contact_id", user.external_id, "audience_csv_import")

            if email:
                _create_identity_if_missing(db, project_id, user.id, "email", email.lower().strip(), "audience_csv_import")
            if user.phone_e164:
                _create_identity_if_missing(db, project_id, user.id, "phone", user.phone_e164, "audience_csv_import")

            if confirm_ads_consent:
                _grant_import_ads_consent(
                    db,
                    user,
                    source=consent_source,
                    policy_version=policy_version,
                    evidence_id=evidence_id,
                    evidence_url=evidence_url,
                    metadata={"import_id": import_id, "row": row_num, "file_name": file.filename},
                )

            membership = _set_audience_membership(db, audience, user)
            imported_user_ids.add(user.id)
            if membership.state == "eligible":
                eligible += 1
            else:
                excluded += 1
                if membership.eligibility_reason == "missing_ads_consent":
                    missing_consent += 1
                elif membership.eligibility_reason == "missing_identifier":
                    missing_identifier += 1
        except Exception as exc:
            skipped += 1
            errors.append({"row": row_num, "error": str(exc)})

    audience.source_config = {**(audience.source_config or {}), "imported_user_count": len(imported_user_ids)}
    audience.last_evaluated_at = datetime.utcnow()
    db.commit()
    db.refresh(audience)
    try:
        for user_id in list(imported_user_ids)[:100]:
            user = db.query(MessagingUser).filter(MessagingUser.id == user_id).first()
            if user and user.created_via == "audience_csv_import":
                lifecycle_emitter.contact_created(db, project_id, user, created_via="audience_csv_import")
    except Exception:
        pass
    return AudienceImportResultResponse(
        audience=MessagingAudienceResponse.model_validate(audience),
        total_rows=len(rows),
        matched_existing=matched_existing,
        created=created,
        updated=updated,
        skipped=skipped,
        eligible=eligible,
        excluded=excluded,
        missing_consent=missing_consent,
        missing_identifier=missing_identifier,
        errors=errors[:100],
    )


@router.post("/import-project/preview", response_model=AudienceProjectImportPreviewResponse)
def preview_project_import(
    project_id: int,
    data: AudienceProjectImportPreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_project = get_project_or_404(db, project_id, current_user.workspace_id)
    source_project = get_project_or_404(db, data.source_project_id, current_user.workspace_id)
    if source_project.id == target_project.id:
        raise HTTPException(status_code=400, detail="Choose another project to copy from")
    source_users = db.query(MessagingUser).filter(
        MessagingUser.project_id == source_project.id,
        MessagingUser.status == "active",
    ).limit(50000).all()
    matched_existing = 0
    will_create = 0
    for source_user in source_users:
        existing = _find_user_for_import(db, project_id, source_user.external_id, source_user.email, source_user.phone)
        if existing:
            matched_existing += 1
        else:
            will_create += 1
    return AudienceProjectImportPreviewResponse(
        source_project_id=source_project.id,
        source_project_name=source_project.name,
        total_contacts=len(source_users),
        matched_existing=matched_existing,
        will_create=will_create,
    )


@router.post("/import-project", response_model=AudienceImportResultResponse)
def import_project_audience(
    project_id: int,
    data: AudienceProjectImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target_project = get_project_or_404(db, project_id, current_user.workspace_id)
    source_project = get_project_or_404(db, data.source_project_id, current_user.workspace_id)
    if source_project.id == target_project.id:
        raise HTTPException(status_code=400, detail="Choose another project to copy from")

    copy_id = f"aud_project_{uuid.uuid4().hex[:12]}"
    audience = MessagingAudience(
        project_id=project_id,
        name=data.name,
        description=data.description,
        source_type="project_copy",
        source_config={
            "import_id": copy_id,
            "source_project_id": source_project.id,
            "source_project_name": source_project.name,
            "overwrite_existing": data.overwrite_existing,
            "consent_evidence": {
                "source": data.consent_source,
                "policy_version": data.policy_version,
                "evidence_id": data.evidence_id,
                "evidence_url": data.evidence_url,
                "confirmed": data.confirm_ads_consent,
            },
        },
        rule_config={"match": "static", "filters": []},
        status="active",
        refresh_mode="manual",
        created_by_user_id=current_user.id,
    )
    db.add(audience)
    db.flush()

    source_users = db.query(MessagingUser).filter(
        MessagingUser.project_id == source_project.id,
        MessagingUser.status == "active",
    ).limit(50000).all()

    created = updated = matched_existing = skipped = eligible = excluded = missing_consent = missing_identifier = 0
    errors: List[Dict[str, Any]] = []
    imported_user_ids: set[int] = set()
    for source_user in source_users:
        try:
            user = _find_user_for_import(db, project_id, source_user.external_id, source_user.email, source_user.phone)
            if user:
                matched_existing += 1
                if _apply_user_fields(
                    db,
                    project_id,
                    user,
                    email=source_user.email,
                    phone=source_user.phone,
                    name=source_user.name,
                    tags=list(source_user.tags or []),
                    lifecycle_stage=source_user.lifecycle_stage,
                    properties=source_user.properties,
                    overwrite_existing=data.overwrite_existing,
                ):
                    updated += 1
            else:
                email_hash, phone_hash = pii_hasher.hash_user_pii(db, project_id, source_user.email, source_user.phone)
                user = MessagingUser(
                    project_id=project_id,
                    external_id=source_user.external_id or f"{copy_id}_{uuid.uuid4().hex[:12]}",
                    email=source_user.email,
                    phone=source_user.phone,
                    phone_e164=source_user.phone_e164,
                    phone_norm_status=source_user.phone_norm_status,
                    name=source_user.name,
                    properties=source_user.properties,
                    tags=source_user.tags,
                    lifecycle_stage=source_user.lifecycle_stage,
                    email_hash=email_hash,
                    phone_hash=phone_hash,
                    created_via="audience_project_import",
                    status="active",
                    is_subscribed=source_user.is_subscribed,
                )
                db.add(user)
                db.flush()
                created += 1
            if data.confirm_ads_consent:
                _grant_import_ads_consent(
                    db,
                    user,
                    source=data.consent_source,
                    policy_version=data.policy_version,
                    evidence_id=data.evidence_id,
                    evidence_url=data.evidence_url,
                    metadata={"import_id": copy_id, "source_project_id": source_project.id, "source_user_id": source_user.id},
                )
            membership = _set_audience_membership(db, audience, user)
            imported_user_ids.add(user.id)
            if membership.state == "eligible":
                eligible += 1
            else:
                excluded += 1
                if membership.eligibility_reason == "missing_ads_consent":
                    missing_consent += 1
                elif membership.eligibility_reason == "missing_identifier":
                    missing_identifier += 1
        except Exception as exc:
            skipped += 1
            errors.append({"source_user_id": source_user.id, "error": str(exc)})

    audience.source_config = {**(audience.source_config or {}), "imported_user_count": len(imported_user_ids)}
    audience.last_evaluated_at = datetime.utcnow()
    db.commit()
    db.refresh(audience)
    return AudienceImportResultResponse(
        audience=MessagingAudienceResponse.model_validate(audience),
        total_rows=len(source_users),
        matched_existing=matched_existing,
        created=created,
        updated=updated,
        skipped=skipped,
        eligible=eligible,
        excluded=excluded,
        missing_consent=missing_consent,
        missing_identifier=missing_identifier,
        errors=errors[:100],
    )


@router.get("/{audience_id}", response_model=MessagingAudienceResponse)
def get_audience(
    project_id: int,
    audience_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    return _audience_or_404(db, project_id, audience_id)


@router.put("/{audience_id}", response_model=MessagingAudienceResponse)
def update_audience(
    project_id: int,
    audience_id: int,
    data: MessagingAudienceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = _audience_or_404(db, project_id, audience_id)
    for field in ("name", "description", "source_type", "source_config", "rule_config", "status", "refresh_mode"):
        value = getattr(data, field)
        if value is not None:
            setattr(audience, field, value)
    db.commit()
    db.refresh(audience)
    return audience


@router.delete("/{audience_id}")
def archive_audience(
    project_id: int,
    audience_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = _audience_or_404(db, project_id, audience_id)
    audience.status = "archived"
    db.commit()
    return {"success": True}


@router.get("/{audience_id}/preview", response_model=MessagingAudiencePreviewResponse)
def preview_audience(
    project_id: int,
    audience_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = _audience_or_404(db, project_id, audience_id)
    return audience_sync_service.preview(db, project_id, audience.rule_config, limit=limit, audience=audience)


@router.post("/{audience_id}/evaluate", response_model=List[MessagingAudienceMembershipResponse])
def evaluate_audience(
    project_id: int,
    audience_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = _audience_or_404(db, project_id, audience_id)
    audience_sync_service.evaluate_memberships(db, audience)
    return db.query(MessagingAudienceMembership).filter(
        MessagingAudienceMembership.project_id == project_id,
        MessagingAudienceMembership.audience_id == audience_id,
    ).order_by(MessagingAudienceMembership.updated_at.desc()).limit(100).all()


@router.get("/{audience_id}/memberships", response_model=List[MessagingAudienceMembershipResponse])
def list_memberships(
    project_id: int,
    audience_id: int,
    state: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    _audience_or_404(db, project_id, audience_id)
    query = db.query(MessagingAudienceMembership).filter(
        MessagingAudienceMembership.project_id == project_id,
        MessagingAudienceMembership.audience_id == audience_id,
    )
    if state:
        query = query.filter(MessagingAudienceMembership.state == state)
    return query.order_by(MessagingAudienceMembership.updated_at.desc()).limit(limit).all()


@router.get("/{audience_id}/destinations", response_model=List[MessagingAudienceDestinationResponse])
def list_audience_destinations(
    project_id: int,
    audience_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    _audience_or_404(db, project_id, audience_id)
    rows = db.query(MessagingAudienceDestination).filter(
        MessagingAudienceDestination.project_id == project_id,
        MessagingAudienceDestination.audience_id == audience_id,
    ).order_by(MessagingAudienceDestination.created_at.desc()).all()
    return [_audience_destination_response(row) for row in rows]


@router.post("/{audience_id}/destinations", response_model=MessagingAudienceDestinationResponse)
def connect_audience_destination(
    project_id: int,
    audience_id: int,
    data: MessagingAudienceDestinationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    _audience_or_404(db, project_id, audience_id)
    if data.destination_id:
        destination = db.query(MessagingDestination).filter(
            MessagingDestination.project_id == project_id,
            MessagingDestination.id == data.destination_id,
        ).first()
        if not destination:
            raise HTTPException(status_code=404, detail="Destination not found")
    row = MessagingAudienceDestination(
        project_id=project_id,
        audience_id=audience_id,
        provider_type=data.provider_type.value,
        destination_id=data.destination_id,
        external_audience_id=data.external_audience_id,
        external_audience_name=data.external_audience_name,
        sync_mode=data.sync_mode,
        is_active=data.is_active,
        config=data.config,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _audience_destination_response(row)


@router.put("/{audience_id}/destinations/{audience_destination_id}", response_model=MessagingAudienceDestinationResponse)
def update_audience_destination(
    project_id: int,
    audience_id: int,
    audience_destination_id: int,
    data: MessagingAudienceDestinationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    row = _audience_destination_or_404(db, project_id, audience_id, audience_destination_id)
    for field in ("destination_id", "external_audience_id", "external_audience_name", "sync_mode", "is_active", "config"):
        value = getattr(data, field)
        if value is not None:
            setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return _audience_destination_response(row)


@router.post("/{audience_id}/destinations/{audience_destination_id}/sync", response_model=MessagingAudienceSyncJobResponse)
def sync_audience_destination(
    project_id: int,
    audience_id: int,
    audience_destination_id: int,
    data: MessagingAudienceSyncRequest = MessagingAudienceSyncRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    audience = _audience_or_404(db, project_id, audience_id)
    row = _audience_destination_or_404(db, project_id, audience_id, audience_destination_id)
    try:
        return audience_sync_service.run_sync(db, audience, row, dry_run=data.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/{audience_id}/jobs", response_model=List[MessagingAudienceSyncJobResponse])
def list_sync_jobs(
    project_id: int,
    audience_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    _audience_or_404(db, project_id, audience_id)
    return db.query(MessagingAudienceSyncJob).filter(
        MessagingAudienceSyncJob.project_id == project_id,
        MessagingAudienceSyncJob.audience_id == audience_id,
    ).order_by(MessagingAudienceSyncJob.created_at.desc()).limit(limit).all()
