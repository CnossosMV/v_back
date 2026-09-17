"""Public, immutable distribution endpoints for Versya tenant-agent skills."""

import hashlib
import json

from fastapi import APIRouter, Header, Response
from fastapi.responses import JSONResponse

from app.services.agent_skill_service import (
    SKILL_NAME,
    SKILL_ROOT,
    SKILL_VERSION,
    build_skill_zip,
    skill_manifest,
)


router = APIRouter(prefix=f"/agent-skills/{SKILL_NAME}", tags=["agent-skills"])


def _cache_headers(etag: str) -> dict:
    return {
        "Cache-Control": "public, max-age=300, stale-while-revalidate=3600",
        "ETag": f'"{etag}"',
        "X-Content-Type-Options": "nosniff",
    }


def _not_modified(if_none_match: str | None, etag: str) -> bool:
    return (if_none_match or "").strip().strip('"') == etag


@router.get("/manifest.json")
def get_skill_manifest(if_none_match: str | None = Header(default=None)):
    manifest = skill_manifest()
    etag = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if _not_modified(if_none_match, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    return JSONResponse(manifest, headers=_cache_headers(etag))


@router.get("/SKILL.md")
def get_skill_entrypoint(if_none_match: str | None = Header(default=None)):
    content = (SKILL_ROOT / "SKILL.md").read_bytes()
    etag = hashlib.sha256(content).hexdigest()
    if _not_modified(if_none_match, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    return Response(content, media_type="text/markdown; charset=utf-8", headers=_cache_headers(etag))


@router.get(f"/{SKILL_NAME}-{SKILL_VERSION}.zip")
def download_skill(if_none_match: str | None = Header(default=None)):
    content = build_skill_zip()
    etag = hashlib.sha256(content).hexdigest()
    if _not_modified(if_none_match, etag):
        return Response(status_code=304, headers=_cache_headers(etag))
    headers = {
        **_cache_headers(etag),
        "Content-Disposition": f'attachment; filename="{SKILL_NAME}-{SKILL_VERSION}.zip"',
    }
    return Response(content, media_type="application/zip", headers=headers)
