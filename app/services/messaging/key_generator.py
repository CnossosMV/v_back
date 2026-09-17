"""
Key Generation Service for Messaging Middleware
Handles generation and verification of write keys (pk_*) and secret keys (sk_*)
Also provides HMAC signing and verification for request integrity
"""
import secrets
import hashlib
import hmac
import logging
import time
from typing import Tuple, Optional

logger = logging.getLogger(__name__)

# HMAC settings
HMAC_ALGORITHM = hashlib.sha256
TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 minutes tolerance for replay protection


class KeyGenerator:
    """
    Generates and verifies API keys for the messaging middleware.

    Key Types:
    - Write Key (pk_live_xxx): Used for frontend SDK authentication, stored in plain text
    - Secret Key (sk_live_xxx): Used for backend API authentication, stored as hash
    """

    WRITE_KEY_PREFIX = "pk_live_"
    SECRET_KEY_PREFIX = "sk_live_"
    KEY_LENGTH = 32  # Length of the random part

    def generate_write_key(self) -> str:
        """
        Generate a write key for frontend SDK authentication.
        Format: pk_live_<32 random chars>

        Returns:
            str: The write key (e.g., pk_live_abc123...)
        """
        random_part = secrets.token_urlsafe(self.KEY_LENGTH)[:self.KEY_LENGTH]
        return f"{self.WRITE_KEY_PREFIX}{random_part}"

    def generate_secret_key(self) -> Tuple[str, str, str]:
        """
        Generate a secret key for backend API authentication.
        Format: sk_live_<32 random chars>

        Returns:
            Tuple[str, str, str]: (full_key, key_hash, key_prefix)
            - full_key: The complete secret key (shown only once)
            - key_hash: SHA256 hash for storage
            - key_prefix: First 10 chars for display (e.g., "sk_live_ab")
        """
        random_part = secrets.token_urlsafe(self.KEY_LENGTH)[:self.KEY_LENGTH]
        full_key = f"{self.SECRET_KEY_PREFIX}{random_part}"
        key_hash = self.hash_secret_key(full_key)
        key_prefix = full_key[:10] + "..."

        return full_key, key_hash, key_prefix

    def hash_secret_key(self, key: str) -> str:
        """
        Hash a secret key for secure storage.

        Args:
            key: The secret key to hash

        Returns:
            str: SHA256 hash of the key
        """
        return hashlib.sha256(key.encode()).hexdigest()

    def verify_secret_key(self, key: str, stored_hash: str) -> bool:
        """
        Verify a secret key against its stored hash.

        Args:
            key: The secret key to verify
            stored_hash: The stored hash to compare against

        Returns:
            bool: True if the key matches the hash
        """
        key_hash = self.hash_secret_key(key)
        return secrets.compare_digest(key_hash, stored_hash)

    def generate_verification_token(self) -> str:
        """
        Generate a verification token for domain verification.
        Format: versya-verify-<24 random chars>

        Returns:
            str: The verification token
        """
        random_part = secrets.token_urlsafe(24)[:24]
        return f"versya-verify-{random_part}"

    def is_write_key(self, key: str) -> bool:
        """
        Check if a key is a write key (pk_live_*).

        Args:
            key: The key to check

        Returns:
            bool: True if it's a write key
        """
        return key.startswith(self.WRITE_KEY_PREFIX)

    def is_secret_key(self, key: str) -> bool:
        """
        Check if a key is a secret key (sk_live_*).

        Args:
            key: The key to check

        Returns:
            bool: True if it's a secret key
        """
        return key.startswith(self.SECRET_KEY_PREFIX)

    def compute_hmac_signature(
        self,
        secret_key: str,
        payload: bytes,
        timestamp: str
    ) -> str:
        """
        Compute HMAC-SHA256 signature for a payload.

        The signature is computed over: timestamp + "." + payload
        This binds the timestamp to the payload to prevent replay attacks.

        Args:
            secret_key: The secret key (sk_live_xxx)
            payload: The raw request body bytes
            timestamp: Unix timestamp as string

        Returns:
            str: Hex-encoded HMAC signature
        """
        # Create the signing message: timestamp.payload
        signing_message = f"{timestamp}.".encode() + payload

        # Compute HMAC using the secret key
        signature = hmac.new(
            secret_key.encode(),
            signing_message,
            HMAC_ALGORITHM
        ).hexdigest()

        return signature

    def verify_hmac_signature(
        self,
        secret_key: str,
        payload: bytes,
        timestamp: str,
        signature: str,
        tolerance_seconds: int = TIMESTAMP_TOLERANCE_SECONDS
    ) -> Tuple[bool, Optional[str]]:
        """
        Verify HMAC-SHA256 signature of a payload.

        Args:
            secret_key: The secret key (sk_live_xxx)
            payload: The raw request body bytes
            timestamp: Unix timestamp as string from header
            signature: The signature from header to verify
            tolerance_seconds: Max age of request in seconds (replay protection)

        Returns:
            Tuple[bool, Optional[str]]: (is_valid, error_message)
        """
        # Validate timestamp format
        try:
            request_time = int(timestamp)
        except (ValueError, TypeError):
            return False, "Invalid timestamp format"

        # Check timestamp is within tolerance (replay protection)
        current_time = int(time.time())
        time_diff = abs(current_time - request_time)

        if time_diff > tolerance_seconds:
            return False, f"Request timestamp expired (diff: {time_diff}s, tolerance: {tolerance_seconds}s)"

        # Compute expected signature
        expected_signature = self.compute_hmac_signature(secret_key, payload, timestamp)

        # Constant-time comparison to prevent timing attacks
        if not secrets.compare_digest(expected_signature, signature):
            return False, "Signature mismatch"

        return True, None


# Singleton instance
key_generator = KeyGenerator()
