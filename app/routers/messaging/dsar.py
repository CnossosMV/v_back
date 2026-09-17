"""
DSAR (Data Subject Access Request) Router
Public endpoints for data export/delete requests and admin endpoints for processing.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models.messaging import (
    MessagingDomain, MessagingDSARRequest, DSARRequestType, DSARRequestStatus
)
from app.schemas.messaging import (
    DSARCreateRequest, DSARCreateResponse, DSARProcessRequest,
    DSARRequestResponse, DSARExportResponse,
    DSARRequestType as DSARTypeSchema,
    DSARRequestStatus as DSARStatusSchema
)
from app.services.messaging.dsar_processor import dsar_processor
from app.routers.messaging.public_api import get_domain_from_write_key
from app.dependencies import require_project_role

router = APIRouter(prefix="/dsar", tags=["messaging-dsar"])


# ==========================================
# Public DSAR Endpoints (Write Key Auth)
# ==========================================

@router.post("/export", response_model=DSARCreateResponse)
async def request_export(
    data: DSARCreateRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Request data export (public endpoint).
    Creates a DSAR export request for the user.
    """
    if data.request_type != DSARTypeSchema.export:
        raise HTTPException(status_code=400, detail="This endpoint only handles export requests")

    if not data.email and not data.external_id:
        raise HTTPException(status_code=400, detail="Either email or external_id required")

    ip = request.client.host if request.client else None

    dsar_request = await dsar_processor.create_dsar_request(
        db=db,
        project_id=domain.project_id,
        request_type=DSARRequestType.export,
        external_id=data.external_id,
        email=data.email,
        ip=ip
    )

    return DSARCreateResponse(
        success=True,
        request_id=dsar_request.id,
        status=DSARStatusSchema(dsar_request.status.value),
        message="Export request created. You will be notified when it's ready."
    )


@router.post("/delete", response_model=DSARCreateResponse)
async def request_delete(
    data: DSARCreateRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Request data deletion (public endpoint).
    Creates a DSAR delete request (right to be forgotten).
    """
    if data.request_type != DSARTypeSchema.delete:
        raise HTTPException(status_code=400, detail="This endpoint only handles delete requests")

    if not data.email and not data.external_id:
        raise HTTPException(status_code=400, detail="Either email or external_id required")

    ip = request.client.host if request.client else None

    dsar_request = await dsar_processor.create_dsar_request(
        db=db,
        project_id=domain.project_id,
        request_type=DSARRequestType.delete,
        external_id=data.external_id,
        email=data.email,
        ip=ip
    )

    return DSARCreateResponse(
        success=True,
        request_id=dsar_request.id,
        status=DSARStatusSchema(dsar_request.status.value),
        message="Delete request created. Data will be deleted after review."
    )


@router.post("/withdraw-consent", response_model=DSARCreateResponse)
async def withdraw_consent_request(
    data: DSARCreateRequest,
    request: Request,
    domain: MessagingDomain = Depends(get_domain_from_write_key),
    db: Session = Depends(get_db)
):
    """
    Withdraw all consent (public endpoint).
    Creates a request to revoke all consent and unsubscribe.
    """
    if not data.email and not data.external_id:
        raise HTTPException(status_code=400, detail="Either email or external_id required")

    ip = request.client.host if request.client else None

    dsar_request = await dsar_processor.create_dsar_request(
        db=db,
        project_id=domain.project_id,
        request_type=DSARRequestType.withdraw_consent,
        external_id=data.external_id,
        email=data.email,
        ip=ip
    )

    return DSARCreateResponse(
        success=True,
        request_id=dsar_request.id,
        status=DSARStatusSchema(dsar_request.status.value),
        message="Consent withdrawal request created."
    )


# ==========================================
# Admin DSAR Endpoints (JWT Auth)
# ==========================================

@router.get("/projects/{project_id}/requests", response_model=List[DSARRequestResponse])
async def list_dsar_requests(
    project_id: int,
    status: Optional[DSARStatusSchema] = None,
    skip: int = 0,
    limit: int = 50,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db)
):
    """List all DSAR requests for a project (admin)."""
    query = db.query(MessagingDSARRequest).filter(
        MessagingDSARRequest.project_id == project_id
    )

    if status:
        query = query.filter(MessagingDSARRequest.status == status.value)

    requests = query.order_by(
        MessagingDSARRequest.requested_at.desc()
    ).offset(skip).limit(limit).all()

    return requests


@router.get("/projects/{project_id}/requests/{request_id}", response_model=DSARRequestResponse)
async def get_dsar_request(
    project_id: int,
    request_id: int,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db)
):
    """Get a specific DSAR request (admin)."""
    dsar_request = db.query(MessagingDSARRequest).filter(
        MessagingDSARRequest.id == request_id,
        MessagingDSARRequest.project_id == project_id
    ).first()

    if not dsar_request:
        raise HTTPException(status_code=404, detail="DSAR request not found")

    return dsar_request


@router.post("/projects/{project_id}/requests/{request_id}/process", response_model=DSARRequestResponse)
async def process_dsar_request(
    project_id: int,
    request_id: int,
    data: DSARProcessRequest = None,
    auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db)
):
    """Process a pending DSAR request (admin)."""
    dsar_request = db.query(MessagingDSARRequest).filter(
        MessagingDSARRequest.id == request_id,
        MessagingDSARRequest.project_id == project_id
    ).first()

    if not dsar_request:
        raise HTTPException(status_code=404, detail="DSAR request not found")

    retrying_remote_delete = (
        dsar_request.request_type == DSARRequestType.delete
        and dsar_request.status == DSARRequestStatus.processing
    )
    if dsar_request.status != DSARRequestStatus.pending and not retrying_remote_delete:
        raise HTTPException(status_code=400, detail="Request is not pending")

    # For rectify requests, add updates to result_data
    if data and data.updates and dsar_request.request_type == DSARRequestType.rectify:
        dsar_request.result_data = data.updates
        db.commit()

    processed = await dsar_processor.process_request(
        db=db,
        project_id=project_id,
        request_id=request_id,
        processed_by_user_id=auth["user_id"]
    )

    return processed


@router.get("/projects/{project_id}/requests/{request_id}/export", response_model=DSARExportResponse)
async def get_export_data(
    project_id: int,
    request_id: int,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db)
):
    """Get export data for a completed export request (admin)."""
    dsar_request = db.query(MessagingDSARRequest).filter(
        MessagingDSARRequest.id == request_id,
        MessagingDSARRequest.project_id == project_id
    ).first()

    if not dsar_request:
        raise HTTPException(status_code=404, detail="DSAR request not found")

    if dsar_request.request_type != DSARRequestType.export:
        raise HTTPException(status_code=400, detail="Not an export request")

    if dsar_request.status != DSARRequestStatus.completed:
        raise HTTPException(status_code=400, detail="Export not yet completed")

    return DSARExportResponse(
        success=True,
        request_id=dsar_request.id,
        data=dsar_request.result_data,
        message="Export data retrieved successfully"
    )


@router.get("/projects/{project_id}/pending-count")
async def get_pending_count(
    project_id: int,
    _auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db)
):
    """Get count of pending DSAR requests (admin)."""
    count = db.query(MessagingDSARRequest).filter(
        MessagingDSARRequest.project_id == project_id,
        MessagingDSARRequest.status == DSARRequestStatus.pending
    ).count()

    return {"pending_count": count}
