"""
Specialists Router

CRUD endpoints for managing specialist agents and their knowledge sources.
"""

import os
import shutil
from typing import List
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import SpecialistAgent, SpecialistKnowledgeSource, AgentTeam
from app.schemas.agent_teams import (
    SpecialistAgentCreate,
    SpecialistAgentUpdate,
    SpecialistAgentResponse,
    SpecialistKnowledgeSourceCreate,
    SpecialistKnowledgeSourceResponse,
    GuardrailConfigUpdate,
)
from app.routers.auth import get_current_user
from app.services.chatbot.specialist_service import SpecialistService
from app.services.chatbot.vector_store import VectorStoreService
from app.services.chatbot.knowledge_loader import KnowledgeLoader
from app.services.chatbot.llm_key_resolver import resolve_embeddings, create_embeddings

router = APIRouter(tags=["specialists"])


def get_knowledge_services(db: Session, project_id: int):
    """Initialize knowledge processing services using project-level LLM config."""
    try:
        emb_cfg = resolve_embeddings(db, project_id)
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    embeddings = create_embeddings(emb_cfg)
    vector_store_path = os.getenv("CHATBOT_VECTOR_STORE_PATH", "./vector_stores")
    knowledge_loader = KnowledgeLoader()
    vector_store = VectorStoreService(vector_store_path, embeddings)

    return vector_store, knowledge_loader


def _get_project_id_for_specialist(db: Session, specialist_id: int) -> int:
    """Resolve project_id from specialist → team → project."""
    specialist = db.query(SpecialistAgent).filter(SpecialistAgent.id == specialist_id).first()
    if not specialist:
        return 0
    team = db.query(AgentTeam).filter(AgentTeam.id == specialist.team_id).first()
    return team.project_id if team else 0


# ============================================================================
# Specialist Agent CRUD
# ============================================================================

@router.post(
    "/agent-teams/{team_id}/specialists",
    response_model=SpecialistAgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_specialist(
    team_id: int,
    specialist_data: SpecialistAgentCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new specialist agent for a team."""
    team = db.query(AgentTeam).filter(AgentTeam.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Agent team not found")

    service = SpecialistService(db)
    try:
        specialist = service.create_specialist(team_id, specialist_data.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return specialist


@router.get("/specialists/{specialist_id}", response_model=SpecialistAgentResponse)
async def get_specialist(
    specialist_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a specialist agent by ID."""
    service = SpecialistService(db)
    specialist = service.get_specialist(specialist_id)

    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    return specialist


@router.put("/specialists/{specialist_id}", response_model=SpecialistAgentResponse)
async def update_specialist(
    specialist_id: int,
    specialist_data: SpecialistAgentUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update a specialist agent."""
    service = SpecialistService(db)
    specialist = service.update_specialist(
        specialist_id, specialist_data.model_dump(exclude_unset=True)
    )

    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    return specialist


@router.delete("/specialists/{specialist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_specialist(
    specialist_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a specialist agent."""
    service = SpecialistService(db)
    if not service.delete_specialist(specialist_id):
        raise HTTPException(status_code=404, detail="Specialist not found")


# ============================================================================
# Knowledge Source Management
# ============================================================================

@router.post(
    "/specialists/{specialist_id}/knowledge/upload",
    response_model=SpecialistKnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_knowledge_file(
    specialist_id: int,
    file: UploadFile = File(...),
    label: str = Form(None),
    expose_to_user: bool = Form(False),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Upload a knowledge file for a specialist."""
    service = SpecialistService(db)
    specialist = service.get_specialist(specialist_id)
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    # Save file
    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")
    specialist_dir = os.path.join(knowledge_base_path, f"specialist_{specialist_id}")
    Path(specialist_dir).mkdir(parents=True, exist_ok=True)

    file_path = os.path.join(specialist_dir, file.filename)
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # Determine file type
    file_type = "internal_doc"
    if file.filename.endswith(".pdf"):
        file_type = "pdf"
    elif file.filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        file_type = "image"
    elif file.filename.endswith((".md", ".markdown")):
        file_type = "internal_doc"
    elif file.filename.endswith(".txt"):
        file_type = "text"

    # Create knowledge source
    source = service.add_knowledge_from_file(
        specialist_id=specialist_id,
        file_path=file_path,
        file_name=file.filename,
        file_type=file_type,
        label=label,
        expose_to_user=expose_to_user,
    )

    # Process the knowledge source
    try:
        project_id = _get_project_id_for_specialist(db, specialist_id)
        vector_store, knowledge_loader = get_knowledge_services(db, project_id)
        service.process_knowledge_source(source.id, vector_store, knowledge_loader)
        db.refresh(source)
    except Exception as e:
        source.processing_error = str(e)
        db.commit()

    return source


@router.post(
    "/specialists/{specialist_id}/knowledge/url",
    response_model=SpecialistKnowledgeSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_knowledge_url(
    specialist_id: int,
    source_data: SpecialistKnowledgeSourceCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Add a URL-based knowledge source to a specialist."""
    service = SpecialistService(db)
    specialist = service.get_specialist(specialist_id)
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    if not source_data.source_url:
        raise HTTPException(status_code=400, detail="source_url is required")

    source = service.add_knowledge_from_url(
        specialist_id=specialist_id,
        source_url=source_data.source_url,
        source_type=source_data.source_type,
        label=source_data.label,
        expose_to_user=source_data.expose_to_user,
    )

    # Process
    try:
        project_id = _get_project_id_for_specialist(db, specialist_id)
        vector_store, knowledge_loader = get_knowledge_services(db, project_id)
        service.process_knowledge_source(source.id, vector_store, knowledge_loader)
        db.refresh(source)
    except Exception as e:
        source.processing_error = str(e)
        db.commit()

    return source


@router.post(
    "/specialists/{specialist_id}/knowledge/batch-upload",
    response_model=List[SpecialistKnowledgeSourceResponse],
    status_code=status.HTTP_201_CREATED,
)
async def batch_upload_knowledge(
    specialist_id: int,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Batch upload knowledge files for a specialist."""
    service = SpecialistService(db)
    specialist = service.get_specialist(specialist_id)
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    knowledge_base_path = os.getenv("CHATBOT_KNOWLEDGE_BASE_PATH", "./knowledge_bases")
    specialist_dir = os.path.join(knowledge_base_path, f"specialist_{specialist_id}")
    Path(specialist_dir).mkdir(parents=True, exist_ok=True)

    sources = []
    for file in files:
        file_path = os.path.join(specialist_dir, file.filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)

        file_type = "internal_doc"
        if file.filename.endswith(".pdf"):
            file_type = "pdf"
        elif file.filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            file_type = "image"

        source = service.add_knowledge_from_file(
            specialist_id=specialist_id,
            file_path=file_path,
            file_name=file.filename,
            file_type=file_type,
        )

        try:
            project_id = _get_project_id_for_specialist(db, specialist_id)
            vector_store, knowledge_loader = get_knowledge_services(db, project_id)
            service.process_knowledge_source(source.id, vector_store, knowledge_loader)
            db.refresh(source)
        except Exception:
            pass

        sources.append(source)

    return sources


@router.get(
    "/specialists/{specialist_id}/knowledge",
    response_model=List[SpecialistKnowledgeSourceResponse],
)
async def list_knowledge_sources(
    specialist_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """List all knowledge sources for a specialist."""
    service = SpecialistService(db)
    specialist = service.get_specialist(specialist_id)
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    return service.list_knowledge_sources(specialist_id)


@router.delete(
    "/specialist-knowledge/{knowledge_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_knowledge_source(
    knowledge_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Delete a knowledge source."""
    service = SpecialistService(db)
    if not service.delete_knowledge_source(knowledge_id):
        raise HTTPException(status_code=404, detail="Knowledge source not found")


@router.post(
    "/specialist-knowledge/{knowledge_id}/reprocess",
    response_model=SpecialistKnowledgeSourceResponse,
)
async def reprocess_knowledge_source(
    knowledge_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Reprocess a knowledge source (re-embed and update vector store)."""
    source = db.query(SpecialistKnowledgeSource).filter(
        SpecialistKnowledgeSource.id == knowledge_id
    ).first()

    if not source:
        raise HTTPException(status_code=404, detail="Knowledge source not found")

    service = SpecialistService(db)

    # Resolve project_id from source → specialist → team
    specialist = db.query(SpecialistAgent).filter(SpecialistAgent.id == source.specialist_id).first()
    project_id = _get_project_id_for_specialist(db, source.specialist_id) if specialist else 0
    vector_store, knowledge_loader = get_knowledge_services(db, project_id)

    try:
        source = service.process_knowledge_source(
            knowledge_id, vector_store, knowledge_loader
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return source


# ============================================================================
# Guardrails Configuration
# ============================================================================

@router.put("/specialists/{specialist_id}/guardrails", response_model=SpecialistAgentResponse)
async def update_guardrails(
    specialist_id: int,
    guardrail_data: GuardrailConfigUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update guardrail settings for a specialist agent."""
    specialist = db.query(SpecialistAgent).filter(
        SpecialistAgent.id == specialist_id
    ).first()
    if not specialist:
        raise HTTPException(status_code=404, detail="Specialist not found")

    update_data = guardrail_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if hasattr(specialist, key):
            setattr(specialist, key, value)

    db.commit()
    db.refresh(specialist)
    return specialist
