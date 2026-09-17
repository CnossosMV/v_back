"""
Pydantic schemas for Event Actions API
"""
from pydantic import BaseModel, Field, validator
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum

from app.schemas.orchestration_attention import AttentionPolicyInput


class ActionType(str, Enum):
    """Supported action types"""
    send_template = "send_template"
    assign_chatbot = "assign_chatbot"
    assign_agent = "assign_agent"
    assign_agent_team = "assign_agent_team"
    update_user = "update_user"
    add_tag = "add_tag"
    webhook = "webhook"
    add_to_funnel = "add_to_funnel"
    run_graph = "run_graph"
    api_call = "api_call"
    send_whatsapp_message = "send_whatsapp_message"


class EventActionLane(str, Enum):
    transactional = "transactional"
    conversational = "conversational"
    manual = "manual"
    promotional = "promotional"


class ConditionOperator(str, Enum):
    """Supported condition operators"""
    eq = "=="
    neq = "!="
    lt = "<"
    lte = "<="
    gt = ">"
    gte = ">="
    contains = "contains"
    not_contains = "not_contains"
    starts_with = "starts_with"
    ends_with = "ends_with"
    matches = "matches"
    in_list = "in"
    not_in_list = "not_in"
    exists = "exists"
    not_exists = "not_exists"
    is_empty = "is_empty"
    is_not_empty = "is_not_empty"


class ConditionSchema(BaseModel):
    """Single condition definition"""
    field: str = Field(..., description="Field to evaluate (supports dot notation)")
    operator: str = Field(..., description="Comparison operator")
    value: Any = Field(None, description="Value to compare against")

    class Config:
        json_schema_extra = {
            "example": {
                "field": "properties.cart_value",
                "operator": ">=",
                "value": 100
            }
        }


class ActionConfigSchema(BaseModel):
    """Single action configuration"""
    type: ActionType = Field(..., description="Action type")
    config: Dict[str, Any] = Field(default={}, description="Action-specific configuration")
    delay_seconds: int = Field(default=0, ge=0, description="Delay before executing this action")

    class Config:
        json_schema_extra = {
            "example": {
                "type": "send_template",
                "config": {
                    "template_id": 1,
                    "recipient_field": "email"
                },
                "delay_seconds": 0
            }
        }


class StopConditionSchema(BaseModel):
    """Stop condition to cancel delayed actions"""
    event: str = Field(..., description="Event name that cancels delayed actions")
    within_seconds: int = Field(default=86400, description="Time window in seconds")

    class Config:
        json_schema_extra = {
            "example": {
                "event": "purchase_completed",
                "within_seconds": 86400
            }
        }


# Request Schemas

class EventActionCreate(BaseModel):
    """Create new event action"""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    trigger_event: str = Field(..., min_length=1, max_length=200, description="Event name to trigger on")
    purpose_key: Optional[str] = Field(None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    attention_policy: Optional[AttentionPolicyInput] = None
    conditions: List[ConditionSchema] = Field(default=[])
    actions: List[ActionConfigSchema] = Field(..., min_items=1, description="Actions to execute")
    stop_conditions: List[StopConditionSchema] = Field(default=[])
    is_active: bool = Field(default=False, description="Create as draft; activation requires impact approval")
    priority: int = Field(default=0, description="Higher priority runs first")
    cooldown_seconds: int = Field(default=86400, ge=0, description="Cooldown between triggers per user")
    react_to_delivery: bool = Field(default=False, description="Opt-in to process channel delivery events")
    lane: EventActionLane = Field(
        default=EventActionLane.promotional,
        description="Outbound intent used by Guardian and consent gates",
    )

    @validator('actions')
    def validate_actions(cls, v):
        if not v:
            raise ValueError('At least one action is required')
        return v


class EventActionUpdate(BaseModel):
    """Update existing event action"""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    trigger_event: Optional[str] = Field(None, min_length=1, max_length=200)
    purpose_key: Optional[str] = Field(None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    attention_policy: Optional[AttentionPolicyInput] = None
    conditions: Optional[List[ConditionSchema]] = None
    actions: Optional[List[ActionConfigSchema]] = None
    stop_conditions: Optional[List[StopConditionSchema]] = None
    is_active: Optional[bool] = None
    priority: Optional[int] = None
    cooldown_seconds: Optional[int] = Field(None, ge=0)
    react_to_delivery: Optional[bool] = None
    lane: Optional[EventActionLane] = None


class EventActionTest(BaseModel):
    """Test event action with sample data"""
    event_name: str = Field(..., description="Event name to simulate")
    properties: Dict[str, Any] = Field(default={}, description="Event properties")
    user_data: Optional[Dict[str, Any]] = Field(None, description="User data to simulate")


class EventActionTrigger(BaseModel):
    """Manually trigger an event action"""
    properties: Dict[str, Any] = Field(default={}, description="Event properties for the synthetic event")
    user_data: Optional[Dict[str, Any]] = Field(None, description="Optional user data")


# Response Schemas

class EventActionResponse(BaseModel):
    """Event action response"""
    id: int
    project_id: int
    name: str
    description: Optional[str]
    trigger_event: str
    purpose_key: Optional[str] = None
    attention_policy: Optional[Dict[str, Any]] = None
    conditions: List[Dict[str, Any]]
    actions: List[Dict[str, Any]]
    stop_conditions: List[Dict[str, Any]]
    is_active: bool
    priority: int
    cooldown_seconds: int
    react_to_delivery: bool = False
    lane: EventActionLane = EventActionLane.promotional
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True


class EventActionListResponse(BaseModel):
    """List response with pagination"""
    items: List[EventActionResponse]
    total: int
    page: int
    page_size: int


class ExecutionActionDetail(BaseModel):
    """Details of a single executed action"""
    index: int
    type: str
    status: str
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    scheduled: Optional[bool] = None
    delay_seconds: Optional[int] = None


class EventActionExecutionResponse(BaseModel):
    """Event action execution history item"""
    id: int
    event_action_id: Optional[int]
    event_id: Optional[int]
    user_id: Optional[int]
    actions_executed: List[Dict[str, Any]]
    status: str
    error_message: Optional[str]
    started_at: datetime
    completed_at: Optional[datetime]
    duration_ms: Optional[int]

    class Config:
        from_attributes = True


class ScheduledActionResponse(BaseModel):
    """Scheduled action response"""
    id: int
    project_id: int
    event_action_id: int
    user_id: Optional[int]
    event_id: Optional[int]
    action_index: int
    action_config: Dict[str, Any]
    scheduled_for: datetime
    status: str
    executed_at: Optional[datetime]
    cancelled_at: Optional[datetime]
    cancel_reason: Optional[str]
    error_message: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class TestResultResponse(BaseModel):
    """Test execution result"""
    success: bool
    matched: bool
    conditions_passed: bool
    actions_would_execute: List[Dict[str, Any]]
    message: str


class TriggerResultResponse(BaseModel):
    """Manual trigger execution result"""
    success: bool
    status: str
    actions_executed: List[Dict[str, Any]]
    message: str
    error: Optional[str] = None


class ActionTypeInfo(BaseModel):
    """Information about an action type"""
    type: str
    description: str
    config_schema: Dict[str, Any]


class ActionTypesResponse(BaseModel):
    """List of available action types"""
    action_types: List[ActionTypeInfo]
