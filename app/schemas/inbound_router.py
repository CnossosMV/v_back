"""
Schemas for the Inbound Message Router.

Includes:
- ContentPiece / InboundMessage (runtime, not persisted directly)
- ContactRoutingState CRUD
- InboxAssignmentRule CRUD
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ── Runtime message types (not DB-backed) ───────────────────────────────

class ContentPiece(BaseModel):
    """A single piece of content within an inbound message."""
    type: str  # text, image, audio, video, document, location, contact, sticker, reaction
    body: Optional[str] = None
    media_url: Optional[str] = None  # URL or media ID for download
    media_mime: Optional[str] = None
    resolved_text: Optional[str] = None  # After processing (transcription, OCR, etc.)


class EmailThreadInfo(BaseModel):
    """Email threading metadata from headers."""
    message_id: Optional[str] = None       # Message-ID header
    in_reply_to: Optional[str] = None      # In-Reply-To header
    references: Optional[List[str]] = None  # References header chain
    subject: Optional[str] = None          # Subject line


class InboundMessage(BaseModel):
    """
    Canonical inbound message — produced by channel adapters, consumed by InboundRouter.
    This is a runtime object, not stored directly.
    """
    channel: str  # whatsapp, sms, web, email
    provider_type: str  # evolution_api, meta_cloud_api, twilio_sms, twilio_whatsapp, web, ses_inbound, mailgun_inbound, sendgrid_inbound
    contact_identifier: str  # Phone number, email, or anonymous ID
    project_id: Optional[int] = None  # Resolved from instance linkage
    instance_id: Optional[int] = None  # WhatsApp instance ID or SMTP config ID
    instance_name: Optional[str] = None  # WhatsApp instance name
    provider_id: Optional[int] = None  # Messaging provider ID (Twilio, etc.)
    content_pieces: List[ContentPiece] = []
    resolved_text: Optional[str] = None  # Full assembled text after media processing
    raw_payload: Optional[Dict[str, Any]] = None
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    push_name: Optional[str] = None
    # Channel-specific context blob (replaces flat WhatsApp-specific fields)
    channel_context: Dict[str, Any] = {}
    # Email threading
    thread: Optional[EmailThreadInfo] = None
    inbound_address_id: Optional[int] = None

    # Backward-compat properties for existing consumers
    @property
    def remote_jid(self) -> Optional[str]:
        return self.channel_context.get("remote_jid")

    @property
    def remote_jid_alt(self) -> Optional[str]:
        return self.channel_context.get("remote_jid_alt")

    @property
    def customer_phone(self) -> Optional[str]:
        return self.channel_context.get("customer_phone")


# ── ContactRoutingState schemas ─────────────────────────────────────────

class ContactRoutingStateResponse(BaseModel):
    id: int
    project_id: int
    contact_identifier: str
    channel: str
    handler_type: str
    handler_id: Optional[int] = None
    handler_priority: int
    session_id: Optional[int] = None
    enrollment_id: Optional[int] = None
    assigned_at: datetime
    expires_at: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = Field(None, validation_alias="routing_metadata")

    class Config:
        from_attributes = True


# ── InboxAssignmentRule schemas ─────────────────────────────────────────

class InboxRuleCreate(BaseModel):
    name: str
    priority: int = 0
    conditions: Optional[List[Dict[str, Any]]] = None
    match_mode: str = "all"
    destination_type: str  # chatbot, agent_team, human_queue
    destination_id: Optional[int] = None
    is_active: bool = True


class InboxRuleUpdate(BaseModel):
    name: Optional[str] = None
    priority: Optional[int] = None
    conditions: Optional[List[Dict[str, Any]]] = None
    match_mode: Optional[str] = None
    destination_type: Optional[str] = None
    destination_id: Optional[int] = None
    is_active: Optional[bool] = None


class InboxRuleResponse(BaseModel):
    id: int
    project_id: int
    name: str
    priority: int
    conditions: Optional[List[Dict[str, Any]]] = None
    match_mode: str
    destination_type: str
    destination_id: Optional[int] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InboxRuleReorderRequest(BaseModel):
    rule_ids: List[int]  # Ordered list of rule IDs
