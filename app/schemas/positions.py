"""Pydantic schemas for the contact Position read API (Phase 3)."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class ContactPositionResponse(BaseModel):
    user_id: int
    lifecycle_model_id: int
    type: str
    stage: Optional[str] = None
    age_bucket: Optional[str] = None
    position_entered_at: Optional[datetime] = None
    type_entered_at: Optional[datetime] = None
    stage_entered_at: Optional[datetime] = None
    computed_at: datetime
    provenance: Optional[dict] = None
    explanation: Optional[dict] = None

    class Config:
        from_attributes = True


class PositionTransitionResponse(BaseModel):
    lifecycle_model_id: int
    from_type: Optional[str] = None
    to_type: Optional[str] = None
    from_stage: Optional[str] = None
    to_stage: Optional[str] = None
    reason: Optional[str] = None
    provenance: Optional[dict] = None
    occurred_at: datetime

    class Config:
        from_attributes = True


class ContactPositionDetailResponse(BaseModel):
    user_id: int
    lifecycle_model_id: Optional[int] = None
    lifecycle_model_status: Optional[str] = None
    position: Optional[ContactPositionResponse] = None
    transitions: List[PositionTransitionResponse] = []


class PositionCellCount(BaseModel):
    type: str
    stage: Optional[str] = None
    age_bucket: Optional[str] = None
    count: int


class PositionGridResponse(BaseModel):
    cells: List[PositionCellCount]
    total: int
    lifecycle_model_id: Optional[int] = None
    lifecycle_model_status: Optional[str] = None
