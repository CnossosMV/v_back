"""Pydantic schemas for Message Effectiveness Scoring (MES)."""

from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# MES Score (per-message)
# ============================================================================

class MESScoreResponse(BaseModel):
    send_log_id: int
    channel: str
    template_id: Optional[int] = None
    source_type: str
    reach_score: float
    reengagement_score: float
    combined_score: float
    score_grade: str
    reach_signals: Optional[Dict[str, Any]] = None
    reengagement_signals: Optional[Dict[str, Any]] = None
    algorithm_version: int
    attribution_window_h: int
    computed_at: Optional[datetime] = None
    stale: bool
    settled: bool = False

    class Config:
        from_attributes = True


# ============================================================================
# Aggregations
# ============================================================================

class MESAggregateItem(BaseModel):
    key: str
    label: Optional[str] = None
    avg_reach: float
    avg_reengagement: float
    avg_combined: float
    sample_size: int
    grade_distribution: Dict[str, int]


class TemplateEffectivenessItem(BaseModel):
    template_id: int
    name: str
    slug: str
    locale: Optional[str] = None
    channel_type: Optional[str] = None
    last_sent_at: Optional[datetime] = None
    total_sends: int
    delivered_count: int
    read_count: int
    failed_count: int
    delivery_rate: float
    failure_rate: float
    read_rate: float
    unique_opens: int
    unique_clicks: int
    open_rate: float
    click_rate: float
    replied_count: int = 0
    reply_rate: float = 0.0
    avg_reach: float
    avg_reengagement: float
    avg_combined: float
    avg_combined_smoothed: float = 0.0
    mes_sample_size: int
    grade_distribution: Dict[str, int]


class MESStatsResponse(BaseModel):
    total_scored: int
    total_pending: int
    avg_reach: float
    avg_reengagement: float
    avg_combined: float
    grade_distribution: Dict[str, int]


class MESFunnelStepItem(BaseModel):
    step_id: int
    step_type: str
    position: int
    branch: str
    avg_reach: float
    avg_reengagement: float
    avg_combined: float
    sample_size: int
    grade_distribution: Dict[str, int]


# ============================================================================
# Configuration
# ============================================================================

class MESConfigResponse(BaseModel):
    project_id: int
    enabled: bool
    attribution_window_hours: int
    pre_session_window_min: int
    reach_weight: float
    reengagement_weight: float
    grade_thresholds: Optional[Dict[str, float]] = None
    reengagement_event_weights: Optional[Dict[str, float]] = None
    exclude_event_patterns: Optional[List[str]] = None
    speed_decay_rate: float
    event_decay_rate: float
    link_tracking_channels: Optional[List[str]] = None

    class Config:
        from_attributes = True


class BatchScoreRequest(BaseModel):
    send_log_ids: List[int] = Field(..., max_length=200)


class MESConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    attribution_window_hours: Optional[int] = None
    pre_session_window_min: Optional[int] = None
    reach_weight: Optional[float] = None
    reengagement_weight: Optional[float] = None
    grade_thresholds: Optional[Dict[str, float]] = None
    reengagement_event_weights: Optional[Dict[str, float]] = None
    exclude_event_patterns: Optional[List[str]] = None
    speed_decay_rate: Optional[float] = None
    event_decay_rate: Optional[float] = None
    link_tracking_channels: Optional[List[str]] = None
