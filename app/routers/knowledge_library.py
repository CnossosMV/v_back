"""Knowledge Library Router

CRUD for assets, collections, and consumer bindings.
Search and retrieval endpoints added in Phase 2.
"""
import logging
import mimetypes
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from typing import Optional, List

from app.database import get_db
from app.routers.auth import get_current_user
from app.dependencies import require_project_role
from app.services.knowledge_library_service import KnowledgeLibraryService
from app.schemas.knowledge_library import (
    AssetCreate, AssetUpdate, AssetResponse, AssetListResponse,
    CollectionCreate, CollectionUpdate, CollectionResponse, CollectionDetailResponse,
    CollectionAssetsUpdate,
    BindingCreate, BindingUpdate, BindingResponse, EffectiveCollectionResponse,
    SearchRequest, SearchResult, RetrieveRequest, RetrieveResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge Library"])


def _asset_to_response(asset, svc: KnowledgeLibraryService) -> AssetResponse:
    return AssetResponse(
        id=asset.id,
        project_id=asset.project_id,
        asset_type=asset.asset_type,
        name=asset.name,
        description=asset.description,
        tags=asset.tags or [],
        language=asset.language,
        source_data=asset.source_data or {},
        processing_status=asset.processing_status,
        processing_error=asset.processing_error,
        chunk_count=asset.chunk_count,
        embedding_model=asset.embedding_model,
        last_processed_at=asset.last_processed_at,
        content_hash=asset.content_hash,
        version=asset.version,
        previous_version_id=asset.previous_version_id,
        status=asset.status,
        created_by=asset.created_by,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        collection_ids=svc.get_asset_collection_ids(asset.id),
        usage_mode=getattr(asset, 'usage_mode', 'rag'),
        slug=getattr(asset, 'slug', None),
        storage_key=getattr(asset, 'storage_key', None),
        file_size=getattr(asset, 'file_size', None),
    )


# ============================================================================
# File Serving (public — URL shared for RAG / direct access)
# ============================================================================

@router.get("/knowledge-library/files/{project_id}/{filename}")
async def serve_knowledge_file(project_id: int, filename: str):
    """Serve an uploaded knowledge asset file."""
    root = os.getenv("STORAGE_LOCAL_ROOT", "/app/data/knowledge")
    file_path = Path(root) / str(project_id) / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    content_type, _ = mimetypes.guess_type(str(file_path))
    return FileResponse(
        path=str(file_path),
        media_type=content_type or "application/octet-stream",
        filename=filename,
    )


# ============================================================================
# Assets
# ============================================================================

MAX_FILE_SIZES = {
    "document": 50 * 1024 * 1024,   # 50 MB
    "image": 10 * 1024 * 1024,      # 10 MB
    "video": 100 * 1024 * 1024,     # 100 MB
    "audio": 50 * 1024 * 1024,      # 50 MB
}

UPLOADABLE_TYPES = {"document", "image", "video", "audio"}


@router.post("/projects/{project_id}/knowledge-library/assets/upload")
async def upload_asset(
    project_id: int,
    file: UploadFile = File(...),
    name: str = Form(...),
    asset_type: str = Form(...),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    usage_mode: Optional[str] = Form(None),
    slug: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    """Upload a file and create a knowledge asset."""
    if asset_type not in UPLOADABLE_TYPES:
        raise HTTPException(status_code=400, detail=f"Upload not supported for type: {asset_type}")

    # Read file data
    data = await file.read()
    file_size = len(data)

    max_size = MAX_FILE_SIZES.get(asset_type, 50 * 1024 * 1024)
    if file_size > max_size:
        max_mb = max_size // (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"File too large. Max {max_mb}MB for {asset_type}")

    # Optimize images: resize to max 1600px, compress JPEG at 85%
    original_filename = file.filename or ""
    ext = Path(original_filename).suffix
    content_type = file.content_type or "application/octet-stream"

    if asset_type == "image" and content_type.startswith("image/"):
        try:
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(data))
            # Resize if larger than 1600px on longest side
            max_dim = 1600
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            # Convert to JPEG (unless transparent PNG)
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            if has_alpha:
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                buf.seek(0)
                data = buf.read()
                ext = ".png"
                content_type = "image/png"
            else:
                img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85, optimize=True)
                buf.seek(0)
                data = buf.read()
                ext = ".jpg"
                content_type = "image/jpeg"
            file_size = len(data)
        except Exception as img_err:
            logger.warning(f"Image optimization skipped: {img_err}")

    # Generate storage key
    storage_key = f"{project_id}/{uuid.uuid4().hex}{ext}"

    from app.services.storage import get_storage_backend
    storage = get_storage_backend()

    try:
        storage.save(storage_key, data, content_type)
    except Exception as e:
        logger.error(f"Storage save failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to store file")

    # Build source_data
    file_url = storage.get_url(storage_key)
    source_data = {
        "file_url": file_url,
        "original_filename": original_filename,
        "content_type": content_type,
    }

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    svc = KnowledgeLibraryService(db)
    user_email = current_user.email if hasattr(current_user, "email") else None

    try:
        asset = svc.create_asset(
            project_id=project_id,
            asset_type=asset_type,
            name=name,
            source_data=source_data,
            description=description,
            tags=tag_list,
            status="active",
            created_by=user_email,
            usage_mode=usage_mode,
            slug=slug,
            storage_key=storage_key,
            file_size=file_size,
        )
    except Exception as e:
        # Clean up stored file on DB failure
        try:
            storage.delete(storage_key)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=str(e))

    return _asset_to_response(asset, svc)


@router.post("/projects/{project_id}/knowledge-library/assets")
def create_asset(
    project_id: int,
    payload: AssetCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    user_email = current_user.email if hasattr(current_user, "email") else None
    try:
        asset = svc.create_asset(
            project_id=project_id,
            asset_type=payload.asset_type,
            name=payload.name,
            source_data=payload.source_data,
            description=payload.description,
            tags=payload.tags,
            language=payload.language,
            status=payload.status or "active",
            created_by=user_email,
            usage_mode=payload.usage_mode,
            slug=payload.slug,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _asset_to_response(asset, svc)


@router.get("/projects/{project_id}/knowledge-library/assets")
def list_assets(
    project_id: int,
    asset_type: Optional[str] = Query(None),
    collection_id: Optional[int] = Query(None),
    tag: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    items, total = svc.list_assets(
        project_id=project_id,
        asset_type=asset_type,
        collection_id=collection_id,
        tag=tag,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
    )
    return AssetListResponse(
        items=[_asset_to_response(a, svc) for a in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/projects/{project_id}/knowledge-library/assets/{asset_id}")
def get_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    asset = svc.get_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_to_response(asset, svc)


@router.put("/projects/{project_id}/knowledge-library/assets/{asset_id}")
def update_asset(
    project_id: int,
    asset_id: int,
    payload: AssetUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    asset = svc.update_asset(project_id, asset_id, payload.model_dump(exclude_unset=True))
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_to_response(asset, svc)


@router.post("/projects/{project_id}/knowledge-library/assets/{asset_id}/archive")
def archive_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    asset = svc.archive_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_to_response(asset, svc)


@router.post("/projects/{project_id}/knowledge-library/assets/{asset_id}/restore")
def restore_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    asset = svc.restore_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return _asset_to_response(asset, svc)


@router.delete("/projects/{project_id}/knowledge-library/assets/{asset_id}")
def delete_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    if not svc.delete_asset(project_id, asset_id):
        raise HTTPException(status_code=404, detail="Asset not found")
    return {"ok": True}


# ============================================================================
# Collections
# ============================================================================

@router.post("/projects/{project_id}/knowledge-library/collections")
def create_collection(
    project_id: int,
    payload: CollectionCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    coll = svc.create_collection(
        project_id=project_id,
        name=payload.name,
        description=payload.description,
        visibility=payload.visibility,
    )
    return CollectionResponse(
        id=coll.id,
        project_id=coll.project_id,
        name=coll.name,
        description=coll.description,
        visibility=coll.visibility,
        asset_count=0,
        consumer_count=0,
        created_at=coll.created_at,
        updated_at=coll.updated_at,
    )


@router.get("/projects/{project_id}/knowledge-library/collections")
def list_collections(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    return svc.list_collections(project_id)


@router.get("/projects/{project_id}/knowledge-library/collections/{collection_id}")
def get_collection(
    project_id: int,
    collection_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    coll = svc.get_collection(project_id, collection_id)
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")
    assets = svc.get_collection_assets(project_id, collection_id)
    from sqlalchemy import func as sqlfunc
    from app.models import CollectionAsset, ConsumerKnowledgeBinding
    asset_count = db.query(sqlfunc.count(CollectionAsset.id)).filter(
        CollectionAsset.collection_id == collection_id
    ).scalar()
    consumer_count = db.query(sqlfunc.count(ConsumerKnowledgeBinding.id)).filter(
        ConsumerKnowledgeBinding.collection_id == collection_id
    ).scalar()
    return CollectionDetailResponse(
        id=coll.id,
        project_id=coll.project_id,
        name=coll.name,
        description=coll.description,
        visibility=coll.visibility,
        asset_count=asset_count,
        consumer_count=consumer_count,
        created_at=coll.created_at,
        updated_at=coll.updated_at,
        assets=[_asset_to_response(a, svc) for a in assets],
    )


@router.put("/projects/{project_id}/knowledge-library/collections/{collection_id}")
def update_collection(
    project_id: int,
    collection_id: int,
    payload: CollectionUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    coll = svc.update_collection(project_id, collection_id, payload.model_dump(exclude_unset=True))
    if not coll:
        raise HTTPException(status_code=404, detail="Collection not found")
    from sqlalchemy import func as sqlfunc
    from app.models import CollectionAsset, ConsumerKnowledgeBinding
    asset_count = db.query(sqlfunc.count(CollectionAsset.id)).filter(
        CollectionAsset.collection_id == collection_id
    ).scalar()
    consumer_count = db.query(sqlfunc.count(ConsumerKnowledgeBinding.id)).filter(
        ConsumerKnowledgeBinding.collection_id == collection_id
    ).scalar()
    return CollectionResponse(
        id=coll.id,
        project_id=coll.project_id,
        name=coll.name,
        description=coll.description,
        visibility=coll.visibility,
        asset_count=asset_count,
        consumer_count=consumer_count,
        created_at=coll.created_at,
        updated_at=coll.updated_at,
    )


@router.delete("/projects/{project_id}/knowledge-library/collections/{collection_id}")
def delete_collection(
    project_id: int,
    collection_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    if not svc.delete_collection(project_id, collection_id):
        raise HTTPException(status_code=404, detail="Collection not found")
    return {"ok": True}


@router.post("/projects/{project_id}/knowledge-library/collections/{collection_id}/assets")
def add_assets_to_collection(
    project_id: int,
    collection_id: int,
    payload: CollectionAssetsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    user_email = current_user.email if hasattr(current_user, "email") else None
    try:
        added = svc.add_assets_to_collection(project_id, collection_id, payload.asset_ids, added_by=user_email)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"added": added}


@router.delete("/projects/{project_id}/knowledge-library/collections/{collection_id}/assets/{asset_id}")
def remove_asset_from_collection(
    project_id: int,
    collection_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    if not svc.remove_asset_from_collection(project_id, collection_id, asset_id):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ============================================================================
# Consumer Bindings
# ============================================================================

@router.get("/projects/{project_id}/knowledge-library/consumers/{consumer_type}/{consumer_id}/knowledge")
def list_bindings(
    project_id: int,
    consumer_type: str,
    consumer_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    bindings = svc.list_bindings(project_id, consumer_type, consumer_id)
    result = []
    for b in bindings:
        coll = svc.get_collection(project_id, b.collection_id)
        result.append(BindingResponse(
            id=b.id,
            project_id=b.project_id,
            consumer_type=b.consumer_type,
            consumer_id=b.consumer_id,
            collection_id=b.collection_id,
            collection_name=coll.name if coll else None,
            permission=b.permission,
            created_at=b.created_at,
        ))
    return result


@router.post("/projects/{project_id}/knowledge-library/consumers/{consumer_type}/{consumer_id}/knowledge")
def create_binding(
    project_id: int,
    consumer_type: str,
    consumer_id: int,
    payload: BindingCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    try:
        binding = svc.create_binding(
            project_id=project_id,
            consumer_type=consumer_type,
            consumer_id=consumer_id,
            collection_id=payload.collection_id,
            permission=payload.permission,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    coll = svc.get_collection(project_id, binding.collection_id)
    return BindingResponse(
        id=binding.id,
        project_id=binding.project_id,
        consumer_type=binding.consumer_type,
        consumer_id=binding.consumer_id,
        collection_id=binding.collection_id,
        collection_name=coll.name if coll else None,
        permission=binding.permission,
        created_at=binding.created_at,
    )


@router.put("/projects/{project_id}/knowledge-library/consumers/{consumer_type}/{consumer_id}/knowledge/{binding_id}")
def update_binding(
    project_id: int,
    consumer_type: str,
    consumer_id: int,
    binding_id: int,
    payload: BindingUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    binding = svc.update_binding(project_id, binding_id, payload.permission)
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")
    coll = svc.get_collection(project_id, binding.collection_id)
    return BindingResponse(
        id=binding.id,
        project_id=binding.project_id,
        consumer_type=binding.consumer_type,
        consumer_id=binding.consumer_id,
        collection_id=binding.collection_id,
        collection_name=coll.name if coll else None,
        permission=binding.permission,
        created_at=binding.created_at,
    )


@router.delete("/projects/{project_id}/knowledge-library/consumers/{consumer_type}/{consumer_id}/knowledge/{binding_id}")
def delete_binding(
    project_id: int,
    consumer_type: str,
    consumer_id: int,
    binding_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    svc = KnowledgeLibraryService(db)
    if not svc.delete_binding(project_id, binding_id):
        raise HTTPException(status_code=404, detail="Binding not found")
    return {"ok": True}


@router.get("/projects/{project_id}/knowledge-library/consumers/{consumer_type}/{consumer_id}/knowledge/effective")
def get_effective_collections(
    project_id: int,
    consumer_type: str,
    consumer_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    svc = KnowledgeLibraryService(db)
    return svc.get_effective_collections(project_id, consumer_type, consumer_id)


# ============================================================================
# Search & Retrieval
# ============================================================================

def _resolve_embedding_key(db: Session, project_id: int) -> str:
    """Resolve embedding API key using the central resolver."""
    from app.services.chatbot.llm_key_resolver import resolve_embeddings
    try:
        emb_cfg = resolve_embeddings(db, project_id)
        return emb_cfg.api_key
    except ValueError:
        return ""


@router.post("/projects/{project_id}/knowledge-library/search")
def search_assets(
    project_id: int,
    payload: SearchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    from app.services.knowledge.retrieval_service import RetrievalService

    api_key = _resolve_embedding_key(db, project_id)
    retrieval = RetrievalService(db, api_key)
    results = retrieval.search(
        project_id=project_id,
        query=payload.query,
        collection_ids=payload.collection_ids,
        consumer_type=payload.consumer_type,
        consumer_id=payload.consumer_id,
        asset_types=payload.types,
        limit=payload.limit,
        min_score=payload.min_score,
    )
    return results


@router.post("/projects/{project_id}/knowledge-library/retrieve")
def retrieve_for_rag(
    project_id: int,
    payload: RetrieveRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    from app.services.knowledge.retrieval_service import RetrievalService

    api_key = _resolve_embedding_key(db, project_id)
    retrieval = RetrievalService(db, api_key)
    return retrieval.retrieve_for_rag(
        project_id=project_id,
        query=payload.query,
        consumer_type=payload.consumer_type,
        consumer_id=payload.consumer_id,
        k=payload.k,
        min_score=payload.min_score,
        mode=payload.mode,
    )


@router.get("/projects/{project_id}/knowledge-library/snippets")
def snippet_autocomplete(
    project_id: int,
    prefix: str = Query(""),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("support_agent")),
):
    from app.services.knowledge.retrieval_service import RetrievalService

    api_key = _resolve_embedding_key(db, project_id)
    retrieval = RetrievalService(db, api_key)
    return retrieval.snippet_autocomplete(
        project_id=project_id,
        prefix=prefix,
        consumer_type="inbox",
        consumer_id=project_id,
    )


@router.post("/projects/{project_id}/knowledge-library/assets/{asset_id}/process")
def process_asset(
    project_id: int,
    asset_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    membership=Depends(require_project_role("admin")),
):
    """Manually trigger processing for a knowledge asset."""
    svc = KnowledgeLibraryService(db)
    asset = svc.get_asset(project_id, asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    api_key = _resolve_embedding_key(db, project_id)
    from app.services.knowledge.asset_processor import AssetProcessor
    processor = AssetProcessor(db, api_key)
    processor.process_asset(asset)
    return _asset_to_response(asset, svc)
