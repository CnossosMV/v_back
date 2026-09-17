"""Pydantic schemas for Funnel Sandbox."""

from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel


# ── Contact ──────────────────────────────────────────────────────────

class SandboxContactCreate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None


class SandboxContactResponse(BaseModel):
    id: int
    project_id: int
    external_id: str
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None
    is_sandbox: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Session ──────────────────────────────────────────────────────────

class SandboxSessionCreate(BaseModel):
    mode: str = "log_only"
    preview_channel: Optional[str] = None
    preview_destination: Optional[str] = None
    preview_instance_id: Optional[int] = None


class SandboxSessionUpdate(BaseModel):
    mode: Optional[str] = None
    preview_channel: Optional[str] = None
    preview_destination: Optional[str] = None
    preview_instance_id: Optional[int] = None


class SandboxSessionResponse(BaseModel):
    id: int
    project_id: int
    funnel_id: int
    contact_id: int
    created_by: Optional[int] = None
    mode: str
    preview_channel: Optional[str] = None
    preview_destination: Optional[str] = None
    preview_instance_id: Optional[int] = None
    status: str
    enrollment_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Enrollment ───────────────────────────────────────────────────────

class SandboxEnrollRequest(BaseModel):
    pass  # No params needed — session already has contact + funnel


class SandboxFireEventRequest(BaseModel):
    event_name: str
    properties: Optional[Dict[str, Any]] = None


class SandboxMockWebhookRequest(BaseModel):
    step_id: int
    response_data: Dict[str, Any]


class SandboxAdvanceRequest(BaseModel):
    pass


class SandboxResetRequest(BaseModel):
    re_enroll: bool = False


class SandboxPropertyUpdateRequest(BaseModel):
    properties: Dict[str, Any]


# ── Action Log ───────────────────────────────────────────────────────

class SandboxActionLogResponse(BaseModel):
    id: int
    session_id: int
    enrollment_id: Optional[int] = None
    step_id: Optional[int] = None
    action_type: str
    action_config: Optional[Dict[str, Any]] = None
    intercepted_mode: str
    preview_result: Optional[Dict[str, Any]] = None
    resolved_variables: Optional[Dict[str, Any]] = None
    suppression_check: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Introspection ────────────────────────────────────────────────────

class FunnelManifestResponse(BaseModel):
    funnel_id: int
    funnel_name: str
    funnel_status: str
    step_count: int
    event_names: List[str]
    condition_fields: List[str]
    webhook_urls: List[str]
    tags_assigned: List[str]
    tags_checked: List[str]
    template_ids: List[int]
    wait_steps: List[Dict[str, Any]]
    wait_until_events: List[str]
    api_connections: List[Dict[str, Any]]
