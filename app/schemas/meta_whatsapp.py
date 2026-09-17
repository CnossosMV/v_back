"""
Schemas for Meta WhatsApp Cloud API endpoints.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# ── Request schemas ─────────────────────────────────────────────────────

class MetaWhatsAppConnectRequest(BaseModel):
    phone_number_id: str = Field(min_length=1)
    waba_id: str = Field(min_length=1)
    access_token: str = Field(min_length=1)
    # Required — webhooks fail closed without signature verification
    app_secret: str = Field(min_length=1)
    business_name: Optional[str] = None
    default_country_code: Optional[str] = None


class MetaWhatsAppUpdateRequest(BaseModel):
    access_token: Optional[str] = None
    app_secret: Optional[str] = None
    business_name: Optional[str] = None
    default_country_code: Optional[str] = None


class MetaTestMessageRequest(BaseModel):
    to_number: str
    message_type: str = "text"  # "text" | "template"
    text: Optional[str] = None
    template_name: Optional[str] = None
    template_language: Optional[str] = "en_US"
    template_components: Optional[List[Dict[str, Any]]] = None


# ── Response schemas ────────────────────────────────────────────────────

class MetaWhatsAppInstanceResponse(BaseModel):
    id: int
    provider_type: str
    phone_number_id: Optional[str] = None
    waba_id: Optional[str] = None
    phone_number: Optional[str] = None
    business_name: Optional[str] = None
    connection_status: str
    webhook_url: Optional[str] = None
    webhook_verify_token: Optional[str] = None
    default_country_code: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContactWindowResponse(BaseModel):
    contact_phone: str
    window_opens_at: datetime
    window_expires_at: datetime
    is_open: bool

    class Config:
        from_attributes = True
