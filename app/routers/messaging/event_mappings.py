"""
Event Mappings Router
CRUD operations for mapping events to destinations.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.models.messaging import (
    MessagingEventMapping, MessagingEventSchema, MessagingDestination
)
from app.schemas.messaging import (
    EventMappingCreate, EventMappingUpdate, EventMappingResponse, EventMappingDetailResponse,
    EventSchemaResponse, DestinationResponse
)
from app.routers.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/projects/{project_id}/messaging/event-mappings", tags=["messaging-event-mappings"])


@router.get("", response_model=List[EventMappingResponse])
async def list_event_mappings(
    project_id: int,
    event_schema_id: Optional[int] = None,
    destination_id: Optional[int] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all event mappings for a project."""
    query = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.project_id == project_id
    )

    if event_schema_id:
        query = query.filter(MessagingEventMapping.event_schema_id == event_schema_id)

    if destination_id:
        query = query.filter(MessagingEventMapping.destination_id == destination_id)

    if is_active is not None:
        query = query.filter(MessagingEventMapping.is_active == is_active)

    mappings = query.order_by(
        MessagingEventMapping.destination_id,
        MessagingEventMapping.event_schema_id
    ).offset(skip).limit(limit).all()

    return mappings


@router.post("/seed-defaults")
async def seed_default_event_mappings(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create default ad-relevant mappings for existing destinations."""
    from app.services.messaging.destination_dispatcher import destination_dispatcher

    destinations = db.query(MessagingDestination).filter(
        MessagingDestination.project_id == project_id
    ).all()

    before = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.project_id == project_id
    ).count()

    for destination in destinations:
        destination_dispatcher.ensure_default_mappings(db, destination)

    db.commit()

    after = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.project_id == project_id
    ).count()

    return {"created": max(after - before, 0), "total": after}


@router.get("/{mapping_id}", response_model=EventMappingDetailResponse)
async def get_event_mapping(
    project_id: int,
    mapping_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific event mapping with related objects."""
    mapping = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.id == mapping_id,
        MessagingEventMapping.project_id == project_id
    ).first()

    if not mapping:
        raise HTTPException(status_code=404, detail="Event mapping not found")

    # Get related objects
    event_schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.id == mapping.event_schema_id
    ).first()

    destination = db.query(MessagingDestination).filter(
        MessagingDestination.id == mapping.destination_id
    ).first()

    return EventMappingDetailResponse(
        id=mapping.id,
        project_id=mapping.project_id,
        event_schema_id=mapping.event_schema_id,
        destination_id=mapping.destination_id,
        destination_event_name=mapping.destination_event_name,
        property_mappings=mapping.property_mappings,
        provider_settings=mapping.provider_settings,
        is_active=mapping.is_active,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
        event_schema=EventSchemaResponse.model_validate(event_schema) if event_schema else None,
        destination=DestinationResponse.model_validate(destination) if destination else None
    )


@router.post("", response_model=EventMappingResponse)
async def create_event_mapping(
    project_id: int,
    data: EventMappingCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new event mapping."""
    # Verify event schema exists
    event_schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.id == data.event_schema_id,
        MessagingEventSchema.project_id == project_id
    ).first()

    if not event_schema:
        raise HTTPException(status_code=404, detail="Event schema not found")

    # Verify destination exists
    destination = db.query(MessagingDestination).filter(
        MessagingDestination.id == data.destination_id,
        MessagingDestination.project_id == project_id
    ).first()

    if not destination:
        raise HTTPException(status_code=404, detail="Destination not found")

    # Check for existing mapping
    existing = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.event_schema_id == data.event_schema_id,
        MessagingEventMapping.destination_id == data.destination_id
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Mapping already exists for this event-destination pair"
        )

    # Convert property mappings to dict format
    property_mappings = None
    if data.property_mappings:
        property_mappings = [pm.model_dump() for pm in data.property_mappings]

    mapping = MessagingEventMapping(
        project_id=project_id,
        event_schema_id=data.event_schema_id,
        destination_id=data.destination_id,
        destination_event_name=data.destination_event_name,
        property_mappings=property_mappings,
        provider_settings=data.provider_settings,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)

    return mapping


@router.put("/{mapping_id}", response_model=EventMappingResponse)
async def update_event_mapping(
    project_id: int,
    mapping_id: int,
    data: EventMappingUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an event mapping."""
    mapping = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.id == mapping_id,
        MessagingEventMapping.project_id == project_id
    ).first()

    if not mapping:
        raise HTTPException(status_code=404, detail="Event mapping not found")

    if data.destination_event_name is not None:
        mapping.destination_event_name = data.destination_event_name

    if data.property_mappings is not None:
        mapping.property_mappings = [pm.model_dump() for pm in data.property_mappings]

    if data.provider_settings is not None:
        mapping.provider_settings = data.provider_settings

    if data.is_active is not None:
        mapping.is_active = data.is_active

    db.commit()
    db.refresh(mapping)

    return mapping


@router.delete("/{mapping_id}")
async def delete_event_mapping(
    project_id: int,
    mapping_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an event mapping."""
    mapping = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.id == mapping_id,
        MessagingEventMapping.project_id == project_id
    ).first()

    if not mapping:
        raise HTTPException(status_code=404, detail="Event mapping not found")

    db.delete(mapping)
    db.commit()

    return {"success": True, "message": "Event mapping deleted"}


@router.get("/by-destination/{destination_id}", response_model=List[EventMappingResponse])
async def get_mappings_by_destination(
    project_id: int,
    destination_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all mappings for a specific destination."""
    mappings = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.project_id == project_id,
        MessagingEventMapping.destination_id == destination_id,
        MessagingEventMapping.is_active == True
    ).all()

    return mappings


@router.get("/by-event/{event_schema_id}", response_model=List[EventMappingResponse])
async def get_mappings_by_event(
    project_id: int,
    event_schema_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all mappings for a specific event schema."""
    mappings = db.query(MessagingEventMapping).filter(
        MessagingEventMapping.project_id == project_id,
        MessagingEventMapping.event_schema_id == event_schema_id,
        MessagingEventMapping.is_active == True
    ).all()

    return mappings
