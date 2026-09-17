"""Storage Backend Protocol.

Defines the interface for pluggable file storage backends.
"""
from typing import Protocol, runtime_checkable


@runtime_checkable
class StorageBackend(Protocol):
    """Protocol for file storage backends."""

    def save(self, key: str, data: bytes, content_type: str) -> str:
        """Store file data under the given key. Returns the key."""
        ...

    def get_url(self, key: str) -> str:
        """Return a publicly accessible URL for the stored file."""
        ...

    def delete(self, key: str) -> bool:
        """Delete the file at the given key. Returns True if deleted."""
        ...

    def get_local_path(self, key: str) -> str:
        """Return a local filesystem path for the file.

        For local storage: direct path.
        For remote storage: downloads to a temp file and returns that path.
        """
        ...
