import httpx
import asyncio
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
import os
from urllib.parse import urljoin
import secrets
import string

logger = logging.getLogger(__name__)

class EvolutionAPIService:
    def __init__(self):
        self.base_url = os.getenv("EVOLUTION_API_BASE_URL", "http://customer-evolution-api:8080")
        self.global_api_key = os.getenv("EVOLUTION_API_KEY")
        self.session = httpx.AsyncClient(timeout=30.0)
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.session.aclose()

    def _generate_instance_name(self, user_email: str, user_name: str) -> str:
        """Generate a unique instance name based on user info"""
        # Clean email and name for safe instance naming
        email_clean = user_email.replace("@", "_").replace(".", "_").lower()
        name_clean = "".join(c for c in user_name.lower() if c.isalnum() or c in "_-")
        
        # Generate a short random suffix for uniqueness
        suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(4))
        
        instance_name = f"wa_{name_clean[:10]}_{email_clean[:15]}_{suffix}"
        return instance_name

    def _generate_instance_token(self) -> str:
        """Generate a secure random token for the instance"""
        return secrets.token_urlsafe(32)

    async def create_instance(self, user_email: str, user_name: str) -> Dict[str, Any]:
        """Create a new WhatsApp instance in Evolution API"""
        try:
            instance_name = self._generate_instance_name(user_email, user_name)
            instance_token = self._generate_instance_token()
            display_name = f"{user_name} ({user_email})" if user_name != user_email else user_email
            
            # Create instance payload for Evolution API v2.x
            webhook_url = f"{os.getenv('BACKEND_BASE_URL', 'http://localhost:8001')}/api/v1/whatsapp/webhook/{instance_name}"
            payload = {
                "instanceName": instance_name,
                "displayName": display_name,
                "description": f"WhatsApp instance for {user_name}",
                "token": instance_token,
                "qrcode": True,
                "integration": "WHATSAPP-BAILEYS",
                # Evolution API v2.x webhook format
                # byEvents: false sends all events to the same URL (our endpoint expects this)
                "webhook": {
                    "enabled": True,
                    "url": webhook_url,
                    "byEvents": False,
                    "base64": False,
                    "events": [
                        "APPLICATION_STARTUP",
                        "QRCODE_UPDATED",
                        "CONNECTION_UPDATE",
                        "MESSAGES_UPSERT",
                        "MESSAGES_UPDATE",
                        "SEND_MESSAGE"
                    ]
                }
            }
            
            headers = {
                "Content-Type": "application/json",
                "apikey": self.global_api_key
            }
            
            url = urljoin(self.base_url, "/instance/create")
            logger.info(f"Creating Evolution API instance: {instance_name} at {url}")
            
            # Use a new session for this request to avoid the closed client issue
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                
                result = response.json()
                
                return {
                    "success": True,
                    "instance_name": instance_name,
                    "instance_token": instance_token,
                    "evolution_data": result,
                    "webhook_url": webhook_url
                }
            
        except httpx.ConnectError as e:
            logger.error(f"Connection error to Evolution API: {e}")
            return {
                "success": False,
                "error": f"Cannot connect to Evolution API service. Please ensure it's running and accessible."
            }
        except httpx.HTTPError as e:
            logger.error(f"HTTP error creating instance: {e}")
            return {
                "success": False,
                "error": f"Failed to create instance: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error creating instance: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    async def get_qr_code(self, instance_name: str, instance_token: Optional[str] = None) -> Dict[str, Any]:
        """Get QR code for WhatsApp connection"""
        try:
            # Use instance token if valid, fallback to global API key
            api_key = instance_token if instance_token and len(instance_token) > 20 else self.global_api_key
            headers = {
                "apikey": api_key
            }
            
            url = urljoin(self.base_url, f"/instance/connect/{instance_name}")
            logger.info(f"Getting QR code for instance: {instance_name}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                result = response.json()
                
                # The QR code should be in the response
                if "base64" in result:
                    return {
                        "success": True,
                        "qr_code": result["base64"],
                        "status": "qr_generated"
                    }
                else:
                    # Try to get instance info to check status
                    return await self.get_instance_status(instance_name, instance_token)
                
        except httpx.ConnectError as e:
            logger.error(f"Connection error to Evolution API: {e}")
            return {
                "success": False,
                "error": f"Cannot connect to Evolution API service. Please ensure it's running and accessible."
            }
        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting QR code: {e}")
            return {
                "success": False,
                "error": f"Failed to get QR code: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error getting QR code: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    async def get_instance_status(self, instance_name: str, instance_token: Optional[str] = None) -> Dict[str, Any]:
        """Get instance connection status"""
        try:
            # Use instance token if valid, fallback to global API key
            api_key = instance_token if instance_token and len(instance_token) > 20 else self.global_api_key
            headers = {
                "apikey": api_key
            }
            
            url = urljoin(self.base_url, f"/instance/connectionState/{instance_name}")
            logger.info(f"Getting status for instance: {instance_name}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                
                result = response.json()
            
            # Extract connection status from nested structure
            connection_status = "disconnected"  # default
            if "instance" in result and "state" in result["instance"]:
                connection_status = result["instance"]["state"]
            elif "state" in result:
                connection_status = result["state"]
            
            return {
                "success": True,
                "connection_status": connection_status,
                "instance_data": result
            }
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error getting instance status: {e}")
            return {
                "success": False,
                "error": f"Failed to get instance status: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error getting instance status: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    async def delete_instance(self, instance_name: str, instance_token: Optional[str] = None) -> Dict[str, Any]:
        """Delete instance from Evolution API"""
        try:
            # Use instance token if valid, fallback to global API key
            api_key = instance_token if instance_token and len(instance_token) > 20 else self.global_api_key
            headers = {
                "apikey": api_key
            }
            
            url = urljoin(self.base_url, f"/instance/delete/{instance_name}")
            logger.info(f"Deleting instance: {instance_name}")
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.delete(url, headers=headers)
                response.raise_for_status()
            
            return {
                "success": True,
                "message": "Instance deleted successfully"
            }
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error deleting instance: {e}")
            return {
                "success": False,
                "error": f"Failed to delete instance: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error deleting instance: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    async def send_message(
        self,
        instance_name: str,
        instance_token: Optional[str],
        to_number: str,
        message: str,
        quoted_message_key: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Send a message through WhatsApp

        Evolution API v2.3.7 with Baileys v7 supports sending to LID contacts directly.
        LID (Linked Identity Device) is WhatsApp's new addressing system for Android users.

        Args:
            instance_name: The WhatsApp instance name
            instance_token: Instance token for auth
            to_number: Recipient number/JID/LID - all formats supported in v2.3.7
            message: Text content to send
            quoted_message_key: Optional message key for quoted reply
        """
        try:
            # Use instance token if valid, fallback to global API key
            api_key = instance_token if instance_token and len(instance_token) > 20 else self.global_api_key
            headers = {
                "Content-Type": "application/json",
                "apikey": api_key
            }

            # Handle different number formats:
            # - Regular phone: clean and use as-is (should include country code)
            # - WhatsApp JID: extract number (e.g., 5511999999999@s.whatsapp.net)
            # - WhatsApp LID: use full LID (e.g., 102898843300066@lid) - supported in v2.3.7

            is_lid = "@lid" in to_number

            if "@s.whatsapp.net" in to_number:
                # JID format - extract phone number
                formatted_number = to_number.replace("@s.whatsapp.net", "")
            elif is_lid:
                # LID format - Evolution API v2.3.7 supports sending to LID directly
                # Per Baileys v7 docs: "You can message anyone using either their LID or their PN"
                formatted_number = to_number
                logger.info(f"Sending to LID contact (v2.3.7 supported): {to_number}")
            else:
                # Clean regular phone number
                formatted_number = to_number.lstrip('+').replace('-', '').replace(' ', '')
                if len(formatted_number) <= 11:
                    logger.warning(f"Phone number {to_number} may be missing country code")

            # Evolution API v2.x payload format (different from v1.x)
            payload = {
                "number": formatted_number,
                "text": message  # v2.x: text at root level, not nested under textMessage
            }

            # Add quoted message if provided (optional, can help maintain conversation context)
            if quoted_message_key:
                payload["quoted"] = {
                    "key": quoted_message_key
                }
                logger.info(f"Using quoted reply")

            url = urljoin(self.base_url, f"/message/sendText/{instance_name}")
            logger.info(f"Sending message via instance: {instance_name} to {formatted_number}")
            logger.info(f"Payload: {payload}")

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)

                # Log response for debugging
                if response.status_code != 200 and response.status_code != 201:
                    logger.error(f"Evolution API error response: {response.text}")

                response.raise_for_status()

                result = response.json()

            return {
                "success": True,
                "message_data": result
            }

        except httpx.HTTPError as e:
            logger.error(f"HTTP error sending message: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response body: {e.response.text}")
            return {
                "success": False,
                "error": f"Failed to send message: {str(e)}"
            }
        except Exception as e:
            logger.error(f"Unexpected error sending message: {e}")
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}"
            }

    async def send_media_message(
        self,
        instance_name: str,
        instance_token: Optional[str],
        to_number: str,
        media_url: str,
        media_type: str = "image",
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a media message through WhatsApp via Evolution API.

        Args:
            instance_name: The WhatsApp instance name
            instance_token: Instance token for auth
            to_number: Recipient number/JID/LID
            media_url: Public URL of the media file
            media_type: One of image, document, video, audio
            caption: Optional caption for the media
            filename: Optional filename for documents
        """
        try:
            api_key = instance_token if instance_token and len(instance_token) > 20 else self.global_api_key
            headers = {
                "Content-Type": "application/json",
                "apikey": api_key
            }

            # Handle different number formats (same as send_message)
            is_lid = "@lid" in to_number

            if "@s.whatsapp.net" in to_number:
                formatted_number = to_number.replace("@s.whatsapp.net", "")
            elif is_lid:
                formatted_number = to_number
            else:
                formatted_number = to_number.lstrip('+').replace('-', '').replace(' ', '')

            payload = {
                "number": formatted_number,
                "mediatype": media_type,
                "media": media_url,
            }
            if caption:
                payload["caption"] = caption
            if filename:
                payload["fileName"] = filename

            url = urljoin(self.base_url, f"/message/sendMedia/{instance_name}")
            logger.info(f"Sending media via instance: {instance_name} to {formatted_number}, type={media_type}")

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)

                if response.status_code not in (200, 201):
                    logger.error(f"Evolution API media error response: {response.text}")

                response.raise_for_status()
                result = response.json()

            return {"success": True, "message_data": result}

        except httpx.HTTPError as e:
            logger.error(f"HTTP error sending media: {e}")
            if hasattr(e, 'response') and e.response:
                logger.error(f"Response body: {e.response.text}")
            return {"success": False, "error": f"Failed to send media: {str(e)}"}
        except Exception as e:
            logger.error(f"Unexpected error sending media: {e}")
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

    async def check_whatsapp_numbers(
        self,
        instance_name: str,
        instance_token: Optional[str],
        numbers: list,
    ) -> Dict[str, Any]:
        """Check if phone numbers are registered on WhatsApp.

        Uses Evolution API's POST /chat/whatsAppNumbers/{instance} endpoint.
        Returns: {"success": True, "valid": [...], "invalid": [...]}
        """
        try:
            if not instance_token:
                return {
                    "success": False,
                    "error": "Evolution instance credential is unavailable",
                    "valid": [],
                    "invalid": [],
                }
            api_key = instance_token
            headers = {
                "Content-Type": "application/json",
                "apikey": api_key,
            }

            # Clean numbers — strip non-digits, remove leading +
            cleaned = []
            for n in numbers:
                clean = "".join(c for c in n if c.isdigit())
                if clean:
                    cleaned.append(clean)

            if not cleaned:
                return {"success": True, "valid": [], "invalid": []}

            url = urljoin(self.base_url, f"/chat/whatsAppNumbers/{instance_name}")
            payload = {"numbers": cleaned}

            logger.info("Checking WhatsApp numbers via instance %s: %d numbers", instance_name, len(cleaned))

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()

            # Evolution API returns array of objects: [{"exists": true, "jid": "...", "number": "..."}]
            valid = []
            invalid = []
            if isinstance(result, list):
                for item in result:
                    number = item.get("number") or item.get("jid", "").split("@")[0]
                    if item.get("exists"):
                        valid.append(number)
                    else:
                        invalid.append(number)
            elif isinstance(result, dict) and "participants" in result:
                # Alternative response format
                for item in result["participants"]:
                    number = item.get("number") or item.get("jid", "").split("@")[0]
                    if item.get("exists"):
                        valid.append(number)
                    else:
                        invalid.append(number)

            return {"success": True, "valid": valid, "invalid": invalid}

        except httpx.HTTPError as e:
            logger.error(f"HTTP error checking WhatsApp numbers: {e}")
            if hasattr(e, "response") and e.response:
                logger.error(f"Response body: {e.response.text}")
            return {"success": False, "error": f"Failed to check numbers: {str(e)}", "valid": [], "invalid": []}
        except Exception as e:
            logger.error(f"Unexpected error checking WhatsApp numbers: {e}")
            return {"success": False, "error": f"Unexpected error: {str(e)}", "valid": [], "invalid": []}


# Singleton instance
evolution_api_service = EvolutionAPIService()
