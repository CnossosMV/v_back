"""
Webhook Dispatcher Service for Messaging Middleware
Handles sending messages to configured webhooks (Twilio, SendGrid, Evolution API, etc.)
"""
import httpx
import base64
import logging
import os
from typing import Dict, Any, Optional
from datetime import datetime
from cryptography.fernet import Fernet

from app.models.messaging import MessagingChannel, MessagingLog, AuthType, MessageStatus

logger = logging.getLogger(__name__)

# Get encryption key from environment
ENCRYPTION_KEY = os.getenv('ENCRYPTION_KEY', Fernet.generate_key().decode())


class WebhookDispatcher:
    """
    Dispatches messages to configured webhooks with various authentication methods.

    Supports:
    - No authentication
    - Bearer token
    - Basic auth (username:password)
    - API key (header-based)
    - Custom headers
    """

    def __init__(self):
        self.fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)
        self.default_timeout = 30

    async def dispatch(
        self,
        channel: MessagingChannel,
        payload: Dict[str, Any],
        log: Optional[MessagingLog] = None
    ) -> Dict[str, Any]:
        """
        Send a message to the channel's webhook.

        Args:
            channel: The messaging channel with webhook configuration
            payload: The message payload to send
            log: Optional log entry to update with results

        Returns:
            Dict[str, Any]: Result with success status, response, and timing
        """
        start_time = datetime.utcnow()
        result = {
            'success': False,
            'status_code': None,
            'response': None,
            'error': None,
            'response_time_ms': None
        }

        try:
            # Build headers
            headers = self._build_headers(channel)

            # Create async client
            async with httpx.AsyncClient(timeout=channel.timeout_seconds) as client:
                response = await client.post(
                    channel.webhook_url,
                    json=payload,
                    headers=headers
                )

                # Calculate response time
                response_time = (datetime.utcnow() - start_time).total_seconds() * 1000
                result['response_time_ms'] = int(response_time)
                result['status_code'] = response.status_code

                # Try to parse JSON response
                try:
                    result['response'] = response.json()
                except Exception:
                    result['response'] = {'text': response.text[:1000]}

                # Check for success
                result['success'] = 200 <= response.status_code < 300

                # Update log if provided
                if log:
                    await self._update_log(log, result)

                logger.info(
                    f"Webhook dispatch to {channel.webhook_url}: "
                    f"status={response.status_code}, time={result['response_time_ms']}ms"
                )

        except httpx.TimeoutException as e:
            result['error'] = f"Timeout after {channel.timeout_seconds}s"
            logger.error(f"Webhook timeout for channel {channel.id}: {e}")
            if log:
                await self._update_log_error(log, result['error'])

        except httpx.RequestError as e:
            result['error'] = f"Request error: {str(e)}"
            logger.error(f"Webhook request error for channel {channel.id}: {e}")
            if log:
                await self._update_log_error(log, result['error'])

        except Exception as e:
            result['error'] = f"Unexpected error: {str(e)}"
            logger.error(f"Webhook unexpected error for channel {channel.id}: {e}")
            if log:
                await self._update_log_error(log, result['error'])

        return result

    def _build_headers(self, channel: MessagingChannel) -> Dict[str, str]:
        """
        Build request headers including authentication.

        Args:
            channel: The messaging channel

        Returns:
            Dict[str, str]: Headers dictionary
        """
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Versya-Messaging/1.0'
        }

        # Add custom headers if configured
        if channel.headers:
            headers.update(channel.headers)

        # Add authentication headers
        if channel.auth_type != AuthType.none and channel.auth_config:
            auth_config = self._decrypt_auth_config(channel.auth_config)
            headers.update(self._build_auth_headers(channel.auth_type, auth_config))

        return headers

    def _build_auth_headers(
        self,
        auth_type: AuthType,
        auth_config: Dict[str, Any]
    ) -> Dict[str, str]:
        """
        Build authentication headers based on auth type.

        Args:
            auth_type: The authentication type
            auth_config: The authentication configuration

        Returns:
            Dict[str, str]: Auth headers
        """
        headers = {}

        if auth_type == AuthType.bearer:
            token = auth_config.get('token', '')
            headers['Authorization'] = f"Bearer {token}"

        elif auth_type == AuthType.basic:
            username = auth_config.get('username', '')
            password = auth_config.get('password', '')
            credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers['Authorization'] = f"Basic {credentials}"

        elif auth_type == AuthType.api_key:
            header_name = auth_config.get('header_name', 'X-API-Key')
            api_key = auth_config.get('api_key', '')
            headers[header_name] = api_key

        elif auth_type == AuthType.custom_header:
            custom_headers = auth_config.get('headers', {})
            headers.update(custom_headers)

        return headers

    def _decrypt_auth_config(self, encrypted_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Decrypt authentication configuration.

        Args:
            encrypted_config: The encrypted config from database

        Returns:
            Dict[str, Any]: Decrypted configuration
        """
        # If the config contains encrypted values, decrypt them
        decrypted = {}
        for key, value in encrypted_config.items():
            if isinstance(value, str) and value.startswith('encrypted:'):
                try:
                    encrypted_value = value[10:]  # Remove 'encrypted:' prefix
                    decrypted[key] = self.fernet.decrypt(encrypted_value.encode()).decode()
                except Exception as e:
                    logger.error(f"Failed to decrypt auth config key {key}: {e}")
                    decrypted[key] = ''
            else:
                decrypted[key] = value
        return decrypted

    def encrypt_auth_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Encrypt sensitive values in authentication configuration.

        Args:
            config: The configuration to encrypt

        Returns:
            Dict[str, Any]: Configuration with encrypted sensitive values
        """
        sensitive_keys = ['token', 'password', 'api_key', 'secret']
        encrypted = {}

        for key, value in config.items():
            if key in sensitive_keys and isinstance(value, str):
                encrypted_value = self.fernet.encrypt(value.encode()).decode()
                encrypted[key] = f"encrypted:{encrypted_value}"
            elif isinstance(value, dict):
                encrypted[key] = self.encrypt_auth_config(value)
            else:
                encrypted[key] = value

        return encrypted

    async def _update_log(self, log: MessagingLog, result: Dict[str, Any]) -> None:
        """
        Update log entry with dispatch result.

        Args:
            log: The log entry to update
            result: The dispatch result
        """
        log.attempt_count += 1
        log.last_attempt_at = datetime.utcnow()
        log.provider_response = result.get('response')

        if result['success']:
            log.status = MessageStatus.sent
            log.sent_at = datetime.utcnow()
            # Extract provider message ID if available
            if result.get('response') and isinstance(result['response'], dict):
                log.provider_message_id = (
                    result['response'].get('id') or
                    result['response'].get('message_id') or
                    result['response'].get('messageId') or
                    result['response'].get('sid')  # Twilio
                )
        else:
            log.error_message = result.get('error')
            # Will be retried by event processor

    async def _update_log_error(self, log: MessagingLog, error: str) -> None:
        """
        Update log entry with error.

        Args:
            log: The log entry to update
            error: The error message
        """
        log.attempt_count += 1
        log.last_attempt_at = datetime.utcnow()
        log.error_message = error

    async def test_channel(self, channel: MessagingChannel) -> Dict[str, Any]:
        """
        Test a channel configuration by sending a test request.

        Args:
            channel: The channel to test

        Returns:
            Dict[str, Any]: Test result
        """
        test_payload = {
            'test': True,
            'timestamp': datetime.utcnow().isoformat(),
            'message': 'Versya Messaging test message'
        }

        return await self.dispatch(channel, test_payload)

    def build_provider_payload(
        self,
        channel: MessagingChannel,
        recipient: str,
        subject: Optional[str],
        body: str,
        metadata: Optional[Dict[str, Any]] = None,
        bcc: Optional[str] = None,
        media_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build a provider-specific payload based on channel type.

        Args:
            channel: The messaging channel
            recipient: The message recipient
            subject: Optional message subject
            body: The message body
            metadata: Optional additional metadata

        Returns:
            Dict[str, Any]: Provider-formatted payload
        """
        channel_type = channel.channel_type.value

        # Base payload
        payload = {
            'to': recipient,
            'body': body,
            'channel': channel_type,
            'timestamp': datetime.utcnow().isoformat()
        }

        if subject:
            payload['subject'] = subject

        # Add channel-specific formatting
        if channel_type == 'email':
            payload = {
                'to': recipient,
                'subject': subject or 'Message from Versya',
                'html': body,
                'text': self._strip_html(body)
            }
            if bcc:
                payload['bcc'] = bcc
        elif channel_type == 'sms':
            payload = {
                'to': recipient,
                'body': body
            }
        elif channel_type == 'whatsapp':
            payload = {
                'number': recipient,
                'text': body
            }
            if media_url:
                payload['media_url'] = media_url

        # Add media/attachments to email payloads
        if media_url and channel_type == 'email':
            payload['attachments'] = [{'url': media_url}]

        # Merge with template metadata if present
        if metadata:
            payload.update(metadata)

        return payload

    def _strip_html(self, html: str) -> str:
        """
        Strip HTML tags from a string for plain text fallback.

        Args:
            html: HTML string

        Returns:
            str: Plain text
        """
        import re
        clean = re.compile('<.*?>')
        return re.sub(clean, '', html)


# Singleton instance
webhook_dispatcher = WebhookDispatcher()
