"""
Smart Analytics API
High-performance event tracking for user-friendly visual analytics
"""

from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, validator
from typing import Dict, Any, Optional, List
from datetime import datetime
import json
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.security import get_current_user
from app.models.user import User

router = APIRouter(prefix="/smart-analytics", tags=["smart-analytics"])

class TrackingEvent(BaseModel):
    tenant_id: int
    event_type: str
    event_data: Dict[str, Any]
    
    @validator('event_type')
    def validate_event_type(cls, v):
        allowed_events = [
            'button_click', 'form_submit', 'page_visit', 'time_spent',
            'scroll_depth', 'file_download', 'custom_event'
        ]
        if v not in allowed_events:
            # Allow custom events but log them
            pass
        return v

class SchemaField(BaseModel):
    id: str
    fieldTypeId: str
    name: str
    emoji: str

class CreateSchemaRequest(BaseModel):
    tenant_id: int
    schema_name: str
    description: str
    fields: List[SchemaField]

class AutomationRule(BaseModel):
    tenant_id: int
    rule_name: str
    trigger: Dict[str, Any]
    actions: List[Dict[str, Any]]
    active: bool = True

@router.post("/track")
async def track_event(
    event: TrackingEvent,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID")
):
    """
    Super fast event tracking endpoint
    Optimized for high-frequency events from child websites
    """
    try:
        # Use tenant ID from header (universal SDK approach)
        if not x_tenant_id:
            raise HTTPException(400, "Missing tenant ID header")
        
        # Create event record with JSONB storage
        event_record = {
            "tenant_id": event.tenant_id,
            "user_id": event.event_data.get("user_id"),
            "event_data": {
                "event": event.event_type,
                **event.event_data,
                "received_at": datetime.utcnow().isoformat()
            },
            "created_at": datetime.utcnow()
        }
        
        # Fast insert to database
        query = text("""
            INSERT INTO tenant_events (tenant_id, user_id, event_data, created_at)
            VALUES (:tenant_id, :user_id, :event_data, :created_at)
            RETURNING id
        """)
        
        result = await db.execute(query, {
            "tenant_id": event.tenant_id,
            "user_id": event.event_data.get("user_id"),
            "event_data": json.dumps(event_record["event_data"]),
            "created_at": event_record["created_at"]
        })
        
        event_id = result.scalar()
        await db.commit()
        
        # Background processing (don't block the response)
        background_tasks.add_task(
            process_event_background,
            event.tenant_id,
            {**event_record, "id": event_id}
        )
        
        return {
            "status": "tracked",
            "event_id": event_id,
            "timestamp": event_record["created_at"].isoformat()
        }
        
    except Exception as e:
        # Never fail tracking requests - just log and return success
        # This ensures users' websites don't break
        print(f"Tracking error: {e}")  # In production, use proper logging
        return {
            "status": "queued",
            "message": "Event queued for processing"
        }

@router.post("/schema")
async def create_schema(
    schema_request: CreateSchemaRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new analytics schema for a user"""
    try:
        # Validate tenant ownership
        if schema_request.tenant_id != current_user.tenant_id:
            raise HTTPException(403, "Access denied")
        
        # Store schema in JSONB format
        query = text("""
            INSERT INTO tenant_schemas (tenant_id, schema_name, description, fields, created_at)
            VALUES (:tenant_id, :schema_name, :description, :fields, :created_at)
            RETURNING id
        """)
        
        result = await db.execute(query, {
            "tenant_id": schema_request.tenant_id,
            "schema_name": schema_request.schema_name,
            "description": schema_request.description,
            "fields": json.dumps([field.dict() for field in schema_request.fields]),
            "created_at": datetime.utcnow()
        })
        
        schema_id = result.scalar()
        await db.commit()
        
        # Generate API key for this schema
        api_key = f"smart_{schema_request.tenant_id}_{schema_id}_{hash(str(datetime.utcnow()))}"
        
        return {
            "schema_id": schema_id,
            "api_key": api_key,
            "snippet_url": f"/api/v1/smart-analytics/snippet/{schema_request.tenant_id}.js"
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to create schema: {str(e)}")

@router.get("/schema/{tenant_id}")
async def get_schema(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the current schema for a tenant"""
    if tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Access denied")
    
    query = text("""
        SELECT id, schema_name, description, fields, created_at
        FROM tenant_schemas 
        WHERE tenant_id = :tenant_id 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    
    result = await db.execute(query, {"tenant_id": tenant_id})
    schema = result.fetchone()
    
    if not schema:
        raise HTTPException(404, "No schema found")
    
    return {
        "schema_id": schema.id,
        "schema_name": schema.schema_name,
        "description": schema.description,
        "fields": json.loads(schema.fields),
        "created_at": schema.created_at.isoformat()
    }

@router.get("/snippet/{tenant_id}.js")
async def get_tracking_snippet(
    tenant_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Generate the JavaScript tracking snippet for a tenant"""
    # Get schema for this tenant
    query = text("""
        SELECT fields FROM tenant_schemas 
        WHERE tenant_id = :tenant_id 
        ORDER BY created_at DESC 
        LIMIT 1
    """)
    
    result = await db.execute(query, {"tenant_id": tenant_id})
    schema_row = result.fetchone()
    
    if not schema_row:
        raise HTTPException(404, "No schema found for tenant")
    
    fields = json.loads(schema_row.fields)
    api_key = f"smart_{tenant_id}_{hash(str(tenant_id))}"  # Simplified for demo
    
    # Generate the JavaScript snippet (same as in SDKSetup.tsx)
    snippet = generate_js_snippet(tenant_id, api_key, fields)
    
    from fastapi.responses import Response
    return Response(
        content=snippet,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"}
    )

@router.get("/dashboard/{tenant_id}")
async def get_dashboard_data(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get analytics dashboard data for a tenant"""
    if tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Access denied")
    
    # Get event counts by type
    query = text("""
        SELECT 
            event_data->>'event' as event_type,
            COUNT(*) as count,
            COUNT(DISTINCT user_id) as unique_users,
            DATE(created_at) as date
        FROM tenant_events 
        WHERE tenant_id = :tenant_id 
        AND created_at >= NOW() - INTERVAL '30 days'
        GROUP BY event_data->>'event', DATE(created_at)
        ORDER BY date DESC, count DESC
    """)
    
    result = await db.execute(query, {"tenant_id": tenant_id})
    events = result.fetchall()
    
    # Get top buttons/interactions
    query = text("""
        SELECT 
            event_data->>'button_text' as button_name,
            COUNT(*) as clicks
        FROM tenant_events 
        WHERE tenant_id = :tenant_id 
        AND event_data->>'event' = 'button_click'
        AND created_at >= NOW() - INTERVAL '7 days'
        GROUP BY event_data->>'button_text'
        ORDER BY clicks DESC
        LIMIT 10
    """)
    
    result = await db.execute(query, {"tenant_id": tenant_id})
    top_buttons = result.fetchall()
    
    # Get recent activity
    query = text("""
        SELECT 
            event_data->>'event' as event_type,
            event_data,
            created_at
        FROM tenant_events 
        WHERE tenant_id = :tenant_id 
        ORDER BY created_at DESC
        LIMIT 50
    """)
    
    result = await db.execute(query, {"tenant_id": tenant_id})
    recent_activity = result.fetchall()
    
    return {
        "events": [
            {
                "event_type": row.event_type,
                "count": row.count,
                "unique_users": row.unique_users,
                "date": row.date.isoformat() if row.date else None
            }
            for row in events
        ],
        "top_buttons": [
            {
                "button_name": row.button_name or "Unknown Button",
                "clicks": row.clicks
            }
            for row in top_buttons
        ],
        "recent_activity": [
            {
                "event_type": row.event_type,
                "data": json.loads(row.event_data) if isinstance(row.event_data, str) else row.event_data,
                "timestamp": row.created_at.isoformat()
            }
            for row in recent_activity
        ]
    }

@router.post("/automation")
async def create_automation_rule(
    rule: AutomationRule,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create an automation rule for a tenant"""
    if rule.tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Access denied")
    
    query = text("""
        INSERT INTO tenant_automations (tenant_id, rule_name, trigger, actions, active, created_at)
        VALUES (:tenant_id, :rule_name, :trigger, :actions, :active, :created_at)
        RETURNING id
    """)
    
    result = await db.execute(query, {
        "tenant_id": rule.tenant_id,
        "rule_name": rule.rule_name,
        "trigger": json.dumps(rule.trigger),
        "actions": json.dumps(rule.actions),
        "active": rule.active,
        "created_at": datetime.utcnow()
    })
    
    rule_id = result.scalar()
    await db.commit()
    
    return {"rule_id": rule_id, "status": "created"}

# Background processing functions
async def process_event_background(tenant_id: int, event_record: dict):
    """
    Process events in the background:
    - Check automation rules
    - Update real-time dashboard
    - Send notifications
    """
    try:
        # Check for automation rule triggers
        await check_automation_triggers(tenant_id, event_record)
        
        # Update real-time dashboard via WebSocket
        await notify_dashboard_update(tenant_id, event_record)
        
        # Check for achievements/milestones
        await check_achievements(tenant_id, event_record)
        
    except Exception as e:
        print(f"Background processing error: {e}")

async def check_automation_triggers(tenant_id: int, event_record: dict):
    """Check if this event should trigger any automation rules"""
    # This would check the tenant_automations table
    # and trigger actions like emails, webhooks, etc.
    pass

async def notify_dashboard_update(tenant_id: int, event_record: dict):
    """Send real-time update to dashboard via WebSocket"""
    # This would use WebSocket connection manager
    # to push updates to connected dashboards
    pass

async def check_achievements(tenant_id: int, event_record: dict):
    """Check if user reached any milestones for celebrations"""
    # This would check event counts and trigger celebrations
    pass

@router.get("/config/{tenant_id}")
async def get_tenant_config(
    tenant_id: int,
    db: AsyncSession = Depends(get_db)
):
    """Get live configuration for universal SDK"""
    try:
        # Get latest schema
        schema_query = text("""
            SELECT fields FROM tenant_schemas 
            WHERE tenant_id = :tenant_id 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        schema_result = await db.execute(schema_query, {"tenant_id": tenant_id})
        schema = schema_result.fetchone()
        
        # Get automation rules
        automation_query = text("""
            SELECT id, rule_name, trigger, actions 
            FROM tenant_automations 
            WHERE tenant_id = :tenant_id AND active = TRUE
        """)
        automation_result = await db.execute(automation_query, {"tenant_id": tenant_id})
        automations = automation_result.fetchall()
        
        # Get settings (if exists, otherwise use defaults)
        settings_query = text("""
            SELECT settings FROM tenant_settings 
            WHERE tenant_id = :tenant_id
        """)
        settings_result = await db.execute(settings_query, {"tenant_id": tenant_id})
        settings_row = settings_result.fetchone()
        
        # Build configuration object
        config = {
            "tenant_id": tenant_id,
            "schema": json.loads(schema.fields) if schema else [],
            "automations": [
                {
                    "id": rule.id,
                    "name": rule.rule_name,
                    "trigger": json.loads(rule.trigger) if isinstance(rule.trigger, str) else rule.trigger,
                    "actions": json.loads(rule.actions) if isinstance(rule.actions, str) else rule.actions
                }
                for rule in automations
            ],
            "showFeedback": True,
            "trackTime": True,
            "persistUsers": False,
            "testMode": False,
            "autoTrack": {
                "buttons": {"enabled": True, "selectors": "button, .btn, [data-track]"},
                "forms": {"enabled": True, "selectors": "form"},
                "pageViews": {"enabled": True}
            }
        }
        
        # Override with custom settings if they exist
        if settings_row:
            custom_settings = json.loads(settings_row.settings) if isinstance(settings_row.settings, str) else settings_row.settings
            config.update(custom_settings)
        
        return config
        
    except Exception as e:
        # Return minimal config if error occurs
        return {
            "tenant_id": tenant_id,
            "schema": [],
            "automations": [],
            "showFeedback": False,
            "trackTime": False,
            "persistUsers": False,
            "testMode": True,
            "autoTrack": {
                "buttons": {"enabled": True, "selectors": "button, .btn, [data-track]"},
                "forms": {"enabled": True, "selectors": "form"},
                "pageViews": {"enabled": True}
            }
        }

@router.get("/config-stream/{tenant_id}")
async def config_stream(tenant_id: int):
    """Server-sent events for live config updates"""
    
    async def event_generator():
        try:
            while True:
                # For now, just keep the connection alive
                # In a real implementation, you'd check for config changes
                # using Redis, WebSocket, or database polling
                yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
                await asyncio.sleep(30)  # Send heartbeat every 30 seconds
                
                # TODO: Implement actual config change detection
                # Example implementation would be:
                # if has_config_changed(tenant_id):
                #     config = await get_tenant_config(tenant_id)
                #     yield f"data: {json.dumps({'type': 'config_update', 'config': config})}\n\n"
                
        except asyncio.CancelledError:
            print(f"Config stream closed for tenant {tenant_id}")
            return
        except Exception as e:
            print(f"Config stream error for tenant {tenant_id}: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
            return
    
    return StreamingResponse(
        event_generator(), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Cache-Control"
        }
    )

@router.put("/config/{tenant_id}")
async def update_tenant_config(
    tenant_id: int,
    config_update: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update live configuration (triggers hot reload)"""
    
    if tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Access denied")
    
    try:
        # Update or create tenant settings
        upsert_query = text("""
            INSERT INTO tenant_settings (tenant_id, settings, updated_at)
            VALUES (:tenant_id, :settings, :updated_at)
            ON CONFLICT (tenant_id) 
            DO UPDATE SET 
                settings = :settings,
                updated_at = :updated_at
            RETURNING id
        """)
        
        result = await db.execute(upsert_query, {
            "tenant_id": tenant_id,
            "settings": json.dumps(config_update),
            "updated_at": datetime.utcnow()
        })
        
        await db.commit()
        
        # TODO: Trigger hot reload notification
        # This would notify all connected SDKs via Server-Sent Events
        # await notify_config_change(tenant_id, config_update)
        
        return {
            "status": "updated",
            "tenant_id": tenant_id,
            "timestamp": datetime.utcnow().isoformat()
        }
        
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"Failed to update config: {str(e)}")

@router.post("/mark-element")
async def mark_element(
    request: Dict[str, Any],
    db: AsyncSession = Depends(get_db),
    x_tenant_id: str = Header(..., alias="X-Tenant-ID")
):
    """Save visually marked element from Chrome extension"""
    
    try:
        tenant_id = int(x_tenant_id)
        
        # Extract data from request
        selector = request.get('selector')
        tracking_type = request.get('tracking_type')
        friendly_name = request.get('friendly_name')
        element_data = request.get('element_data', {})
        
        if not selector or not tracking_type or not friendly_name:
            raise HTTPException(400, "Missing required fields: selector, tracking_type, friendly_name")
        
        # Create or update marked element record
        upsert_query = text("""
            INSERT INTO tenant_marked_elements (
                tenant_id, selector, tracking_type, friendly_name, element_data, created_at
            ) VALUES (
                :tenant_id, :selector, :tracking_type, :friendly_name, :element_data, :created_at
            )
            ON CONFLICT (tenant_id, selector) DO UPDATE SET
                tracking_type = :tracking_type,
                friendly_name = :friendly_name,
                element_data = :element_data,
                updated_at = :created_at
            RETURNING id
        """)
        
        result = await db.execute(upsert_query, {
            "tenant_id": tenant_id,
            "selector": selector,
            "tracking_type": tracking_type,
            "friendly_name": friendly_name,
            "element_data": json.dumps(element_data),
            "created_at": datetime.utcnow()
        })
        
        element_id = result.scalar()
        await db.commit()
        
        # Update tenant schema with this new field
        await update_tenant_schema_from_marked_element(db, tenant_id, {
            "id": f"element_{element_id}",
            "fieldTypeId": tracking_type,
            "name": friendly_name,
            "emoji": get_tracking_emoji(tracking_type),
            "selector": selector
        })
        
        # Trigger config update for hot reload
        await trigger_config_update_notification(tenant_id)
        
        return {
            "status": "marked",
            "element_id": element_id,
            "message": f"Element '{friendly_name}' marked for {tracking_type} tracking",
            "selector": selector
        }
        
    except ValueError as e:
        raise HTTPException(400, f"Invalid tenant ID: {x_tenant_id}")
    except Exception as e:
        await db.rollback()
        raise HTTPException(500, f"Failed to mark element: {str(e)}")

@router.get("/marked-elements/{tenant_id}")
async def get_marked_elements(
    tenant_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all marked elements for a tenant"""
    
    if tenant_id != current_user.tenant_id:
        raise HTTPException(403, "Access denied")
    
    try:
        query = text("""
            SELECT id, selector, tracking_type, friendly_name, element_data, created_at, updated_at
            FROM tenant_marked_elements 
            WHERE tenant_id = :tenant_id 
            ORDER BY created_at DESC
        """)
        
        result = await db.execute(query, {"tenant_id": tenant_id})
        elements = result.fetchall()
        
        return {
            "tenant_id": tenant_id,
            "total_elements": len(elements),
            "elements": [
                {
                    "id": element.id,
                    "selector": element.selector,
                    "tracking_type": element.tracking_type,
                    "friendly_name": element.friendly_name,
                    "element_data": json.loads(element.element_data) if isinstance(element.element_data, str) else element.element_data,
                    "created_at": element.created_at.isoformat(),
                    "updated_at": element.updated_at.isoformat() if element.updated_at else None
                }
                for element in elements
            ]
        }
        
    except Exception as e:
        raise HTTPException(500, f"Failed to retrieve marked elements: {str(e)}")

# Helper functions
async def update_tenant_schema_from_marked_element(db: AsyncSession, tenant_id: int, field_data: dict):
    """Update or create tenant schema with new marked element"""
    try:
        # Get existing schema
        schema_query = text("""
            SELECT id, fields FROM tenant_schemas 
            WHERE tenant_id = :tenant_id 
            ORDER BY created_at DESC 
            LIMIT 1
        """)
        
        result = await db.execute(schema_query, {"tenant_id": tenant_id})
        schema_row = result.fetchone()
        
        if schema_row:
            # Update existing schema
            existing_fields = json.loads(schema_row.fields) if isinstance(schema_row.fields, str) else schema_row.fields
            
            # Check if field already exists
            field_exists = False
            for i, existing_field in enumerate(existing_fields):
                if existing_field.get('selector') == field_data.get('selector'):
                    existing_fields[i] = field_data
                    field_exists = True
                    break
            
            if not field_exists:
                existing_fields.append(field_data)
            
            # Update schema
            update_query = text("""
                UPDATE tenant_schemas 
                SET fields = :fields, updated_at = :updated_at
                WHERE id = :schema_id
            """)
            
            await db.execute(update_query, {
                "schema_id": schema_row.id,
                "fields": json.dumps(existing_fields),
                "updated_at": datetime.utcnow()
            })
        else:
            # Create new schema
            create_query = text("""
                INSERT INTO tenant_schemas (tenant_id, schema_name, description, fields, created_at)
                VALUES (:tenant_id, :schema_name, :description, :fields, :created_at)
            """)
            
            await db.execute(create_query, {
                "tenant_id": tenant_id,
                "schema_name": "Chrome Extension Marked Elements",
                "description": "Elements marked using the Smart Analytics Chrome extension",
                "fields": json.dumps([field_data]),
                "created_at": datetime.utcnow()
            })
        
        await db.commit()
        
    except Exception as e:
        print(f"Error updating schema from marked element: {e}")
        # Don't raise exception, as this is a background task

async def trigger_config_update_notification(tenant_id: int):
    """Trigger hot reload notification for tenant configuration update"""
    try:
        # This would integrate with your WebSocket/SSE system for real-time updates
        # For now, we'll just log it
        print(f"Configuration updated for tenant {tenant_id} - triggering hot reload")
        
        # TODO: Implement actual notification system
        # Examples:
        # - Send WebSocket message to connected dashboards
        # - Trigger Server-Sent Event for config stream
        # - Update Redis cache for config changes
        
    except Exception as e:
        print(f"Error triggering config update notification: {e}")

def get_tracking_emoji(tracking_type: str) -> str:
    """Get emoji for tracking type"""
    emoji_map = {
        'button_click': '👆',
        'form_submit': '📝',
        'page_view': '👀',
        'time_spent': '⏱️',
        'scroll_depth': '📜',
        'custom_event': '⚡',
        'link_click': '🔗',
        'file_download': '💾'
    }
    return emoji_map.get(tracking_type, '📊')

def generate_js_snippet(tenant_id: int, api_key: str, fields: List[dict]) -> str:
    """Generate the JavaScript tracking snippet"""
    # Return the same snippet as in SDKSetup.tsx but server-generated
    return f"""
// Smart Analytics Tracking Code for Tenant {tenant_id}
(function() {{
  const CHILD_CONFIG = {{
    tenantId: {tenant_id},
    apiKey: "{api_key}",
    apiUrl: "/api/v1/smart-analytics/track",
    fields: {json.dumps(fields)}
  }};
  
  // ... (rest of the tracking code would go here)
  // For brevity, shortened in this example
  
  console.log("Smart Analytics initialized for tenant {tenant_id}");
}})();
"""
