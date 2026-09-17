from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field


VerificationType = Literal["email", "whatsapp"]
SelectionScope = Literal["all", "never_verified", "stale", "recent"]
SendPolicy = Literal["report_only", "block_invalid", "block_invalid_and_risky"]


class VerificationSelection(BaseModel):
    scope: SelectionScope = "all"
    days: Optional[int] = Field(None, ge=1, le=3650)
    recent_field: Optional[Literal["created_at", "updated_at", "last_seen_at"]] = None


class VerificationPreviewRequest(BaseModel):
    verification_type: VerificationType
    selection: VerificationSelection


class VerificationJobCreate(VerificationPreviewRequest):
    expected_candidate_count: int = Field(..., ge=0)
    expected_unique_count: int = Field(..., ge=0)
    idempotency_key: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class VerificationSettingsUpdate(BaseModel):
    email_auto_verify: Optional[bool] = None
    whatsapp_auto_verify: Optional[bool] = None
    email_verify_on_first_seen: Optional[bool] = None
    email_recheck_enabled: Optional[bool] = None
    whatsapp_verify_on_first_seen: Optional[bool] = None
    whatsapp_recheck_enabled: Optional[bool] = None
    email_recheck_days: Optional[int] = Field(None, ge=1, le=3650)
    whatsapp_recheck_days: Optional[int] = Field(None, ge=1, le=3650)
    email_send_policy: Optional[SendPolicy] = None
    whatsapp_send_policy: Optional[SendPolicy] = None
    whatsapp_instance_id: Optional[int] = None


class VerificationSettingsResponse(BaseModel):
    project_id: int
    email_provider: str
    whatsapp_provider: str
    whatsapp_instance_id: Optional[int]
    email_auto_verify: bool
    whatsapp_auto_verify: bool
    email_verify_on_first_seen: bool = False
    email_recheck_enabled: bool = False
    whatsapp_verify_on_first_seen: bool = False
    whatsapp_recheck_enabled: bool = False
    email_recheck_days: int
    whatsapp_recheck_days: int
    email_send_policy: str
    whatsapp_send_policy: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class VerificationJobResponse(BaseModel):
    id: int
    project_id: int
    endpoint_id: Optional[int] = None
    verification_type: str
    provider: str
    source_type: str
    trigger_type: str
    status: str
    selection: Optional[Dict[str, Any]] = None
    candidate_count: int
    unique_count: int
    processed_count: int
    provider_progress: Optional[int] = None
    valid_count: int
    invalid_count: int
    risky_count: int
    skipped_count: int
    failed_count: int
    provider_units: int
    billing_disposition: str
    cancel_requested: bool
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ContactVerificationSnapshot(BaseModel):
    verification_type: str
    status: str
    provider: str
    provider_key_source: str
    provider_version: Optional[str] = None
    provider_status: Optional[str] = None
    checked_at: datetime
    expires_at: datetime
    is_expired: bool
    last_attempt_status: str
    last_error_code: Optional[str] = None


class VerifyContactRequest(BaseModel):
    verification_type: VerificationType
    idempotency_key: str = Field(
        ...,
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
