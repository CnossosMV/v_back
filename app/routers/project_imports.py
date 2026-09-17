"""Project Import, lifecycle model and orchestration cutover APIs."""

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import (
    require_project_import_upload_access,
    require_project_role,
    require_project_role_or_mcp_scope,
)
from app.models.project_import import LifecycleModel, ProjectImport, ProjectLifecycleCutover
from app.schemas.project_import import (
    CutoverResponse,
    CutoverUpdate,
    LifecycleModelActivation,
    LifecycleModelCompareRequest,
    LifecycleModelCreate,
    LifecycleModelResponse,
    ProjectImportCreate,
    ProjectImportList,
    ProjectImportResponse,
)
from app.services.lifecycle_model_service import LifecycleModelError, LifecycleModelService
from app.services.project_import_service import (
    MAX_BUNDLE_BYTES,
    ProjectImportError,
    ProjectImportService,
    contract_document,
)
from app.services.orchestration_cutover_service import (
    OrchestrationCutoverError,
    OrchestrationCutoverService,
)


router = APIRouter(prefix="/projects/{project_id}", tags=["project-imports"])


def _http_error(exc: Exception, status_code: int = 409) -> HTTPException:
    return HTTPException(status_code=status_code, detail=str(exc))


@router.get("/project-imports/contract")
def get_project_import_contract(
    project_id: int,
    _auth=Depends(require_project_role("viewer")),
):
    return contract_document()


@router.get("/project-imports", response_model=ProjectImportList)
def list_project_imports(
    project_id: int,
    limit: int = Query(50, ge=1, le=200),
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    rows = db.query(ProjectImport).filter(
        ProjectImport.project_id == project_id,
    ).order_by(ProjectImport.created_at.desc()).limit(limit).all()
    return {"items": rows}


@router.post("/project-imports", response_model=ProjectImportResponse)
def create_project_import(
    project_id: int,
    payload: ProjectImportCreate,
    auth=Depends(require_project_role("editor")),
    db: Session = Depends(get_db),
):
    try:
        return ProjectImportService(db).create(project_id, payload.model_dump(), auth["user_id"])
    except ProjectImportError as exc:
        raise _http_error(exc) from exc


@router.get("/project-imports/{import_id}", response_model=ProjectImportResponse)
def get_project_import(
    project_id: int,
    import_id: str,
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        return ProjectImportService(db).get(project_id, import_id)
    except ProjectImportError as exc:
        raise _http_error(exc, 404) from exc


@router.put("/project-imports/{import_id}/bundle", response_model=ProjectImportResponse)
async def upload_project_import_bundle(
    project_id: int,
    import_id: str,
    file: UploadFile = File(...),
    _auth=Depends(require_project_role_or_mcp_scope("editor", "versya.imports:write")),
    db: Session = Depends(get_db),
):
    if file.content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Project Import bundle must be a ZIP file")
    content = await file.read(MAX_BUNDLE_BYTES + 1)
    try:
        service = ProjectImportService(db)
        return service.upload(service.get(project_id, import_id), content)
    except ProjectImportError as exc:
        raise _http_error(exc, 400) from exc


@router.put("/project-imports/{import_id}/bundle/raw", response_model=ProjectImportResponse)
async def upload_project_import_bundle_raw(
    project_id: int,
    import_id: str,
    request: Request,
    auth=Depends(require_project_import_upload_access()),
    db: Session = Depends(get_db),
):
    """Agent-friendly binary upload; the existing multipart route remains for the UI."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
        raise HTTPException(status_code=415, detail="Project Import bundle must be sent as ZIP bytes")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BUNDLE_BYTES:
                raise HTTPException(status_code=413, detail="Project Import bundle exceeds the compressed size limit")
        except ValueError:
            raise HTTPException(status_code=400, detail="Content-Length must be an integer")
    service = ProjectImportService(db)
    grant_id = auth.get("upload_grant_id")
    grant_claimed = False
    try:
        if grant_id:
            service.claim_upload_grant(grant_id)
            grant_claimed = True
        content = await request.body()
        row = service.upload(service.get(project_id, import_id), content)
        if grant_id:
            service.consume_upload_grant(grant_id)
            grant_claimed = False
        return row
    except ProjectImportError as exc:
        if grant_id and grant_claimed:
            service.release_upload_grant(grant_id)
        raise _http_error(exc, 400) from exc
    except Exception:
        if grant_id and grant_claimed:
            service.release_upload_grant(grant_id)
        raise


@router.post("/project-imports/{import_id}/validate")
def validate_project_import(
    project_id: int,
    import_id: str,
    _auth=Depends(require_project_role("editor")),
    db: Session = Depends(get_db),
):
    try:
        service = ProjectImportService(db)
        row = service.get(project_id, import_id)
        return {"import_id": row.id, "status": "ready" if service.validate(row)["valid"] else "blocked", "report": row.validation_report}
    except ProjectImportError as exc:
        raise _http_error(exc, 400) from exc


@router.get("/project-imports/{import_id}/audit")
def audit_project_import(
    project_id: int,
    import_id: str,
    issue_limit: int = Query(100, ge=1, le=500),
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        service = ProjectImportService(db)
        return service.audit(service.get(project_id, import_id), issue_limit=issue_limit)
    except ProjectImportError as exc:
        raise _http_error(exc, 404) from exc


@router.get("/project-imports/{import_id}/records")
def list_project_import_records(
    project_id: int,
    import_id: str,
    record_type: str | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        service = ProjectImportService(db)
        row = service.get(project_id, import_id)
        return service.list_records(
            row,
            record_type=record_type,
            status=status,
            offset=offset,
            limit=limit,
        )
    except ProjectImportError as exc:
        raise _http_error(exc, 404) from exc


@router.post("/project-imports/{import_id}/apply")
def apply_project_import(
    project_id: int,
    import_id: str,
    auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    try:
        service = ProjectImportService(db)
        row = service.get(project_id, import_id)
        report = service.apply(row, auth["user_id"])
        return {"import_id": row.id, "status": "completed", "report": report}
    except ProjectImportError as exc:
        raise _http_error(exc) from exc


@router.get("/lifecycle-models", response_model=list[LifecycleModelResponse])
def list_lifecycle_models(
    project_id: int,
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    return LifecycleModelService(db).list(project_id)


@router.post("/lifecycle-models", response_model=LifecycleModelResponse)
def create_lifecycle_model(
    project_id: int,
    payload: LifecycleModelCreate,
    auth=Depends(require_project_role("editor")),
    db: Session = Depends(get_db),
):
    try:
        row = LifecycleModelService(db).create(
            project_id,
            name=payload.name,
            definition=payload.definition,
            actor_user_id=auth["user_id"],
            requested_status=payload.status,
        )
        db.commit()
        db.refresh(row)
        return row
    except LifecycleModelError as exc:
        db.rollback()
        raise _http_error(exc, 400) from exc


@router.post("/lifecycle-models/{model_id}/validate")
def validate_lifecycle_model(
    project_id: int,
    model_id: int,
    _auth=Depends(require_project_role("editor")),
    db: Session = Depends(get_db),
):
    try:
        service = LifecycleModelService(db)
        row = service.get(project_id, model_id)
        report = service.validate(row)
        db.commit()
        return {"model_id": row.id, "status": row.status, "report": report}
    except LifecycleModelError as exc:
        db.rollback()
        raise _http_error(exc, 400) from exc


@router.post("/lifecycle-models/compare")
def compare_lifecycle_models(
    project_id: int,
    payload: LifecycleModelCompareRequest,
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    try:
        service = LifecycleModelService(db)
        left = service.get(project_id, payload.left_model_id)
        right = service.get(project_id, payload.right_model_id)
        return service.compare(left, right, sample_limit=payload.sample_limit, as_of=payload.as_of)
    except LifecycleModelError as exc:
        raise _http_error(exc, 400) from exc


@router.post("/lifecycle-models/{model_id}/activate")
def activate_lifecycle_model(
    project_id: int,
    model_id: int,
    payload: LifecycleModelActivation,
    auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    try:
        service = LifecycleModelService(db)
        row = service.get(project_id, model_id)
        result = service.activate(row, target_status=payload.target_status, actor_user_id=auth["user_id"])
        result["reason"] = payload.reason
        db.commit()
        return result
    except LifecycleModelError as exc:
        db.rollback()
        raise _http_error(exc) from exc


@router.get("/orchestration-cutovers", response_model=list[CutoverResponse])
def list_orchestration_cutovers(
    project_id: int,
    _auth=Depends(require_project_role("viewer")),
    db: Session = Depends(get_db),
):
    return db.query(ProjectLifecycleCutover).filter(
        ProjectLifecycleCutover.project_id == project_id,
    ).order_by(ProjectLifecycleCutover.purpose_key).all()


@router.put("/orchestration-cutovers/{purpose_key}", response_model=CutoverResponse)
def update_orchestration_cutover(
    project_id: int,
    purpose_key: str,
    payload: CutoverUpdate,
    auth=Depends(require_project_role("admin")),
    db: Session = Depends(get_db),
):
    if payload.purpose_key != purpose_key:
        raise HTTPException(status_code=400, detail="purpose_key path and payload must match")
    try:
        row = OrchestrationCutoverService(db).update(
            project_id,
            purpose_key=purpose_key,
            mode=payload.mode,
            expected_epoch=payload.expected_epoch,
            lifecycle_model_id=payload.lifecycle_model_id,
            reason=payload.reason,
            actor_user_id=auth["user_id"],
        )
        db.commit()
        db.refresh(row)
        return row
    except OrchestrationCutoverError as exc:
        db.rollback()
        raise _http_error(exc) from exc
