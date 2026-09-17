"""
Pydantic schemas for project channel configurations.
"""
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel


class ProjectChannelConfigResponse(BaseModel):
    id: int
    project_id: int
    channel: str
    enabled: bool
    config: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ProjectChannelConfigUpsert(BaseModel):
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
