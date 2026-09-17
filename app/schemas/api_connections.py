"""
Pydantic schemas for Project API Connections.

Covers connections, endpoints, executions, spec parsing, testing, and tool generation.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


# ============================================================================
# Connection Schemas
# ============================================================================

class ConnectionCreate(BaseModel):
    """Create a new API connection."""
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    base_url: str = Field(..., min_length=1, max_length=500)
    auth_type: str = Field("none", description="api_key_header, bearer_token, basic_auth, custom_header, none")
    auth_secret: Optional[str] = Field(None, description="The secret value (will be encrypted)")
    auth_header_name: Optional[str] = Field(None, max_length=100)
    auth_header_prefix: Optional[str] = Field(None, max_length=50)
    default_headers: Optional[Dict[str, str]] = None
    timeout_ms: int = Field(30000, ge=1000, le=120000)
    rate_limit_rpm: Optional[int] = Field(None, ge=1)


class ConnectionUpdate(BaseModel):
    """Update an existing API connection."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    base_url: Optional[str] = Field(None, min_length=1, max_length=500)
    status: Optional[str] = Field(None, description="active or inactive")
    auth_type: Optional[str] = None
    auth_secret: Optional[str] = Field(None, description="New secret (will be encrypted)")
    auth_header_name: Optional[str] = None
    auth_header_prefix: Optional[str] = None
    default_headers: Optional[Dict[str, str]] = None
    api_documentation: Optional[str] = None
    timeout_ms: Optional[int] = Field(None, ge=1000, le=120000)
    rate_limit_rpm: Optional[int] = Field(None, ge=1)


class ConnectionResponse(BaseModel):
    """Response for a single API connection — never exposes the secret."""
    id: int
    project_id: int
    name: str
    description: Optional[str] = None
    status: str
    base_url: str
    auth_type: str
    has_auth: bool = False
    auth_header_name: Optional[str] = None
    auth_header_prefix: Optional[str] = None
    default_headers: Optional[Dict[str, str]] = None
    api_documentation: Optional[str] = None
    parsed_spec: Optional[Dict[str, Any]] = None
    timeout_ms: int
    rate_limit_rpm: Optional[int] = None
    connection_metadata: Optional[Dict[str, Any]] = None
    endpoint_count: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConnectionListResponse(BaseModel):
    """List of connections."""
    connections: List[ConnectionResponse]


# ============================================================================
# Endpoint Schemas
# ============================================================================

class EndpointCreate(BaseModel):
    """Create a new endpoint within a connection."""
    name: str = Field(..., min_length=1, max_length=200)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-z0-9_]+$')
    method: str = Field(..., description="GET, POST, PUT, PATCH, DELETE")
    path: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    parameters_schema: Optional[Dict[str, Any]] = None
    request_body_schema: Optional[Dict[str, Any]] = None
    response_example: Optional[Dict[str, Any]] = None
    when_to_use: Optional[str] = None
    is_active: bool = True


class EndpointUpdate(BaseModel):
    """Update an endpoint."""
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    slug: Optional[str] = Field(None, min_length=1, max_length=100, pattern=r'^[a-z0-9_]+$')
    method: Optional[str] = None
    path: Optional[str] = None
    description: Optional[str] = None
    parameters_schema: Optional[Dict[str, Any]] = None
    request_body_schema: Optional[Dict[str, Any]] = None
    response_example: Optional[Dict[str, Any]] = None
    when_to_use: Optional[str] = None
    is_active: Optional[bool] = None


class EndpointResponse(BaseModel):
    """Response for a single endpoint."""
    id: int
    connection_id: int
    name: str
    slug: str
    method: str
    path: str
    description: Optional[str] = None
    parameters_schema: Optional[Dict[str, Any]] = None
    request_body_schema: Optional[Dict[str, Any]] = None
    response_example: Optional[Dict[str, Any]] = None
    when_to_use: Optional[str] = None
    is_active: bool
    endpoint_metadata: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EndpointListResponse(BaseModel):
    """List of endpoints."""
    endpoints: List[EndpointResponse]


# ============================================================================
# Spec Parsing Schemas
# ============================================================================

class ParseSpecRequest(BaseModel):
    """Request to parse API documentation."""
    markdown_text: str = Field(..., min_length=10, description="Markdown API documentation")


class ParsedEndpoint(BaseModel):
    """An endpoint parsed from API docs."""
    name: str
    slug: str
    method: str
    path: str
    description: Optional[str] = None
    parameters_schema: Optional[Dict[str, Any]] = None
    request_body_schema: Optional[Dict[str, Any]] = None
    when_to_use: Optional[str] = None


class ParseSpecResponse(BaseModel):
    """Response from parsing API documentation."""
    base_url_suggestion: Optional[str] = None
    auth_type_suggestion: Optional[str] = None
    endpoints: List[ParsedEndpoint]


# ============================================================================
# Test & Call Schemas
# ============================================================================

class TestConnectionResponse(BaseModel):
    """Result of testing a connection."""
    success: bool
    status_code: Optional[int] = None
    message: str
    duration_ms: int


class CallEndpointRequest(BaseModel):
    """Request to call an endpoint."""
    parameters: Optional[Dict[str, Any]] = None
    body: Optional[Dict[str, Any]] = None


class CallEndpointResponse(BaseModel):
    """Response from calling an endpoint."""
    execution_id: int
    status: str
    status_code: Optional[int] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: int


# ============================================================================
# Execution History Schemas
# ============================================================================

class ExecutionResponse(BaseModel):
    """Response for a single execution log entry."""
    id: int
    connection_id: int
    endpoint_id: Optional[int] = None
    trigger_source: str
    trigger_source_id: Optional[str] = None
    session_id: Optional[int] = None
    status: str
    request_data: Optional[Dict[str, Any]] = None
    response_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ExecutionListResponse(BaseModel):
    """List of executions."""
    executions: List[ExecutionResponse]
    total: int


# ============================================================================
# Tool Generation Schemas
# ============================================================================

class GenerateToolsRequest(BaseModel):
    """Request to create specialist tools from connection endpoints."""
    specialist_id: int
    endpoint_ids: Optional[List[int]] = None  # If None, all active endpoints


class GenerateToolsResponse(BaseModel):
    """Result of tool generation."""
    tools_created: int
    tool_ids: List[int]
