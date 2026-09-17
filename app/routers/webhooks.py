"""
Webhooks Router

Handles incoming webhooks from messaging providers (Twilio SMS/WhatsApp).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Header
from sqlalchemy.orm import Session
import logging
from typing import Optional

from app.database import get_db
from app.models import MessagingProvider
from app.services.twilio_service import twilio_service
from app.services.unified_chat_service import get_unified_chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/twilio/{provider_id}")
async def twilio_incoming_webhook(
    provider_id: int,
    request: Request,
    x_twilio_signature: Optional[str] = Header(None, alias="X-Twilio-Signature"),
    db: Session = Depends(get_db)
):
    """
    Handle incoming messages from Twilio (SMS and WhatsApp).

    Twilio sends webhooks as form-encoded POST data.
    Returns TwiML response.
    """
    try:
        # Get form data
        form_data = await request.form()
        form_dict = dict(form_data)

        logger.info(f"Received Twilio webhook for provider {provider_id}")

        # Find the provider
        provider = db.query(MessagingProvider).filter(
            MessagingProvider.id == provider_id,
            MessagingProvider.is_active == True
        ).first()

        if not provider:
            logger.warning(f"Provider {provider_id} not found or inactive")
            return Response(
                content=twilio_service.generate_twiml_response(),
                media_type="application/xml"
            )

        # Verify webhook signature (if signature header present)
        if x_twilio_signature:
            try:
                credentials = twilio_service.decrypt_credentials(provider.credentials_encrypted)
                auth_token = credentials.get("auth_token")

                # Build full URL
                url = str(request.url)

                if not twilio_service.verify_webhook_signature(
                    url=url,
                    form_data=form_dict,
                    signature=x_twilio_signature,
                    auth_token=auth_token
                ):
                    logger.warning(f"Invalid Twilio signature for provider {provider_id}")
                    raise HTTPException(status_code=403, detail="Invalid signature")
            except Exception as e:
                logger.error(f"Error verifying Twilio signature: {e}")
                # Continue processing even if signature verification fails in development
                # In production, you might want to reject the request

        # Parse the incoming message and route via InboundRouter
        message_data = twilio_service.parse_incoming_message(form_dict)

        from app.services.inbound.adapters import normalize_twilio
        from app.services.inbound.router import InboundRouter

        inbound_msg = normalize_twilio(message_data, provider.provider_type, provider_id=provider.id)
        inbound_msg.project_id = provider.project_id
        router = InboundRouter(db)
        result = await router.route(inbound_msg)

        # Generate TwiML response
        # If bot responded, we don't need to send a TwiML reply
        # (response was sent via API)
        # Return empty TwiML to acknowledge receipt
        return Response(
            content=twilio_service.generate_twiml_response(),
            media_type="application/xml"
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error processing Twilio webhook: {e}", exc_info=True)
        return Response(
            content=twilio_service.generate_twiml_response(),
            media_type="application/xml"
        )


@router.post("/twilio/{provider_id}/status")
async def twilio_status_webhook(
    provider_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Handle delivery status updates from Twilio.
    """
    try:
        form_data = await request.form()
        form_dict = dict(form_data)

        logger.info(f"Received Twilio status webhook for provider {provider_id}")

        # Extract status info
        message_sid = form_dict.get("MessageSid")
        status = form_dict.get("MessageStatus") or form_dict.get("SmsStatus")

        if message_sid and status:
            # Map Twilio status to our status
            status_map = {
                "queued": "pending",
                "sending": "pending",
                "sent": "sent",
                "delivered": "delivered",
                "read": "read",
                "failed": "failed",
                "undelivered": "failed",
            }
            mapped_status = status_map.get(status, status)

            # Update legacy ChatMessage
            from app.models import ChatMessage
            message = db.query(ChatMessage).filter(
                ChatMessage.external_message_id == message_sid
            ).first()
            if message:
                message.delivery_status = mapped_status

            # Also update SendLog via DeliveryTracker (for Message Tracker + feedback)
            if mapped_status in ("sent", "delivered", "read", "failed"):
                from app.services.channels.delivery_tracker import DeliveryTracker
                error_code = form_dict.get("ErrorCode")
                error_msg = form_dict.get("ErrorMessage")
                DeliveryTracker(db).record_status(
                    provider_message_id=message_sid,
                    status=mapped_status,
                    provider_status=status,
                    error_code=error_code,
                    error_message=error_msg,
                    raw_payload=form_dict,
                )

            db.commit()

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing Twilio status webhook: {e}")
        return {"status": "error", "error": str(e)}
