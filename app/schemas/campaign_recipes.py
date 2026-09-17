"""Agent-first contracts for persistent campaign recipes and episodes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.campaigns import CampaignActionInput
from app.schemas.commercial_calendar import OpportunityRuleInput
from app.schemas.orchestration_attention import AttentionPolicyInput


class CampaignContentBrief(BaseModel):
    objective: str = Field(..., min_length=3, max_length=2000)
    call_to_action: str = Field(..., min_length=2, max_length=1000)
    locales: list[str] = Field(..., min_length=1, max_length=20)
    proposition: str | None = Field(None, max_length=4000)
    audience_context: str | None = Field(None, max_length=4000)
    required_points: list[str] = Field(default_factory=list, max_length=50)
    forbidden_claims: list[str] = Field(default_factory=list, max_length=50)
    tone: str | None = Field(None, max_length=500)
    source_facts: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(None, max_length=4000)


class CampaignRecipeAutonomyPolicy(BaseModel):
    episode_materialization: Literal["automatic", "agent_requested"] = "automatic"
    copy_review: Literal["every_episode", "within_declared_bounds"] = "every_episode"
    activation: Literal["manual"] = "manual"
    run_authorization: Literal["consequence_gate"] = "consequence_gate"
    unattended_behavior: Literal["expire_episode_keep_base"] = "expire_episode_keep_base"


class CampaignRecipeCreate(BaseModel):
    external_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    status: Literal["draft"] = "draft"
    purpose_key: str = Field(..., min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    lifecycle_model_id: int = Field(..., ge=1)
    timezone: str = Field("UTC", min_length=1, max_length=64)
    country_code: str | None = Field(None, min_length=2, max_length=2)
    region_code: str | None = Field(None, min_length=1, max_length=80)
    default_channel: str = Field("email", min_length=1, max_length=50)
    selection_config: dict[str, Any]
    policy_config: dict[str, Any] | None = None
    attention_policy: AttentionPolicyInput
    schedule_rules: list[OpportunityRuleInput] = Field(..., min_length=1, max_length=50)
    include_persisted_opportunities: bool = True
    content_mode: Literal["fixed", "agent_draft", "bounded_autonomy"] = "agent_draft"
    content_brief: CampaignContentBrief
    fixed_actions: list[CampaignActionInput] | None = Field(None, min_length=1, max_length=20)
    autonomy_policy: CampaignRecipeAutonomyPolicy = Field(default_factory=CampaignRecipeAutonomyPolicy)
    planning_horizon_days: int = Field(45, ge=7, le=370)
    decision_lead_hours: int = Field(168, ge=1, le=24 * 31)

    @model_validator(mode="after")
    def validate_agent_first_contract(self):
        if self.country_code:
            self.country_code = self.country_code.upper()
        if self.selection_config.get("group_id"):
            raise ValueError(
                "campaign recipes v1 require selection_config.rule_config so lifecycle_model_id remains explicit"
            )
        if not isinstance(self.selection_config.get("rule_config"), dict):
            raise ValueError("selection_config.rule_config is required")
        if self.content_mode == "fixed" and not self.fixed_actions:
            raise ValueError("fixed content_mode requires fixed_actions")
        if self.content_mode != "fixed" and self.fixed_actions:
            raise ValueError("fixed_actions are only allowed when content_mode=fixed")
        if self.content_mode == "bounded_autonomy" and self.autonomy_policy.copy_review != "within_declared_bounds":
            raise ValueError("bounded_autonomy requires copy_review=within_declared_bounds")
        return self


class CampaignRecipeUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    purpose_key: str | None = Field(None, min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    lifecycle_model_id: int | None = Field(None, ge=1)
    timezone: str | None = Field(None, min_length=1, max_length=64)
    country_code: str | None = Field(None, min_length=2, max_length=2)
    region_code: str | None = Field(None, min_length=1, max_length=80)
    default_channel: str | None = Field(None, min_length=1, max_length=50)
    selection_config: dict[str, Any] | None = None
    policy_config: dict[str, Any] | None = None
    attention_policy: AttentionPolicyInput | None = None
    schedule_rules: list[OpportunityRuleInput] | None = Field(None, min_length=1, max_length=50)
    include_persisted_opportunities: bool | None = None
    content_mode: Literal["fixed", "agent_draft", "bounded_autonomy"] | None = None
    content_brief: CampaignContentBrief | None = None
    fixed_actions: list[CampaignActionInput] | None = Field(None, min_length=1, max_length=20)
    autonomy_policy: CampaignRecipeAutonomyPolicy | None = None
    planning_horizon_days: int | None = Field(None, ge=7, le=370)
    decision_lead_hours: int | None = Field(None, ge=1, le=24 * 31)


class CampaignEpisodeMaterialize(BaseModel):
    actions: list[CampaignActionInput] | None = Field(None, min_length=1, max_length=20)
    copy_rationale: str | None = Field(None, min_length=3, max_length=4000)
