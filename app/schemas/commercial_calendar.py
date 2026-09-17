from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class CommercialOpportunityInput(BaseModel):
    external_key: str = Field(..., min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]*$")
    name: str = Field(..., min_length=1, max_length=255)
    opportunity_type: str = Field(..., min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    status: Literal["draft", "active", "archived"] = "draft"
    country_code: str | None = Field(None, min_length=2, max_length=2)
    region_code: str | None = Field(None, min_length=1, max_length=80)
    timezone: str = Field("UTC", min_length=1, max_length=64)
    starts_at: datetime
    peak_at: datetime | None = None
    expires_at: datetime
    priority: int = Field(0, ge=-1000, le=1000)
    priority_source: Literal[
        "tenant_policy", "historical_evidence", "bounded_learning", "tie_requires_decision"
    ] = "tie_requires_decision"
    priority_reason: str | None = Field(None, min_length=3, max_length=2000)
    purpose_keys: list[str] = Field(default_factory=list, max_length=100)
    context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_window(self):
        if self.expires_at <= self.starts_at:
            raise ValueError("expires_at must be after starts_at")
        if self.peak_at is not None and not self.starts_at <= self.peak_at <= self.expires_at:
            raise ValueError("peak_at must fall inside the opportunity window")
        if self.country_code:
            self.country_code = self.country_code.upper()
        if self.status == "active" and self.priority_source == "tie_requires_decision":
            raise ValueError("an active opportunity requires an explicit priority policy")
        if self.status == "active" and not self.priority_reason:
            raise ValueError("an active opportunity requires priority_reason")
        return self


class OpportunityRuleInput(BaseModel):
    rule_key: str = Field(..., min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(..., min_length=1, max_length=255)
    rule_type: Literal["weekly", "month_boundary"]
    timezone: str = Field("UTC", min_length=1, max_length=64)
    country_code: str | None = Field(None, min_length=2, max_length=2)
    region_code: str | None = Field(None, min_length=1, max_length=80)
    priority: int = Field(0, ge=-1000, le=1000)
    priority_source: Literal[
        "tenant_policy", "historical_evidence", "bounded_learning", "tie_requires_decision"
    ] = "tie_requires_decision"
    priority_reason: str | None = Field(None, min_length=3, max_length=2000)
    purpose_keys: list[str] = Field(default_factory=list, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)


class OpportunityPreviewInput(BaseModel):
    horizon_start: datetime
    horizon_end: datetime
    rules: list[OpportunityRuleInput] = Field(default_factory=list, max_length=100)
    include_persisted: bool = True
    include_drafts: bool = False
    purpose_key: str | None = Field(None, min_length=1, max_length=120)
    country_code: str | None = Field(None, min_length=2, max_length=2)
    region_code: str | None = Field(None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_horizon(self):
        if self.horizon_end <= self.horizon_start:
            raise ValueError("horizon_end must be after horizon_start")
        if (self.horizon_end - self.horizon_start).days > 370:
            raise ValueError("opportunity preview horizon cannot exceed 370 days")
        if self.country_code:
            self.country_code = self.country_code.upper()
        return self
