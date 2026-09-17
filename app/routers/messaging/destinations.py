"""
Destinations Router
CRUD operations for analytics destination configurations.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any

from app.database import get_db
from app.models.messaging import MessagingDestination, DestinationType, MessagingDestinationDelivery, MessagingEvent
from app.schemas.messaging import (
    DestinationCreate, DestinationUpdate, DestinationResponse, DestinationDetailResponse,
    DestinationType as DestTypeSchema, DestinationDeliveryResponse
)
from app.services.messaging.destination_config import destination_config
from app.services.messaging.destination_dispatcher import destination_dispatcher
from app.routers.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/projects/{project_id}/messaging/destinations", tags=["messaging-destinations"])

IMPORTANT_DELIVERY_EVENT_NAMES = {
    "trial_started",
    "billing.checkout_started",
    "purchase",
    "billing.payment_success",
    "payment_success",
    "payment_confirmed",
    "editor_export_success",
    "lead_captured",
}


@router.get("", response_model=List[DestinationResponse])
async def list_destinations(
    project_id: int,
    destination_type: Optional[DestTypeSchema] = None,
    is_active: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all destinations for a project."""
    query = db.query(MessagingDestination).filter(
        MessagingDestination.project_id == project_id
    )

    if destination_type:
        query = query.filter(MessagingDestination.destination_type == destination_type.value)

    if is_active is not None:
        query = query.filter(MessagingDestination.is_active == is_active)

    destinations = query.order_by(MessagingDestination.name).all()
    return destinations


@router.get("/templates")
async def get_templates(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get available destination templates."""
    return {"templates": destination_config.get_all_templates()}


@router.get("/deliveries/recent", response_model=List[DestinationDeliveryResponse])
async def list_recent_deliveries(
    project_id: int,
    destination_id: Optional[int] = Query(None),
    event_id: Optional[int] = Query(None),
    event_name: Optional[str] = Query(None),
    important_only: bool = Query(False),
    status: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List recent server-side destination delivery attempts."""
    query = db.query(MessagingDestinationDelivery).filter(
        MessagingDestinationDelivery.project_id == project_id
    )
    if event_name or important_only:
        query = query.join(MessagingEvent, MessagingDestinationDelivery.event_id == MessagingEvent.id)
    if destination_id:
        query = query.filter(MessagingDestinationDelivery.destination_id == destination_id)
    if event_id:
        query = query.filter(MessagingDestinationDelivery.event_id == event_id)
    if event_name:
        query = query.filter(MessagingEvent.event_name.ilike(f"%{event_name}%"))
    if important_only:
        query = query.filter(MessagingEvent.event_name.in_(IMPORTANT_DELIVERY_EVENT_NAMES))
    if status:
        query = query.filter(MessagingDestinationDelivery.status == status)
    return query.order_by(MessagingDestinationDelivery.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/{destination_id}", response_model=DestinationDetailResponse)
async def get_destination(
    project_id: int,
    destination_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific destination with decrypted config."""
    dest = db.query(MessagingDestination).filter(
        MessagingDestination.id == destination_id,
        MessagingDestination.project_id == project_id
    ).first()

    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    # Decrypt config
    config = destination_config.decrypt_config(dest.config_encrypted)

    # Mask sensitive fields
    template = destination_config.get_destination_template(dest.destination_type.value)
    if template and config:
        for field_name, field_def in template.get('fields', {}).items():
            if field_def.get('sensitive') and field_name in config:
                config[field_name] = "***" if config[field_name] else None

    return DestinationDetailResponse(
        id=dest.id,
        project_id=dest.project_id,
        destination_type=dest.destination_type.value,
        name=dest.name,
        is_active=dest.is_active,
        consent_required=dest.consent_required,
        config=config,
        created_at=dest.created_at,
        updated_at=dest.updated_at
    )


@router.post("", response_model=DestinationResponse)
async def create_destination(
    project_id: int,
    data: DestinationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new destination."""
    # Validate config against template
    errors = destination_config.validate_destination_config(
        data.destination_type.value,
        data.config
    )
    if errors:
        raise HTTPException(status_code=400, detail={"validation_errors": errors})

    # Get default consent if not provided
    consent = data.consent_required
    if consent is None:
        consent = destination_config.get_default_consent(data.destination_type.value)

    # Encrypt config
    encrypted_config = destination_config.encrypt_config(data.config)

    dest = MessagingDestination(
        project_id=project_id,
        destination_type=data.destination_type.value,
        name=data.name,
        config_encrypted=encrypted_config,
        consent_required=consent
    )
    db.add(dest)
    db.flush()
    destination_dispatcher.ensure_default_mappings(db, dest)
    db.commit()
    db.refresh(dest)

    return dest


@router.get("/{destination_id}/deliveries", response_model=List[DestinationDeliveryResponse])
async def list_destination_deliveries(
    project_id: int,
    destination_id: int,
    status: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List recent delivery attempts for a destination."""
    query = db.query(MessagingDestinationDelivery).filter(
        MessagingDestinationDelivery.project_id == project_id,
        MessagingDestinationDelivery.destination_id == destination_id,
    )
    if status:
        query = query.filter(MessagingDestinationDelivery.status == status)
    return query.order_by(MessagingDestinationDelivery.created_at.desc()).limit(limit).all()


@router.put("/{destination_id}", response_model=DestinationResponse)
async def update_destination(
    project_id: int,
    destination_id: int,
    data: DestinationUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a destination."""
    dest = db.query(MessagingDestination).filter(
        MessagingDestination.id == destination_id,
        MessagingDestination.project_id == project_id
    ).first()

    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    if data.name is not None:
        dest.name = data.name

    if data.config is not None:
        # Merge with existing config before validation so edits that preserve
        # masked sensitive fields, or submit only changed fields, are accepted.
        existing_config = destination_config.decrypt_config(dest.config_encrypted) or {}
        template = destination_config.get_destination_template(dest.destination_type.value) or {}
        fields = template.get("fields", {})
        clean_config = {}
        for key, value in data.config.items():
            if fields.get(key, {}).get("sensitive") and value == "***":
                continue
            clean_config[key] = value
        merged_config = {**existing_config, **clean_config}

        errors = destination_config.validate_destination_config(
            dest.destination_type.value,
            merged_config
        )
        if errors:
            raise HTTPException(status_code=400, detail={"validation_errors": errors})

        dest.config_encrypted = destination_config.encrypt_config(merged_config)

    if data.consent_required is not None:
        dest.consent_required = data.consent_required

    if data.is_active is not None:
        dest.is_active = data.is_active

    db.commit()
    db.refresh(dest)

    return dest


@router.delete("/{destination_id}")
async def delete_destination(
    project_id: int,
    destination_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a destination."""
    dest = db.query(MessagingDestination).filter(
        MessagingDestination.id == destination_id,
        MessagingDestination.project_id == project_id
    ).first()

    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    db.delete(dest)
    db.commit()

    return {"success": True, "message": "Destination deleted"}


@router.post("/{destination_id}/test")
async def test_destination(
    project_id: int,
    destination_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Test a destination connection."""
    dest = db.query(MessagingDestination).filter(
        MessagingDestination.id == destination_id,
        MessagingDestination.project_id == project_id
    ).first()

    if not dest:
        raise HTTPException(status_code=404, detail="Destination not found")

    # For now, just validate config exists
    config = destination_config.decrypt_config(dest.config_encrypted)
    if not config:
        return {
            "success": False,
            "message": "No configuration found"
        }

    # TODO: Implement actual destination testing (API calls, etc.)
    return {
        "success": True,
        "message": f"Configuration for {dest.destination_type.value} is valid"
    }
