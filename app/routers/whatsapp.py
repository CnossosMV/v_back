from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from pydantic import BaseModel
from app.database import get_db
from app import models, schemas
from app.services.evolution_api_service import evolution_api_service
from app.services.whatsapp_sender import WhatsAppSender
from app.schemas.messaging import WhatsAppTestMessageRequest, WhatsAppTestMessageResponse, WhatsAppMessageStatusResponse
from app.dependencies import require_project_role
from cryptography.fernet import Fernet
import os
import base64
import re
from app.routers.auth import get_current_user
from typing import List, Optional
import logging
from datetime import datetime


class WhatsAppCheckNumbersRequest(BaseModel):
    numbers: List[str]
    instance_id: Optional[int] = None
    update_contacts: bool = True


class WhatsAppNumberResult(BaseModel):
    number: str
    status: str  # valid, invalid, unverified


class WhatsAppCheckNumbersResponse(BaseModel):
    results: List[WhatsAppNumberResult]
    provider_type: str
    instance_id: int

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------
def get_encryption_key():
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        print(f"Generated encryption key: {key}")
    return key.encode()


def encrypt_token(token: str) -> str:
    f = Fernet(get_encryption_key())
    return f.encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    f = Fernet(get_encryption_key())
    return f.decrypt(encrypted_token.encode()).decode()


async def get_instance_token(instance: models.WhatsAppInstance, db: Session) -> Optional[str]:
    """Get instance token from database with decryption"""
    try:
        if instance.instance_key:
            return decrypt_token(instance.instance_key)
        return None
    except Exception as e:
        logger.error(f"Error getting instance token: {e}")
        if instance.instance_key and len(instance.instance_key) > 32:
            logger.warning("Using direct token fallback due to error (DEMO ONLY)")
            return instance.instance_key
        return None


def extract_user_info(current_user):
    """Extract user info from either dict (demo) or User object"""
    if isinstance(current_user, dict):
        return {
            'user_id': int(current_user.get('id', 1)),
            'workspace_id': current_user.get('workspace', {}).get('id', 1),
            'user_email': current_user.get('email', 'demo@example.com'),
            'user_name': current_user.get('name', 'Demo User')
        }
    else:
        return {
            'user_id': current_user.id,
            'workspace_id': current_user.workspace_id,
            'user_email': current_user.email,
            'user_name': current_user.name
        }


def _get_owned_instance(db: Session, instance_id: int, project_id: int) -> models.WhatsAppInstance:
    """Fetch an instance and verify it belongs to the given project."""
    instance = db.query(models.WhatsAppInstance).filter(
        models.WhatsAppInstance.id == instance_id,
        models.WhatsAppInstance.provider_type == "evolution_api",
        models.WhatsAppInstance.is_active == True,
    ).first()
    if not instance:
        raise HTTPException(status_code=404, detail="WhatsApp instance not found")
    if instance.project_id != project_id:
        raise HTTPException(status_code=403, detail="Instance does not belong to this project")
    return instance


# Legacy helper — used by deprecated single-instance endpoints
def get_user_whatsapp_instance(db: Session, user_id: int, workspace_id: int) -> Optional[models.WhatsAppInstance]:
    """Get the user's WhatsApp instance (legacy single-instance)"""
    return db.query(models.WhatsAppInstance).filter(
        and_(
            models.WhatsAppInstance.user_id == user_id,
            models.WhatsAppInstance.workspace_id == workspace_id,
            models.WhatsAppInstance.provider_type == "evolution_api",
            models.WhatsAppInstance.is_active == True,
        )
    ).first()


async def generate_qr_code_background(instance_id: int, db: Session):
    """Background task to generate QR code"""
    try:
        instance = db.query(models.WhatsAppInstance).filter(
            models.WhatsAppInstance.id == instance_id
        ).first()
        if not instance:
            return

        instance_token = await get_instance_token(instance, db)

        import asyncio
        await asyncio.sleep(3)

        async with evolution_api_service as service:
            qr_result = await service.get_qr_code(instance.instance_name, instance_token)

        if qr_result["success"] and "qr_code" in qr_result:
            instance.qr_code_data = qr_result["qr_code"]
            instance.updated_at = datetime.utcnow()
            db.commit()

    except Exception as e:
        logger.error(f"Error in background QR code generation: {e}")


async def _disconnect_instance(instance: models.WhatsAppInstance, db: Session) -> dict:
    """Shared disconnect logic for both new and legacy endpoints."""
    try:
        instance_token = await get_instance_token(instance, db)
        async with evolution_api_service as service:
            await service.delete_instance(instance.instance_name, instance_token)
    except Exception as e:
        logger.error(f"Error deleting from Evolution API: {e}")

    try:
        from app.services.handler_channel_link_service import HandlerChannelLinkService
        link_svc = HandlerChannelLinkService(db)
        links_deleted = link_svc.remove_by_instance("whatsapp", instance.id)
        if links_deleted > 0:
            logger.info(f"Removed {links_deleted} handler_channel_link(s) for WhatsApp instance {instance.instance_name}")

        from app.models import Chatbot
        chatbots_updated = db.query(Chatbot).filter(
            Chatbot.whatsapp_instance_id == instance.id
        ).update({
            Chatbot.whatsapp_instance_id: None,
            Chatbot.auto_respond_whatsapp: False
        }, synchronize_session=False)

        if chatbots_updated > 0:
            logger.info(f"Unlinked {chatbots_updated} chatbot(s) from WhatsApp instance {instance.instance_name}")

        deleted_messages = db.query(models.WhatsAppMessage).filter(
            models.WhatsAppMessage.instance_id == instance.id
        ).delete(synchronize_session=False)

        if deleted_messages > 0:
            logger.info(f"Deleted {deleted_messages} WhatsApp message(s) for instance {instance.instance_name}")

        db.delete(instance)
        db.commit()

        return {
            "message": "WhatsApp instance disconnected and deleted successfully",
            "chatbots_unlinked": chatbots_updated,
            "messages_deleted": deleted_messages,
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Error cleaning up WhatsApp instance: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to disconnect WhatsApp instance: {str(e)}",
        )


# ============================================================================
# NEW multi-instance, project-scoped endpoints
# ============================================================================

@router.get("/project/{project_id}/instances", response_model=List[schemas.WhatsAppInstanceListItem])
async def list_project_instances(
    project_id: int,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("viewer")),
):
    """List all Evolution API WhatsApp instances for a project."""
    instances = (
        db.query(models.WhatsAppInstance)
        .filter(
            models.WhatsAppInstance.project_id == project_id,
            models.WhatsAppInstance.provider_type == "evolution_api",
            models.WhatsAppInstance.is_active == True,
        )
        .order_by(models.WhatsAppInstance.created_at.desc())
        .all()
    )
    return instances


@router.post("/project/{project_id}/instances", response_model=schemas.WhatsAppConnectionResponse)
async def create_project_instance(
    project_id: int,
    body: schemas.WhatsAppInstanceCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("editor")),
    current_user=Depends(get_current_user),
):
    """Create a new Evolution API WhatsApp instance for a project."""
    user_info = extract_user_info(current_user)

    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        async with evolution_api_service as service:
            instance_result = await service.create_instance(
                user_info["user_email"], user_info["user_name"]
            )

        if not instance_result["success"]:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create WhatsApp instance: {instance_result.get('error', 'Unknown error')}",
            )

        encrypted_token = encrypt_token(instance_result["instance_token"])

        new_instance = models.WhatsAppInstance(
            user_id=user_info["user_id"],
            workspace_id=project.workspace_id,
            project_id=project_id,
            instance_name=instance_result["instance_name"],
            instance_key=encrypted_token,
            connection_status="disconnected",
            webhook_url=instance_result["webhook_url"],
            is_active=True,
        )

        db.add(new_instance)
        db.commit()
        db.refresh(new_instance)

        background_tasks.add_task(generate_qr_code_background, new_instance.id, db)

        return schemas.WhatsAppConnectionResponse(
            success=True,
            message="WhatsApp instance created! QR code will be available shortly.",
            qr_code=None,
            instance_name=new_instance.instance_name,
            connection_status=new_instance.connection_status,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating WhatsApp instance: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create WhatsApp instance",
        )


@router.get("/instances/{instance_id}/status")
async def get_instance_status(
    instance_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("viewer")),
):
    """Get status for a specific WhatsApp Evolution instance."""
    instance = _get_owned_instance(db, instance_id, project_id)

    try:
        instance_token = await get_instance_token(instance, db)
        status_result = await evolution_api_service.get_instance_status(
            instance.instance_name, instance_token
        )

        if status_result["success"]:
            new_status = status_result["connection_status"]
            if new_status != instance.connection_status:
                instance.connection_status = new_status
                if new_status == "open":
                    instance.last_connected_at = datetime.utcnow()
                    instance.qr_code_data = None
                    instance_data = status_result.get("instance_data", {})
                    if instance_data and "id" in instance_data:
                        instance.phone_number = instance_data["id"].replace("@s.whatsapp.net", "")
                instance.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(instance)

    except Exception as e:
        logger.error(f"Error getting fresh status from Evolution API: {e}")

    return {
        "id": instance.id,
        "instance_name": instance.instance_name,
        "connection_status": instance.connection_status,
        "phone_number": instance.phone_number,
        "last_connected_at": instance.last_connected_at,
        "is_active": instance.is_active,
        "exists": True,
        "provider_type": instance.provider_type,
    }


@router.get("/instances/{instance_id}/qr-code")
async def get_instance_qr_code(
    instance_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("editor")),
):
    """Get QR code for a specific WhatsApp Evolution instance."""
    instance = _get_owned_instance(db, instance_id, project_id)

    if instance.connection_status == "open":
        return {"message": "WhatsApp is already connected", "qr_code": None}

    if instance.qr_code_data:
        return {"qr_code": instance.qr_code_data, "message": "Scan this QR code with WhatsApp"}

    try:
        instance_token = await get_instance_token(instance, db)
        async with evolution_api_service as service:
            qr_result = await service.get_qr_code(instance.instance_name, instance_token)

        if qr_result.get("success") and qr_result.get("connection_status") in ["open", "connecting"]:
            instance.qr_code_data = None
            instance.connection_status = "open"
            instance.updated_at = datetime.utcnow()
            db.commit()
            return {"message": "WhatsApp is already connected", "qr_code": None}

        if qr_result["success"] and "qr_code" in qr_result:
            instance.qr_code_data = qr_result["qr_code"]
            instance.updated_at = datetime.utcnow()
            db.commit()
            return {"qr_code": qr_result["qr_code"], "message": "Scan this QR code with WhatsApp"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to generate QR code: {qr_result.get('error', 'Unknown error')}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating QR code: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate QR code",
        )


@router.post("/instances/{instance_id}/send-test", response_model=WhatsAppTestMessageResponse)
async def send_instance_test_message(
    instance_id: int,
    request: WhatsAppTestMessageRequest,
    project_id: int,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("editor")),
):
    """Send a test message via a specific WhatsApp Evolution instance."""
    instance = _get_owned_instance(db, instance_id, project_id)

    if instance.connection_status not in ["open", "connecting"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp is not connected. Please scan the QR code first.",
        )

    phone_number = re.sub(r'[^\d]', '', request.to_number)
    if len(phone_number) < 10 or len(phone_number) > 15:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone number format. Use E.164 format (e.g., 5511999999999)",
        )

    try:
        sender = WhatsAppSender(db)
        result = await sender.send_message(
            instance=instance,
            to_number=phone_number,
            message=request.message,
        )

        if not result["success"]:
            return WhatsAppTestMessageResponse(
                success=False, message_id=None, status="failed",
                to_number=phone_number, error=result.get("error", "Unknown error"),
            )

        message_data = result.get("message_data", {})
        message_key = message_data.get("key", {})
        message_id = message_key.get("id", "") or result.get("message_id", "")

        whatsapp_message = models.WhatsAppMessage(
            instance_id=instance.id,
            message_id=message_id,
            from_number=instance.phone_number or "",
            to_number=phone_number,
            message_type="text",
            content=request.message,
            status="sent",
            direction="outbound",
            sent_at=datetime.utcnow(),
        )
        db.add(whatsapp_message)

        from app.services.channels.send_log_helper import record_direct_send
        record_direct_send(
            db=db, project_id=project_id, channel="whatsapp",
            recipient=phone_number, content_summary=request.message[:500] if request.message else "",
            source_type="whatsapp_test", instance_id=instance.id,
            status="sent", provider_message_id=message_id,
        )

        db.commit()

        return WhatsAppTestMessageResponse(
            success=True, message_id=message_id, status="sent",
            to_number=phone_number, error=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test message: {str(e)}",
        )


@router.delete("/instances/{instance_id}")
async def delete_instance(
    instance_id: int,
    project_id: int,
    db: Session = Depends(get_db),
    _auth: dict = Depends(require_project_role("editor")),
):
    """Disconnect and delete a specific WhatsApp Evolution instance."""
    instance = _get_owned_instance(db, instance_id, project_id)
    return await _disconnect_instance(instance, db)


# ============================================================================
# DEPRECATED legacy single-instance endpoints (backward compatibility)
# ============================================================================

@router.post("/connect", response_model=schemas.WhatsAppConnectionResponse, deprecated=True)
async def connect_whatsapp(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Connect WhatsApp for the current user (single-instance)."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']
    user_email = user_info['user_email']
    user_name = user_info['user_name']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    existing_instance = get_user_whatsapp_instance(db, user_id, workspace_id)

    if existing_instance:
        if existing_instance.connection_status in ["open", "connecting", "connected"]:
            return schemas.WhatsAppConnectionResponse(
                success=True,
                message="WhatsApp is already connected!",
                qr_code=None,
                instance_name=existing_instance.instance_name,
                connection_status=existing_instance.connection_status,
            )
        else:
            try:
                instance_token = await get_instance_token(existing_instance, db)
                async with evolution_api_service as service:
                    qr_result = await service.get_qr_code(existing_instance.instance_name, instance_token)

                if qr_result["success"] and "qr_code" in qr_result:
                    existing_instance.qr_code_data = qr_result["qr_code"]
                    existing_instance.updated_at = datetime.utcnow()
                    db.commit()
                    db.refresh(existing_instance)

                    return schemas.WhatsAppConnectionResponse(
                        success=True,
                        message="QR code generated! Scan with your WhatsApp mobile app.",
                        qr_code=qr_result["qr_code"],
                        instance_name=existing_instance.instance_name,
                        connection_status=existing_instance.connection_status,
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail=f"Failed to generate QR code: {qr_result.get('error', 'Unknown error')}",
                    )

            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"Error getting QR code for existing instance: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to generate QR code",
                )

    try:
        async with evolution_api_service as service:
            instance_result = await service.create_instance(user_email, user_name)

        if not instance_result["success"]:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create WhatsApp instance: {instance_result.get('error', 'Unknown error')}",
            )

        encrypted_token = encrypt_token(instance_result['instance_token'])

        new_instance = models.WhatsAppInstance(
            user_id=user_id,
            workspace_id=workspace_id,
            instance_name=instance_result['instance_name'],
            instance_key=encrypted_token,
            connection_status="disconnected",
            webhook_url=instance_result['webhook_url'],
            is_active=True,
        )

        db.add(new_instance)
        db.commit()
        db.refresh(new_instance)

        background_tasks.add_task(generate_qr_code_background, new_instance.id, db)

        return schemas.WhatsAppConnectionResponse(
            success=True,
            message="WhatsApp instance created! QR code will be available shortly.",
            qr_code=None,
            instance_name=new_instance.instance_name,
            connection_status=new_instance.connection_status,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating WhatsApp instance: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create WhatsApp instance",
        )


@router.get("/status", deprecated=True)
async def get_whatsapp_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Get WhatsApp connection status for current user (single-instance)."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)

    if not instance:
        return {
            "instance_name": None,
            "connection_status": "disconnected",
            "phone_number": None,
            "last_connected_at": None,
            "is_active": False,
            "exists": False,
            "provider_type": None,
        }

    try:
        instance_token = await get_instance_token(instance, db)
        status_result = await evolution_api_service.get_instance_status(instance.instance_name, instance_token)

        if status_result["success"]:
            new_status = status_result["connection_status"]
            if new_status != instance.connection_status:
                logger.info(f"Status changed from {instance.connection_status} to {new_status}")
                instance.connection_status = new_status
                if new_status == "open":
                    instance.last_connected_at = datetime.utcnow()
                    instance.qr_code_data = None
                    instance_data = status_result.get("instance_data", {})
                    if instance_data and "id" in instance_data:
                        instance.phone_number = instance_data["id"].replace("@s.whatsapp.net", "")
                instance.updated_at = datetime.utcnow()
                db.commit()
                db.refresh(instance)

    except Exception as e:
        logger.error(f"Error getting fresh status from Evolution API: {e}")

    return {
        "instance_name": instance.instance_name,
        "connection_status": instance.connection_status,
        "phone_number": instance.phone_number,
        "last_connected_at": instance.last_connected_at,
        "is_active": instance.is_active,
        "exists": True,
        "provider_type": instance.provider_type,
    }


@router.get("/instance", response_model=schemas.WhatsAppInstance, deprecated=True)
async def get_whatsapp_instance(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Get WhatsApp instance details for current user."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found")

    return schemas.WhatsAppInstance.from_orm(instance)


@router.get("/qr-code", deprecated=True)
async def get_qr_code(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Get QR code for WhatsApp connection (single-instance)."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found. Please connect first.")

    if instance.connection_status == "open":
        return {"message": "WhatsApp is already connected", "qr_code": None}

    if instance.qr_code_data:
        return {"qr_code": instance.qr_code_data, "message": "Scan this QR code with WhatsApp"}

    try:
        instance_token = await get_instance_token(instance, db)
        async with evolution_api_service as service:
            qr_result = await service.get_qr_code(instance.instance_name, instance_token)

        if qr_result.get("success") and qr_result.get("connection_status") in ["open", "connecting"]:
            instance.qr_code_data = None
            instance.connection_status = "open"
            instance.updated_at = datetime.utcnow()
            db.commit()
            return {"message": "WhatsApp is already connected", "qr_code": None}

        if qr_result["success"] and "qr_code" in qr_result:
            instance.qr_code_data = qr_result["qr_code"]
            instance.updated_at = datetime.utcnow()
            db.commit()
            return {"qr_code": qr_result["qr_code"], "message": "Scan this QR code with WhatsApp"}
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to generate QR code: {qr_result.get('error', 'Unknown error')}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating QR code: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to generate QR code",
        )


@router.delete("/disconnect", deprecated=True)
async def disconnect_whatsapp(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Disconnect and delete WhatsApp instance (single-instance)."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found")

    return await _disconnect_instance(instance, db)


@router.post("/send-qr-email", deprecated=True)
async def send_qr_code_email(
    email_data: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Send WhatsApp QR code to email (single-instance)."""
    from app.services.email_service import email_service

    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']
    user_name = user_info['user_name']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found. Please connect first.")

    if not instance.qr_code_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No QR code available. Please generate QR code first.",
        )

    to_email = email_data.get("email")
    if not to_email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email address is required")

    try:
        result = email_service.send_whatsapp_qr_code(
            to_email=to_email,
            qr_code_base64=instance.qr_code_data,
            user_name=user_name,
        )
        if result["success"]:
            return {"message": result["message"], "success": True}
        else:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result["message"])
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending QR code email: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to send email")


@router.post("/send-test", response_model=WhatsAppTestMessageResponse, deprecated=True)
async def send_test_message(
    request: WhatsAppTestMessageRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Send a test WhatsApp message (single-instance)."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found. Please connect first.")

    if instance.connection_status not in ["open", "connecting"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="WhatsApp is not connected. Please scan the QR code first.",
        )

    phone_number = re.sub(r'[^\d]', '', request.to_number)
    if len(phone_number) < 10 or len(phone_number) > 15:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid phone number format. Use E.164 format (e.g., 5511999999999)",
        )

    try:
        sender = WhatsAppSender(db)
        result = await sender.send_message(instance=instance, to_number=phone_number, message=request.message)

        if not result["success"]:
            return WhatsAppTestMessageResponse(
                success=False, message_id=None, status="failed",
                to_number=phone_number, error=result.get("error", "Unknown error"),
            )

        message_data = result.get("message_data", {})
        message_key = message_data.get("key", {})
        message_id = message_key.get("id", "") or result.get("message_id", "")

        whatsapp_message = models.WhatsAppMessage(
            instance_id=instance.id,
            message_id=message_id,
            from_number=instance.phone_number or "",
            to_number=phone_number,
            message_type="text",
            content=request.message,
            status="sent",
            direction="outbound",
            sent_at=datetime.utcnow(),
        )
        db.add(whatsapp_message)

        from app.services.channels.send_log_helper import record_direct_send
        record_direct_send(
            db=db, project_id=None, channel="whatsapp",
            recipient=phone_number, content_summary=request.message[:500] if request.message else "",
            source_type="whatsapp_test", instance_id=instance.id,
            status="sent", provider_message_id=message_id,
        )

        db.commit()

        return WhatsAppTestMessageResponse(
            success=True, message_id=message_id, status="sent",
            to_number=phone_number, error=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error sending test message: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send test message: {str(e)}",
        )


@router.get("/message/{message_id}/status", response_model=WhatsAppMessageStatusResponse, deprecated=True)
async def get_message_status(
    message_id: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """[DEPRECATED] Get the delivery status of a WhatsApp message."""
    user_info = extract_user_info(current_user)
    user_id = user_info['user_id']
    workspace_id = user_info['workspace_id']

    if not workspace_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User must be associated with a workspace",
        )

    instance = get_user_whatsapp_instance(db, user_id, workspace_id)
    if not instance:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WhatsApp instance not found")

    message = db.query(models.WhatsAppMessage).filter(
        and_(
            models.WhatsAppMessage.instance_id == instance.id,
            models.WhatsAppMessage.message_id == message_id,
        )
    ).first()

    if not message:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")

    return WhatsAppMessageStatusResponse(
        message_id=message.message_id,
        status=message.status,
        to_number=message.to_number,
        updated_at=message.created_at,
    )


# ============================================================================
# Webhook endpoint (unchanged — Evolution API calls by instance_name)
# ============================================================================

@router.post("/webhook/{instance_name}")
async def whatsapp_webhook(
    instance_name: str,
    webhook_data: dict,
    db: Session = Depends(get_db),
):
    """Handle webhook callbacks from Evolution API"""
    try:
        # Evolution payloads may contain instance API keys, access tokens and
        # full QR images.  Log only routing metadata; the payload itself must
        # never be copied to application logs.
        logger.info(
            "Received WhatsApp webhook instance=%s event=%s",
            instance_name,
            webhook_data.get("event", "unknown"),
        )

        instance = db.query(models.WhatsAppInstance).filter(
            models.WhatsAppInstance.instance_name == instance_name
        ).first()

        if not instance:
            logger.warning(f"Webhook received for unknown instance: {instance_name}")
            return {"status": "ignored", "reason": "unknown_instance"}

        event_type = webhook_data.get("event", "").upper().replace(".", "_")
        logger.info(f"Normalized event type: {event_type}")

        if event_type == "CONNECTION_UPDATE":
            connection_data = webhook_data.get("data", {})
            new_status = connection_data.get("state", "disconnected")

            if new_status == "open":
                instance.connection_status = new_status
            elif new_status in ["close", "disconnected"]:
                instance.connection_status = new_status
            elif instance.connection_status != "open":
                instance.connection_status = new_status

            if new_status == "open":
                instance.last_connected_at = datetime.utcnow()
                instance.qr_code_data = None

                phone_info = connection_data.get("instance", {})
                if phone_info:
                    instance.phone_number = phone_info.get("wid", {}).get("user")

            instance.updated_at = datetime.utcnow()
            db.commit()

        elif event_type == "QRCODE_UPDATED":
            qr_data = webhook_data.get("data", {})
            qr_code_value = qr_data.get("qrcode")
            if qr_code_value:
                if isinstance(qr_code_value, dict):
                    qr_base64 = qr_code_value.get("base64", "")
                else:
                    qr_base64 = qr_code_value
                instance.qr_code_data = qr_base64
                instance.updated_at = datetime.utcnow()
                db.commit()
                logger.info(f"Updated QR code for instance {instance_name}")

        elif event_type == "MESSAGES_UPSERT":
            from app.services.inbound.adapters import normalize_evolution_api
            from app.services.inbound.router import InboundRouter

            message_data = webhook_data.get("data", {})
            key = message_data.get("key", {})
            if key.get("fromMe", False):
                logger.info(f"Skipping outgoing message for instance {instance_name}")
                return {"status": "processed", "event": event_type, "direction": "outbound"}

            inbound_msg = normalize_evolution_api(
                webhook_data, instance_name, instance_id=instance.id,
            )
            if inbound_msg:
                logger.info(
                    f"Incoming WhatsApp: remoteJid={inbound_msg.remote_jid}, "
                    f"phone={inbound_msg.customer_phone}, pushName={inbound_msg.push_name}"
                )
                inbound_router = InboundRouter(db)
                result = await inbound_router.route(inbound_msg)
                logger.info(f"Processed WhatsApp message for instance {instance_name}: {result}")
            else:
                logger.warning(f"Could not extract content from message: {message_data.get('message', {})}")

        elif event_type == "MESSAGES_UPDATE":
            update_data = webhook_data.get("data", {})
            updates = update_data if isinstance(update_data, list) else [update_data]

            for update in updates:
                message_key = update.get("key", {})
                message_id = message_key.get("id")
                if not message_id:
                    continue

                update_status = update.get("status")
                status_map = {
                    "PENDING": "sent", "SENT": "sent", "SERVER_ACK": "sent",
                    "DELIVERY_ACK": "delivered", "READ": "read", "PLAYED": "read",
                    "ERROR": "failed", "FAILED": "failed",
                }
                new_status = status_map.get(update_status, update_status)

                if new_status:
                    whatsapp_message = db.query(models.WhatsAppMessage).filter(
                        and_(
                            models.WhatsAppMessage.instance_id == instance.id,
                            models.WhatsAppMessage.message_id == message_id,
                        )
                    ).first()

                    if whatsapp_message:
                        status_order = {"sent": 1, "delivered": 2, "read": 3, "failed": 0}
                        current_order = status_order.get(whatsapp_message.status, 0)
                        new_order = status_order.get(new_status, 0)

                        if new_order > current_order or new_status == "failed":
                            whatsapp_message.status = new_status
                            db.commit()
                            logger.info(f"Updated message {message_id} status to {new_status}")

                    try:
                        from app.services.channels.delivery_tracker import DeliveryTracker
                        DeliveryTracker(db).record_status(
                            provider_message_id=message_id,
                            status=new_status,
                            provider_status=update_status,
                        )
                        db.commit()
                    except Exception as dt_err:
                        logger.debug(f"DeliveryTracker update skipped: {dt_err}")

        return {"status": "processed", "event": event_type}

    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# WhatsApp Number Validation
# ---------------------------------------------------------------------------

@router.post(
    "/project/{project_id}/check-numbers",
    response_model=WhatsAppCheckNumbersResponse,
)
async def check_whatsapp_numbers(
    project_id: int,
    payload: WhatsAppCheckNumbersRequest,
    db: Session = Depends(get_db),
    user_info: dict = Depends(get_current_user),
):
    """Check if phone numbers are registered on WhatsApp.

    For Evolution API instances: real-time lookup via Baileys.
    For Meta Cloud API instances: returns 'unverified' (no lookup API).
    """
    if not payload.numbers or len(payload.numbers) > 50:
        raise HTTPException(status_code=400, detail="Provide 1-50 numbers")

    # Resolve instance
    instance = None
    if payload.instance_id:
        instance = db.query(models.WhatsAppInstance).filter(
            models.WhatsAppInstance.id == payload.instance_id,
            models.WhatsAppInstance.is_active == True,
        ).first()
        if not instance:
            raise HTTPException(status_code=404, detail="Instance not found")
    else:
        # Find first active instance for the project's workspace
        project = db.query(models.Project).filter(models.Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        instance = db.query(models.WhatsAppInstance).filter(
            models.WhatsAppInstance.workspace_id == project.workspace_id,
            models.WhatsAppInstance.is_active == True,
        ).first()
        if not instance:
            raise HTTPException(status_code=404, detail="No active WhatsApp instance found")

    results: List[WhatsAppNumberResult] = []

    if instance.provider_type == "evolution_api":
        # Real-time check via Evolution API
        token = await get_instance_token(instance, db)
        check_result = await evolution_api_service.check_whatsapp_numbers(
            instance_name=instance.instance_name,
            instance_token=token,
            numbers=payload.numbers,
        )

        if not check_result.get("success"):
            raise HTTPException(
                status_code=502,
                detail=check_result.get("error", "Failed to check numbers"),
            )

        valid_set = set(check_result.get("valid", []))
        invalid_set = set(check_result.get("invalid", []))

        # Build results — map original numbers to validation status
        for number in payload.numbers:
            clean = "".join(c for c in number if c.isdigit())
            if clean in valid_set:
                results.append(WhatsAppNumberResult(number=number, status="valid"))
            elif clean in invalid_set:
                results.append(WhatsAppNumberResult(number=number, status="invalid"))
            else:
                results.append(WhatsAppNumberResult(number=number, status="unverified"))

    else:
        # Meta Cloud API — no number lookup available
        results = [
            WhatsAppNumberResult(number=n, status="unverified")
            for n in payload.numbers
        ]

    # Optionally update MessagingUser.whatsapp_status for matching contacts
    if payload.update_contacts:
        from app.models.messaging import MessagingUser
        for r in results:
            if r.status in ("valid", "invalid"):
                clean_phone = "".join(c for c in r.number if c.isdigit())
                users = db.query(MessagingUser).filter(
                    MessagingUser.project_id == project_id,
                    or_(
                        MessagingUser.phone == r.number,
                        MessagingUser.phone_e164 == clean_phone,
                    ),
                ).all()
                for u in users:
                    u.whatsapp_status = r.status
                    u.whatsapp_checked_at = datetime.utcnow()
        db.commit()

    return WhatsAppCheckNumbersResponse(
        results=results,
        provider_type=instance.provider_type,
        instance_id=instance.id,
    )
