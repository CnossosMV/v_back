"""
Tool Executor Service

Executes tools (webhooks, MCP servers) on behalf of specialist agents.
Handles HTTP requests, variable interpolation, auth decryption, and execution logging.
"""

import os
import re
import json
import time
import logging
from typing import Dict, Optional, List, Any
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from app.models import SpecialistTool, ToolExecution, ChatSession
from app.services.encryption_service import encrypt_value, decrypt_value

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes webhook and MCP tools for specialist agents."""

    def __init__(self, db: Session):
        self.db = db

    def execute_tool(
        self,
        tool: SpecialistTool,
        parameters: Dict[str, Any],
        session_id: Optional[int] = None,
        message_id: Optional[int] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute a tool and log the result.

        Args:
            tool: The tool to execute
            parameters: Parameters from the LLM tool call
            session_id: Current chat session ID
            message_id: Current message ID
            session_context: Context data for variable interpolation

        Returns:
            Dict with: status, result, error, duration_ms
        """
        if tool.tool_type == "webhook":
            return self._execute_webhook(
                tool, parameters, session_id, message_id, session_context
            )
        elif tool.tool_type == "mcp_server":
            return self._execute_mcp_tool(
                tool, parameters, session_id, message_id
            )
        elif tool.tool_type == "api_connection":
            return self._execute_api_connection(
                tool, parameters, session_id, message_id, session_context
            )
        else:
            return {
                "status": "error",
                "result": None,
                "error": f"Unsupported tool type: {tool.tool_type}",
                "duration_ms": 0,
            }

    def _execute_webhook(
        self,
        tool: SpecialistTool,
        parameters: Dict[str, Any],
        session_id: Optional[int] = None,
        message_id: Optional[int] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a webhook tool."""
        config = tool.config or {}
        method = config.get("method", "POST").upper()
        url = config.get("url", "")
        headers = config.get("headers", {})
        body_template = config.get("body_template", "")

        if not url:
            return self._log_execution(
                tool, session_id, message_id,
                "error", {}, None, "No URL configured", 0,
            )

        # Build interpolation context
        context = {**(session_context or {}), **(parameters or {})}

        # Interpolate {{variables}} in URL, headers, and body
        url = self._interpolate(url, context)
        headers = {k: self._interpolate(v, context) for k, v in headers.items()}

        # Build request body
        body = None
        if body_template and method in ("POST", "PUT", "PATCH"):
            if isinstance(body_template, str):
                body = self._interpolate(body_template, context)
                try:
                    body = json.loads(body)
                except json.JSONDecodeError:
                    pass
            elif isinstance(body_template, dict):
                body = json.loads(self._interpolate(json.dumps(body_template), context))

        # If no body template, use parameters directly
        if body is None and method in ("POST", "PUT", "PATCH"):
            body = parameters

        # Decrypt auth headers if present
        if tool.auth_config_encrypted:
            try:
                auth_json = decrypt_value(tool.auth_config_encrypted)
                auth_config = json.loads(auth_json)
                # Merge auth headers
                if "headers" in auth_config:
                    headers.update(auth_config["headers"])
            except Exception as e:
                logger.warning(f"Failed to decrypt auth config for tool {tool.id}: {e}")

        # Set default content type
        if "Content-Type" not in headers and "content-type" not in headers:
            headers["Content-Type"] = "application/json"

        request_data = {
            "method": method,
            "url": url,
            "headers": {k: v for k, v in headers.items() if k.lower() != "authorization"},
            "body": body,
        }

        start_time = time.time()

        try:
            with httpx.Client(timeout=tool.timeout_ms / 1000.0) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    json=body if isinstance(body, (dict, list)) else None,
                    content=body if isinstance(body, str) else None,
                )

            duration_ms = int((time.time() - start_time) * 1000)

            # Parse response
            try:
                response_data = response.json()
            except (json.JSONDecodeError, ValueError):
                response_data = {"text": response.text[:2000]}

            status = "success" if response.is_success else "error"
            error_msg = None if response.is_success else f"HTTP {response.status_code}"

            return self._log_execution(
                tool, session_id, message_id,
                status, request_data, response_data, error_msg, duration_ms,
            )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            return self._log_execution(
                tool, session_id, message_id,
                "timeout", request_data, None,
                f"Request timed out after {tool.timeout_ms}ms", duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return self._log_execution(
                tool, session_id, message_id,
                "error", request_data, None, str(e), duration_ms,
            )

    def _execute_mcp_tool(
        self,
        tool: SpecialistTool,
        parameters: Dict[str, Any],
        session_id: Optional[int] = None,
        message_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Execute an MCP server tool via HTTP.
        Uses the MCP Streamable HTTP transport (POST to server URL).
        """
        config = tool.config or {}
        server_url = config.get("server_url", "")
        enabled_tools = config.get("enabled_tools", [])

        if not server_url:
            return self._log_execution(
                tool, session_id, message_id,
                "error", {}, None, "No MCP server URL configured", 0,
            )

        # Determine which MCP tool to call
        tool_name = parameters.pop("_tool_name", tool.name)

        if enabled_tools and tool_name not in enabled_tools:
            return self._log_execution(
                tool, session_id, message_id,
                "error", {"tool_name": tool_name}, None,
                f"Tool '{tool_name}' not enabled on this MCP server", 0,
            )

        # Build JSON-RPC request for MCP
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": parameters,
            },
        }

        request_data = {"server_url": server_url, "rpc_request": rpc_request}
        start_time = time.time()

        try:
            headers = {"Content-Type": "application/json"}

            # Decrypt auth if present
            if tool.auth_config_encrypted:
                try:
                    auth_json = decrypt_value(tool.auth_config_encrypted)
                    auth_config = json.loads(auth_json)
                    if "headers" in auth_config:
                        headers.update(auth_config["headers"])
                except Exception:
                    pass

            with httpx.Client(timeout=tool.timeout_ms / 1000.0) as client:
                response = client.post(
                    server_url,
                    headers=headers,
                    json=rpc_request,
                )

            duration_ms = int((time.time() - start_time) * 1000)

            try:
                rpc_response = response.json()
            except (json.JSONDecodeError, ValueError):
                return self._log_execution(
                    tool, session_id, message_id,
                    "error", request_data, {"text": response.text[:1000]},
                    "Invalid JSON response from MCP server", duration_ms,
                )

            # Check for JSON-RPC error
            if "error" in rpc_response:
                err = rpc_response["error"]
                return self._log_execution(
                    tool, session_id, message_id,
                    "error", request_data, rpc_response,
                    f"MCP error: {err.get('message', str(err))}", duration_ms,
                )

            result = rpc_response.get("result", {})
            return self._log_execution(
                tool, session_id, message_id,
                "success", request_data, result, None, duration_ms,
            )

        except httpx.TimeoutException:
            duration_ms = int((time.time() - start_time) * 1000)
            return self._log_execution(
                tool, session_id, message_id,
                "timeout", request_data, None,
                f"MCP request timed out after {tool.timeout_ms}ms", duration_ms,
            )
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            return self._log_execution(
                tool, session_id, message_id,
                "error", request_data, None, str(e), duration_ms,
            )

    def _execute_api_connection(
        self,
        tool: SpecialistTool,
        parameters: Dict[str, Any],
        session_id: Optional[int] = None,
        message_id: Optional[int] = None,
        session_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a tool backed by a Project API Connection endpoint."""
        config = tool.config or {}
        connection_id = config.get("api_connection_id")
        endpoint_id = config.get("endpoint_id")
        endpoint_slug = config.get("endpoint_slug")

        if not connection_id or (not endpoint_id and not endpoint_slug):
            return self._log_execution(
                tool, session_id, message_id,
                "error", {}, None, "Missing api_connection_id or endpoint in tool config", 0,
            )

        from app.services.api_connector_service import ApiConnectorService
        connector = ApiConnectorService(self.db)

        context = {**(session_context or {}), **(parameters or {})}
        start_time = time.time()

        result = connector.call_endpoint(
            connection_id=connection_id,
            endpoint_id_or_slug=endpoint_id or endpoint_slug,
            parameters=parameters,
            context_vars=context,
            trigger_source="specialist_tool",
            trigger_source_id=str(tool.id),
            session_id=session_id,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        return self._log_execution(
            tool, session_id, message_id,
            result.get("status", "error"),
            {"parameters": parameters, "connection_id": connection_id},
            result.get("result"),
            result.get("error"),
            duration_ms,
        )

    def discover_mcp_tools(self, server_url: str, timeout_ms: int = 10000) -> List[Dict]:
        """
        Discover tools from an MCP server.

        Args:
            server_url: MCP server URL
            timeout_ms: Request timeout

        Returns:
            List of tool definitions [{name, description, inputSchema}]
        """
        rpc_request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }

        try:
            with httpx.Client(timeout=timeout_ms / 1000.0) as client:
                response = client.post(
                    server_url,
                    headers={"Content-Type": "application/json"},
                    json=rpc_request,
                )

            rpc_response = response.json()

            if "error" in rpc_response:
                raise ValueError(f"MCP error: {rpc_response['error']}")

            result = rpc_response.get("result", {})
            tools = result.get("tools", [])

            return [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                }
                for t in tools
            ]

        except Exception as e:
            logger.error(f"MCP tool discovery failed for {server_url}: {e}")
            raise ValueError(f"Failed to discover tools: {str(e)}")

    def test_tool(
        self,
        tool_id: int,
        test_params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Test a tool with optional parameters."""
        tool = self.db.query(SpecialistTool).filter(
            SpecialistTool.id == tool_id
        ).first()

        if not tool:
            raise ValueError(f"Tool {tool_id} not found")

        return self.execute_tool(
            tool=tool,
            parameters=test_params or {},
            session_context={},
        )

    def get_execution_history(
        self,
        tool_id: int,
        limit: int = 20,
    ) -> List[ToolExecution]:
        """Get execution history for a tool."""
        return (
            self.db.query(ToolExecution)
            .filter(ToolExecution.tool_id == tool_id)
            .order_by(ToolExecution.created_at.desc())
            .limit(limit)
            .all()
        )

    def _interpolate(self, template: str, context: Dict[str, Any]) -> str:
        """Replace {{variable}} placeholders with values from context."""
        def replacer(match):
            key = match.group(1).strip()
            value = context.get(key, match.group(0))
            return str(value) if value is not None else match.group(0)

        return re.sub(r'\{\{(\s*\w+\s*)\}\}', replacer, template)

    def _log_execution(
        self,
        tool: SpecialistTool,
        session_id: Optional[int],
        message_id: Optional[int],
        status: str,
        request_data: Dict,
        response_data: Optional[Any],
        error_message: Optional[str],
        duration_ms: int,
    ) -> Dict[str, Any]:
        """Log the tool execution and update last_used_at."""
        execution = ToolExecution(
            tool_id=tool.id,
            session_id=session_id,
            message_id=message_id,
            status=status,
            request_data=request_data,
            response_data=response_data,
            error_message=error_message,
            duration_ms=duration_ms,
        )
        self.db.add(execution)

        # Update last_used_at
        tool.last_used_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(execution)

        logger.info(
            f"Tool {tool.id} ({tool.name}) executed: "
            f"status={status}, duration={duration_ms}ms"
        )

        return {
            "execution_id": execution.id,
            "status": status,
            "result": response_data,
            "error": error_message,
            "duration_ms": duration_ms,
        }
