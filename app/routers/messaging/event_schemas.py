"""
Event Schemas Router
CRUD operations for event schema definitions.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional, Dict, Any

from app.database import get_db
from app.models.messaging import MessagingEventSchema, MessagingEvent
from app.schemas.messaging import (
    EventSchemaCreate, EventSchemaUpdate, EventSchemaResponse,
    ImportStandardEventsResponse
)
from app.services.messaging.event_schema_validator import event_schema_validator
from app.services.messaging.schema_discovery import schema_discovery
from app.routers.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/projects/{project_id}/messaging/event-schemas", tags=["messaging-event-schemas"])


@router.get("", response_model=List[EventSchemaResponse])
async def list_event_schemas(
    project_id: int,
    category: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all event schemas for a project."""
    query = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.project_id == project_id
    )

    if category:
        query = query.filter(MessagingEventSchema.category == category)

    if is_active is not None:
        query = query.filter(MessagingEventSchema.is_active == is_active)

    schemas = query.order_by(
        MessagingEventSchema.category,
        MessagingEventSchema.event_name
    ).offset(skip).limit(limit).all()

    return schemas


@router.get("/categories")
async def list_categories(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get unique categories for event schemas."""
    categories = db.query(MessagingEventSchema.category).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.category.isnot(None)
    ).distinct().all()

    return {"categories": [c[0] for c in categories if c[0]]}


@router.get("/{schema_id}", response_model=EventSchemaResponse)
async def get_event_schema(
    project_id: int,
    schema_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific event schema."""
    schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.id == schema_id,
        MessagingEventSchema.project_id == project_id
    ).first()

    if not schema:
        raise HTTPException(status_code=404, detail="Event schema not found")

    return schema


@router.post("", response_model=EventSchemaResponse)
async def create_event_schema(
    project_id: int,
    data: EventSchemaCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new event schema."""
    # Check for duplicate
    existing = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.event_name == data.event_name
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Event schema '{data.event_name}' already exists"
        )

    schema = MessagingEventSchema(
        project_id=project_id,
        event_name=data.event_name,
        display_name=data.display_name or data.event_name.replace("_", " ").title(),
        description=data.description,
        category=data.category,
        properties_schema=data.properties_schema,
        required_properties=data.required_properties,
        is_standard=False
    )
    db.add(schema)
    db.commit()
    db.refresh(schema)

    return schema


@router.put("/{schema_id}", response_model=EventSchemaResponse)
async def update_event_schema(
    project_id: int,
    schema_id: int,
    data: EventSchemaUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update an event schema."""
    schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.id == schema_id,
        MessagingEventSchema.project_id == project_id
    ).first()

    if not schema:
        raise HTTPException(status_code=404, detail="Event schema not found")

    if data.display_name is not None:
        schema.display_name = data.display_name
    if data.description is not None:
        schema.description = data.description
    if data.category is not None:
        schema.category = data.category
    if data.properties_schema is not None:
        schema.properties_schema = data.properties_schema
    if data.required_properties is not None:
        schema.required_properties = data.required_properties
    if data.is_active is not None:
        schema.is_active = data.is_active

    db.commit()
    db.refresh(schema)

    return schema


@router.delete("/{schema_id}")
async def delete_event_schema(
    project_id: int,
    schema_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete an event schema."""
    schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.id == schema_id,
        MessagingEventSchema.project_id == project_id
    ).first()

    if not schema:
        raise HTTPException(status_code=404, detail="Event schema not found")

    db.delete(schema)
    db.commit()

    return {"success": True, "message": "Event schema deleted"}


@router.post("/import-standard", response_model=ImportStandardEventsResponse)
async def import_standard_events(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Import GA4 standard event schemas."""
    # Count existing standard events
    existing_count = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.is_standard == True
    ).count()

    imported = await event_schema_validator.import_standard_events(db, project_id)

    total_standard = len(event_schema_validator.GA4_STANDARD_EVENTS)
    skipped = total_standard - len(imported)

    return ImportStandardEventsResponse(
        success=True,
        imported_count=len(imported),
        skipped_count=skipped,
        events=[s.event_name for s in imported]
    )


@router.get("/standard/list")
async def list_standard_events(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get list of available GA4 standard events."""
    return {
        "events": event_schema_validator.get_ga4_standard_schemas()
    }


@router.post("/import-json")
async def import_json_schemas(
    project_id: int,
    data: List[EventSchemaCreate],
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Bulk import event schemas from a JSON array.
    Upserts: creates new schemas, skips existing ones.
    """
    imported = 0
    skipped = 0

    for item in data:
        existing = db.query(MessagingEventSchema).filter(
            MessagingEventSchema.project_id == project_id,
            MessagingEventSchema.event_name == item.event_name
        ).first()

        if existing:
            skipped += 1
            continue

        schema = MessagingEventSchema(
            project_id=project_id,
            event_name=item.event_name,
            display_name=item.display_name or item.event_name.replace("_", " ").title(),
            description=item.description,
            category=item.category or "custom",
            properties_schema=item.properties_schema,
            required_properties=item.required_properties,
            is_standard=False,
        )
        db.add(schema)
        imported += 1

    if imported > 0:
        db.commit()

    return {"imported": imported, "skipped": skipped}


@router.post("/backfill")
async def backfill_schemas(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Scan existing events and create schema entries for any missing event names.
    Uses auto-discovery to infer categories and property types from sample events.
    """
    # Get all distinct event names from raw events
    raw_names = db.query(MessagingEvent.event_name).filter(
        MessagingEvent.project_id == project_id
    ).distinct().all()
    event_names = [name[0] for name in raw_names]

    # Get existing schema names
    existing_names = db.query(MessagingEventSchema.event_name).filter(
        MessagingEventSchema.project_id == project_id
    ).all()
    existing_set = {name[0] for name in existing_names}

    backfilled = 0
    for event_name in event_names:
        if event_name in existing_set:
            continue

        # Grab a sample event to infer properties
        sample = db.query(MessagingEvent).filter(
            MessagingEvent.project_id == project_id,
            MessagingEvent.event_name == event_name,
        ).order_by(MessagingEvent.created_at.desc()).first()

        sample_props = sample.properties if sample else None
        schema_discovery.discover_from_event(db, project_id, event_name, sample_props)
        backfilled += 1

    return {"backfilled": backfilled, "total_event_names": len(event_names)}
