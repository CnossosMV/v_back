"""API contracts for reusable contact groups."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ContactGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    group_type: Literal["static", "dynamic", "imported"] = "dynamic"
    rule_config: dict[str, Any] | None = None
    source_ref: str | None = Field(None, max_length=255)


class ContactGroupUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    group_type: Literal["static", "dynamic", "imported"] | None = None
    rule_config: dict[str, Any] | None = None
    source_ref: str | None = Field(None, max_length=255)
    status: Literal["active", "paused", "archived"] | None = None


class ContactGroupPreviewRequest(BaseModel):
    rule_config: dict[str, Any] | None = None
    limit: int = Field(20, ge=0, le=100)


class ContactGroupMembersRequest(BaseModel):
    user_ids: list[int] = Field(..., min_length=1, max_length=10000)
    source: str = Field("manual", min_length=1, max_length=50)


class ContactGroupResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: str | None = None
    group_type: str
    rule_config: dict[str, Any] | None = None
    source_ref: str | None = None
    status: str
    created_by_user_id: int | None = None
    last_evaluated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
