"""
API Connector Service

Core HTTP execution engine for Project API Connections.
Handles credential decryption, header construction, request execution, and logging.
"""
import re
import time
import json
import base64
import logging
from typing import Dict, Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.models import (
    ProjectApiConnection, ApiConnectionEndpoint, ApiConnectionExecution,
)
from app.services.encryption_service import decrypt_value

logger = logging.getLogger(__name__)


class ApiConnectorService:
    """Executes API connection endpoint calls with auth handling and logging."""

    def __init__(self, db: Session):
        self.db = db

    def call_endpoint(
        self,
        connection_id: int,
        endpoint_id_or_slug: Any,
        parameters: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        context_vars: Optional[Dict[str, Any]] = None,
        trigger_source: str = "manual_test",
        trigger_source_id: Optional[str] = None,
        session_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute an API endpoint call.

        Args:
            connection_id: ID of the ProjectApiConnection
            endpoint_id_or_slug: Endpoint ID (int) or slug (str)
            parameters: Path/query parameters
            body: Request body override
            context_vars: Variables for {{interpolation}}
            trigger_source: What triggered this call
            trigger_source_id: ID of the trigger entity
            session_id: Chat session ID (if applicable)

        Returns:
            Dict with: execution_id, status, status_code, result, error, duration_ms
        """
        parameters = parameters or {}
        context_vars = context_vars or {}

        # Load connection
        connection = self.db.query(ProjectApiConnection).filter(
            ProjectApiConnection.id == connection_id
        ).first()
        if not connection:
            return {"execution_id": None, "status": "error", "status_code": None,
                    "result": None, "error": "Connection not found", "duration_ms": 0}

        # Load endpoint
        endpoint = self._resolve_endpoint(connection_id, endpoint_id_or_slug)
        if not endpoint:
            return {"execution_id": None, "status": "error", "status_code": None,
                    "result": None, "error": "Endpoint not found", "duration_ms": 0}

        # Build interpolation context
        ctx = {**context_vars, **parameters}

        # Build URL
        path = self._interpolate(endpoint.path, ctx)
        base = connection.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"

        # Build headers
        headers = dict(connection.default_headers or {})
        auth_headers = self._build_auth_headers(connection)
        headers.update(auth_headers)
        if "Content-Type" not in headers and "content-type" not in headers:
            headers["Content-Type"] = "application/json"

        # Build request body
        method = endpoint.method.upper()
        request_body = None
        if method in ("POST", "PUT", "PATCH"):
            if body is not None:
                request_body = self._interpolate_obj(body, ctx)
            elif endpoint.request_body_schema:
                # Build body from parameters using schema as template
                request_body = {k: parameters.get(k) for k in parameters if k not in self._path_params(endpoint.path)}
                if not request_body:
                    request_body = parameters
            else:
                request_body = parameters

        # Build query params for GET/DELETE
        query_params = None
        if method in ("GET", "DELETE"):
            path_param_names = self._path_params(endpoint.path)
            query_params = {k: v for k, v in parameters.items() if k not in path_param_names}

        request_data = {
            "method": method,
            "url": url,
            "headers": {k: v for k, v in headers.items() if k.lower() not in ("authorization",)},
            "body": request_body,
            "query_params": query_params,
        }

        start_time = time.time()

        try:
            timeout = connection.timeout_ms / 1000.0
            with httpx.Client(timeout=timeout) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=query_params,
                    json=request_body if isinstance(request_body, (dict, list)) else None,
                    content=request_body if isinstance(request_body, str) else None,
                )

            duration_ms = int((time.time() - start_time) * 1000)
            success = 200 <= response.status_code < 300

            # Parse response
            try:
                result = response.json()
            except Exception:
                result = response.text[:5000]

            status = "success" if success else "error"
            error_msg = None if success else f"HTTP {response.status_code}"

            return self._log_execution(
                connection_id=connection_id,
                endpoint_id=endpoint.id,
                trigger_source=trigger_source,
                trigger_source_id=trigger_source_id,
                session_id=session_id,
                status=status,
                request_data=request_data,
                response_data={"status_code": response.status_code, "body": result},
                error_message=error_msg,
                duration_ms=duration_ms,
                status_code=response.status_code,
                result=result,
            )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            return self._log_execution(
                connection_id=connection_id,
                endpoint_id=endpoint.id,
                trigger_source=trigger_source,
                trigger_source_id=trigger_source_id,
                session_id=session_id,
                status="timeout",
                request_data=request_data,
                response_data=None,
                error_message=f"Request timed out after {connection.timeout_ms}ms",
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"API call failed for connection {connection_id}: {e}")
            return self._log_execution(
                connection_id=connection_id,
                endpoint_id=endpoint.id,
                trigger_source=trigger_source,
                trigger_source_id=trigger_source_id,
                session_id=session_id,
                status="error",
                request_data=request_data,
                response_data=None,
                error_message=str(e),
                duration_ms=duration_ms,
            )

    def test_connection(self, connection_id: int) -> Dict[str, Any]:
        """Test a connection by making a HEAD/GET request to the base URL."""
        connection = self.db.query(ProjectApiConnection).filter(
            ProjectApiConnection.id == connection_id
        ).first()
        if not connection:
            return {"success": False, "status_code": None, "message": "Connection not found", "duration_ms": 0}

        headers = dict(connection.default_headers or {})
        auth_headers = self._build_auth_headers(connection)
        headers.update(auth_headers)

        start_time = time.time()
        try:
            timeout = min(connection.timeout_ms / 1000.0, 15.0)
            with httpx.Client(timeout=timeout) as client:
                response = client.get(connection.base_url, headers=headers)

            duration_ms = int((time.time() - start_time) * 1000)
            success = response.status_code < 500

            return {
                "success": success,
                "status_code": response.status_code,
                "message": f"Connection responded with HTTP {response.status_code}",
                "duration_ms": duration_ms,
            }
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return {
                "success": False,
                "status_code": None,
                "message": f"Connection failed: {str(e)}",
                "duration_ms": duration_ms,
            }

    def _build_auth_headers(self, connection: ProjectApiConnection) -> Dict[str, str]:
        """Build auth headers from connection configuration."""
        if connection.auth_type == "none" or not connection.auth_config_encrypted:
            return {}

        try:
            secret = decrypt_value(connection.auth_config_encrypted)
        except Exception as e:
            logger.warning(f"Failed to decrypt auth for connection {connection.id}: {e}")
            return {}

        auth_type = connection.auth_type

        if auth_type == "api_key_header":
            header_name = connection.auth_header_name or "X-API-Key"
            return {header_name: secret}

        elif auth_type == "bearer_token":
            return {"Authorization": f"Bearer {secret}"}

        elif auth_type == "basic_auth":
            encoded = base64.b64encode(secret.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}

        elif auth_type == "custom_header":
            header_name = connection.auth_header_name or "Authorization"
            prefix = connection.auth_header_prefix or ""
            value = f"{prefix} {secret}".strip() if prefix else secret
            return {header_name: value}

        return {}

    def _resolve_endpoint(
        self, connection_id: int, endpoint_id_or_slug: Any
    ) -> Optional[ApiConnectionEndpoint]:
        """Resolve endpoint by ID or slug."""
        if isinstance(endpoint_id_or_slug, int):
            return self.db.query(ApiConnectionEndpoint).filter(
                ApiConnectionEndpoint.id == endpoint_id_or_slug,
                ApiConnectionEndpoint.connection_id == connection_id,
            ).first()
        else:
            return self.db.query(ApiConnectionEndpoint).filter(
                ApiConnectionEndpoint.slug == str(endpoint_id_or_slug),
                ApiConnectionEndpoint.connection_id == connection_id,
            ).first()

    def _interpolate(self, template: str, context: Dict[str, Any]) -> str:
        """Replace {{variable}} and {variable} placeholders with values from context."""
        def replacer(match):
            key = match.group(1).strip()
            value = context.get(key, match.group(0))
            return str(value) if value is not None else match.group(0)

        # Handle both {{var}} and {var} patterns
        result = re.sub(r'\{\{(\s*\w+\s*)\}\}', replacer, template)
        result = re.sub(r'\{(\w+)\}', replacer, result)
        return result

    def _interpolate_obj(self, obj: Any, context: Dict[str, Any]) -> Any:
        """Recursively interpolate variables in an object."""
        if isinstance(obj, str):
            return self._interpolate(obj, context)
        elif isinstance(obj, dict):
            return {k: self._interpolate_obj(v, context) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._interpolate_obj(item, context) for item in obj]
        return obj

    def _path_params(self, path: str) -> set:
        """Extract parameter names from a URL path template."""
        return set(re.findall(r'\{(\w+)\}', path))

    def _log_execution(
        self,
        connection_id: int,
        endpoint_id: Optional[int],
        trigger_source: str,
        trigger_source_id: Optional[str],
        session_id: Optional[int],
        status: str,
        request_data: Dict,
        response_data: Optional[Any],
        error_message: Optional[str],
        duration_ms: int,
        status_code: Optional[int] = None,
        result: Any = None,
    ) -> Dict[str, Any]:
        """Log the execution and return result dict."""
        execution = ApiConnectionExecution(
            connection_id=connection_id,
            endpoint_id=endpoint_id,
            trigger_source=trigger_source,
            trigger_source_id=trigger_source_id,
            session_id=session_id,
            status=status,
            request_data=request_data,
            response_data=response_data if isinstance(response_data, (dict, list)) else {"raw": str(response_data)[:5000]} if response_data else None,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self.db.add(execution)
        self.db.commit()
        self.db.refresh(execution)

        return {
            "execution_id": execution.id,
            "status": status,
            "status_code": status_code,
            "result": result,
            "error": error_message,
            "duration_ms": duration_ms,
        }
