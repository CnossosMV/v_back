"""Pydantic schemas for Email Instances."""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ApiConfigSchema(BaseModel):
    endpoint_url: str
    method: str = "POST"
    auth_type: str = "api_key_header"  # api_key_header | bearer | basic | query_param
    auth_header_name: Optional[str] = "X-API-Key"
    auth_param_name: Optional[str] = None
    extra_headers: Dict[str, str] = {}
    body_template: Dict[str, str] = {
        "to_email_address": "{{to_email}}",
        "subject": "{{subject}}",
        "body": "{{html_body}}",
        "from": "{{from_email}}",
        "from_name": "{{from_name}}",
        "reply_to": "{{reply_to}}",
    }
    success_check: Dict[str, Any] = {"type": "status_code", "expected": [200, 201, 202]}
    message_id_path: Optional[str] = None


class EmailInstanceCreate(BaseModel):
    instance_name: str = Field(..., max_length=100)
    provider_type: str = Field("smtp", pattern="^(smtp|api)$")
    from_email: str = Field(..., max_length=255)
    from_name: Optional[str] = Field(None, max_length=100)
    project_id: Optional[int] = None
    # SMTP fields
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    # API fields
    api_key: Optional[str] = None
    api_config: Optional[ApiConfigSchema] = None


class EmailInstanceUpdate(BaseModel):
    instance_name: Optional[str] = Field(None, max_length=100)
    from_email: Optional[str] = Field(None, max_length=255)
    from_name: Optional[str] = Field(None, max_length=100)
    project_id: Optional[int] = None
    is_active: Optional[bool] = None
    # SMTP fields
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    smtp_use_ssl: Optional[bool] = None
    # API fields
    api_key: Optional[str] = None
    api_config: Optional[ApiConfigSchema] = None


class EmailInstanceResponse(BaseModel):
    id: int
    user_id: int
    workspace_id: int
    project_id: Optional[int] = None
    provider_type: str
    instance_name: str
    from_email: str
    from_name: Optional[str] = None
    # SMTP (secrets masked)
    smtp_server: Optional[str] = None
    smtp_port: Optional[int] = None
    smtp_username: Optional[str] = None
    smtp_use_tls: Optional[bool] = None
    smtp_use_ssl: Optional[bool] = None
    # API (secrets masked)
    api_config: Optional[Dict[str, Any]] = None
    has_api_key: bool = False
    # Lifecycle
    connection_status: str
    is_active: bool
    created_at: datetime
    updated_at: datetime
    last_verified_at: Optional[datetime] = None
    # Delivery feedback
    feedback_webhook_url: Optional[str] = None
    feedback_configured: bool = False

    class Config:
        from_attributes = True


class EmailInstanceTestRequest(BaseModel):
    to_email: str
    subject: str = "Test email from Versya"
    message: str = "<p>This is a test email sent through your email instance.</p>"
