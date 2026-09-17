"""
Encryption service for sensitive data
Uses Fernet (symmetric encryption) for encrypting API keys, passwords, and other secrets

Fail-closed: ENCRYPTION_KEY is mandatory. There is no plaintext fallback and no
ephemeral key generation — data encrypted with a generated key would become
permanently unreadable after a restart.
"""
import os
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def get_encryption_key() -> bytes:
    """Get the encryption key from the environment. Raises if unset."""
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not configured — refusing to handle secrets. "
            "Set the ENCRYPTION_KEY environment variable."
        )
    return key.encode() if isinstance(key, str) else key


def encrypt_value(plain_text: str) -> str:
    """
    Encrypt a string value

    Args:
        plain_text: The text to encrypt

    Returns:
        Base64-encoded encrypted string
    """
    if not plain_text:
        return ""

    cipher = Fernet(get_encryption_key())
    encrypted_bytes = cipher.encrypt(plain_text.encode())
    return encrypted_bytes.decode()


def decrypt_value(encrypted_text: str) -> str:
    """
    Decrypt an encrypted string value

    Args:
        encrypted_text: The Base64-encoded encrypted string

    Returns:
        Decrypted plain text

    Raises:
        InvalidToken: if the value is not valid ciphertext for the current key
        RuntimeError: if ENCRYPTION_KEY is not configured
    """
    if not encrypted_text:
        return ""

    cipher = Fernet(get_encryption_key())
    decrypted_bytes = cipher.decrypt(encrypted_text.encode())
    return decrypted_bytes.decode()


def decrypt_value_or_none(encrypted_text: Optional[str]) -> Optional[str]:
    """Decrypt for webhook-path callers that must not raise mid-request.

    Returns None (and logs) on invalid ciphertext or missing key instead of
    propagating — callers must treat None as "secret unavailable" and fail
    the operation safely, never fall back to plaintext.
    """
    if not encrypted_text:
        return None
    try:
        return decrypt_value(encrypted_text)
    except (InvalidToken, RuntimeError) as e:
        logger.error(f"Secret decryption failed ({type(e).__name__}) — treating as unavailable")
        return None


def encrypt_dict(data: dict, keys_to_encrypt: list) -> dict:
    """
    Encrypt specific keys in a dictionary

    Args:
        data: Dictionary containing data
        keys_to_encrypt: List of keys to encrypt

    Returns:
        Dictionary with specified keys encrypted
    """
    encrypted_data = data.copy()

    for key in keys_to_encrypt:
        if key in encrypted_data and encrypted_data[key]:
            encrypted_data[key] = encrypt_value(str(encrypted_data[key]))

    return encrypted_data


def decrypt_dict(data: dict, keys_to_decrypt: list) -> dict:
    """
    Decrypt specific keys in a dictionary

    Args:
        data: Dictionary containing encrypted data
        keys_to_decrypt: List of keys to decrypt

    Returns:
        Dictionary with specified keys decrypted
    """
    decrypted_data = data.copy()

    for key in keys_to_decrypt:
        if key in decrypted_data and decrypted_data[key]:
            decrypted_data[key] = decrypt_value(str(decrypted_data[key]))

    return decrypted_data


# Alias functions for backwards compatibility with existing code
encrypt_password = encrypt_value
decrypt_password = decrypt_value
