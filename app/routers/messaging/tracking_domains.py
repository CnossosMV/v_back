"""Tracking-domain onboarding and edge configuration APIs."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Project, User
from app.models.messaging import (
    MessagingProxyRequestLog,
    MessagingTrackingDomain,
)
from app.routers.auth import get_current_user
from app.schemas.messaging import (
    MessagingTrackingDomainCreate,
    MessagingTrackingDomainResponse,
    MessagingTrackingDomainUpdate,
    MessagingTrackingDomainVerifyResponse,
    ProxyRequestLogCreate,
    ProxyRequestLogResponse,
    TrackingDomainDnsInstructions,
    TrackingDomainEdgeConfigResponse,
)
from app.services.messaging.tracking_domain_service import (
    cloudflare_tracking_service,
    dns_instructions,
    edge_config_for_domain,
    new_verification_token,
    normalize_hostname,
    resolve_domain_for_tracking,
    tracking_cname_target,
    verify_cname,
    verify_tracking_txt,
)


router = APIRouter(
    prefix="/projects/{project_id}/messaging/tracking-domains",
    tags=["messaging-tracking-domains"],
)
internal_router = APIRouter(
    prefix="/internal/tracking-domains",
    tags=["messaging-tracking-domains-internal"],
)


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True,
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _edge_secret_or_503() -> str:
    secret = os.getenv("VERSYA_EDGE_CONFIG_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="VERSYA_EDGE_CONFIG_SECRET is not configured")
    return secret


def _verify_edge_secret(x_versya_edge_secret: Optional[str]) -> None:
    if x_versya_edge_secret != _edge_secret_or_503():
        raise HTTPException(status_code=401, detail="Invalid edge secret")


def _serialize(domain: MessagingTrackingDomain) -> Dict:
    instructions = dns_instructions(domain)
    return {
        "id": domain.id,
        "project_id": domain.project_id,
        "domain_id": domain.domain_id,
        "hostname": domain.hostname,
        "mode": domain.mode,
        "cname_target": domain.cname_target,
        "verification_token": domain.verification_token,
        "cloudflare_custom_hostname_id": domain.cloudflare_custom_hostname_id,
        "dns_status": domain.dns_status,
        "ssl_status": domain.ssl_status,
        "proxy_status": domain.proxy_status,
        "cookie_keeper_enabled": domain.cookie_keeper_enabled,
        "last_seen_at": domain.last_seen_at,
        "config_version": domain.config_version,
        "status_details": domain.status_details,
        "dns_instructions": TrackingDomainDnsInstructions(**instructions),
        "created_at": domain.created_at,
        "updated_at": domain.updated_at,
    }


@router.get("", response_model=List[MessagingTrackingDomainResponse])
def list_tracking_domains(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domains = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.project_id == project_id
    ).order_by(MessagingTrackingDomain.created_at.desc()).all()
    return [_serialize(domain) for domain in domains]


@router.post("", response_model=MessagingTrackingDomainResponse, status_code=status.HTTP_201_CREATED)
def create_tracking_domain(
    project_id: int,
    data: MessagingTrackingDomainCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    try:
        hostname = normalize_hostname(data.hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    messaging_domain = resolve_domain_for_tracking(db, project_id=project_id, domain_id=data.domain_id)
    if not messaging_domain:
        raise HTTPException(status_code=400, detail="Create or select a Messaging Domain/write key before adding a tracking domain")

    tracking_domain = MessagingTrackingDomain(
        project_id=project_id,
        domain_id=messaging_domain.id,
        hostname=hostname,
        mode="managed_cname",
        cname_target=tracking_cname_target(),
        verification_token=new_verification_token(),
        dns_status="pending",
        ssl_status="pending",
        proxy_status="pending",
        cookie_keeper_enabled=data.cookie_keeper_enabled,
        status_details={
            "decision": "Versya-managed Cloudflare for SaaS / Custom Hostnames",
            "provider_endpoint_proxying": "deferred_v2",
        },
    )
    db.add(tracking_domain)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Tracking hostname already exists")
    db.refresh(tracking_domain)
    return _serialize(tracking_domain)


@router.get("/proxy-logs", response_model=List[ProxyRequestLogResponse])
def list_proxy_logs(
    project_id: int,
    tracking_domain_id: Optional[int] = Query(None),
    hostname: Optional[str] = Query(None),
    event_id: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List recent first-party proxy observations for debugging."""
    get_project_or_404(db, project_id, current_user.workspace_id)
    query = db.query(MessagingProxyRequestLog).filter(
        MessagingProxyRequestLog.project_id == project_id
    )
    if tracking_domain_id:
        query = query.filter(MessagingProxyRequestLog.tracking_domain_id == tracking_domain_id)
    if hostname:
        try:
            clean_hostname = normalize_hostname(hostname)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        query = query.filter(MessagingProxyRequestLog.hostname == clean_hostname)
    if event_id:
        query = query.filter(MessagingProxyRequestLog.event_id == event_id)
    return query.order_by(MessagingProxyRequestLog.created_at.desc()).offset(offset).limit(limit).all()


@router.get("/{tracking_domain_id}", response_model=MessagingTrackingDomainResponse)
def get_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")
    return _serialize(domain)


@router.put("/{tracking_domain_id}", response_model=MessagingTrackingDomainResponse)
def update_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    data: MessagingTrackingDomainUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")

    if data.domain_id is not None:
        messaging_domain = resolve_domain_for_tracking(db, project_id=project_id, domain_id=data.domain_id)
        if not messaging_domain:
            raise HTTPException(status_code=400, detail="Messaging domain not found")
        domain.domain_id = messaging_domain.id
    if data.cookie_keeper_enabled is not None:
        domain.cookie_keeper_enabled = data.cookie_keeper_enabled
    if data.proxy_status is not None:
        domain.proxy_status = data.proxy_status
    domain.config_version = (domain.config_version or 1) + 1
    db.commit()
    db.refresh(domain)
    return _serialize(domain)


@router.delete("/{tracking_domain_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")
    db.delete(domain)
    db.commit()


@router.post("/{tracking_domain_id}/verify", response_model=MessagingTrackingDomainVerifyResponse)
def verify_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")

    cname_ok, cname_found, cname_error = verify_cname(domain.hostname, domain.cname_target)
    txt_ok, txt_found, txt_error = verify_tracking_txt(domain.hostname, domain.verification_token)
    verified = cname_ok or txt_ok

    domain.dns_status = "verified" if verified else "pending"
    domain.status_details = {
        **(domain.status_details or {}),
        "dns": {
            "cname_ok": cname_ok,
            "cname_found": cname_found,
            "cname_error": cname_error,
            "txt_ok": txt_ok,
            "txt_found": txt_found,
            "txt_error": txt_error,
        },
    }
    db.commit()
    db.refresh(domain)

    message = "Tracking DNS verified" if verified else "Tracking DNS is not verified yet"
    return MessagingTrackingDomainVerifyResponse(
        verified=verified,
        dns_status=domain.dns_status,
        ssl_status=domain.ssl_status,
        proxy_status=domain.proxy_status,
        message=message,
        details=domain.status_details,
    )


@router.post("/{tracking_domain_id}/provision", response_model=MessagingTrackingDomainResponse)
def provision_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")

    result = cloudflare_tracking_service.provision(domain)
    if result.custom_hostname_id:
        domain.cloudflare_custom_hostname_id = result.custom_hostname_id
    if result.ssl_status:
        domain.ssl_status = result.ssl_status
    if result.proxy_status:
        domain.proxy_status = result.proxy_status
    if result.configured and result.success and domain.proxy_status in ("active", "pending"):
        domain.proxy_status = domain.proxy_status or "pending"
    domain.status_details = {
        **(domain.status_details or {}),
        "cloudflare": {
            "configured": result.configured,
            "success": result.success,
            "message": result.message,
            "details": result.details,
        },
    }
    domain.config_version = (domain.config_version or 1) + 1
    db.commit()
    db.refresh(domain)
    return _serialize(domain)


@router.post("/{tracking_domain_id}/sync", response_model=MessagingTrackingDomainResponse)
def sync_tracking_domain(
    project_id: int,
    tracking_domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    get_project_or_404(db, project_id, current_user.workspace_id)
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.id == tracking_domain_id,
        MessagingTrackingDomain.project_id == project_id,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking domain not found")

    result = cloudflare_tracking_service.sync(domain)
    if result.ssl_status:
        domain.ssl_status = result.ssl_status
    if result.proxy_status:
        domain.proxy_status = result.proxy_status
    domain.status_details = {
        **(domain.status_details or {}),
        "cloudflare": {
            "configured": result.configured,
            "success": result.success,
            "message": result.message,
            "details": result.details,
        },
    }
    db.commit()
    db.refresh(domain)
    return _serialize(domain)


@internal_router.get("/edge-config", response_model=TrackingDomainEdgeConfigResponse)
def get_edge_config(
    hostname: str = Query(...),
    x_versya_edge_secret: Optional[str] = Header(None, alias="X-Versya-Edge-Secret"),
    db: Session = Depends(get_db),
):
    _verify_edge_secret(x_versya_edge_secret)
    try:
        clean_hostname = normalize_hostname(hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.hostname == clean_hostname,
    ).first()
    if not domain or not domain.domain:
        raise HTTPException(status_code=404, detail="Tracking hostname not found")
    domain.last_seen_at = datetime.utcnow()
    if domain.proxy_status in ("pending", "unknown"):
        domain.proxy_status = "seen"
    db.commit()
    return edge_config_for_domain(domain)


@internal_router.post("/proxy-log", response_model=ProxyRequestLogResponse)
def create_proxy_log(
    data: ProxyRequestLogCreate,
    x_versya_edge_secret: Optional[str] = Header(None, alias="X-Versya-Edge-Secret"),
    db: Session = Depends(get_db),
):
    _verify_edge_secret(x_versya_edge_secret)
    try:
        clean_hostname = normalize_hostname(data.hostname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    domain = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.hostname == clean_hostname,
    ).first()
    if not domain:
        raise HTTPException(status_code=404, detail="Tracking hostname not found")

    domain.last_seen_at = datetime.utcnow()
    if data.status_code and 200 <= data.status_code < 500:
        domain.proxy_status = "seen"
    log = MessagingProxyRequestLog(
        project_id=domain.project_id,
        tracking_domain_id=domain.id,
        hostname=clean_hostname,
        method=data.method[:12],
        path=data.path[:500],
        action=data.action,
        event_id=data.event_id,
        anonymous_id=data.anonymous_id,
        status_code=data.status_code,
        click_ids=data.click_ids,
        cookies_refreshed=data.cookies_refreshed,
        request_summary=data.request_summary,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log
