"""
Identity Resolution Pydantic Schemas
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# ==========================================
# Contact Identity Schemas
# ==========================================

class ContactIdentityResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    identity_type: str
    identity_value: str
    channel_instance_id: Optional[int] = None
    verified: bool
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


# ==========================================
# Merge Log Schemas
# ==========================================

class MergeLogResponse(BaseModel):
    id: int
    project_id: int
    winner_id: Optional[int] = None
    loser_id: Optional[int] = None
    triggered_by: str
    triggered_by_user_id: Optional[int] = None
    snapshot_winner: Dict[str, Any]
    snapshot_loser: Dict[str, Any]
    identities_transferred: Optional[Any] = None
    properties_resolved: Optional[Dict[str, Any]] = None
    created_at: datetime
    undone_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ==========================================
# Merge Suggestion Schemas
# ==========================================

class MergeSuggestionResponse(BaseModel):
    id: int
    project_id: int
    contact_a_id: int
    contact_b_id: int
    match_reason: str
    match_confidence: str
    status: str
    reviewed_by: Optional[int] = None
    reviewed_at: Optional[datetime] = None
    created_at: datetime
    # Inline contact summaries for UI display
    contact_a_summary: Optional[Dict[str, Any]] = None
    contact_b_summary: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


# ==========================================
# Account Schemas
# ==========================================

class AccountCreate(BaseModel):
    external_id: str = Field(..., min_length=1, max_length=255)
    name: Optional[str] = Field(None, max_length=255)
    properties: Optional[Dict[str, Any]] = None


class AccountUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    properties: Optional[Dict[str, Any]] = None


class AccountResponse(BaseModel):
    id: int
    project_id: int
    external_id: str
    name: Optional[str] = None
    properties: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime
    contact_count: Optional[int] = None

    class Config:
        from_attributes = True


# ==========================================
# Manual Merge Request
# ==========================================

class ManualMergeRequest(BaseModel):
    winner_id: int = Field(..., description="Contact ID to keep")
    loser_id: int = Field(..., description="Contact ID to merge into winner")
