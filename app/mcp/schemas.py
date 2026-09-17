"""MCP-specific shared schemas."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class McpToolResult(BaseModel):
    ok: bool = True
    data: Dict[str, Any] = Field(default_factory=dict)


class McpProjectRef(BaseModel):
    id: int
    name: str
    role: str


class McpPendingConfirmation(BaseModel):
    status: str = "pending_confirmation"
    pending_action_id: int
    confirm_token: str
    expires_at: datetime


class McpAuditSummary(BaseModel):
    tool_name: str
    status: str
    project_id: Optional[int] = None
    input_summary: Optional[Dict[str, Any]] = None
    output_summary: Optional[Dict[str, Any]] = None


class McpReadonlySqlResult(BaseModel):
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool = False
