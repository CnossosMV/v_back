"""Pydantic schemas for Event Graph / Journey API."""

from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List, Dict, Any


# ============================================================================
# Sub-DTOs
# ============================================================================

class ThermalBreakdownDTO(BaseModel):
    hot: int = 0
    warm: int = 0
    cold: int = 0
    dead: int = 0


# ============================================================================
# Node / Edge / Intervention
# ============================================================================

class JourneyNodeDTO(BaseModel):
    event_name: str
    display_name: str
    category: str
    frequency: int
    unique_users: int
    current_occupancy: int
    thermal: ThermalBreakdownDTO
    throughput_rate: Optional[float] = None
    avg_dwell_seconds: Optional[float] = None
    conversion_rate: Optional[float] = None
    is_bottleneck: bool = False
    position: Optional[Dict[str, float]] = None


class JourneyEdgeDTO(BaseModel):
    source: str
    target: str
    volume: int
    pct_from_source: float
    median_seconds: Optional[float] = None
    p90_seconds: Optional[float] = None
    converted_volume: int = 0
    beta_alpha: float = 1.0
    beta_beta: float = 1.0
    beta_mean: Optional[float] = None
    beta_ci_lower: Optional[float] = None
    beta_ci_upper: Optional[float] = None
    is_conversion_path: bool = False
    drop_off_rate: Optional[float] = None


class JourneyInterventionDTO(BaseModel):
    pre_event: str
    post_event: Optional[str] = None
    channel: str
    source_type: str
    arm_id: str
    total_sent: int
    total_responded: int
    response_rate: float = 0.0
    beta_alpha: float = 1.0
    beta_beta: float = 1.0


class TerminalNodeDTO(BaseModel):
    type: str  # converted, churned, engagement_timeout
    count: int
    from_events: List[Dict[str, Any]] = []


class GraphMetadataDTO(BaseModel):
    total_users: int
    total_events: int
    conversion_event: Optional[str] = None
    has_goal_configured: bool = False
    date_range: Dict[str, str]
    last_materialized_at: Optional[str] = None


# ============================================================================
# Aggregate Graph Response
# ============================================================================

class JourneyGraphResponse(BaseModel):
    nodes: List[JourneyNodeDTO]
    edges: List[JourneyEdgeDTO]
    interventions: Optional[List[JourneyInterventionDTO]] = None
    terminal_nodes: List[TerminalNodeDTO] = []
    metadata: GraphMetadataDTO


# ============================================================================
# User Journey
# ============================================================================

class StepWarning(BaseModel):
    type: str  # burst, shadow_pair, repeated
    message: str
    related_steps: List[int] = []


class UserPathStepDTO(BaseModel):
    event_name: str
    timestamp: str
    session_id: Optional[str] = None
    is_intervention: bool = False
    properties: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    dwell_seconds: Optional[float] = None
    warnings: List[StepWarning] = []


class UserInterventionDTO(BaseModel):
    arm_id: str
    channel: str
    event_name: str
    timestamp: str
    responded: bool = False


class UserJourneyResponse(JourneyGraphResponse):
    user_path: List[UserPathStepDTO] = []
    current_position: Optional[str] = None
    thermal_state: Optional[str] = None
    terminal_state: Optional[str] = None
    interventions_received: List[UserInterventionDTO] = []


# ============================================================================
# Node Detail
# ============================================================================

class EdgeSummaryDTO(BaseModel):
    event_name: str
    volume: int
    pct: float


class NodeDetailResponse(BaseModel):
    event_name: str
    display_name: str
    category: str
    frequency: int
    unique_users: int
    current_occupancy: int
    thermal: ThermalBreakdownDTO
    throughput_rate: Optional[float] = None
    avg_dwell_seconds: Optional[float] = None
    conversion_rate: Optional[float] = None
    is_bottleneck: bool = False
    top_incoming: List[EdgeSummaryDTO] = []
    top_outgoing: List[EdgeSummaryDTO] = []


# ============================================================================
# Backfill / Config
# ============================================================================

class BackfillJobResponse(BaseModel):
    id: int
    project_id: int
    job_type: str
    status: str
    total_rows: Optional[int] = None
    processed_rows: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        from_attributes = True


class GoalEventUpdate(BaseModel):
    goal_event: Optional[str] = Field(None, max_length=255)


# ============================================================================
# Data Health
# ============================================================================

class DataHealthRecommendation(BaseModel):
    severity: str  # high, medium, low
    category: str  # shadow_pair, burst, repeated
    title: str
    description: str
    affected_events: List[str] = []
    suggestion: str


class DataHealthResponse(BaseModel):
    score: int  # 0-100
    total_events_analyzed: int
    recommendations: List[DataHealthRecommendation] = []
