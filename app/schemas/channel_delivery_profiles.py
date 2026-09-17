"""API contracts for sender identities and channel capacity profiles."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class SenderIdentityCreate(BaseModel):
    channel: str = Field(..., min_length=1, max_length=50)
    provider: str = Field(..., min_length=1, max_length=80)
    identity_key: str = Field(..., min_length=1, max_length=255)
    address: str | None = Field(None, max_length=500)
    display_name: str | None = Field(None, max_length=255)
    reply_to: str | None = Field(None, max_length=500)
    external_instance_id: str | None = Field(None, max_length=255)
    is_default: bool = False
    capabilities: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class SenderIdentityUpdate(BaseModel):
    address: str | None = Field(None, max_length=500)
    display_name: str | None = Field(None, max_length=255)
    reply_to: str | None = Field(None, max_length=500)
    external_instance_id: str | None = Field(None, max_length=255)
    status: Literal["active", "paused", "disabled"] | None = None
    is_default: bool | None = None
    capabilities: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class DeliveryProfileCreate(BaseModel):
    channel: str = Field(..., min_length=1, max_length=50)
    provider: str = Field(..., min_length=1, max_length=80)
    name: str = Field(..., min_length=1, max_length=255)
    sender_identity_id: int | None = None
    max_per_minute: int | None = Field(None, ge=1)
    max_per_hour: int | None = Field(None, ge=1)
    max_per_day: int | None = Field(None, ge=1)
    concurrency_limit: int | None = Field(None, ge=1)
    priority: int = 0
    weight: int = Field(100, ge=1)
    timezone: str | None = Field(None, max_length=64)
    warmup_config: dict[str, Any] | None = None
    config: dict[str, Any] | None = None


class AvailableSenderResponse(BaseModel):
    kind: Literal["email_instance", "smtp_config"]
    id: int
    provider: str
    name: str
    address: str


class DeliveryProfileUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(None, min_length=1, max_length=255)
    sender_identity_id: int | None = None
    max_per_minute: int | None = Field(None, ge=1)
    max_per_hour: int | None = Field(None, ge=1)
    max_per_day: int | None = Field(None, ge=1)
    concurrency_limit: int | None = Field(None, ge=1)
    priority: int | None = None
    weight: int | None = Field(None, ge=1)
    timezone: str | None = Field(None, max_length=64)
    warmup_config: dict[str, Any] | None = None
    status: Literal["active", "paused", "disabled"] | None = None
    config: dict[str, Any] | None = None


class DeliveryProfileResponse(BaseModel):
    id: int
    project_id: int
    channel: str
    provider: str
    name: str
    sender_identity_id: int | None = None
    max_per_minute: int | None = None
    max_per_hour: int | None = None
    max_per_day: int | None = None
    concurrency_limit: int | None = None
    priority: int
    weight: int
    timezone: str | None = None
    warmup_config: dict[str, Any] | None = None
    health_status: str
    status: str
    config: dict[str, Any] | None = None
    last_health_check_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
