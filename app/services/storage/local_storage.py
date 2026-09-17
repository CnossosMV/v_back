"""Local filesystem storage backend."""
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STORAGE_LOCAL_ROOT = os.getenv("STORAGE_LOCAL_ROOT", "/app/data/knowledge")


class LocalStorageBackend:
    """Store files on the local filesystem."""

    def __init__(self):
        self.root = Path(STORAGE_LOCAL_ROOT)

    def save(self, key: str, data: bytes, content_type: str) -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        logger.info(f"Saved {len(data)} bytes to {path}")
        return key

    def get_url(self, key: str) -> str:
        return f"/api/v1/knowledge-library/files/{key}"

    def delete(self, key: str) -> bool:
        path = self.root / key
        if path.exists():
            path.unlink()
            logger.info(f"Deleted {path}")
            return True
        return False

    def get_local_path(self, key: str) -> str:
        return str(self.root / key)
