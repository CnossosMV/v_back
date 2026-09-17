"""Pydantic schemas for the Policy Layer."""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Config nested types
# ============================================================================

class ContactCapsConfig(BaseModel):
    daily: Optional[Dict[str, int]] = None  # {email: 3, whatsapp: 5, total: 8}
    weekly: Optional[Dict[str, int]] = None
    monthly: Optional[Dict[str, int]] = None


class ChannelCooldownsConfig(BaseModel):
    email: Optional[int] = None  # seconds
    whatsapp: Optional[int] = None
    sms: Optional[int] = None
    push: Optional[int] = None


class QuietHoursConfig(BaseModel):
    enabled: bool = False
    start: str = "22:00"
    end: str = "08:00"
    timezone: str = "UTC"
    channels: List[str] = []  # channels affected, empty = all


class PriorityRulesConfig(BaseModel):
    order: List[str] = ["agent_team", "event_action", "funnel", "template"]
    conflict_window_seconds: int = 60


# ============================================================================
# Policy CRUD
# ============================================================================

class ProjectPolicyCreate(BaseModel):
    contact_caps: Optional[Dict[str, Any]] = None
    channel_cooldowns: Optional[Dict[str, Any]] = None
    quiet_hours: Optional[Dict[str, Any]] = None
    suppression_config: Optional[Dict[str, Any]] = None
    priority_rules: Optional[Dict[str, Any]] = None
    is_active: bool = True


class ProjectPolicyResponse(BaseModel):
    id: int
    project_id: int
    contact_caps: Optional[Dict[str, Any]] = None
    channel_cooldowns: Optional[Dict[str, Any]] = None
    quiet_hours: Optional[Dict[str, Any]] = None
    suppression_config: Optional[Dict[str, Any]] = None
    priority_rules: Optional[Dict[str, Any]] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Policy Decision
# ============================================================================

class PolicyDecision(BaseModel):
    allowed: bool = True
    reason: Optional[str] = None
    policy_violated: Optional[str] = None
    deferrable: bool = False
    defer_until: Optional[datetime] = None


# ============================================================================
# Contact Ledger
# ============================================================================

class ContactLedgerEntry(BaseModel):
    id: int
    user_id: int
    channel: str
    source: str
    source_id: Optional[str] = None
    sent_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Stats
# ============================================================================

class ChannelStats(BaseModel):
    channel: str
    today: int = 0
    this_week: int = 0
    this_month: int = 0


class ContactStatsResponse(BaseModel):
    channels: List[ChannelStats] = []
    total_today: int = 0
    total_this_week: int = 0
    total_this_month: int = 0
