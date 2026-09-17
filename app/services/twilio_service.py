"""
Twilio Service

Handles SMS and WhatsApp messaging via Twilio API.
Supports sending messages, verifying webhooks, and parsing incoming messages.
"""

import logging
import hmac
import hashlib
import base64
from typing import Dict, Any, Optional
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet
import os
import json

logger = logging.getLogger(__name__)


class TwilioService:
    """Service for Twilio SMS and WhatsApp messaging."""

    def __init__(self):
        """Initialize the Twilio service."""
        self.base_url = "https://api.twilio.com/2010-04-01"

    def _get_encryption_key(self) -> bytes:
        """Get the Fernet encryption key from environment."""
        key = os.getenv("ENCRYPTION_KEY")
        if not key:
            raise ValueError("ENCRYPTION_KEY environment variable not set")
        return key.encode()

    def encrypt_credentials(self, credentials: Dict[str, str]) -> str:
        """
        Encrypt Twilio credentials for storage.

        Args:
            credentials: Dict with account_sid, auth_token, phone_number

        Returns:
            Encrypted credentials string
        """
        f = Fernet(self._get_encryption_key())
        return f.encrypt(json.dumps(credentials).encode()).decode()

    def decrypt_credentials(self, encrypted: str) -> Dict[str, str]:
        """
        Decrypt stored Twilio credentials.

        Args:
            encrypted: Encrypted credentials string

        Returns:
            Dict with account_sid, auth_token, phone_number
        """
        f = Fernet(self._get_encryption_key())
        return json.loads(f.decrypt(encrypted.encode()).decode())

    async def send_sms(
        self,
        to: str,
        body: str,
        credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Send an SMS message via Twilio.

        Args:
            to: Recipient phone number (E.164 format)
            body: Message content
            credentials: Decrypted Twilio credentials

        Returns:
            Dict with success status and message details
        """
        account_sid = credentials["account_sid"]
        auth_token = credentials["auth_token"]
        messaging_service_sid = credentials.get("messaging_service_sid")

        url = f"{self.base_url}/Accounts/{account_sid}/Messages.json"

        data: Dict[str, str] = {
            "To": to,
            "Body": body,
        }

        if messaging_service_sid:
            data["MessagingServiceSid"] = messaging_service_sid
        else:
            from_value = credentials.get("sender_id") or credentials.get("phone_number")
            if not from_value:
                return {
                    "success": False,
                    "error": "No sender ID, phone number, or Messaging Service SID configured"
                }
            data["From"] = from_value

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    data=data,
                    auth=(account_sid, auth_token),
                    timeout=30.0
                )

                if response.status_code in (200, 201):
                    result = response.json()
                    logger.info(f"SMS sent successfully to {to}, SID: {result.get('sid')}")
                    return {
                        "success": True,
                        "message_sid": result.get("sid"),
                        "status": result.get("status"),
                        "to": to,
                        "from": result.get("from", messaging_service_sid or data.get("From"))
                    }
                else:
                    error_data = response.json()
                    logger.error(f"Failed to send SMS: {error_data}")
                    return {
                        "success": False,
                        "error": error_data.get("message", "Unknown error"),
                        "error_code": error_data.get("code")
                    }

        except Exception as e:
            logger.error(f"Exception sending SMS: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def send_whatsapp(
        self,
        to: str,
        body: str,
        credentials: Dict[str, str],
        media_url: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Send a WhatsApp message via Twilio.

        Args:
            to: Recipient phone number (E.164 format, without whatsapp: prefix)
            body: Message content
            credentials: Decrypted Twilio credentials
            media_url: Optional media URL to send

        Returns:
            Dict with success status and message details
        """
        account_sid = credentials["account_sid"]
        auth_token = credentials["auth_token"]
        from_number = credentials["phone_number"]

        # Format numbers for WhatsApp
        whatsapp_to = f"whatsapp:{to}" if not to.startswith("whatsapp:") else to
        whatsapp_from = f"whatsapp:{from_number}" if not from_number.startswith("whatsapp:") else from_number

        url = f"{self.base_url}/Accounts/{account_sid}/Messages.json"

        data = {
            "To": whatsapp_to,
            "From": whatsapp_from,
            "Body": body
        }

        if media_url:
            data["MediaUrl"] = media_url

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    data=data,
                    auth=(account_sid, auth_token),
                    timeout=30.0
                )

                if response.status_code in (200, 201):
                    result = response.json()
                    logger.info(f"WhatsApp message sent to {to}, SID: {result.get('sid')}")
                    return {
                        "success": True,
                        "message_sid": result.get("sid"),
                        "status": result.get("status"),
                        "to": to,
                        "from": from_number
                    }
                else:
                    error_data = response.json()
                    logger.error(f"Failed to send WhatsApp message: {error_data}")
                    return {
                        "success": False,
                        "error": error_data.get("message", "Unknown error"),
                        "error_code": error_data.get("code")
                    }

        except Exception as e:
            logger.error(f"Exception sending WhatsApp message: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def verify_webhook_signature(
        self,
        url: str,
        form_data: Dict[str, str],
        signature: str,
        auth_token: str
    ) -> bool:
        """
        Verify Twilio webhook signature.

        Twilio signs webhooks using HMAC-SHA1 with the auth token as the key.
        The signature is computed over the full URL plus all POST parameters
        sorted alphabetically by key and concatenated.

        Args:
            url: Full webhook URL (including https://)
            form_data: Form data from the webhook
            signature: X-Twilio-Signature header value
            auth_token: Twilio auth token

        Returns:
            True if signature is valid
        """
        # Build the string to sign: URL + sorted params
        params_str = "".join(
            f"{k}{v}" for k, v in sorted(form_data.items())
        )
        data_to_sign = url + params_str

        # Compute HMAC-SHA1
        computed = hmac.new(
            auth_token.encode(),
            data_to_sign.encode(),
            hashlib.sha1
        ).digest()

        computed_signature = base64.b64encode(computed).decode()

        # Constant-time comparison
        return hmac.compare_digest(computed_signature, signature)

    def parse_incoming_message(self, form_data: Dict[str, str]) -> Dict[str, Any]:
        """
        Parse incoming Twilio webhook message.

        Twilio sends webhooks as form-encoded POST data.

        Args:
            form_data: Form data from the webhook

        Returns:
            Normalized message data
        """
        # Determine channel (SMS or WhatsApp)
        from_number = form_data.get("From", "")
        to_number = form_data.get("To", "")

        is_whatsapp = from_number.startswith("whatsapp:") or to_number.startswith("whatsapp:")

        # Clean up phone numbers
        clean_from = from_number.replace("whatsapp:", "")
        clean_to = to_number.replace("whatsapp:", "")

        # Extract media if present
        media = []
        num_media = int(form_data.get("NumMedia", 0))
        for i in range(num_media):
            media_url = form_data.get(f"MediaUrl{i}")
            media_type = form_data.get(f"MediaContentType{i}")
            if media_url:
                media.append({
                    "url": media_url,
                    "content_type": media_type
                })

        return {
            "message_sid": form_data.get("MessageSid"),
            "account_sid": form_data.get("AccountSid"),
            "from_number": clean_from,
            "to_number": clean_to,
            "body": form_data.get("Body", ""),
            "channel": "whatsapp" if is_whatsapp else "sms",
            "status": form_data.get("SmsStatus") or form_data.get("MessageStatus"),
            "media": media,
            "num_media": num_media,
            "num_segments": int(form_data.get("NumSegments", 1)),
            "api_version": form_data.get("ApiVersion"),
            # Location data (if shared)
            "latitude": form_data.get("Latitude"),
            "longitude": form_data.get("Longitude"),
            # Profile info (WhatsApp)
            "profile_name": form_data.get("ProfileName"),
            # Raw data for debugging
            "raw": form_data
        }

    def generate_twiml_response(
        self,
        message: Optional[str] = None,
        media_url: Optional[str] = None
    ) -> str:
        """
        Generate TwiML response for Twilio webhook.

        Args:
            message: Optional text message to send back
            media_url: Optional media URL to include

        Returns:
            TwiML XML string
        """
        if message is None and media_url is None:
            # Empty response - acknowledge receipt but don't reply
            return '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

        parts = ['<?xml version="1.0" encoding="UTF-8"?><Response><Message>']

        if message:
            # Escape XML special characters
            escaped_message = (
                message
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&apos;")
            )
            parts.append(f"<Body>{escaped_message}</Body>")

        if media_url:
            parts.append(f"<Media>{media_url}</Media>")

        parts.append("</Message></Response>")
        return "".join(parts)

    async def test_connection(self, credentials: Dict[str, str]) -> Dict[str, Any]:
        """
        Test Twilio credentials by fetching account info.

        Args:
            credentials: Twilio credentials to test

        Returns:
            Dict with success status and account info
        """
        account_sid = credentials["account_sid"]
        auth_token = credentials["auth_token"]

        url = f"{self.base_url}/Accounts/{account_sid}.json"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    auth=(account_sid, auth_token),
                    timeout=10.0
                )

                if response.status_code == 200:
                    result = response.json()
                    return {
                        "success": True,
                        "account_name": result.get("friendly_name"),
                        "account_status": result.get("status"),
                        "account_type": result.get("type")
                    }
                else:
                    error_data = response.json()
                    return {
                        "success": False,
                        "error": error_data.get("message", "Invalid credentials")
                    }

        except Exception as e:
            logger.error(f"Exception testing Twilio connection: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    async def get_phone_number_info(
        self,
        phone_number: str,
        credentials: Dict[str, str]
    ) -> Dict[str, Any]:
        """
        Get information about a Twilio phone number.

        Args:
            phone_number: Phone number to look up
            credentials: Twilio credentials

        Returns:
            Dict with phone number info
        """
        account_sid = credentials["account_sid"]
        auth_token = credentials["auth_token"]

        # URL encode the phone number
        encoded_number = phone_number.replace("+", "%2B")
        url = f"{self.base_url}/Accounts/{account_sid}/IncomingPhoneNumbers.json?PhoneNumber={encoded_number}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    auth=(account_sid, auth_token),
                    timeout=10.0
                )

                if response.status_code == 200:
                    result = response.json()
                    numbers = result.get("incoming_phone_numbers", [])
                    if numbers:
                        number_info = numbers[0]
                        return {
                            "success": True,
                            "phone_number": number_info.get("phone_number"),
                            "friendly_name": number_info.get("friendly_name"),
                            "sms_enabled": number_info.get("capabilities", {}).get("sms", False),
                            "voice_enabled": number_info.get("capabilities", {}).get("voice", False),
                            "mms_enabled": number_info.get("capabilities", {}).get("mms", False)
                        }
                    return {
                        "success": False,
                        "error": "Phone number not found in account"
                    }
                else:
                    error_data = response.json()
                    return {
                        "success": False,
                        "error": error_data.get("message", "Failed to fetch number info")
                    }

        except Exception as e:
            logger.error(f"Exception getting phone number info: {e}")
            return {
                "success": False,
                "error": str(e)
            }


# Singleton instance
twilio_service = TwilioService()
