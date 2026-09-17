"""Fail-closed behavior of the shared encryption service."""
import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.services import encryption_service
from app.services.encryption_service import (
    decrypt_value,
    decrypt_value_or_none,
    encrypt_value,
    get_encryption_key,
)


@pytest.fixture
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    return key


def test_get_encryption_key_raises_when_unset(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        get_encryption_key()


def test_encrypt_raises_when_key_unset(monkeypatch):
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encrypt_value("secret")


def test_roundtrip(fernet_key):
    assert decrypt_value(encrypt_value("my-secret")) == "my-secret"


def test_decrypt_rejects_plaintext(fernet_key):
    with pytest.raises(InvalidToken):
        decrypt_value("this-was-stored-in-plaintext")


def test_decrypt_or_none_on_garbage(fernet_key):
    assert decrypt_value_or_none("not-ciphertext") is None


def test_decrypt_or_none_on_missing_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", key)
    token = encrypt_value("value")
    monkeypatch.delenv("ENCRYPTION_KEY")
    assert decrypt_value_or_none(token) is None


def test_decrypt_or_none_empty_values(fernet_key):
    assert decrypt_value_or_none(None) is None
    assert decrypt_value_or_none("") is None


def test_no_ephemeral_key_generation(monkeypatch):
    """The old code generated a random key when ENCRYPTION_KEY was unset,
    silently producing undecryptable-after-restart data. Must be gone."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        encryption_service.get_encryption_key()
