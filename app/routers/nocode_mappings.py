"""
NoCode Mappings Router — CRUD + publish + debug for Chrome Extension mappings.
"""
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.extension_auth_service import ExtensionAuthService
from app.services.nocode_mapping_service import NoCodeMappingService
from app.services.nocode_debug_service import NoCodeDebugService
from app.schemas.nocode_mappings import (
    MappingBulkUpsert, MappingResponse, PublishResponse,
    ConfigSnapshotResponse, DebugEventCreate, DebugEventResponse,
)

router = APIRouter(tags=["NoCode Mappings"])


# ── Extension Token Dependency ────────────────────────────────────────

def get_extension_auth(
    x_extension_token: str = Header(..., alias="X-Extension-Token"),
    db: Session = Depends(get_db),
):
    """Verify extension token and return (project_id, user_id, role)."""
    svc = ExtensionAuthService(db)
    result = svc.verify_token(x_extension_token)
    if not result:
        raise HTTPException(status_code=401, detail="Invalid or expired extension token")
    return {"project_id": result[0], "user_id": result[1], "role": result[2]}


def require_publisher(auth: dict = Depends(get_extension_auth)):
    """Require publisher role."""
    if auth["role"] not in ("publisher", "owner"):
        raise HTTPException(status_code=403, detail="Publisher role required")
    return auth


# ── Mapping CRUD ──────────────────────────────────────────────────────

@router.get("/projects/{project_id}/mappings", response_model=List[MappingResponse])
async def list_mappings(
    project_id: int,
    status: Optional[str] = Query(None),
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """List all mappings (draft + published)."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    mappings = svc.get_mappings(project_id, status)
    return mappings


@router.post("/projects/{project_id}/mappings", response_model=List[MappingResponse])
async def upsert_mappings(
    project_id: int,
    data: MappingBulkUpsert,
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """Bulk upsert mappings."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    try:
        mappings_data = []
        for m in data.mappings:
            d = m.model_dump(by_alias=False)
            d["vef"] = m.vef
            d["properties"] = m.properties
            mappings_data.append(d)
        result = svc.upsert_mappings(project_id, mappings_data, auth["user_id"])
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/projects/{project_id}/mappings/{mapping_uid}")
async def delete_mapping(
    project_id: int,
    mapping_uid: str,
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """Archive a mapping."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    success = svc.delete_mapping(project_id, mapping_uid, auth["user_id"])
    if not success:
        raise HTTPException(status_code=404, detail="Mapping not found")
    return {"status": "archived"}


# ── Publish ───────────────────────────────────────────────────────────

@router.post("/projects/{project_id}/mappings/publish", response_model=PublishResponse)
async def publish_mappings(
    project_id: int,
    auth: dict = Depends(require_publisher),
    db: Session = Depends(get_db),
):
    """Publish all draft mappings → create immutable snapshot."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    try:
        snapshot = svc.publish(project_id, auth["user_id"])
        return PublishResponse(
            version=snapshot.version,
            mappingCount=snapshot.mapping_count,
            checksum=snapshot.checksum,
            updatedAt=snapshot.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Debug Events ──────────────────────────────────────────────────────

@router.post("/projects/{project_id}/mappings/debug-event", response_model=DebugEventResponse)
async def store_debug_event(
    project_id: int,
    data: DebugEventCreate,
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """Store a debug event from the extension."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeDebugService(db)
    event = svc.store_event(project_id, data.session_id, data.model_dump(by_alias=False))
    return event


@router.get("/projects/{project_id}/mappings/debug/{session_id}", response_model=List[DebugEventResponse])
async def get_debug_events(
    project_id: int,
    session_id: str,
    since: Optional[str] = Query(None, description="ISO datetime"),
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """Get debug events for polling."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeDebugService(db)
    since_dt = datetime.fromisoformat(since) if since else None
    events = svc.get_events(session_id, since_dt)
    return events


# ── Config Versions ───────────────────────────────────────────────────

@router.get("/projects/{project_id}/config/versions", response_model=List[ConfigSnapshotResponse])
async def list_config_versions(
    project_id: int,
    auth: dict = Depends(get_extension_auth),
    db: Session = Depends(get_db),
):
    """Get last 10 config snapshots."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    versions = svc.get_versions(project_id)
    return versions


@router.post("/projects/{project_id}/config/rollback/{version_id}", response_model=PublishResponse)
async def rollback_config(
    project_id: int,
    version_id: int,
    auth: dict = Depends(require_publisher),
    db: Session = Depends(get_db),
):
    """Rollback to a previous config version."""
    if auth["project_id"] != project_id:
        raise HTTPException(status_code=403, detail="Token not scoped to this project")
    svc = NoCodeMappingService(db)
    try:
        snapshot = svc.rollback(project_id, version_id, auth["user_id"])
        return PublishResponse(
            version=snapshot.version,
            mappingCount=snapshot.mapping_count,
            checksum=snapshot.checksum,
            updatedAt=snapshot.created_at,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
