"""Pydantic schemas for the Scoring Engine."""

from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Signal / Decay / Threshold configs
# ============================================================================

class SignalConfig(BaseModel):
    event_name: str
    weight: float = 1.0
    aggregate: str = "count"  # count, sum, exists, last_value
    window_days: Optional[int] = None  # None = all-time
    decay_type: Optional[str] = None  # exponential, linear
    half_life_days: Optional[float] = None
    max_contribution: Optional[float] = None
    label: Optional[str] = None


class DecayConfig(BaseModel):
    type: str = "exponential"  # exponential, linear
    half_life_days: float = 7.0


class ThresholdConfig(BaseModel):
    hot: float = 70.0
    warm: float = 40.0
    cold: float = 0.0


# ============================================================================
# Score Definition CRUD
# ============================================================================

class ScoreDefinitionCreate(BaseModel):
    name: str
    slug: str
    description: Optional[str] = None
    score_type: str = "intent"  # intent, friction, churn_risk, custom
    signals: List[SignalConfig] = []
    decay_config: Optional[DecayConfig] = None
    thresholds: Optional[ThresholdConfig] = None
    normalization_max: float = 100
    recalc_on_event: bool = True
    recalc_interval_minutes: int = 0


class ScoreDefinitionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    signals: Optional[List[SignalConfig]] = None
    decay_config: Optional[DecayConfig] = None
    thresholds: Optional[ThresholdConfig] = None
    normalization_max: Optional[float] = None
    recalc_on_event: Optional[bool] = None
    recalc_interval_minutes: Optional[int] = None


class ScoreDefinitionResponse(BaseModel):
    id: int
    project_id: int
    name: str
    slug: str
    description: Optional[str] = None
    score_type: str
    version: int
    status: str
    signals: List[Dict[str, Any]] = []
    decay_config: Optional[Dict[str, Any]] = None
    thresholds: Optional[Dict[str, Any]] = None
    normalization_max: float
    recalc_on_event: bool
    recalc_interval_minutes: int
    last_recalc_at: Optional[datetime] = None
    created_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    user_count: int = 0

    class Config:
        from_attributes = True


# ============================================================================
# User Score
# ============================================================================

class UserScoreResponse(BaseModel):
    score_definition_id: int
    slug: str
    score_type: str
    name: str
    score: float
    tier: str
    previous_score: Optional[float] = None
    score_delta: Optional[float] = None
    calculated_at: datetime

    class Config:
        from_attributes = True


class ScoreExplanationItem(BaseModel):
    signal: str
    event_name: str
    raw_value: float
    weight: float
    decay_factor: float
    weighted_value: float
    contribution_pct: float


class ScoreExplanationResponse(BaseModel):
    score: float
    tier: str
    explanation: List[ScoreExplanationItem] = []
    calculated_at: datetime


# ============================================================================
# Analytics
# ============================================================================

class ScoreDistributionBucket(BaseModel):
    bucket_start: float
    bucket_end: float
    count: int


class TierCountResponse(BaseModel):
    hot: int = 0
    warm: int = 0
    cold: int = 0
    total: int = 0
