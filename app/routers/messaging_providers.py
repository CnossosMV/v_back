"""
Messaging Providers Router

Handles CRUD operations for messaging providers (Twilio SMS/WhatsApp, Evolution API).
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel, model_validator, field_validator
import re
import os

from app.database import get_db
from app.routers.auth import get_current_user
from app.models import MessagingProvider, Project, Chatbot
from app.services.twilio_service import twilio_service

router = APIRouter(tags=["messaging-providers"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class TwilioCredentials(BaseModel):
    account_sid: str
    auth_token: str
    phone_number: Optional[str] = None
    sender_id: Optional[str] = None
    messaging_service_sid: Optional[str] = None

    @model_validator(mode='after')
    def at_least_one_from(self):
        if not self.phone_number and not self.sender_id and not self.messaging_service_sid:
            raise ValueError('At least one of phone_number, sender_id, or messaging_service_sid must be provided')
        return self

    @field_validator('sender_id')
    @classmethod
    def validate_sender_id(cls, v):
        if v is not None:
            if not re.match(r'^(?=.*[a-zA-Z])[a-zA-Z0-9]{1,11}$', v):
                raise ValueError('Sender ID must be alphanumeric, max 11 chars, with at least 1 letter')
        return v

    @field_validator('messaging_service_sid')
    @classmethod
    def validate_messaging_service_sid(cls, v):
        if v is not None:
            if not re.match(r'^MG[a-f0-9]{32}$', v):
                raise ValueError('Messaging Service SID must start with MG followed by 32 hex characters')
        return v


class ProviderCreateRequest(BaseModel):
    name: str
    provider_type: str  # twilio_sms, twilio_whatsapp, evolution_api
    chatbot_id: Optional[int] = None
    credentials: Optional[TwilioCredentials] = None


class TwilioCredentialsUpdate(BaseModel):
    """Partial credentials update — empty strings are ignored, existing values kept."""
    account_sid: Optional[str] = None
    auth_token: Optional[str] = None
    phone_number: Optional[str] = None
    sender_id: Optional[str] = None
    messaging_service_sid: Optional[str] = None

    @field_validator('sender_id')
    @classmethod
    def validate_sender_id(cls, v):
        if v is not None and v != '':
            if not re.match(r'^(?=.*[a-zA-Z])[a-zA-Z0-9]{1,11}$', v):
                raise ValueError('Sender ID must be alphanumeric, max 11 chars, with at least 1 letter')
        return v

    @field_validator('messaging_service_sid')
    @classmethod
    def validate_messaging_service_sid(cls, v):
        if v is not None and v != '':
            if not re.match(r'^MG[a-f0-9]{32}$', v):
                raise ValueError('Messaging Service SID must start with MG followed by 32 hex characters')
        return v


class ProviderUpdateRequest(BaseModel):
    name: Optional[str] = None
    chatbot_id: Optional[int] = None
    is_active: Optional[bool] = None
    credentials: Optional[TwilioCredentialsUpdate] = None


class ProviderResponse(BaseModel):
    id: int
    project_id: int
    chatbot_id: Optional[int]
    provider_type: str
    name: str
    phone_number: Optional[str] = None
    sender_id: Optional[str] = None
    messaging_service_sid: Optional[str] = None
    webhook_url: Optional[str] = None
    is_active: bool
    is_verified: bool
    last_used_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


def _provider_to_response(p: MessagingProvider) -> ProviderResponse:
    metadata = p.provider_metadata or {}
    return ProviderResponse(
        id=p.id,
        project_id=p.project_id,
        chatbot_id=p.chatbot_id,
        provider_type=p.provider_type,
        name=p.name,
        phone_number=p.phone_number,
        sender_id=metadata.get("sender_id"),
        messaging_service_sid=metadata.get("messaging_service_sid"),
        webhook_url=p.webhook_url,
        is_active=p.is_active,
        is_verified=p.is_verified,
        last_used_at=p.last_used_at,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


class ProviderTestResponse(BaseModel):
    success: bool
    message: str
    account_name: Optional[str] = None
    account_status: Optional[str] = None


# ============================================================================
# Routes
# ============================================================================

@router.get("/projects/{project_id}/providers", response_model=List[ProviderResponse])
async def list_providers(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """List all messaging providers for a project."""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    providers = db.query(MessagingProvider).filter(
        MessagingProvider.project_id == project_id
    ).order_by(MessagingProvider.created_at.desc()).all()

    return [_provider_to_response(p) for p in providers]


@router.post("/projects/{project_id}/providers", response_model=ProviderResponse)
async def create_provider(
    project_id: int,
    request: ProviderCreateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Create a new messaging provider."""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Verify chatbot if provided
    if request.chatbot_id:
        chatbot = db.query(Chatbot).filter(
            Chatbot.id == request.chatbot_id,
            Chatbot.project_id == project_id
        ).first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")

    # Validate provider type
    valid_types = ["twilio_sms", "twilio_whatsapp", "evolution_api"]
    if request.provider_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider type. Must be one of: {', '.join(valid_types)}"
        )

    # For Twilio providers, encrypt credentials
    credentials_encrypted = None
    phone_number = None
    provider_metadata = {}
    if request.provider_type in ["twilio_sms", "twilio_whatsapp"]:
        if not request.credentials:
            raise HTTPException(
                status_code=400,
                detail="Credentials required for Twilio providers"
            )
        cred_dict = {
            "account_sid": request.credentials.account_sid,
            "auth_token": request.credentials.auth_token,
        }
        if request.credentials.phone_number:
            cred_dict["phone_number"] = request.credentials.phone_number
        if request.credentials.sender_id:
            cred_dict["sender_id"] = request.credentials.sender_id
            provider_metadata["sender_id"] = request.credentials.sender_id
        if request.credentials.messaging_service_sid:
            cred_dict["messaging_service_sid"] = request.credentials.messaging_service_sid
            provider_metadata["messaging_service_sid"] = request.credentials.messaging_service_sid
        credentials_encrypted = twilio_service.encrypt_credentials(cred_dict)
        phone_number = request.credentials.phone_number

    # Generate webhook URL
    api_base_url = os.getenv("API_BASE_URL", "https://api.versya.io")

    # Create provider
    provider = MessagingProvider(
        project_id=project_id,
        chatbot_id=request.chatbot_id,
        provider_type=request.provider_type,
        name=request.name,
        credentials_encrypted=credentials_encrypted,
        phone_number=phone_number,
        provider_metadata=provider_metadata or None,
        is_active=True,
        is_verified=False
    )

    db.add(provider)
    db.commit()
    db.refresh(provider)

    # Generate webhook URL with provider ID
    provider.webhook_url = f"{api_base_url}/webhooks/twilio/{provider.id}"
    db.commit()
    db.refresh(provider)

    return _provider_to_response(provider)


@router.get("/providers/{provider_id}", response_model=ProviderResponse)
async def get_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get a specific messaging provider."""
    provider = db.query(MessagingProvider).filter(
        MessagingProvider.id == provider_id
    ).first()

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    return _provider_to_response(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: int,
    request: ProviderUpdateRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Update a messaging provider."""
    provider = db.query(MessagingProvider).filter(
        MessagingProvider.id == provider_id
    ).first()

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if request.name is not None:
        provider.name = request.name

    if request.chatbot_id is not None:
        # Verify chatbot
        chatbot = db.query(Chatbot).filter(
            Chatbot.id == request.chatbot_id,
            Chatbot.project_id == provider.project_id
        ).first()
        if not chatbot:
            raise HTTPException(status_code=404, detail="Chatbot not found")
        provider.chatbot_id = request.chatbot_id

    if request.is_active is not None:
        provider.is_active = request.is_active

    if request.credentials is not None:
        # Merge with existing credentials (partial update)
        existing_creds = {}
        if provider.credentials_encrypted:
            try:
                existing_creds = twilio_service.decrypt_credentials(provider.credentials_encrypted)
            except Exception:
                pass

        cred_dict = dict(existing_creds)
        rc = request.credentials
        if rc.account_sid:
            cred_dict["account_sid"] = rc.account_sid
        if rc.auth_token:
            cred_dict["auth_token"] = rc.auth_token
        if rc.phone_number is not None:
            if rc.phone_number:
                cred_dict["phone_number"] = rc.phone_number
            else:
                cred_dict.pop("phone_number", None)
        if rc.sender_id is not None:
            if rc.sender_id:
                cred_dict["sender_id"] = rc.sender_id
            else:
                cred_dict.pop("sender_id", None)
        if rc.messaging_service_sid is not None:
            if rc.messaging_service_sid:
                cred_dict["messaging_service_sid"] = rc.messaging_service_sid
            else:
                cred_dict.pop("messaging_service_sid", None)

        provider.credentials_encrypted = twilio_service.encrypt_credentials(cred_dict)
        if rc.phone_number is not None:
            provider.phone_number = rc.phone_number or None
        metadata = provider.provider_metadata or {}
        if rc.sender_id is not None:
            if rc.sender_id:
                metadata["sender_id"] = rc.sender_id
            else:
                metadata.pop("sender_id", None)
        if rc.messaging_service_sid is not None:
            if rc.messaging_service_sid:
                metadata["messaging_service_sid"] = rc.messaging_service_sid
            else:
                metadata.pop("messaging_service_sid", None)
        provider.provider_metadata = metadata or None
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(provider, "provider_metadata")
        provider.is_verified = False  # Need to re-verify

    db.commit()
    db.refresh(provider)

    return _provider_to_response(provider)


@router.delete("/providers/{provider_id}")
async def delete_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Delete a messaging provider."""
    provider = db.query(MessagingProvider).filter(
        MessagingProvider.id == provider_id
    ).first()

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    db.delete(provider)
    db.commit()

    return {"message": "Provider deleted successfully"}


@router.post("/providers/{provider_id}/test", response_model=ProviderTestResponse)
async def test_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Test a messaging provider's connection."""
    provider = db.query(MessagingProvider).filter(
        MessagingProvider.id == provider_id
    ).first()

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    if provider.provider_type == "evolution_api":
        # For Evolution API, we'd check the instance status
        return ProviderTestResponse(
            success=True,
            message="Evolution API provider - use WhatsApp page to verify connection"
        )

    # For Twilio providers
    if not provider.credentials_encrypted:
        return ProviderTestResponse(
            success=False,
            message="No credentials configured"
        )

    try:
        credentials = twilio_service.decrypt_credentials(provider.credentials_encrypted)
        result = await twilio_service.test_connection(credentials)

        if result["success"]:
            provider.is_verified = True
            db.commit()

            return ProviderTestResponse(
                success=True,
                message="Connection successful",
                account_name=result.get("account_name"),
                account_status=result.get("account_status")
            )
        else:
            return ProviderTestResponse(
                success=False,
                message=result.get("error", "Connection failed")
            )

    except Exception as e:
        return ProviderTestResponse(
            success=False,
            message=f"Error testing connection: {str(e)}"
        )
