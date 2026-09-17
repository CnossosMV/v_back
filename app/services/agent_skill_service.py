"""Versioned distribution for Versya's public tenant-agent skill."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict

from app.mcp.config import mcp_resource_url


SKILL_NAME = "versya-project-onboarding"
SKILL_VERSION = "1.1.0"
SKILL_ROOT = Path(__file__).resolve().parent.parent / "agent_skills" / SKILL_NAME
PUBLIC_ROOT = f"/agent-skills/{SKILL_NAME}"
_ZIP_TIMESTAMP = (2020, 1, 1, 0, 0, 0)


def _skill_files() -> Dict[str, bytes]:
    files: Dict[str, bytes] = {}
    for path in sorted(SKILL_ROOT.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            relative = path.relative_to(SKILL_ROOT).as_posix()
            files[relative] = path.read_bytes()
    if "SKILL.md" not in files:
        raise RuntimeError("Packaged Versya skill is missing SKILL.md")
    return files


def build_skill_zip() -> bytes:
    files = _skill_files()
    package_manifest = {
        "name": SKILL_NAME,
        "version": SKILL_VERSION,
        "files": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in files.items()
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, content in files.items():
            info = zipfile.ZipInfo(f"{SKILL_NAME}/{relative}", date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
        info = zipfile.ZipInfo(f"{SKILL_NAME}/manifest.json", date_time=_ZIP_TIMESTAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(
            info,
            json.dumps(package_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
    return buffer.getvalue()


def skill_manifest() -> Dict[str, Any]:
    files = _skill_files()
    zip_bytes = build_skill_zip()
    return {
        "schema_version": "1.0",
        "name": SKILL_NAME,
        "version": SKILL_VERSION,
        "description": "Agent-first onboarding and safe configuration of a Versya project through MCP.",
        "entrypoint": "SKILL.md",
        "skill_url": mcp_resource_url(f"{PUBLIC_ROOT}/SKILL.md"),
        "download_url": mcp_resource_url(f"{PUBLIC_ROOT}/{SKILL_NAME}-{SKILL_VERSION}.zip"),
        "manifest_url": mcp_resource_url(f"{PUBLIC_ROOT}/manifest.json"),
        "zip_sha256": hashlib.sha256(zip_bytes).hexdigest(),
        "files": {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            for name, content in files.items()
        },
        "installation": {
            "generic": "Download the ZIP, verify zip_sha256, and install the contained skill directory in the agent's supported skill location.",
            "openai_api": "The ZIP is a file collection suitable for creating a versioned Skill through the OpenAI Skills API.",
            "fallback": "If skill installation is unavailable, call get_project_onboarding_contract; the MCP remains self-describing.",
        },
        "external_sends": 0,
    }
