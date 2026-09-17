"""
GTM Router
GTM container generation and management.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List
import json

from app.database import get_db
from app.models.messaging import MessagingGTMContainer, MessagingDomain
from app.schemas.messaging import (
    GTMContainerCreate, GTMContainerUpdate, GTMContainerResponse,
    GTMContainerGenerateResponse
)
from app.services.messaging.gtm_generator import gtm_generator
from app.routers.auth import get_current_user
from app.models import User

router = APIRouter(prefix="/projects/{project_id}/messaging/gtm", tags=["messaging-gtm"])


@router.get("/containers", response_model=List[GTMContainerResponse])
async def list_containers(
    project_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all GTM container configs for a project."""
    containers = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.project_id == project_id
    ).order_by(MessagingGTMContainer.created_at.desc()).all()

    return containers


@router.get("/containers/{container_id}", response_model=GTMContainerResponse)
async def get_container(
    project_id: int,
    container_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get a specific GTM container config."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    return container


@router.post("/containers", response_model=GTMContainerResponse)
async def create_container(
    project_id: int,
    data: GTMContainerCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new GTM container config."""
    # Verify domain exists if provided
    if data.domain_id:
        domain = db.query(MessagingDomain).filter(
            MessagingDomain.id == data.domain_id,
            MessagingDomain.project_id == project_id
        ).first()

        if not domain:
            raise HTTPException(status_code=404, detail="Domain not found")

    container = MessagingGTMContainer(
        project_id=project_id,
        container_name=data.container_name,
        domain_id=data.domain_id,
        gtm_container_id=data.gtm_container_id
    )
    db.add(container)
    db.commit()
    db.refresh(container)

    return container


@router.put("/containers/{container_id}", response_model=GTMContainerResponse)
async def update_container(
    project_id: int,
    container_id: int,
    data: GTMContainerUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update a GTM container config."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    if data.container_name is not None:
        container.container_name = data.container_name

    if data.domain_id is not None:
        if data.domain_id:
            domain = db.query(MessagingDomain).filter(
                MessagingDomain.id == data.domain_id,
                MessagingDomain.project_id == project_id
            ).first()
            if not domain:
                raise HTTPException(status_code=404, detail="Domain not found")
        container.domain_id = data.domain_id

    if data.gtm_container_id is not None:
        container.gtm_container_id = data.gtm_container_id

    db.commit()
    db.refresh(container)

    return container


@router.delete("/containers/{container_id}")
async def delete_container(
    project_id: int,
    container_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Delete a GTM container config."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    db.delete(container)
    db.commit()

    return {"success": True, "message": "GTM container deleted"}


@router.post("/containers/{container_id}/generate", response_model=GTMContainerGenerateResponse)
async def generate_container(
    project_id: int,
    container_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate GTM container JSON."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    try:
        generated = await gtm_generator.generate_gtm_container(
            db=db,
            project_id=project_id,
            container_id=container_id
        )

        return GTMContainerGenerateResponse(
            success=True,
            version=container.version,
            generated_at=container.last_generated_at,
            container_json=generated,
            message="GTM container generated successfully"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/containers/{container_id}/download")
async def download_container(
    project_id: int,
    container_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Download GTM container JSON file."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    if not container.generated_json:
        raise HTTPException(status_code=400, detail="Container not yet generated")

    # Return as downloadable JSON
    return JSONResponse(
        content=container.generated_json,
        headers={
            "Content-Disposition": f'attachment; filename="{container.container_name.replace(" ", "_")}_gtm.json"'
        }
    )


@router.get("/containers/{container_id}/preview")
async def preview_container(
    project_id: int,
    container_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Preview generated GTM container structure."""
    container = db.query(MessagingGTMContainer).filter(
        MessagingGTMContainer.id == container_id,
        MessagingGTMContainer.project_id == project_id
    ).first()

    if not container:
        raise HTTPException(status_code=404, detail="GTM container not found")

    if not container.generated_json:
        return {
            "generated": False,
            "message": "Container not yet generated"
        }

    # Return summary of container contents
    cv = container.generated_json.get("containerVersion", {})

    return {
        "generated": True,
        "version": container.version,
        "generated_at": container.last_generated_at,
        "summary": {
            "tags": len(cv.get("tag", [])),
            "triggers": len(cv.get("trigger", [])),
            "variables": len(cv.get("variable", [])),
            "folders": len(cv.get("folder", []))
        },
        "tags": [
            {"name": t.get("name"), "type": t.get("type")}
            for t in cv.get("tag", [])
        ],
        "triggers": [
            {"name": t.get("name"), "type": t.get("type")}
            for t in cv.get("trigger", [])
        ],
        "variables": [
            {"name": v.get("name"), "type": v.get("type")}
            for v in cv.get("variable", [])
        ]
    }
