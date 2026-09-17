"""
Schemas for No-Code Event Mappings (Chrome Extension).
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, validator
from enum import Enum


# ── Enums ──────────────────────────────────────────────────────────────

class TriggerType(str, Enum):
    click = "click"
    submit = "submit"
    change = "change"
    focus = "focus"
    visibility = "visibility"
    conditional = "conditional"


class PropertySource(str, Enum):
    this_element = "this_element"
    another_element = "another_element"
    window_path = "window_path"
    datalayer = "datalayer"
    cookie = "cookie"
    localstorage_key = "localstorage_key"
    sessionstorage_key = "sessionstorage_key"


class PropertyExtract(str, Enum):
    text_content = "text_content"
    attribute = "attribute"
    value = "value"


class PropertyTransform(str, Enum):
    none = "none"
    number = "number"
    currency = "currency"
    hash = "hash"
    lowercase = "lowercase"
    uppercase = "uppercase"
    trim = "trim"


class PageScopeType(str, Enum):
    exact = "exact"
    starts_with = "starts_with"
    contains = "contains"
    glob = "glob"


class ViewportScope(str, Enum):
    all = "all"
    desktop = "desktop"
    tablet = "tablet"
    mobile = "mobile"


class DataClassification(str, Enum):
    anonymous = "anonymous"
    behavioral = "behavioral"
    profile = "profile"
    pii = "pii"


class VEFStrategy(str, Enum):
    data_attribute = "data_attribute"
    semantic_stable = "semantic_stable"
    structural_landmark = "structural_landmark"
    text_content = "text_content"
    visual_position = "visual_position"


class DebugEventStatus(str, Enum):
    received = "received"
    matched = "matched"
    unresolved = "unresolved"
    failed = "failed"


class MappingStatus(str, Enum):
    draft = "draft"
    published = "published"
    archived = "archived"


class ExtensionRole(str, Enum):
    editor = "editor"
    publisher = "publisher"


# ── VEF Schemas ────────────────────────────────────────────────────────

class VEFLayer(BaseModel):
    layer: int = Field(..., ge=1, le=5)
    strategy: VEFStrategy
    selector: Optional[str] = None  # CSS selector or attribute value
    value: Optional[str] = None  # Match value
    confidence: float = Field(..., ge=0.0, le=1.0)
    note: Optional[str] = None
    context: Optional[str] = None  # Context selector for text_content/visual_position
    element_type: Optional[str] = Field(None, alias="elementType")

    class Config:
        populate_by_name = True


class VEFDescriptor(BaseModel):
    vef_id: str = Field(..., alias="vefId", max_length=64)
    layers: List[VEFLayer] = Field(..., min_length=1, max_length=5)
    element_tag: Optional[str] = Field(None, alias="elementTag")
    element_text: Optional[str] = Field(None, alias="elementText", max_length=200)

    class Config:
        populate_by_name = True

    @validator("layers")
    def validate_layers(cls, v):
        strategies = [layer.strategy for layer in v]
        if len(strategies) != len(set(strategies)):
            raise ValueError("Duplicate VEF strategies not allowed")
        return v


# ── Property Schemas ───────────────────────────────────────────────────

class MappingPropertyDef(BaseModel):
    name: str = Field(..., max_length=100)
    source: PropertySource
    extract: Optional[PropertyExtract] = None
    selector: Optional[str] = Field(None, max_length=500)
    attribute: Optional[str] = Field(None, max_length=100)
    transform: PropertyTransform = PropertyTransform.none
    data_classification: DataClassification = Field(..., alias="dataClassification")
    consent_required: bool = Field(False, alias="consentRequired")
    max_length: int = Field(512, alias="maxLength", ge=1, le=4096)

    class Config:
        populate_by_name = True

    @validator("consent_required", always=True)
    def pii_requires_consent(cls, v, values):
        if values.get("data_classification") == DataClassification.pii and not v:
            return True  # Force consent for PII
        return v


# ── Mapping CRUD Schemas ───────────────────────────────────────────────

class MappingCreate(BaseModel):
    mapping_uid: str = Field(..., alias="mappingUid", max_length=36)
    event_name: str = Field(..., alias="eventName", max_length=200)
    trigger: TriggerType
    vef: VEFDescriptor
    page_pattern: Optional[str] = Field(None, alias="pagePattern", max_length=500)
    page_scope_type: PageScopeType = Field(PageScopeType.glob, alias="pageScopeType")
    viewport_scope: ViewportScope = Field(ViewportScope.all, alias="viewportScope")
    properties: Optional[List[MappingPropertyDef]] = None
    display_name: Optional[str] = Field(None, alias="displayName", max_length=200)
    consent_required: bool = Field(False, alias="consentRequired")

    class Config:
        populate_by_name = True


class MappingUpdate(BaseModel):
    event_name: Optional[str] = Field(None, alias="eventName", max_length=200)
    trigger: Optional[TriggerType] = None
    vef: Optional[VEFDescriptor] = None
    page_pattern: Optional[str] = Field(None, alias="pagePattern", max_length=500)
    page_scope_type: Optional[PageScopeType] = Field(None, alias="pageScopeType")
    viewport_scope: Optional[ViewportScope] = Field(None, alias="viewportScope")
    properties: Optional[List[MappingPropertyDef]] = None
    display_name: Optional[str] = Field(None, alias="displayName", max_length=200)
    consent_required: Optional[bool] = Field(None, alias="consentRequired")

    class Config:
        populate_by_name = True


class MappingResponse(BaseModel):
    id: int
    mapping_uid: str = Field(..., alias="mappingUid")
    event_name: str = Field(..., alias="eventName")
    trigger: str
    vef: dict
    page_pattern: Optional[str] = Field(None, alias="pagePattern")
    page_scope_type: str = Field(..., alias="pageScopeType")
    viewport_scope: str = Field(..., alias="viewportScope")
    properties: Optional[list] = None
    status: str
    version: int
    display_name: Optional[str] = Field(None, alias="displayName")
    consent_required: bool = Field(..., alias="consentRequired")
    created_by_id: Optional[int] = Field(None, alias="createdById")
    updated_by_id: Optional[int] = Field(None, alias="updatedById")
    created_at: datetime = Field(..., alias="createdAt")
    updated_at: datetime = Field(..., alias="updatedAt")

    class Config:
        from_attributes = True
        populate_by_name = True


class MappingBulkUpsert(BaseModel):
    mappings: List[MappingCreate]


# ── Publish / Snapshot Schemas ─────────────────────────────────────────

class PublishResponse(BaseModel):
    version: int
    mapping_count: int = Field(..., alias="mappingCount")
    checksum: str
    updated_at: datetime = Field(..., alias="updatedAt")

    class Config:
        from_attributes = True
        populate_by_name = True


class ConfigSnapshotResponse(BaseModel):
    id: int
    version: int
    checksum: str
    mapping_count: Optional[int] = Field(None, alias="mappingCount")
    published_by_id: Optional[int] = Field(None, alias="publishedById")
    created_at: datetime = Field(..., alias="createdAt")

    class Config:
        from_attributes = True
        populate_by_name = True


# ── Debug Event Schemas ────────────────────────────────────────────────

class DebugEventCreate(BaseModel):
    session_id: str = Field(..., alias="sessionId", max_length=64)
    mapping_uid: Optional[str] = Field(None, alias="mappingUid", max_length=36)
    event_name: Optional[str] = Field(None, alias="eventName", max_length=200)
    payload: Optional[dict] = None
    vef_resolution: Optional[dict] = Field(None, alias="vefResolution")
    status: DebugEventStatus = DebugEventStatus.received

    class Config:
        populate_by_name = True


class DebugEventResponse(BaseModel):
    id: int
    session_id: str = Field(..., alias="sessionId")
    mapping_uid: Optional[str] = Field(None, alias="mappingUid")
    event_name: Optional[str] = Field(None, alias="eventName")
    payload: Optional[dict] = None
    vef_resolution: Optional[dict] = Field(None, alias="vefResolution")
    status: str
    created_at: datetime = Field(..., alias="createdAt")

    class Config:
        from_attributes = True
        populate_by_name = True


# ── Extension Token Schemas ────────────────────────────────────────────

class ExtensionTokenCreate(BaseModel):
    project_id: int = Field(..., alias="projectId")
    role: ExtensionRole = ExtensionRole.editor

    class Config:
        populate_by_name = True


class ExtensionTokenResponse(BaseModel):
    id: int
    project_id: int = Field(..., alias="projectId")
    token_prefix: str = Field(..., alias="tokenPrefix")
    role: str
    is_active: bool = Field(..., alias="isActive")
    expires_at: datetime = Field(..., alias="expiresAt")
    created_at: datetime = Field(..., alias="createdAt")
    revoked_at: Optional[datetime] = Field(None, alias="revokedAt")

    class Config:
        from_attributes = True
        populate_by_name = True


class ExtensionTokenCreateResponse(BaseModel):
    """Returned only on token creation - includes the raw token (only time it's visible)."""
    token: str  # Raw token - ext_live_xxxxx
    id: int
    project_id: int = Field(..., alias="projectId")
    token_prefix: str = Field(..., alias="tokenPrefix")
    role: str
    expires_at: datetime = Field(..., alias="expiresAt")

    class Config:
        from_attributes = True
        populate_by_name = True
