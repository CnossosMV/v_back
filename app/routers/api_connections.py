"""
API Connections Router

CRUD for project-level API connections, endpoints, spec parsing, testing, and tool generation.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.models import (
    User, Project, ProjectApiConnection, ApiConnectionEndpoint,
    ApiConnectionExecution, SpecialistAgent, SpecialistTool,
)
from app.schemas.api_connections import (
    ConnectionCreate, ConnectionUpdate, ConnectionResponse, ConnectionListResponse,
    EndpointCreate, EndpointUpdate, EndpointResponse, EndpointListResponse,
    ParseSpecRequest, ParseSpecResponse,
    TestConnectionResponse, CallEndpointRequest, CallEndpointResponse,
    ExecutionResponse, ExecutionListResponse,
    GenerateToolsRequest, GenerateToolsResponse,
)
from app.services.encryption_service import encrypt_value
from app.routers.auth import get_current_user

router = APIRouter(
    prefix="/projects/{project_id}/api-connections",
    tags=["API Connections"],
)


# ============================================================================
# Helpers
# ============================================================================

def _get_project(db: Session, project_id: int, user: User) -> Project:
    """Validate project access."""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == user.workspace_id,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _get_connection(db: Session, project_id: int, connection_id: int) -> ProjectApiConnection:
    """Get connection with project scope check."""
    conn = db.query(ProjectApiConnection).filter(
        ProjectApiConnection.id == connection_id,
        ProjectApiConnection.project_id == project_id,
    ).first()
    if not conn:
        raise HTTPException(status_code=404, detail="API connection not found")
    return conn


def _conn_to_response(conn: ProjectApiConnection) -> ConnectionResponse:
    """Convert model to response schema."""
    endpoint_count = len(conn.endpoints) if conn.endpoints else 0
    return ConnectionResponse(
        id=conn.id,
        project_id=conn.project_id,
        name=conn.name,
        description=conn.description,
        status=conn.status,
        base_url=conn.base_url,
        auth_type=conn.auth_type,
        has_auth=bool(conn.auth_config_encrypted),
        auth_header_name=conn.auth_header_name,
        auth_header_prefix=conn.auth_header_prefix,
        default_headers=conn.default_headers,
        api_documentation=conn.api_documentation,
        parsed_spec=conn.parsed_spec,
        timeout_ms=conn.timeout_ms,
        rate_limit_rpm=conn.rate_limit_rpm,
        connection_metadata=conn.connection_metadata,
        endpoint_count=endpoint_count,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
    )


def _endpoint_to_response(ep: ApiConnectionEndpoint) -> EndpointResponse:
    """Convert endpoint model to response schema."""
    return EndpointResponse(
        id=ep.id,
        connection_id=ep.connection_id,
        name=ep.name,
        slug=ep.slug,
        method=ep.method,
        path=ep.path,
        description=ep.description,
        parameters_schema=ep.parameters_schema,
        request_body_schema=ep.request_body_schema,
        response_example=ep.response_example,
        when_to_use=ep.when_to_use,
        is_active=ep.is_active,
        endpoint_metadata=ep.endpoint_metadata,
        created_at=ep.created_at,
        updated_at=ep.updated_at,
    )


# ============================================================================
# Connection CRUD
# ============================================================================

@router.get("", response_model=ConnectionListResponse)
def list_connections(
    project_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all API connections for the project."""
    _get_project(db, project_id, user)
    connections = (
        db.query(ProjectApiConnection)
        .filter(ProjectApiConnection.project_id == project_id)
        .order_by(ProjectApiConnection.created_at.desc())
        .all()
    )
    return ConnectionListResponse(
        connections=[_conn_to_response(c) for c in connections]
    )


@router.post("", response_model=ConnectionResponse, status_code=201)
def create_connection(
    project_id: int,
    data: ConnectionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new API connection."""
    _get_project(db, project_id, user)

    # Validate auth_type
    valid_auth = ("api_key_header", "bearer_token", "basic_auth", "custom_header", "none")
    if data.auth_type not in valid_auth:
        raise HTTPException(status_code=400, detail=f"Invalid auth_type. Must be one of: {', '.join(valid_auth)}")

    conn = ProjectApiConnection(
        project_id=project_id,
        name=data.name,
        description=data.description,
        base_url=data.base_url.rstrip("/"),
        auth_type=data.auth_type,
        auth_config_encrypted=encrypt_value(data.auth_secret) if data.auth_secret else None,
        auth_header_name=data.auth_header_name,
        auth_header_prefix=data.auth_header_prefix,
        default_headers=data.default_headers or {},
        timeout_ms=data.timeout_ms,
        rate_limit_rpm=data.rate_limit_rpm,
        created_by=user.id,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return _conn_to_response(conn)


@router.get("/{connection_id}", response_model=ConnectionResponse)
def get_connection(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a connection with endpoint count."""
    _get_project(db, project_id, user)
    conn = _get_connection(db, project_id, connection_id)
    return _conn_to_response(conn)


@router.put("/{connection_id}", response_model=ConnectionResponse)
def update_connection(
    project_id: int,
    connection_id: int,
    data: ConnectionUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update an API connection."""
    _get_project(db, project_id, user)
    conn = _get_connection(db, project_id, connection_id)

    if data.name is not None:
        conn.name = data.name
    if data.description is not None:
        conn.description = data.description
    if data.base_url is not None:
        conn.base_url = data.base_url.rstrip("/")
    if data.status is not None:
        if data.status not in ("active", "inactive"):
            raise HTTPException(status_code=400, detail="Status must be 'active' or 'inactive'")
        conn.status = data.status
    if data.auth_type is not None:
        conn.auth_type = data.auth_type
    if data.auth_secret is not None:
        conn.auth_config_encrypted = encrypt_value(data.auth_secret)
    if data.auth_header_name is not None:
        conn.auth_header_name = data.auth_header_name
    if data.auth_header_prefix is not None:
        conn.auth_header_prefix = data.auth_header_prefix
    if data.default_headers is not None:
        conn.default_headers = data.default_headers
    if data.api_documentation is not None:
        conn.api_documentation = data.api_documentation
    if data.timeout_ms is not None:
        conn.timeout_ms = data.timeout_ms
    if data.rate_limit_rpm is not None:
        conn.rate_limit_rpm = data.rate_limit_rpm

    db.commit()
    db.refresh(conn)
    return _conn_to_response(conn)


@router.delete("/{connection_id}", status_code=204)
def delete_connection(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete an API connection and all its endpoints/executions."""
    _get_project(db, project_id, user)
    conn = _get_connection(db, project_id, connection_id)
    db.delete(conn)
    db.commit()


# ============================================================================
# Spec Parsing
# ============================================================================

@router.post("/{connection_id}/parse-spec", response_model=ParseSpecResponse)
def parse_spec(
    project_id: int,
    connection_id: int,
    data: ParseSpecRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Parse markdown API documentation into endpoint suggestions."""
    _get_project(db, project_id, user)
    conn = _get_connection(db, project_id, connection_id)

    from app.services.api_spec_parser import ApiSpecParser
    parser = ApiSpecParser(db, project_id)
    result = parser.parse_markdown_spec(data.markdown_text)

    # Store the raw doc and parsed spec on the connection
    conn.api_documentation = data.markdown_text
    conn.parsed_spec = result
    db.commit()

    return ParseSpecResponse(**result)


# ============================================================================
# Connection Test
# ============================================================================

@router.post("/{connection_id}/test", response_model=TestConnectionResponse)
def test_connection(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Test a connection by pinging its base URL."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    from app.services.api_connector_service import ApiConnectorService
    service = ApiConnectorService(db)
    result = service.test_connection(connection_id)
    return TestConnectionResponse(**result)


# ============================================================================
# Endpoint CRUD
# ============================================================================

@router.get("/{connection_id}/endpoints", response_model=EndpointListResponse)
def list_endpoints(
    project_id: int,
    connection_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all endpoints for a connection."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    endpoints = (
        db.query(ApiConnectionEndpoint)
        .filter(ApiConnectionEndpoint.connection_id == connection_id)
        .order_by(ApiConnectionEndpoint.created_at.asc())
        .all()
    )
    return EndpointListResponse(
        endpoints=[_endpoint_to_response(ep) for ep in endpoints]
    )


@router.post("/{connection_id}/endpoints", response_model=EndpointResponse, status_code=201)
def create_endpoint(
    project_id: int,
    connection_id: int,
    data: EndpointCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create a new endpoint."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    # Validate method
    valid_methods = ("GET", "POST", "PUT", "PATCH", "DELETE")
    if data.method.upper() not in valid_methods:
        raise HTTPException(status_code=400, detail=f"Invalid method. Must be one of: {', '.join(valid_methods)}")

    # Check slug uniqueness
    existing = db.query(ApiConnectionEndpoint).filter(
        ApiConnectionEndpoint.connection_id == connection_id,
        ApiConnectionEndpoint.slug == data.slug,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Endpoint slug '{data.slug}' already exists")

    ep = ApiConnectionEndpoint(
        connection_id=connection_id,
        name=data.name,
        slug=data.slug,
        method=data.method.upper(),
        path=data.path,
        description=data.description,
        parameters_schema=data.parameters_schema or {},
        request_body_schema=data.request_body_schema or {},
        response_example=data.response_example or {},
        when_to_use=data.when_to_use,
        is_active=data.is_active,
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return _endpoint_to_response(ep)


@router.put("/{connection_id}/endpoints/{endpoint_id}", response_model=EndpointResponse)
def update_endpoint(
    project_id: int,
    connection_id: int,
    endpoint_id: int,
    data: EndpointUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Update an endpoint."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    ep = db.query(ApiConnectionEndpoint).filter(
        ApiConnectionEndpoint.id == endpoint_id,
        ApiConnectionEndpoint.connection_id == connection_id,
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    if data.name is not None:
        ep.name = data.name
    if data.slug is not None:
        # Check uniqueness
        existing = db.query(ApiConnectionEndpoint).filter(
            ApiConnectionEndpoint.connection_id == connection_id,
            ApiConnectionEndpoint.slug == data.slug,
            ApiConnectionEndpoint.id != endpoint_id,
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Endpoint slug '{data.slug}' already exists")
        ep.slug = data.slug
    if data.method is not None:
        ep.method = data.method.upper()
    if data.path is not None:
        ep.path = data.path
    if data.description is not None:
        ep.description = data.description
    if data.parameters_schema is not None:
        ep.parameters_schema = data.parameters_schema
    if data.request_body_schema is not None:
        ep.request_body_schema = data.request_body_schema
    if data.response_example is not None:
        ep.response_example = data.response_example
    if data.when_to_use is not None:
        ep.when_to_use = data.when_to_use
    if data.is_active is not None:
        ep.is_active = data.is_active

    db.commit()
    db.refresh(ep)
    return _endpoint_to_response(ep)


@router.delete("/{connection_id}/endpoints/{endpoint_id}", status_code=204)
def delete_endpoint(
    project_id: int,
    connection_id: int,
    endpoint_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete an endpoint."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    ep = db.query(ApiConnectionEndpoint).filter(
        ApiConnectionEndpoint.id == endpoint_id,
        ApiConnectionEndpoint.connection_id == connection_id,
    ).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    db.delete(ep)
    db.commit()


# ============================================================================
# Endpoint Test & Call
# ============================================================================

@router.post("/{connection_id}/endpoints/{endpoint_id}/test", response_model=CallEndpointResponse)
def test_endpoint(
    project_id: int,
    connection_id: int,
    endpoint_id: int,
    data: Optional[CallEndpointRequest] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Test an endpoint with optional parameters."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    from app.services.api_connector_service import ApiConnectorService
    service = ApiConnectorService(db)
    result = service.call_endpoint(
        connection_id=connection_id,
        endpoint_id_or_slug=endpoint_id,
        parameters=data.parameters if data else {},
        body=data.body if data else None,
        trigger_source="manual_test",
    )
    return CallEndpointResponse(**result)


@router.post("/{connection_id}/endpoints/{endpoint_id}/call", response_model=CallEndpointResponse)
def call_endpoint(
    project_id: int,
    connection_id: int,
    endpoint_id: int,
    data: CallEndpointRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Execute an endpoint call."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    from app.services.api_connector_service import ApiConnectorService
    service = ApiConnectorService(db)
    result = service.call_endpoint(
        connection_id=connection_id,
        endpoint_id_or_slug=endpoint_id,
        parameters=data.parameters or {},
        body=data.body,
        trigger_source="manual_test",
    )
    return CallEndpointResponse(**result)


# ============================================================================
# Execution History
# ============================================================================

@router.get("/{connection_id}/executions", response_model=ExecutionListResponse)
def list_executions(
    project_id: int,
    connection_id: int,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List execution history for a connection."""
    _get_project(db, project_id, user)
    _get_connection(db, project_id, connection_id)

    total = db.query(ApiConnectionExecution).filter(
        ApiConnectionExecution.connection_id == connection_id
    ).count()

    executions = (
        db.query(ApiConnectionExecution)
        .filter(ApiConnectionExecution.connection_id == connection_id)
        .order_by(ApiConnectionExecution.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return ExecutionListResponse(
        executions=[ExecutionResponse.model_validate(e) for e in executions],
        total=total,
    )


# ============================================================================
# Generate Specialist Tools
# ============================================================================

@router.post("/{connection_id}/generate-tools", response_model=GenerateToolsResponse)
def generate_tools(
    project_id: int,
    connection_id: int,
    data: GenerateToolsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Create specialist tools from connection endpoints."""
    _get_project(db, project_id, user)
    conn = _get_connection(db, project_id, connection_id)

    # Validate specialist
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == data.specialist_id
    ).first()
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist agent not found")

    # Get endpoints
    query = db.query(ApiConnectionEndpoint).filter(
        ApiConnectionEndpoint.connection_id == connection_id,
        ApiConnectionEndpoint.is_active == True,
    )
    if data.endpoint_ids:
        query = query.filter(ApiConnectionEndpoint.id.in_(data.endpoint_ids))
    endpoints = query.all()

    if not endpoints:
        raise HTTPException(status_code=400, detail="No active endpoints found")

    tool_ids = []
    for ep in endpoints:
        tool = SpecialistTool(
            specialist_id=data.specialist_id,
            tool_type="api_connection",
            name=ep.name,
            description=ep.description or f"{ep.method} {ep.path}",
            when_to_use=ep.when_to_use,
            config={
                "api_connection_id": conn.id,
                "endpoint_id": ep.id,
                "endpoint_slug": ep.slug,
                "parameters_schema": ep.parameters_schema or {},
            },
            is_active=True,
            timeout_ms=conn.timeout_ms,
        )
        db.add(tool)
        db.flush()
        tool_ids.append(tool.id)

    db.commit()

    return GenerateToolsResponse(
        tools_created=len(tool_ids),
        tool_ids=tool_ids,
    )
