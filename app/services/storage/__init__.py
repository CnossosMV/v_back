"""Storage backend factory.

Reads STORAGE_BACKEND env var to select the appropriate backend.
"""
import os

from .base import StorageBackend


def get_storage_backend() -> StorageBackend:
    """Return a storage backend based on STORAGE_BACKEND env var."""
    backend = os.getenv("STORAGE_BACKEND", "local")
    if backend == "s3":
        from .s3_storage import S3StorageBackend
        return S3StorageBackend()
    from .local_storage import LocalStorageBackend
    return LocalStorageBackend()
