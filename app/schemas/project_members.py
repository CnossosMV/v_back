"""
Project Members schemas.
"""

from pydantic import BaseModel, EmailStr
from typing import Optional, List
from datetime import datetime


class ProjectMemberResponse(BaseModel):
    id: int
    project_id: int
    user_id: int
    role: str
    is_active: bool
    invited_by_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    user_name: Optional[str] = None
    user_email: Optional[str] = None

    class Config:
        from_attributes = True


class AddMemberRequest(BaseModel):
    email: EmailStr
    role: str = "viewer"
    name: Optional[str] = None


class UpdateMemberRequest(BaseModel):
    role: Optional[str] = None
    is_active: Optional[bool] = None


class AgentResponse(BaseModel):
    user_id: int
    name: str
    email: str
    role: str

    class Config:
        from_attributes = True


class UserProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    workspace_id: int
    is_active: bool
    role: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
