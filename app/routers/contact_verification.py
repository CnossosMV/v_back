from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import require_project_role
from app.models import Project, User, WhatsAppInstance
from app.models.messaging import (
    ContactVerificationItem,
    ContactVerificationJob,
    ContactVerificationOperation,
    ContactVerificationState,
    MessagingUser,
)
from app.routers.auth import get_current_user
from app.schemas.contact_verification import (
    VerificationJobCreate,
    VerificationJobResponse,
    VerificationPreviewRequest,
    VerificationSettingsResponse,
    VerificationSettingsUpdate,
    VerifyContactRequest,
)
from app.services.contact_verification.access import VerificationAccessError
from app.services.contact_verification.providers import ProviderConfigurationError, ProviderRequestError, provider_registry
from app.services.contact_verification.service import ContactVerificationService

router = APIRouter(
    prefix="/projects/{project_id}/contact-verification",
    tags=["contact-verification"],
)


def _project_admin(
    project_id: int,
    membership=Depends(require_project_role("admin")),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        authorization = ContactVerificationService(db).access.authorize(project_id)
    except VerificationAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {**membership, "authorization": authorization, "project": project}


def _job_or_404(db: Session, project_id: int, job_id: int) -> ContactVerificationJob:
    job = db.query(ContactVerificationJob).filter(
        ContactVerificationJob.id == job_id,
        ContactVerificationJob.project_id == project_id,
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="Verification job not found")
    return job


def _summary(db: Session, project_id: int, verification_type: str) -> dict:
    identifier_filter = (
        MessagingUser.email.isnot(None)
        if verification_type == "email"
        else ((MessagingUser.phone_e164.isnot(None)) | (MessagingUser.phone.isnot(None)))
    )
    eligible = db.query(func.count(MessagingUser.id)).filter(
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox == False,
        identifier_filter,
    ).scalar() or 0
    current_hash_column = (
        MessagingUser.email_hash if verification_type == "email" else MessagingUser.phone_hash
    )
    state_base = db.query(ContactVerificationState).join(
        MessagingUser, MessagingUser.id == ContactVerificationState.user_id,
    ).filter(
        ContactVerificationState.project_id == project_id,
        ContactVerificationState.verification_type == verification_type,
        MessagingUser.project_id == project_id,
        MessagingUser.status == "active",
        MessagingUser.is_sandbox == False,
        ContactVerificationState.identifier_hash == current_hash_column,
    )
    rows = state_base.with_entities(
        ContactVerificationState.canonical_status,
        func.count(ContactVerificationState.id),
    ).group_by(ContactVerificationState.canonical_status).all()
    counts = {status: count for status, count in rows}
    expired = state_base.filter(
        ContactVerificationState.expires_at <= datetime.utcnow(),
    ).count()
    return {
        "eligible": eligible,
        "valid": counts.get("valid", 0),
        "invalid": counts.get("invalid", 0),
        "risky": counts.get("risky", 0),
        "expired": expired,
        "unverified": max(0, eligible - sum(counts.values())),
    }


@router.get("/overview")
async def overview(
    project_id: int,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    settings = ContactVerificationService(db).get_or_create_settings(project_id)
    email_adapter = provider_registry.email(settings.email_provider)
    email_provider = {
        "provider": email_adapter.provider_code,
        "key_source": "platform",
        "version": email_adapter.provider_version,
        "configured": email_adapter.configured,
        "available": False,
        "credits": None,
        "error": None,
    }
    if email_adapter.configured:
        try:
            email_provider["credits"] = await email_adapter.credits()
            email_provider["available"] = True
        except (ProviderConfigurationError, ProviderRequestError) as exc:
            email_provider["error"] = str(exc)

    instances = db.query(WhatsAppInstance).filter(
        WhatsAppInstance.project_id == project_id,
        WhatsAppInstance.provider_type == "evolution_api",
        WhatsAppInstance.is_active == True,
    ).order_by(WhatsAppInstance.instance_name).all()
    selected_instance = next(
        (instance for instance in instances if instance.id == settings.whatsapp_instance_id),
        None,
    )
    return {
        "entitlement": {
            "mode": access["authorization"].access_mode,
            "billing_disposition": access["authorization"].billing_disposition,
        },
        "providers": {
            "email": email_provider,
            "whatsapp": {
                "provider": "evolution_api",
                "key_source": "project",
                "version": "whatsappNumbers",
                "configured": selected_instance is not None,
                "available": bool(
                    selected_instance
                    and selected_instance.connection_status in {"connected", "open"}
                ),
                "daily_limit_per_instance": 250,
                "interval_seconds": 10,
                "risk_warning": True,
                "instances": [
                    {
                        "id": i.id,
                        "name": i.instance_name,
                        "connection_status": i.connection_status,
                        "selected": i.id == settings.whatsapp_instance_id,
                    }
                    for i in instances
                ],
            },
        },
        "summary": {
            "email": _summary(db, project_id, "email"),
            "whatsapp": _summary(db, project_id, "whatsapp"),
        },
    }


@router.get("/settings", response_model=VerificationSettingsResponse)
def get_settings(
    project_id: int,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    return ContactVerificationService(db).get_or_create_settings(project_id)


@router.put("/settings", response_model=VerificationSettingsResponse)
def update_settings(
    project_id: int,
    payload: VerificationSettingsUpdate,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    try:
        return ContactVerificationService(db).update_settings(
            project_id, payload.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/jobs/preview")
async def preview_job(
    project_id: int,
    payload: VerificationPreviewRequest,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    service = ContactVerificationService(db)
    result = service.preview(
        project_id, payload.verification_type, payload.selection.model_dump(exclude_none=True),
    )
    if payload.verification_type == "email":
        adapter = provider_registry.email(service.get_or_create_settings(project_id).email_provider)
        result["available_credits"] = None
        if adapter.configured:
            try:
                result["available_credits"] = (await adapter.credits()).get("bulk_credits")
            except ProviderRequestError as exc:
                result["provider_error"] = str(exc)
    result["estimated_seconds"] = (
        result["unique_count"] * 10
        if payload.verification_type == "whatsapp"
        else max(60, ((result["unique_count"] + 49_999) // 50_000) * 60)
    ) if result["unique_count"] else 0
    return result


@router.post("/jobs", response_model=VerificationJobResponse, status_code=201)
async def create_job(
    project_id: int,
    payload: VerificationJobCreate,
    response: Response,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    service = ContactVerificationService(db)
    selection = payload.selection.model_dump(exclude_none=True)
    try:
        replay = service.find_manual_job_replay(
            project_id,
            payload.verification_type,
            selection,
            expected_candidate_count=payload.expected_candidate_count,
            expected_unique_count=payload.expected_unique_count,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if replay:
        response.status_code = 200
        response.headers["Idempotent-Replay"] = "true"
        return replay
    if payload.verification_type == "email":
        adapter = provider_registry.email(service.get_or_create_settings(project_id).email_provider)
        try:
            credits = await adapter.credits()
        except (ProviderConfigurationError, ProviderRequestError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if int(credits.get("bulk_credits") or 0) < payload.expected_unique_count:
            raise HTTPException(status_code=409, detail="Insufficient MillionVerifier bulk credits")
    try:
        job = service.create_job(
            project_id,
            payload.verification_type,
            selection,
            access["user_id"],
            expected_candidate_count=payload.expected_candidate_count,
            expected_unique_count=payload.expected_unique_count,
            idempotency_key=payload.idempotency_key,
        )
        if getattr(job, "_idempotency_replayed", False):
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return job
    except (ValueError, VerificationAccessError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs", response_model=list[VerificationJobResponse])
def list_jobs(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    return db.query(ContactVerificationJob).filter(
        ContactVerificationJob.project_id == project_id,
    ).order_by(ContactVerificationJob.created_at.desc()).limit(limit).all()


@router.get("/jobs/{job_id}")
def get_job(
    project_id: int,
    job_id: int,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    job = _job_or_404(db, project_id, job_id)
    operations = db.query(ContactVerificationOperation).filter(
        ContactVerificationOperation.job_id == job.id,
        ContactVerificationOperation.project_id == project_id,
    ).order_by(ContactVerificationOperation.id).all()
    return {
        "job": VerificationJobResponse.model_validate(job).model_dump(),
        "operations": [
            {
                "id": op.id,
                "provider": op.provider,
                "operation_type": op.operation_type,
                "status": op.status,
                "item_count": op.item_count,
                "provider_units": op.provider_units,
                "billing_disposition": op.billing_disposition,
                "response_summary": op.response_summary,
                "error_message": op.error_message,
                "created_at": op.created_at,
            }
            for op in operations
        ],
    }


@router.post("/jobs/{job_id}/cancel", response_model=VerificationJobResponse)
def cancel_job(
    project_id: int,
    job_id: int,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    return ContactVerificationService(db).cancel_job(_job_or_404(db, project_id, job_id))


@router.post("/contacts/{contact_id}/verify", response_model=VerificationJobResponse, status_code=201)
def verify_contact(
    project_id: int,
    contact_id: int,
    payload: VerifyContactRequest,
    response: Response,
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    service = ContactVerificationService(db)
    try:
        endpoint = service.resolve_individual_endpoint(
            project_id,
            contact_id,
            payload.verification_type,
        )
        job = service.create_job(
            project_id,
            payload.verification_type,
            {"scope": "contact", "contact_id": contact_id},
            access["user_id"],
            trigger_type="individual",
            contact_id=contact_id,
            endpoint_id=endpoint.id,
            idempotency_key=payload.idempotency_key,
        )
        if getattr(job, "_idempotency_replayed", False):
            response.status_code = 200
            response.headers["Idempotent-Replay"] = "true"
        return job
    except (ValueError, VerificationAccessError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/contacts/{contact_id}/history")
def contact_history(
    project_id: int,
    contact_id: int,
    verification_type: Optional[str] = Query(None, pattern="^(email|whatsapp)$"),
    access=Depends(_project_admin),
    db: Session = Depends(get_db),
):
    contact = db.query(MessagingUser.id).filter(
        MessagingUser.id == contact_id,
        MessagingUser.project_id == project_id,
    ).first()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    query = db.query(ContactVerificationItem, ContactVerificationJob).join(
        ContactVerificationJob, ContactVerificationJob.id == ContactVerificationItem.job_id,
    ).filter(
        ContactVerificationItem.project_id == project_id,
        ContactVerificationItem.user_id == contact_id,
    )
    if verification_type:
        query = query.filter(ContactVerificationItem.verification_type == verification_type)
    rows = query.order_by(ContactVerificationItem.created_at.desc()).limit(100).all()
    return [
        {
            "id": item.id,
            "job_id": job.id,
            "verification_type": item.verification_type,
            "provider": job.provider,
            "trigger_type": job.trigger_type,
            "status": item.status,
            "canonical_status": item.canonical_status,
            "provider_status": item.provider_status,
            "error_code": item.error_code,
            "created_at": item.created_at,
            "finished_at": item.finished_at,
        }
        for item, job in rows
    ]
