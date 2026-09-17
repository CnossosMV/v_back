"""
Pydantic schemas for Email Inbound management.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class EmailInboundAddressCreate(BaseModel):
    address: str
    label: str
    is_active: bool = True
    default_handler_type: Optional[str] = None
    default_handler_id: Optional[int] = None


class EmailInboundAddressUpdate(BaseModel):
    label: Optional[str] = None
    is_active: Optional[bool] = None
    default_handler_type: Optional[str] = None
    default_handler_id: Optional[int] = None


class EmailInboundAddressResponse(BaseModel):
    id: int
    instance_id: int
    project_id: int
    address: str
    label: str
    is_active: bool
    default_handler_type: Optional[str] = None
    default_handler_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MXVerificationResponse(BaseModel):
    status: str  # verified | failed | pending
    mx_records_found: list = []
    message: Optional[str] = None


class InboundEnableRequest(BaseModel):
    provider: str  # ses_inbound | mailgun_inbound | sendgrid_inbound | imap_poll | cloudflare_email_workers


class InboundStatusResponse(BaseModel):
    inbound_enabled: bool
    inbound_provider: Optional[str] = None
    mx_record_status: Optional[str] = None
    mx_record_verified_at: Optional[datetime] = None
    webhook_url: Optional[str] = None
    address_count: int = 0
