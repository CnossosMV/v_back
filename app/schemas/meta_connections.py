"""Schemas for Meta (Messenger/Instagram) page connections + OAuth flow."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class MetaOAuthStartResponse(BaseModel):
    session_id: int
    auth_url: str


class MetaOAuthPageOption(BaseModel):
    page_id: str
    name: Optional[str] = None
    ig_account_id: Optional[str] = None
    ig_username: Optional[str] = None
    already_connected: bool = False


class MetaOAuthSessionResponse(BaseModel):
    session_id: int
    status: str  # pending | authorized | completed | error
    pages: List[MetaOAuthPageOption] = []
    error: Optional[str] = None


class MetaFinalizePageSelection(BaseModel):
    page_id: str
    messenger_enabled: bool = True
    instagram_enabled: bool = True
    fb_comments_enabled: bool = False
    ig_comments_enabled: bool = False


class MetaFinalizeRequest(BaseModel):
    pages: List[MetaFinalizePageSelection]


class MetaConnectionUpdateRequest(BaseModel):
    messenger_enabled: Optional[bool] = None
    instagram_enabled: Optional[bool] = None
    fb_comments_enabled: Optional[bool] = None
    ig_comments_enabled: Optional[bool] = None
    is_active: Optional[bool] = None


class MetaConnectionResponse(BaseModel):
    id: int
    project_id: int
    page_id: str
    page_name: Optional[str] = None
    ig_account_id: Optional[str] = None
    ig_username: Optional[str] = None
    messenger_enabled: bool
    instagram_enabled: bool
    fb_comments_enabled: bool
    ig_comments_enabled: bool
    status: str
    last_error: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MetaFinalizeResponse(BaseModel):
    connections: List[MetaConnectionResponse]
    errors: List[str] = []
