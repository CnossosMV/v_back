from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from app.database import get_db
from app import models, schemas
from app.routers.auth import get_current_user
from cryptography.fernet import Fernet
import os
import base64
import secrets

router = APIRouter(prefix="/smtp-config", tags=["smtp-config"])

# Simple encryption for SMTP passwords (in production, use proper key management)
def get_encryption_key():
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        # Generate a key for development (in production, use proper key management)
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        print(f"Generated encryption key: {key}")
    return key.encode()

def encrypt_password(password: str) -> str:
    f = Fernet(get_encryption_key())
    return f.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password: str) -> str:
    f = Fernet(get_encryption_key())
    return f.decrypt(encrypted_password.encode()).decode()


def _to_smtp_response(config: models.CustomerSMTPConfig, db: Session) -> schemas.SMTPConfig:
    """Build SMTPConfig response with computed feedback fields and lazy backfill."""
    # Lazy backfill: generate secret for existing configs that don't have one
    if not config.feedback_webhook_secret:
        config.feedback_webhook_secret = secrets.token_hex(32)
        db.add(config)
        db.flush()

    api_base = os.getenv("API_BASE_URL", "").rstrip("/")
    feedback_url = f"{api_base}/webhooks/email/smtp-config/{config.id}/feedback" if api_base else None

    response = schemas.SMTPConfig.from_orm(config)
    response.smtp_password = "***ENCRYPTED***"
    response.feedback_webhook_url = feedback_url
    response.feedback_configured = bool(config.feedback_webhook_secret)
    return response

@router.post("/project/{project_id}", response_model=schemas.SMTPConfig, status_code=status.HTTP_201_CREATED)
def create_smtp_config(
    project_id: int, 
    smtp_config: schemas.SMTPConfigCreate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Check if project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Store config in database with encrypted password
    config_data = smtp_config.dict()
    config_data["smtp_password"] = encrypt_password(config_data["smtp_password"])
    config_data["project_id"] = project_id
    # Remove customer_id if it exists in the data
    config_data.pop("customer_id", None)
    
    db_config = models.CustomerSMTPConfig(**config_data)
    db_config.feedback_webhook_secret = secrets.token_hex(32)
    db.add(db_config)
    db.commit()
    db.refresh(db_config)

    return _to_smtp_response(db_config, db)

@router.get("/project/{project_id}", response_model=Optional[schemas.SMTPConfig])
def get_project_smtp_config(
    project_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Check if project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    config = db.query(models.CustomerSMTPConfig).filter(
        models.CustomerSMTPConfig.project_id == project_id
    ).first()
    
    if not config:
        return None

    return _to_smtp_response(config, db)


@router.get("/project/{project_id}/list", response_model=List[schemas.SMTPConfig])
def list_project_smtp_configs(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """List all SMTP configurations for a project."""
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )

    configs = db.query(models.CustomerSMTPConfig).filter(
        models.CustomerSMTPConfig.project_id == project_id
    ).order_by(models.CustomerSMTPConfig.id).all()

    result = [_to_smtp_response(c, db) for c in configs]
    db.commit()
    return result


@router.put("/{config_id}", response_model=schemas.SMTPConfig)
def update_smtp_config(
    config_id: int, 
    smtp_config: schemas.SMTPConfigUpdate, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Get config and verify it belongs to user's workspace
    db_config = db.query(models.CustomerSMTPConfig).join(models.Project).filter(
        models.CustomerSMTPConfig.id == config_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not db_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SMTP configuration not found"
        )
    
    update_data = smtp_config.dict(exclude_unset=True)
    
    # Encrypt password if provided
    if "smtp_password" in update_data and update_data["smtp_password"]:
        update_data["smtp_password"] = encrypt_password(update_data["smtp_password"])
    
    for field, value in update_data.items():
        setattr(db_config, field, value)
    
    db.commit()
    db.refresh(db_config)

    return _to_smtp_response(db_config, db)

@router.delete("/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_smtp_config(
    config_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    # Get config and verify it belongs to user's workspace
    config = db.query(models.CustomerSMTPConfig).join(models.Project).filter(
        models.CustomerSMTPConfig.id == config_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SMTP configuration not found"
        )
    
    db.delete(config)
    db.commit()
    return None

# Add endpoint to get decrypted SMTP password when needed (for testing purposes)
@router.get("/project/{project_id}/credentials", response_model=Optional[schemas.SMTPConfig])
def get_project_smtp_credentials(
    project_id: int, 
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Get SMTP config with decrypted password - use carefully, only for email sending"""
    # Check if project exists and belongs to user's workspace
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    config = db.query(models.CustomerSMTPConfig).filter(
        models.CustomerSMTPConfig.project_id == project_id
    ).first()
    
    if not config:
        return None

    response_config = _to_smtp_response(config, db)
    db.commit()
    # Override masked password with decrypted one for actual use
    try:
        response_config.smtp_password = decrypt_password(config.smtp_password)
    except Exception:
        response_config.smtp_password = "***DECRYPTION_ERROR***"

    return response_config

@router.post("/project/{project_id}/test")
def test_smtp_connection(
    project_id: int,
    test_request: schemas.FunnelTestRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """Test SMTP connection by sending a test email"""
    # Get project and verify access
    project = db.query(models.Project).filter(
        models.Project.id == project_id,
        models.Project.workspace_id == current_user.workspace_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get SMTP config for this project
    config = db.query(models.CustomerSMTPConfig).filter(
        models.CustomerSMTPConfig.project_id == project_id,
        models.CustomerSMTPConfig.is_active == True
    ).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SMTP configuration not found for this project"
        )
    
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        # Decrypt password
        decrypted_password = decrypt_password(config.smtp_password)
        
        # Create test message
        msg = MIMEMultipart()
        msg['From'] = config.from_email
        msg['To'] = test_request.test_email
        msg['Subject'] = "SMTP Configuration Test"
        
        body = f"""
This is a test email to verify your SMTP configuration.

Configuration Details:
- SMTP Server: {config.smtp_server}
- Port: {config.smtp_port}
- Username: {config.smtp_username}
- From Email: {config.from_email}
- From Name: {config.from_name or 'N/A'}
- TLS: {'Enabled' if config.smtp_use_tls else 'Disabled'}
- SSL: {'Enabled' if config.smtp_use_ssl else 'Disabled'}

If you received this email, your SMTP configuration is working correctly!

Sent from AutoFlow Email System
        """
        
        msg.attach(MIMEText(body, 'plain'))
        
        # Connect to SMTP server and send email
        if config.smtp_use_ssl:
            server = smtplib.SMTP_SSL(config.smtp_server, config.smtp_port)
        else:
            server = smtplib.SMTP(config.smtp_server, config.smtp_port)
            if config.smtp_use_tls:
                server.starttls()
        
        server.login(config.smtp_username, decrypted_password)
        
        # Send the email
        server.send_message(msg)
        server.quit()
        
        return {
            "success": True,
            "message": f"Test email sent successfully to {test_request.test_email}",
            "smtp_server": config.smtp_server,
            "from_email": config.from_email
        }
        
    except smtplib.SMTPAuthenticationError as e:
        error_detail = f"SMTP authentication failed: {str(e)}"
        
        # Add helpful message for AWS SES
        if "email-smtp" in config.smtp_server and "amazonaws.com" in config.smtp_server:
            error_detail += "\n\nAWS SES Troubleshooting:\n"
            error_detail += "1. Verify your AWS Access Key ID and Secret Access Key\n"
            error_detail += "2. Ensure SES is enabled in the correct AWS region\n"
            error_detail += "3. Check that the IAM user has 'ses:SendEmail' permission\n"
            error_detail += "4. Verify the 'from' email address is verified in AWS SES\n"
            error_detail += f"5. Current region from server: {config.smtp_server.split('.')[1] if '.' in config.smtp_server else 'unknown'}"
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_detail
        )
    except smtplib.SMTPConnectError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to connect to SMTP server: {str(e)}"
        )
    except smtplib.SMTPException as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"SMTP error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected error: {str(e)}"
        )