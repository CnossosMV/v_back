"""Pydantic schemas for the Segment Rules engine."""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Segment Rule CRUD
# ============================================================================

class SegmentCondition(BaseModel):
    field: str
    operator: str  # ==, !=, >, >=, <, <=, contains, not_contains, exists, not_exists, in, not_in
    value: Any = None


class SegmentRuleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    conditions: List[SegmentCondition] = []
    match_mode: str = "all"  # all (AND), any (OR)
    is_catch_all: bool = False
    is_active: bool = True


class SegmentRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    conditions: Optional[List[SegmentCondition]] = None
    match_mode: Optional[str] = None
    is_catch_all: Optional[bool] = None
    is_active: Optional[bool] = None


class SegmentRuleResponse(BaseModel):
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    priority: int
    conditions: List[Dict[str, Any]] = []
    match_mode: str
    is_catch_all: bool
    is_active: bool
    contact_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ============================================================================
# Reorder
# ============================================================================

class SegmentRuleReorder(BaseModel):
    rule_ids: List[int]


# ============================================================================
# Preview / Test / Batch
# ============================================================================

class SegmentPreviewItem(BaseModel):
    rule_id: int
    rule_name: str
    contact_count: int


class SegmentPreviewResponse(BaseModel):
    items: List[SegmentPreviewItem] = []
    unmatched_count: int = 0


class SegmentTestRuleResult(BaseModel):
    rule_id: int
    rule_name: str
    priority: int
    matched: bool
    is_catch_all: bool


class SegmentTestResponse(BaseModel):
    user_id: int
    current_segment: Optional[str] = None
    evaluated_segment: Optional[str] = None
    results: List[SegmentTestRuleResult] = []


class SegmentBatchEvalResponse(BaseModel):
    total_evaluated: int = 0
    transitions: int = 0
    duration_ms: int = 0
