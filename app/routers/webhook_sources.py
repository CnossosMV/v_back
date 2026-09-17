"""
Webhook Sources Router (Authenticated)

CRUD for webhook source configuration, ingest log browsing, retry management,
and dry-run testing. All endpoints scoped to project_id with JWT auth.

IMPORTANT: Literal path routes (ingest-log, failed, bulk-retry) MUST be
defined BEFORE parameterized routes (/{source_id}) to avoid FastAPI
matching "ingest-log" as an integer source_id and returning 422.
"""
import secrets
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import User, Project, WebhookSource, WebhookIngest
from app.schemas.webhook_sources import (
    WebhookSourceCreate, WebhookSourceUpdate,
    WebhookSourceResponse, WebhookSourceCreatedResponse,
    WebhookIngestResponse, WebhookIngestDetailResponse, WebhookIngestListResponse,
    TestWebhookRequest, TestWebhookResponse,
    BulkRetryRequest, BulkRetryResponse,
)
from app.services.encryption_service import encrypt_value, decrypt_value
from app.routers.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/webhook-sources",
    tags=["Webhook Sources"],
)


# ============================================================================
# Helpers
# ============================================================================

def _get_project(db: Session, project_id: int, user: User) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_source(db: Session, project_id: int, source_id: int) -> WebhookSource:
    source = db.query(WebhookSource).filter(
        WebhookSource.id == source_id,
        WebhookSource.project_id == project_id,
    ).first()
    if not source:
        raise HTTPException(status_code=404, detail="Webhook source not found")
    return source


def _source_response(source: WebhookSource) -> dict:
    """Build response dict from model."""
    return {
        "id": source.id,
        "project_id": source.project_id,
        "source_slug": source.source_slug,
        "display_name": source.display_name,
        "source_type": source.source_type,
        "status": source.status,
        "transformer_config": source.transformer_config,
        "rate_limit_per_minute": source.rate_limit_per_minute,
        "last_received_at": source.last_received_at,
        "total_received": source.total_received or 0,
        "total_failed": source.total_failed or 0,
        "endpoint_url": f"/api/v1/projects/{source.project_id}/webhooks/{source.source_slug}",
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }


# ============================================================================
# Ingest Log & Failed Queue (MUST be before /{source_id} routes)
# ============================================================================

@router.get("/ingest-log", response_model=WebhookIngestListResponse)
def list_ingest_log(
    project_id: int,
    source_slug: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    q = db.query(WebhookIngest).filter(WebhookIngest.project_id == project_id)
    if source_slug:
        q = q.filter(WebhookIngest.source_slug == source_slug)
    if status:
        q = q.filter(WebhookIngest.processing_status == status)

    total = q.count()
    items = q.order_by(WebhookIngest.received_at.desc()).offset(
        (page - 1) * page_size
    ).limit(page_size).all()

    return {
        "items": [
            {
                "id": i.id, "project_id": i.project_id, "source_id": i.source_id,
                "source_slug": i.source_slug, "received_at": i.received_at,
                "signature_valid": i.signature_valid, "idempotency_key": i.idempotency_key,
                "processing_status": i.processing_status, "error_message": i.error_message,
                "retry_count": i.retry_count, "next_retry_at": i.next_retry_at,
                "resulting_event_id": i.resulting_event_id,
                "created_at": i.created_at, "updated_at": i.updated_at,
            }
            for i in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/ingest-log/{ingest_id}", response_model=WebhookIngestDetailResponse)
def get_ingest_detail(
    project_id: int,
    ingest_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    ingest = db.query(WebhookIngest).filter(
        WebhookIngest.id == ingest_id,
        WebhookIngest.project_id == project_id,
    ).first()
    if not ingest:
        raise HTTPException(status_code=404, detail="Ingest record not found")

    return {
        "id": ingest.id, "project_id": ingest.project_id,
        "source_id": ingest.source_id, "source_slug": ingest.source_slug,
        "received_at": ingest.received_at,
        "headers": ingest.headers, "raw_payload": ingest.raw_payload,
        "signature_valid": ingest.signature_valid,
        "idempotency_key": ingest.idempotency_key,
        "processing_status": ingest.processing_status,
        "error_message": ingest.error_message,
        "retry_count": ingest.retry_count, "next_retry_at": ingest.next_retry_at,
        "resulting_event_id": ingest.resulting_event_id,
        "created_at": ingest.created_at, "updated_at": ingest.updated_at,
    }


@router.post("/ingest-log/{ingest_id}/retry", response_model=WebhookIngestResponse)
def retry_ingest(
    project_id: int,
    ingest_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    ingest = db.query(WebhookIngest).filter(
        WebhookIngest.id == ingest_id,
        WebhookIngest.project_id == project_id,
    ).first()
    if not ingest:
        raise HTTPException(status_code=404, detail="Ingest record not found")

    if ingest.processing_status not in ("failed", "skipped"):
        raise HTTPException(status_code=400, detail="Only failed or skipped ingests can be retried")

    ingest.processing_status = "queued"
    ingest.retry_count = 0
    ingest.next_retry_at = None
    ingest.error_message = None
    ingest.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ingest)

    return {
        "id": ingest.id, "project_id": ingest.project_id, "source_id": ingest.source_id,
        "source_slug": ingest.source_slug, "received_at": ingest.received_at,
        "signature_valid": ingest.signature_valid, "idempotency_key": ingest.idempotency_key,
        "processing_status": ingest.processing_status, "error_message": ingest.error_message,
        "retry_count": ingest.retry_count, "next_retry_at": ingest.next_retry_at,
        "resulting_event_id": ingest.resulting_event_id,
        "created_at": ingest.created_at, "updated_at": ingest.updated_at,
    }


@router.get("/failed", response_model=list[WebhookIngestResponse])
def list_failed_ingests(
    project_id: int,
    source_slug: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    q = db.query(WebhookIngest).filter(
        WebhookIngest.project_id == project_id,
        WebhookIngest.processing_status == "failed",
    )
    if source_slug:
        q = q.filter(WebhookIngest.source_slug == source_slug)

    items = q.order_by(WebhookIngest.received_at.desc()).limit(limit).all()
    return [
        {
            "id": i.id, "project_id": i.project_id, "source_id": i.source_id,
            "source_slug": i.source_slug, "received_at": i.received_at,
            "signature_valid": i.signature_valid, "idempotency_key": i.idempotency_key,
            "processing_status": i.processing_status, "error_message": i.error_message,
            "retry_count": i.retry_count, "next_retry_at": i.next_retry_at,
            "resulting_event_id": i.resulting_event_id,
            "created_at": i.created_at, "updated_at": i.updated_at,
        }
        for i in items
    ]


@router.post("/bulk-retry", response_model=BulkRetryResponse)
def bulk_retry(
    project_id: int,
    data: BulkRetryRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    q = db.query(WebhookIngest).filter(
        WebhookIngest.project_id == project_id,
        WebhookIngest.processing_status == "failed",
    )
    if data.source_slug:
        q = q.filter(WebhookIngest.source_slug == data.source_slug)

    count = q.update({
        WebhookIngest.processing_status: "queued",
        WebhookIngest.retry_count: 0,
        WebhookIngest.next_retry_at: None,
        WebhookIngest.error_message: None,
        WebhookIngest.updated_at: datetime.utcnow(),
    }, synchronize_session="fetch")

    db.commit()
    return {"queued_count": count}


# ============================================================================
# Source CRUD (parameterized /{source_id} routes AFTER literal routes)
# ============================================================================

@router.get("/", response_model=list[WebhookSourceResponse])
def list_sources(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    sources = db.query(WebhookSource).filter(
        WebhookSource.project_id == project_id
    ).order_by(WebhookSource.created_at.desc()).all()
    return [_source_response(s) for s in sources]


@router.post("/", response_model=WebhookSourceCreatedResponse, status_code=201)
def create_source(
    project_id: int,
    data: WebhookSourceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)

    # Check uniqueness
    existing = db.query(WebhookSource).filter(
        WebhookSource.project_id == project_id,
        WebhookSource.source_slug == data.source_slug,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Source slug already exists for this project")

    # Validate source_type
    if data.source_type not in ("built_in", "custom"):
        raise HTTPException(status_code=400, detail="source_type must be 'built_in' or 'custom'")

    # Generate signing secret
    plain_secret = secrets.token_hex(32)
    encrypted_secret = encrypt_value(plain_secret)

    source = WebhookSource(
        project_id=project_id,
        source_slug=data.source_slug,
        display_name=data.display_name,
        source_type=data.source_type,
        status="active",
        secret_enc=encrypted_secret,
        transformer_config=data.transformer_config,
        rate_limit_per_minute=data.rate_limit_per_minute,
    )
    db.add(source)
    db.commit()
    db.refresh(source)

    resp = _source_response(source)
    resp["secret"] = plain_secret  # One-time reveal
    return resp


@router.get("/{source_id}", response_model=WebhookSourceResponse)
def get_source(
    project_id: int,
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    source = _get_source(db, project_id, source_id)
    return _source_response(source)


@router.put("/{source_id}", response_model=WebhookSourceResponse)
def update_source(
    project_id: int,
    source_id: int,
    data: WebhookSourceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    source = _get_source(db, project_id, source_id)

    if data.display_name is not None:
        source.display_name = data.display_name
    if data.status is not None:
        if data.status not in ("active", "paused", "disabled"):
            raise HTTPException(status_code=400, detail="status must be 'active', 'paused', or 'disabled'")
        source.status = data.status
    if data.transformer_config is not None:
        source.transformer_config = data.transformer_config
    if data.rate_limit_per_minute is not None:
        source.rate_limit_per_minute = data.rate_limit_per_minute

    source.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(source)
    return _source_response(source)


@router.delete("/{source_id}", status_code=204)
def delete_source(
    project_id: int,
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    source = _get_source(db, project_id, source_id)
    db.delete(source)
    db.commit()


@router.post("/{source_id}/rotate-secret", response_model=WebhookSourceCreatedResponse)
def rotate_secret(
    project_id: int,
    source_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    source = _get_source(db, project_id, source_id)

    plain_secret = secrets.token_hex(32)
    source.secret_enc = encrypt_value(plain_secret)
    source.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(source)

    resp = _source_response(source)
    resp["secret"] = plain_secret
    return resp


@router.post("/{source_id}/test", response_model=TestWebhookResponse)
def test_source(
    project_id: int,
    source_id: int,
    data: TestWebhookRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_project(db, project_id, user)
    source = _get_source(db, project_id, source_id)

    from app.services.webhook.transformers import get_transformer

    transformer = get_transformer(source.source_type, source.source_slug, source.transformer_config)

    try:
        normalized = transformer.transform(data.payload, 0)
        idempotency_key = transformer.get_idempotency_key(data.payload)

        if not normalized:
            return {
                "signature_valid": True,
                "transformed_event": None,
                "idempotency_key": idempotency_key,
                "contact_ref": None,
                "error": "Transformer returned no event",
            }

        return {
            "signature_valid": True,
            "transformed_event": {
                "event_name": normalized.event_name,
                "properties": normalized.properties,
                "occurred_at": normalized.occurred_at.isoformat() if normalized.occurred_at else None,
            },
            "idempotency_key": idempotency_key,
            "contact_ref": normalized.contact_ref,
            "error": None,
        }
    except Exception as e:
        return {
            "signature_valid": True,
            "transformed_event": None,
            "idempotency_key": None,
            "contact_ref": None,
            "error": str(e),
        }
