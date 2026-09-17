"""
Event Actions API Router

CRUD endpoints for managing Event Action automation rules.
"""
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session
from sqlalchemy import and_
from typing import List, Optional
from datetime import datetime

from app.database import get_db
from app.models import (
    EventAction, EventActionExecution, ScheduledEventAction,
    Funnel, FunnelEnrollment,
    Project, User
)
from app.models.messaging import MessagingEvent, MessagingEventSchema
from app.routers.auth import get_current_user
from app.schemas.event_actions import (
    EventActionCreate, EventActionUpdate, EventActionTest,
    EventActionResponse, EventActionListResponse,
    EventActionExecutionResponse, ScheduledActionResponse,
    TestResultResponse, ActionTypesResponse, ActionTypeInfo,
    EventActionTrigger, TriggerResultResponse
)
from app.schemas.orchestration_attention import AttentionActivationApproval
from app.services.event_actions.conditions import ConditionEvaluator
from app.services.event_actions.funnel_compiler import (
    compile_event_action,
    delete_system_funnel,
)

router = APIRouter(prefix="/event-actions", tags=["event-actions"])


def _ensure_attention_activation(
    db: Session,
    event_action: EventAction,
    impact_fingerprint: str | None = None,
) -> dict:
    from app.services.orchestration_impact_service import (
        OrchestrationImpactError,
        OrchestrationImpactService,
    )
    try:
        service = OrchestrationImpactService(db)
        if impact_fingerprint is not None:
            return service.ensure_approved(
                "event_action", event_action, impact_fingerprint,
            )
        return service.ensure_can_activate("event_action", event_action)
    except OrchestrationImpactError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "orchestration_impact_blocked",
                "message": "Resolve attention impacts before activating this Event Action.",
                "impact": exc.report,
            },
        ) from exc


def get_project_with_access(project_id: int, db: Session, current_user: User) -> Project:
    """Verify user has access to project"""
    if not current_user.workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace"
        )

    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == current_user.workspace_id,
        Project.is_active == True
    ).first()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found or access denied"
        )

    return project


# List event actions for a project
@router.get("/projects/{project_id}", response_model=EventActionListResponse)
def list_event_actions(
    project_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trigger_event: Optional[str] = None,
    is_active: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all event actions for a project"""
    project = get_project_with_access(project_id, db, current_user)

    query = db.query(EventAction).filter(EventAction.project_id == project_id)

    if trigger_event:
        query = query.filter(EventAction.trigger_event == trigger_event)
    if is_active is not None:
        query = query.filter(EventAction.is_active == is_active)

    total = query.count()
    items = query.order_by(EventAction.priority.desc(), EventAction.created_at.desc()) \
        .offset((page - 1) * page_size) \
        .limit(page_size) \
        .all()

    return EventActionListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size
    )


# Create event action
@router.post("/projects/{project_id}", response_model=EventActionResponse, status_code=status.HTTP_201_CREATED)
def create_event_action(
    project_id: int,
    data: EventActionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new event action"""
    project = get_project_with_access(project_id, db, current_user)
    if data.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Create Event Actions inactive, preview attention impact, then activate with its fingerprint",
        )

    # Convert pydantic models to dicts for JSON columns
    conditions = [c.dict() for c in data.conditions]
    actions = [a.dict() for a in data.actions]
    stop_conditions = [s.dict() for s in data.stop_conditions]

    event_action = EventAction(
        project_id=project_id,
        name=data.name,
        description=data.description,
        trigger_event=data.trigger_event,
        purpose_key=data.purpose_key,
        attention_policy=(data.attention_policy.model_dump(mode="json") if data.attention_policy else None),
        conditions=conditions,
        actions=actions,
        stop_conditions=stop_conditions,
        is_active=data.is_active,
        priority=data.priority,
        cooldown_seconds=data.cooldown_seconds,
        react_to_delivery=data.react_to_delivery,
        lane=data.lane.value,
    )

    db.add(event_action)
    db.flush()
    db.commit()
    db.refresh(event_action)

    compile_event_action(db, event_action)
    db.commit()
    db.refresh(event_action)

    return event_action


# Get single event action
@router.get("/{action_id}", response_model=EventActionResponse)
def get_event_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get event action by ID"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    requested = data.model_dump(exclude_unset=True)
    if requested.get("is_active") is True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use the impact-gated activation endpoint",
        )
    material_changes = set(requested) - {"is_active"}
    if event_action.is_active and material_changes:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Pause this Event Action before changing its active definition",
        )

    return event_action


@router.get("/{action_id}/attention-impact")
def preview_event_action_attention_impact(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()
    if not event_action:
        raise HTTPException(status_code=404, detail="Event action not found")
    get_project_with_access(event_action.project_id, db, current_user)
    from app.services.orchestration_impact_service import OrchestrationImpactService

    return OrchestrationImpactService(db).preview("event_action", event_action)


# Update event action
@router.put("/{action_id}", response_model=EventActionResponse)
def update_event_action(
    action_id: int,
    data: EventActionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    # Update fields
    update_data = data.dict(exclude_unset=True)

    # Convert nested models to dicts
    if "conditions" in update_data and update_data["conditions"] is not None:
        update_data["conditions"] = [c.dict() if hasattr(c, 'dict') else c for c in update_data["conditions"]]
    if "actions" in update_data and update_data["actions"] is not None:
        update_data["actions"] = [a.dict() if hasattr(a, 'dict') else a for a in update_data["actions"]]
    if "stop_conditions" in update_data and update_data["stop_conditions"] is not None:
        update_data["stop_conditions"] = [s.dict() if hasattr(s, 'dict') else s for s in update_data["stop_conditions"]]
    if "lane" in update_data and hasattr(update_data["lane"], "value"):
        update_data["lane"] = update_data["lane"].value

    for field, value in update_data.items():
        setattr(event_action, field, value)

    event_action.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event_action)

    compile_event_action(db, event_action)
    db.commit()
    db.refresh(event_action)

    return event_action


# Delete event action
@router.delete("/{action_id}")
def delete_event_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    delete_system_funnel(db, event_action.id)
    db.delete(event_action)
    db.commit()

    return {"message": "Event action deleted successfully"}


# Activate event action
@router.post("/{action_id}/activate")
def activate_event_action(
    action_id: int,
    approval: AttentionActivationApproval,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Activate event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    event_action.is_active = True
    event_action.updated_at = datetime.utcnow()
    _ensure_attention_activation(db, event_action, approval.impact_fingerprint)
    db.commit()
    db.refresh(event_action)

    compile_event_action(db, event_action)
    db.commit()

    return {"message": "Event action activated", "is_active": True}


# Pause event action
@router.post("/{action_id}/pause")
def pause_event_action(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Pause event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    event_action.is_active = False
    event_action.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event_action)

    compile_event_action(db, event_action)
    db.commit()

    return {"message": "Event action paused", "is_active": False}


# Get execution history
@router.get("/{action_id}/executions", response_model=List[EventActionExecutionResponse])
def get_executions(
    action_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status_filter: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get execution history for an event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    query = db.query(EventActionExecution).filter(
        EventActionExecution.event_action_id == action_id
    )

    if status_filter:
        query = query.filter(EventActionExecution.status == status_filter)

    executions = query.order_by(EventActionExecution.started_at.desc()) \
        .offset(offset) \
        .limit(limit) \
        .all()

    return executions


# Get scheduled actions
@router.get("/{action_id}/scheduled", response_model=List[ScheduledActionResponse])
def get_scheduled_actions(
    action_id: int,
    status_filter: str = Query("pending", description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get scheduled actions for an event action"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    query = db.query(ScheduledEventAction).filter(
        ScheduledEventAction.event_action_id == action_id
    )

    if status_filter:
        query = query.filter(ScheduledEventAction.status == status_filter)

    scheduled = query.order_by(ScheduledEventAction.scheduled_for.asc()) \
        .limit(limit) \
        .all()

    return scheduled


# Cancel scheduled action
@router.post("/scheduled/{scheduled_id}/cancel")
def cancel_scheduled_action(
    scheduled_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Cancel a scheduled action"""
    scheduled = db.query(ScheduledEventAction).filter(
        ScheduledEventAction.id == scheduled_id
    ).first()

    if not scheduled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled action not found"
        )

    # Verify access
    get_project_with_access(scheduled.project_id, db, current_user)

    if scheduled.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel scheduled action with status '{scheduled.status}'"
        )

    scheduled.status = "cancelled"
    scheduled.cancelled_at = datetime.utcnow()
    scheduled.cancel_reason = "Manually cancelled by user"
    db.commit()

    return {"message": "Scheduled action cancelled", "id": scheduled_id}


# Test event action
@router.post("/{action_id}/test", response_model=TestResultResponse)
def test_event_action(
    action_id: int,
    data: EventActionTest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Test event action with sample event data"""
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    # Check if trigger event matches
    matched = data.event_name == event_action.trigger_event

    if not matched:
        return TestResultResponse(
            success=True,
            matched=False,
            conditions_passed=False,
            actions_would_execute=[],
            message=f"Event '{data.event_name}' does not match trigger '{event_action.trigger_event}'"
        )

    # Evaluate conditions
    evaluator = ConditionEvaluator()
    event_data = {
        "event_name": data.event_name,
        "properties": data.properties
    }

    conditions_passed = evaluator.evaluate_all(
        event_action.conditions or [],
        event_data,
        data.user_data
    )

    if not conditions_passed:
        return TestResultResponse(
            success=True,
            matched=True,
            conditions_passed=False,
            actions_would_execute=[],
            message="Conditions not satisfied"
        )

    # List actions that would execute
    actions_would_execute = []
    for idx, action in enumerate(event_action.actions):
        actions_would_execute.append({
            "index": idx,
            "type": action.get("type"),
            "config": action.get("config"),
            "delay_seconds": action.get("delay_seconds", 0),
            "would_schedule": action.get("delay_seconds", 0) > 0
        })

    return TestResultResponse(
        success=True,
        matched=True,
        conditions_passed=True,
        actions_would_execute=actions_would_execute,
        message=f"Would execute {len(actions_would_execute)} action(s)"
    )


# Manually trigger event action
@router.post("/{action_id}/trigger", response_model=TriggerResultResponse)
async def trigger_event_action(
    action_id: int,
    data: EventActionTrigger = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Manually trigger an event action (fire now)"""
    from app.services.event_actions.engine import event_action_engine

    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()

    if not event_action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event action not found"
        )

    # Verify access
    get_project_with_access(event_action.project_id, db, current_user)

    if data is None:
        data = EventActionTrigger()

    # Create a synthetic MessagingEvent
    synthetic_event = MessagingEvent(
        project_id=event_action.project_id,
        event_name=event_action.trigger_event,
        properties=data.properties,
        source="manual_trigger",
        user_id=None
    )
    db.add(synthetic_event)
    db.flush()

    # Build event_data and user_data
    event_data = {
        "event_name": synthetic_event.event_name,
        "properties": synthetic_event.properties or {},
        "source": "manual_trigger",
        "created_at": datetime.utcnow().isoformat()
    }

    user_data = data.user_data

    try:
        execution = await event_action_engine._process_action(
            db=db,
            action=event_action,
            event=synthetic_event,
            event_data=event_data,
            user=None,
            user_data=user_data
        )
        db.commit()

        if execution:
            return TriggerResultResponse(
                success=execution.status in ("success", "partial"),
                status=execution.status,
                actions_executed=execution.actions_executed or [],
                message=f"Executed {len(execution.actions_executed or [])} action(s)",
                error=execution.error_message
            )
        else:
            return TriggerResultResponse(
                success=False,
                status="skipped",
                actions_executed=[],
                message="Action was skipped (cooldown or conditions not met)"
            )
    except Exception as e:
        db.rollback()
        return TriggerResultResponse(
            success=False,
            status="failed",
            actions_executed=[],
            message=f"Trigger failed: {str(e)}",
            error=str(e)
        )


# Get available action types
@router.get("/meta/action-types", response_model=ActionTypesResponse)
def get_action_types(
    current_user: User = Depends(get_current_user)
):
    """Get list of available action types with their schemas"""
    action_types = [
        ActionTypeInfo(
            type="send_template",
            description="Send message using a MessagingTemplate",
            config_schema={
                "template_id": {"type": "integer", "required": True},
                "channel_id": {"type": "integer", "required": False},
                "recipient_field": {"type": "string", "required": False, "default": "email"},
                "variable_mapping": {"type": "object", "required": False, "description": "Maps template variables to event property paths"},
                "use_direct_smtp": {"type": "boolean", "required": False, "default": False, "description": "Send directly via project SMTP"}
            }
        ),
        ActionTypeInfo(
            type="assign_chatbot",
            description="Assign user to a chatbot session",
            config_schema={
                "chatbot_id": {"type": "integer", "required": True},
                "greeting_message": {"type": "string", "required": False}
            }
        ),
        ActionTypeInfo(
            type="assign_agent",
            description="Create support ticket for human agent",
            config_schema={
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"], "default": "medium"},
                "tags": {"type": "array", "items": "string", "required": False},
                "reason": {"type": "string", "required": False}
            }
        ),
        ActionTypeInfo(
            type="update_user",
            description="Update MessagingUser properties",
            config_schema={
                "properties": {"type": "object", "required": True}
            }
        ),
        ActionTypeInfo(
            type="add_tag",
            description="Add tag to user properties",
            config_schema={
                "tag": {"type": "string", "required": True}
            }
        ),
        ActionTypeInfo(
            type="webhook",
            description="Call external webhook",
            config_schema={
                "url": {"type": "string", "required": True},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH"], "default": "POST"},
                "headers": {"type": "object", "required": False},
                "body_template": {"type": "object", "required": False}
            }
        ),
        ActionTypeInfo(
            type="add_to_funnel",
            description="Add user to email funnel",
            config_schema={
                "funnel_id": {"type": "integer", "required": True}
            }
        ),
        ActionTypeInfo(
            type="run_graph",
            description="Execute LangGraph workflow",
            config_schema={
                "graph_id": {"type": "string", "required": True},
                "input_mapping": {"type": "object", "required": False}
            }
        )
    ]

    return ActionTypesResponse(action_types=action_types)


# Get event properties from schema (fast) with fallback to recent events
@router.get("/meta/event-properties/{event_name}")
def get_event_properties(
    event_name: str,
    project_id: int = Query(..., description="Project ID"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get known properties for an event type.
    Checks schema table first (fast), falls back to scanning recent events.
    """
    get_project_with_access(project_id, db, current_user)

    # Fast path: check schema table
    schema = db.query(MessagingEventSchema).filter(
        MessagingEventSchema.project_id == project_id,
        MessagingEventSchema.event_name == event_name,
    ).first()

    if schema and schema.properties_schema:
        schema_props = schema.properties_schema.get("properties", {})
        if schema_props:
            properties = []
            for key in sorted(schema_props.keys()):
                prop_def = schema_props[key]
                properties.append({
                    "key": key,
                    "path": f"properties.{key}",
                    "sample_value": f"[{prop_def.get('type', 'string')}]",
                })
            return {
                "event_name": event_name,
                "properties": properties,
                "event_count": -1,  # from schema, not raw events
                "source": "schema",
            }

    # Fallback: query last 100 events with this event name
    events = db.query(MessagingEvent).filter(
        MessagingEvent.project_id == project_id,
        MessagingEvent.event_name == event_name
    ).order_by(MessagingEvent.created_at.desc()).limit(100).all()

    # Extract unique property keys from all events
    property_keys = set()
    sample_values = {}

    for event in events:
        if event.properties:
            for key, value in event.properties.items():
                property_keys.add(key)
                # Store a sample value (non-empty) for preview
                if key not in sample_values and value is not None:
                    # Truncate long strings for preview
                    if isinstance(value, str) and len(value) > 50:
                        sample_values[key] = value[:47] + "..."
                    elif isinstance(value, (dict, list)):
                        sample_values[key] = f"[{type(value).__name__}]"
                    else:
                        sample_values[key] = value

    # Sort alphabetically and add common prefixes for nested access
    sorted_keys = sorted(property_keys)

    # Build response with suggested paths
    properties = []
    for key in sorted_keys:
        properties.append({
            "key": key,
            "path": f"properties.{key}",
            "sample_value": sample_values.get(key)
        })

    return {
        "event_name": event_name,
        "properties": properties,
        "event_count": len(events),
        "source": "raw_events",
    }


# Get condition operators
@router.get("/meta/operators")
def get_operators(
    current_user: User = Depends(get_current_user)
):
    """Get list of available condition operators"""
    return {
        "operators": [
            {"value": "==", "label": "Equals", "types": ["string", "number", "boolean"]},
            {"value": "!=", "label": "Not equals", "types": ["string", "number", "boolean"]},
            {"value": "<", "label": "Less than", "types": ["number"]},
            {"value": "<=", "label": "Less than or equal", "types": ["number"]},
            {"value": ">", "label": "Greater than", "types": ["number"]},
            {"value": ">=", "label": "Greater than or equal", "types": ["number"]},
            {"value": "contains", "label": "Contains", "types": ["string"]},
            {"value": "not_contains", "label": "Does not contain", "types": ["string"]},
            {"value": "starts_with", "label": "Starts with", "types": ["string"]},
            {"value": "ends_with", "label": "Ends with", "types": ["string"]},
            {"value": "matches", "label": "Matches regex", "types": ["string"]},
            {"value": "not_matches", "label": "Does not match regex", "types": ["string"]},
            {"value": "in", "label": "In list", "types": ["string", "number"]},
            {"value": "not_in", "label": "Not in list", "types": ["string", "number"]},
            {"value": "exists", "label": "Exists", "types": ["any"]},
            {"value": "not_exists", "label": "Does not exist", "types": ["any"]},
            {"value": "is_empty", "label": "Is empty", "types": ["string", "array", "object"]},
            {"value": "is_not_empty", "label": "Is not empty", "types": ["string", "array", "object"]}
        ]
    }


# Resolve the mirrored system funnel id for an EventAction.
@router.get("/{action_id}/funnel-id")
def get_event_action_funnel_id(
    action_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    event_action = db.query(EventAction).filter(EventAction.id == action_id).first()
    if not event_action:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event action not found")
    get_project_with_access(event_action.project_id, db, current_user)

    funnel = db.query(Funnel).filter(Funnel.event_action_id == action_id).first()
    if not funnel:
        # Lazy compile if the rule pre-dates the unification migration.
        funnel = compile_event_action(db, event_action)
        db.commit()
    return {"funnel_id": funnel.id}


# Project-wide Event Actions analytics summary.
@router.get("/projects/{project_id}/analytics/summary")
def event_actions_analytics_summary(
    project_id: int,
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_with_access(project_id, db, current_user)
    from datetime import timedelta
    from sqlalchemy import func

    since = datetime.utcnow() - timedelta(days=days)

    rules_active = (
        db.query(func.count(EventAction.id))
        .filter(EventAction.project_id == project_id, EventAction.is_active == True)
        .scalar() or 0
    )
    rules_total = (
        db.query(func.count(EventAction.id))
        .filter(EventAction.project_id == project_id)
        .scalar() or 0
    )

    enrollment_base = (
        db.query(FunnelEnrollment)
        .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
        .filter(
            Funnel.project_id == project_id,
            Funnel.source == "event_action",
            FunnelEnrollment.enrolled_at >= since,
        )
    )
    total_executions = enrollment_base.count()
    completed = enrollment_base.filter(FunnelEnrollment.status == "completed").count()
    exited = enrollment_base.filter(FunnelEnrollment.status == "exited").count()
    error_rate = round((exited / total_executions) * 100, 2) if total_executions else 0.0

    top_rows = (
        db.query(
            Funnel.event_action_id,
            EventAction.name,
            func.count(FunnelEnrollment.id).label("volume"),
        )
        .join(Funnel, FunnelEnrollment.funnel_id == Funnel.id)
        .join(EventAction, EventAction.id == Funnel.event_action_id)
        .filter(
            Funnel.project_id == project_id,
            Funnel.source == "event_action",
            FunnelEnrollment.enrolled_at >= since,
        )
        .group_by(Funnel.event_action_id, EventAction.name)
        .order_by(func.count(FunnelEnrollment.id).desc())
        .limit(5)
        .all()
    )
    top_rules = [
        {"event_action_id": r.event_action_id, "name": r.name, "volume": int(r.volume)}
        for r in top_rows
    ]

    return {
        "days": days,
        "rules_active": rules_active,
        "rules_total": rules_total,
        "total_executions": total_executions,
        "completed": completed,
        "exited": exited,
        "error_rate": error_rate,
        "top_rules": top_rules,
    }
