"""Schemas for project variables."""
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime
import re


class UrlParam(BaseModel):
    key: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., min_length=1)
    transform: str = Field(default="none")

    @field_validator("transform")
    @classmethod
    def validate_transform(cls, v: str) -> str:
        allowed = {"none", "encrypt", "base64", "sha256"}
        if v not in allowed:
            raise ValueError(f"transform must be one of {allowed}")
        return v


class ProjectVariableCreate(BaseModel):
    key: str = Field(..., min_length=1, max_length=100)
    var_type: str = Field(default="text")
    value: Optional[str] = None
    url_params: Optional[List[UrlParam]] = None
    description: Optional[str] = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                "key must start with a lowercase letter and contain only "
                "lowercase letters, numbers, and underscores"
            )
        return v

    @field_validator("var_type")
    @classmethod
    def validate_var_type(cls, v: str) -> str:
        if v not in ("text", "url"):
            raise ValueError("var_type must be 'text' or 'url'")
        return v


class ProjectVariableUpdate(BaseModel):
    key: Optional[str] = None
    var_type: Optional[str] = None
    value: Optional[str] = None
    url_params: Optional[List[UrlParam]] = None
    description: Optional[str] = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                "key must start with a lowercase letter and contain only "
                "lowercase letters, numbers, and underscores"
            )
        return v

    @field_validator("var_type")
    @classmethod
    def validate_var_type(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("text", "url"):
            raise ValueError("var_type must be 'text' or 'url'")
        return v


class ProjectVariableResponse(BaseModel):
    id: int
    project_id: int
    key: str
    var_type: str
    value: Optional[str] = None
    url_params: Optional[List[UrlParam]] = None
    description: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectVariablePreview(BaseModel):
    key: str
    rendered_value: str
