"""API contracts for campaigns, runs and waves."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field

from app.schemas.orchestration_attention import AttentionPolicyInput


class ContactEndpointCreate(BaseModel):
    user_id: int
    endpoint_type: str = Field(..., min_length=1, max_length=30)
    value: str = Field(..., min_length=1, max_length=500)
    normalized_value: str | None = Field(None, max_length=500)
    value_hash: str | None = Field(None, min_length=32, max_length=128)
    is_primary: bool = False
    status: str = Field("active", max_length=30)
    source: str | None = Field(None, max_length=50)
    metadata: dict[str, Any] | None = None


class ContactEndpointUpdate(BaseModel):
    normalized_value: str | None = Field(None, max_length=500)
    value_hash: str | None = Field(None, min_length=32, max_length=128)
    is_primary: bool | None = None
    status: str | None = Field(None, max_length=30)
    metadata: dict[str, Any] | None = None


class ContactEndpointResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    endpoint_type: str
    value: str
    normalized_value: str | None = None
    value_hash: str
    is_primary: bool
    status: str
    source: str
    metadata: dict[str, Any] | None = Field(
        None,
        validation_alias=AliasChoices("metadata", "endpoint_metadata"),
    )
    first_seen_at: datetime
    last_seen_at: datetime
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContactPermissionEvidenceCreate(BaseModel):
    user_id: int
    endpoint_id: int | None = None
    channel: str = Field(..., min_length=1, max_length=50)
    permission_type: str = Field("marketing", min_length=1, max_length=50)
    status: str = Field(..., min_length=1, max_length=30)
    source: str | None = Field(None, max_length=100)
    policy_version: str | None = Field(None, max_length=100)
    evidence_ref: str | None = Field(None, max_length=255)
    captured_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = None


class ContactPermissionEvidenceResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    endpoint_id: int | None = None
    channel: str
    permission_type: str
    status: str
    source: str | None = None
    policy_version: str | None = None
    evidence_ref: str | None = None
    captured_at: datetime
    expires_at: datetime | None = None
    metadata: dict[str, Any] | None = Field(
        None,
        validation_alias=AliasChoices("metadata", "evidence_metadata"),
    )
    created_at: datetime

    class Config:
        from_attributes = True


class CampaignVariantInput(BaseModel):
    variant_key: str | None = Field(None, max_length=100)
    locale: str | None = Field(None, max_length=20)
    template_id: int | None = None
    subject: str | None = Field(None, max_length=500)
    body: str | None = None
    variant_config: dict[str, Any] | None = None
    weight: int = Field(100, ge=1, le=100000)
    status: Literal["active", "paused"] = "active"


class CampaignActionInput(BaseModel):
    position: int | None = Field(None, ge=0)
    action_type: Literal["send_message"] = "send_message"
    channel: str = Field("email", min_length=1, max_length=50)
    config: dict[str, Any] | None = None
    status: Literal["active", "paused"] = "active"
    variants: list[CampaignVariantInput] = Field(default_factory=list, max_length=100)


class CampaignOpportunityInput(BaseModel):
    """Business context submitted to Selection; never a send permission."""

    opportunity_type: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    priority: int = Field(0, ge=-1000, le=1000)
    priority_source: Literal[
        "tenant_policy", "historical_evidence", "bounded_learning", "tie_requires_decision"
    ] = "tie_requires_decision"
    priority_reason: str | None = Field(None, min_length=3, max_length=2000)
    decision_lead_hours: int = Field(0, ge=0, le=24 * 31)
    attention_window_hours: int = Field(72, ge=1, le=24 * 31)
    calendar_external_key: str | None = Field(None, min_length=1, max_length=255)
    combine_with: list[str] = Field(default_factory=list, max_length=50)
    context: dict[str, Any] | None = None


class CampaignCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    campaign_type: Literal["one_off", "recurring", "automation"] = "one_off"
    status: Literal["draft", "paused"] = "draft"
    default_channel: str = Field("email", min_length=1, max_length=50)
    selection_config: dict[str, Any]
    policy_config: dict[str, Any] | None = None
    recurrence_config: dict[str, Any] | None = None
    opportunity_config: CampaignOpportunityInput | None = None
    timezone: str | None = Field(None, max_length=64)
    starts_at: datetime | None = None
    target_at: datetime | None = None
    external_key: str | None = Field(None, max_length=255)
    purpose_key: str | None = Field(None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    attention_policy: AttentionPolicyInput | None = None
    actions: list[CampaignActionInput] = Field(..., min_length=1, max_length=20)


class CampaignUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    campaign_type: Literal["one_off", "recurring", "automation"] | None = None
    status: Literal["draft", "paused"] | None = None
    default_channel: str | None = Field(None, min_length=1, max_length=50)
    selection_config: dict[str, Any] | None = None
    policy_config: dict[str, Any] | None = None
    recurrence_config: dict[str, Any] | None = None
    opportunity_config: CampaignOpportunityInput | None = None
    timezone: str | None = Field(None, max_length=64)
    starts_at: datetime | None = None
    target_at: datetime | None = None
    external_key: str | None = Field(None, max_length=255)
    purpose_key: str | None = Field(None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    attention_policy: AttentionPolicyInput | None = None
    actions: list[CampaignActionInput] | None = Field(None, min_length=1, max_length=20)


class CampaignPreviewRequest(BaseModel):
    schedule: dict[str, Any] = Field(default_factory=dict)


class CampaignPolicyOverrideRequest(BaseModel):
    """Explicit, one-run relaxation of soft pacing rules only."""

    rules: list[Literal["channel_cooldown", "contact_caps", "quiet_hours"]] = Field(
        ..., min_length=1, max_length=3,
    )
    reason: str = Field(..., min_length=10, max_length=2000)
    risk_acknowledged: Literal[True]
    expires_in_hours: int = Field(24, ge=1, le=72)


class CampaignConsequenceRequest(CampaignPreviewRequest):
    policy_override: CampaignPolicyOverrideRequest | None = None


class CampaignRunCreate(CampaignPreviewRequest):
    expected_campaign_version: int = Field(..., ge=1)
    expected_candidate_count: int = Field(..., ge=0)
    expected_eligible_count: int = Field(..., ge=0)
    expected_planned_count: int = Field(..., ge=0)
    # Client-generated and stable across HTTP retries.  The backend returns
    # the existing run when the key and request fingerprint match.
    run_key: str = Field(..., min_length=8, max_length=255)
    policy_override: CampaignPolicyOverrideRequest | None = None
    decision_choice_id: int | None = Field(None, ge=1)


class CampaignVariantResponse(BaseModel):
    id: int
    variant_key: str
    locale: str | None = None
    template_id: int | None = None
    subject: str | None = None
    body: str | None = None
    variant_config: dict[str, Any] | None = None
    weight: int
    status: str

    class Config:
        from_attributes = True


class CampaignActionResponse(BaseModel):
    id: int
    position: int
    action_type: str
    channel: str | None = None
    config: dict[str, Any] | None = None
    status: str
    variants: list[CampaignVariantResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class CampaignResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: str | None = None
    campaign_type: str
    status: str
    default_channel: str
    selection_config: dict[str, Any] | None = None
    policy_config: dict[str, Any] | None = None
    recurrence_config: dict[str, Any] | None = None
    opportunity_config: dict[str, Any] | None = None
    timezone: str | None = None
    starts_at: datetime | None = None
    target_at: datetime | None = None
    external_key: str | None = None
    purpose_key: str | None = None
    attention_policy: dict[str, Any] | None = None
    version: int
    created_by_user_id: int | None = None
    created_at: datetime
    updated_at: datetime
    audience_count: int = 0
    eligible_count: int = 0
    sent_count: int = 0
    delivered_count: int = 0
    failed_count: int = 0
    actions: list[CampaignActionResponse] = Field(default_factory=list)

    class Config:
        from_attributes = True


class CampaignPolicyOverrideResponse(BaseModel):
    id: int
    project_id: int
    run_id: int
    status: str
    override_keys: list[str]
    reason: str
    risk_acknowledged: bool
    dual_approval_required: bool
    planned_count_snapshot: int
    requested_by_user_id: int | None = None
    approved_by_user_id: int | None = None
    revoked_by_user_id: int | None = None
    requested_at: datetime
    approved_at: datetime | None = None
    expires_at: datetime
    revoked_at: datetime | None = None

    class Config:
        from_attributes = True


class CampaignRunResponse(BaseModel):
    id: int
    project_id: int
    campaign_id: int
    run_key: str
    trigger_type: str
    status: str
    audience_snapshot: dict[str, Any] | None = None
    audience_hash: str | None = None
    timezone: str | None = None
    starts_at: datetime | None = None
    target_at: datetime | None = None
    deadline_at: datetime | None = None
    capacity_plan: dict[str, Any] | None = None
    candidate_count: int
    eligible_count: int
    planned_count: int
    queued_count: int
    sent_count: int
    delivered_count: int
    failed_count: int
    skipped_count: int
    canceled_count: int
    cancel_requested: bool
    error_message: str | None = None
    requested_by_user_id: int | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    policy_override: CampaignPolicyOverrideResponse | None = None
    decision_choice_id: int | None = None

    class Config:
        from_attributes = True
class CampaignWaveCreate(BaseModel):
    position: int = Field(0, ge=0)
    status: str = Field("planned", max_length=30)
    approval_mode: str = Field("none", max_length=30)
    is_canary: bool = False
    scheduled_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    planned_count: int = Field(0, ge=0)


class CampaignWaveResponse(BaseModel):
    id: int
    project_id: int
    run_id: int
    position: int
    status: str
    approval_mode: str = "none"
    is_canary: bool = False
    approved_at: datetime | None = None
    approved_by_user_id: int | None = None
    scheduled_at: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    planned_count: int
    queued_count: int
    sent_count: int
    failed_count: int
    skipped_count: int
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    class Config:
        from_attributes = True
