from typing import Any, Literal
from pydantic import BaseModel, Field


class CapabilityInvoke(BaseModel):
    phase: Literal["inspect", "simulate", "propose", "execute"]
    idempotency_key: str = Field(..., min_length=8, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    decision_choice_id: int | None = Field(None, ge=1)
