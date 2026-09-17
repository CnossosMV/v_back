"""Pydantic schemas for the Funnel Engine."""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.schemas.orchestration_attention import AttentionPolicyInput


# ============================================================================
# Funnel
# ============================================================================

class FunnelCreate(BaseModel):
    name: str
    description: Optional[str] = None
    trigger_type: str  # event, segment
    trigger_config: Optional[Dict[str, Any]] = {}
    global_exit_config: Optional[Dict[str, Any]] = {}
    purpose_key: Optional[str] = None
    attention_policy: Optional[AttentionPolicyInput] = None


class FunnelUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_config: Optional[Dict[str, Any]] = None
    global_exit_config: Optional[Dict[str, Any]] = None
    purpose_key: Optional[str] = None
    attention_policy: Optional[AttentionPolicyInput] = None
    debug_mode: Optional[bool] = None


class FunnelStepResponse(BaseModel):
    id: int
    funnel_id: int
    step_type: str
    step_config: Dict[str, Any] = {}
    position: int
    parent_step_id: Optional[int] = None
    branch: str = "main"
    created_at: datetime

    class Config:
        from_attributes = True


class FunnelResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    status: str
    trigger_type: str
    trigger_config: Dict[str, Any] = {}
    global_exit_config: Dict[str, Any] = {}
    purpose_key: Optional[str] = None
    attention_policy: Optional[Dict[str, Any]] = None
    debug_mode: bool = False
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    steps: List[FunnelStepResponse] = []
    step_count: int = 0
    enrollment_count: int = 0

    class Config:
        from_attributes = True


class FunnelListResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    status: str
    trigger_type: str
    purpose_key: Optional[str] = None
    attention_policy: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    step_count: int = 0
    enrollment_count: int = 0

    class Config:
        from_attributes = True


# ============================================================================
# Funnel Steps
# ============================================================================

class FunnelStepCreate(BaseModel):
    step_type: str  # wait, condition, action, exit, wait_for_reply, wait_until, fork
    step_config: Optional[Dict[str, Any]] = {}
    position: Optional[int] = None
    parent_step_id: Optional[int] = None
    branch: Optional[str] = "main"
    adopt_branch: Optional[str] = None


class FunnelStepUpdate(BaseModel):
    step_type: Optional[str] = None
    step_config: Optional[Dict[str, Any]] = None
    position: Optional[int] = None
    parent_step_id: Optional[int] = None
    branch: Optional[str] = None


class StepReorderItem(BaseModel):
    id: int
    position: int
    parent_step_id: Optional[int] = None
    branch: Optional[str] = None


class StepReorderRequest(BaseModel):
    steps: List[StepReorderItem]


class MoveStepRequest(BaseModel):
    target_parent_step_id: Optional[int] = None
    target_branch: str = "main"
    target_position: int


class StepSnapshotItem(BaseModel):
    id: int
    step_type: str
    step_config: Optional[Dict[str, Any]] = {}
    position: int
    parent_step_id: Optional[int] = None
    branch: Optional[str] = "main"

    class Config:
        extra = "ignore"


class StepRestoreRequest(BaseModel):
    steps: List[StepSnapshotItem]


# ============================================================================
# Enrollment (read-only for Phase 1)
# ============================================================================

class FunnelEnrollmentResponse(BaseModel):
    id: int
    funnel_id: int
    user_id: int
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    status: str
    current_step_id: Optional[int] = None
    current_branch: str = "main"
    messages_sent: int = 0
    exit_reason: Optional[str] = None
    exited_at: Optional[datetime] = None
    enrolled_at: datetime
    enrollment_metadata: Optional[Dict[str, Any]] = {}

    class Config:
        from_attributes = True


class FunnelEnrollmentLogResponse(BaseModel):
    id: int
    enrollment_id: int
    step_id: Optional[int] = None
    action: str
    details: Dict[str, Any] = {}
    created_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Analytics
# ============================================================================

class EnrollmentStatsResponse(BaseModel):
    total: int = 0
    active: int = 0
    completed: int = 0
    exited: int = 0
    exited_by_reason: Dict[str, int] = {}
    avg_completion_hours: Optional[float] = None


class StepMetricsResponse(BaseModel):
    step_id: int
    step_type: str
    position: int
    users_entered: int = 0
    users_completed: int = 0
    avg_time_seconds: Optional[float] = None
    drop_off_rate: float = 0


class ConversionFunnelResponse(BaseModel):
    step_name: str
    position: int
    entered: int = 0
    pct: float = 0


class EnrollmentTrendResponse(BaseModel):
    date: str
    enrolled: int = 0
    completed: int = 0
    exited: int = 0


class MilestoneResponse(BaseModel):
    goal_id: Optional[str] = None
    step_id: Optional[int] = None
    message: Optional[str] = None
    confidence: float = 0
    at: Optional[str] = None


class PauseInfoResponse(BaseModel):
    is_paused: bool = False
    paused_step_id: Optional[int] = None
    paused_at: Optional[str] = None
    reason: Optional[str] = None
    ticket_id: Optional[int] = None


# ============================================================================
# Export / Import
# ============================================================================

class FunnelExportReference(BaseModel):
    ref_type: str
    original_id: int
    label: str
    found_in: List[Dict[str, Any]]


class FunnelImportRequest(BaseModel):
    versya_funnel_export: bool
    version: int
    funnel: Dict[str, Any]
    steps: List[Dict[str, Any]]
    references: List[Dict[str, Any]] = []


class FunnelImportResponse(BaseModel):
    funnel_id: int
    name: str
    status: str
    step_count: int
    unresolved_references: List[Dict[str, Any]]


class ReferenceResolution(BaseModel):
    ref_type: str
    original_id: int
    resolved_id: int


class ResolveReferencesRequest(BaseModel):
    resolutions: List[ReferenceResolution]


# ============================================================================
# Manual Enrollment
# ============================================================================

class ManualEnrollRequest(BaseModel):
    user_id: int
    target_step_ids: List[int] = []  # empty = first step; multiple = one per fork path
    source_enrollment_id: Optional[int] = None
    copy_metadata: bool = True
    metadata_overrides: Optional[Dict[str, Any]] = None
    force_exit_active: bool = False


class ManualEnrollResponse(BaseModel):
    enrollment_id: int
    user_id: int
    target_step_ids: List[int] = []
    copied_metadata: bool
    source_enrollment_id: Optional[int] = None
    force_exited_enrollment_id: Optional[int] = None
