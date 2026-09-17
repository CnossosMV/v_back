"""API schemas for Project Import and lifecycle model management."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectImportCreate(BaseModel):
    client_import_id: str = Field(..., min_length=8, max_length=255)
    source_system: str = Field(..., min_length=1, max_length=100)
    source_project: str | None = Field(None, max_length=255)
    source_version: str | None = Field(None, max_length=100)
    contract_version: str = "1.0"
    import_mode: Literal["fill_missing", "authoritative_fields"] = "fill_missing"


class ProjectImportResponse(BaseModel):
    id: str
    project_id: int
    client_import_id: str
    contract_version: str
    status: str
    import_mode: str
    source_system: str
    source_project: str | None = None
    source_version: str | None = None
    manifest: dict[str, Any] | None = None
    validation_report: dict[str, Any] | None = None
    reconciliation_report: dict[str, Any] | None = None
    record_counts: dict[str, Any] | None = None
    bundle_checksum: str | None = None
    bundle_size_bytes: int | None = None
    lifecycle_model_id: int | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime
    uploaded_at: datetime | None = None
    validated_at: datetime | None = None
    apply_requested_at: datetime | None = None
    applied_at: datetime | None = None
    completed_at: datetime | None = None
    expires_at: datetime | None = None

    class Config:
        from_attributes = True


class ProjectImportList(BaseModel):
    items: list[ProjectImportResponse]


class LifecycleModelCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    definition: dict[str, Any]
    status: Literal["draft", "validated"] = "draft"


class LifecycleModelResponse(BaseModel):
    id: int
    project_id: int
    version: int
    name: str
    status: str
    contract_version: str
    definition: dict[str, Any]
    checksum: str
    validation_report: dict[str, Any] | None = None
    source_import_id: str | None = None
    approved_by_user_id: int | None = None
    approved_at: datetime | None = None
    activated_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class LifecycleModelActivation(BaseModel):
    target_status: Literal["shadow", "active"]
    reason: str = Field(..., min_length=8, max_length=1000)


class LifecycleModelCompareRequest(BaseModel):
    left_model_id: int = Field(..., ge=1)
    right_model_id: int = Field(..., ge=1)
    sample_limit: int = Field(100, ge=1, le=1000)
    as_of: datetime | None = None


class CutoverUpdate(BaseModel):
    purpose_key: str = Field(..., min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    mode: Literal["legacy", "shadow", "versya"]
    expected_epoch: int = Field(..., ge=0)
    lifecycle_model_id: int | None = Field(None, ge=1)
    reason: str = Field(..., min_length=8, max_length=1000)


class CutoverResponse(BaseModel):
    purpose_key: str
    mode: str
    previous_mode: str | None = None
    orchestration_epoch: int
    lifecycle_model_id: int | None = None
    reason: str | None = None
    changed_at: datetime

    class Config:
        from_attributes = True
