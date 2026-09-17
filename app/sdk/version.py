"""
SDK version helper — single source of truth.

Parses VERSION from the JS file at import time and computes a SHA-256 ETag
from the file content. Both values are cached as module-level constants.
"""
import hashlib
import re
from pathlib import Path

_SDK_FILE = Path(__file__).resolve().parent / "versya-messaging.js"

# Parse VERSION from the JS source
_content = _SDK_FILE.read_text(encoding="utf-8")
_match = re.search(r"var\s+VERSION\s*=\s*['\"]([^'\"]+)['\"]", _content)
SDK_VERSION: str = _match.group(1) if _match else "0.0.0"

# Compute ETag from file content (strong ETag)
SDK_ETAG: str = '"' + hashlib.sha256(_content.encode("utf-8")).hexdigest()[:16] + '"'
