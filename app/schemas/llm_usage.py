"""Schemas for LLM usage tracking API."""
from datetime import datetime, date
from typing import List, Optional
from pydantic import BaseModel


class UsageByGroup(BaseModel):
    group: str  # provider name, model name, purpose, or key_source
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_estimate: float = 0.0
    call_count: int = 0


class UsageSummaryResponse(BaseModel):
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_cost: float = 0.0
    total_calls: int = 0
    by_provider: List[UsageByGroup] = []
    by_model: List[UsageByGroup] = []
    by_purpose: List[UsageByGroup] = []
    by_key_source: List[UsageByGroup] = []


class DailyUsagePoint(BaseModel):
    date: date
    total_tokens: int = 0
    cost_estimate: float = 0.0
    call_count: int = 0


class DailyUsageResponse(BaseModel):
    days: List[DailyUsagePoint] = []


class SystemProviderStatus(BaseModel):
    provider: str
    available: bool
    has_rate_limits: bool = False
