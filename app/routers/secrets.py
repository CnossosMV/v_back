from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app import models
from app.routers.auth import get_current_user
import os

router = APIRouter(prefix="/secrets", tags=["secrets"])

@router.get("/project/{project_id}/smtp")
def get_project_smtp_secrets(
    project_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Internal endpoint for backend services and n8n to get SMTP secrets
    Requires authentication for security
    """
    # Verify project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get SMTP config from database
    config = db.query(models.CustomerSMTPConfig).filter(
        models.CustomerSMTPConfig.project_id == project_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SMTP configuration not found for project"
        )
    
    # Import here to avoid circular imports
    from app.routers.smtp_config import decrypt_password
    
    # Return decrypted secrets from database
    secrets = {
        "smtp_password": decrypt_password(config.smtp_password),
        "smtp_username": config.smtp_username,
        "smtp_server": config.smtp_server,
        "smtp_port": config.smtp_port,
        "smtp_use_tls": config.smtp_use_tls,
        "smtp_use_ssl": config.smtp_use_ssl,
        "from_email": config.from_email,
        "from_name": config.from_name
    }
    
    return {"source": "database", "secrets": secrets}

@router.get("/project/{project_id}/whatsapp")
def get_project_whatsapp_secrets(
    project_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Internal endpoint for backend services and n8n to get WhatsApp/Evolution API secrets
    Requires authentication for security
    """
    # Verify project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get WhatsApp config from database
    config = db.query(models.CustomerWhatsAppConfig).filter(
        models.CustomerWhatsAppConfig.project_id == project_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="WhatsApp configuration not found for project"
        )
    
    # Get environment variables for Evolution API
    EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL")
    EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY")
    
    # Return secrets from database and environment
    secrets = {
        "instance_name": config.instance_name,
        "evolution_instance_id": config.evolution_instance_id,
        "evolution_api_key": EVOLUTION_API_KEY,
        "evolution_api_url": EVOLUTION_API_URL,
        "connection_status": config.connection_status
    }
    
    return {"source": "database", "secrets": secrets}

@router.get("/project/{project_id}/all")
def get_all_project_secrets(
    project_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Get all secrets for a project - useful for n8n workflows that need multiple integrations
    """
    # Verify project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    secrets = {
        "project_id": project_id,
        "project_name": project.name,
        "project_description": project.description,
        "workspace_id": project.workspace_id
    }
    
    # Get SMTP secrets if they exist
    try:
        smtp_response = get_project_smtp_secrets(project_id, db, current_user)
        secrets["smtp"] = smtp_response
    except HTTPException:
        secrets["smtp"] = None
    
    # Get WhatsApp secrets if they exist
    try:
        whatsapp_response = get_project_whatsapp_secrets(project_id, db, current_user)
        secrets["whatsapp"] = whatsapp_response
    except HTTPException:
        secrets["whatsapp"] = None
    
    return secrets