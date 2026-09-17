from typing import Any, Literal
from pydantic import BaseModel, Field


class EngineRolloutUpdate(BaseModel):
    feature_key: str = Field(..., min_length=1, max_length=80)
    mode: Literal["inherit", "off", "shadow", "enforce"]
    config: dict[str, Any] | None = None
    expected_version: int = Field(0, ge=0)


class EngineRolloutRequest(BaseModel):
    updates: list[EngineRolloutUpdate] = Field(..., min_length=1, max_length=30)
