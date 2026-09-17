"""
Messaging Domains Router
Manages verified domains for frontend SDK authentication
"""
import os
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.models import Project, User
from app.models.messaging import MessagingDomain, MessagingTrackingDomain
from app.schemas.messaging import (
    MessagingDomainCreate,
    MessagingDomainUpdate,
    MessagingDomainResponse,
    MessagingDomainSnippetResponse,
    MessagingDomainVerifyResponse
)
from app.routers.auth import get_current_user
from app.services.messaging import key_generator
from app.sdk import SDK_VERSION

router = APIRouter(prefix="/projects/{project_id}/messaging/domains", tags=["messaging-domains"])

# Get API base URL from environment (where API endpoints are)
API_BASE_URL = os.getenv('API_BASE_URL', os.getenv('VITE_API_BASE_URL', 'http://localhost:8001'))
# Get SDK CDN URL from environment (where SDK JS file is hosted - CDN in production)
SDK_CDN_URL = os.getenv('SDK_CDN_URL', 'http://localhost:3001')


def _sdk_endpoint_for_domain(db: Session, project_id: int, domain_id: int) -> str:
    """
    Prefer the customer's first-party tracking host once it is linked to this
    write-key domain. Fall back to the API origin so snippets still work before
    tracking-domain onboarding is complete.
    """
    tracking_domains = db.query(MessagingTrackingDomain).filter(
        MessagingTrackingDomain.project_id == project_id,
        MessagingTrackingDomain.domain_id == domain_id,
        MessagingTrackingDomain.cookie_keeper_enabled == True,
    ).order_by(MessagingTrackingDomain.updated_at.desc()).all()

    if not tracking_domains:
        return API_BASE_URL.rstrip("/")

    preferred = next(
        (
            td for td in tracking_domains
            if (td.proxy_status or "").lower() in {"seen", "active"}
        ),
        None,
    )
    preferred = preferred or next(
        (
            td for td in tracking_domains
            if (td.dns_status or "").lower() in {"verified", "active"}
        ),
        None,
    )
    if not preferred:
        return API_BASE_URL.rstrip("/")

    return f"https://{preferred.hostname.strip('/')}"


def get_project_or_404(db: Session, project_id: int, workspace_id: int) -> Project:
    """Get project and verify workspace access"""
    project = db.query(Project).filter(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
        Project.is_active == True
    ).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/", response_model=List[MessagingDomainResponse])
def list_domains(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all messaging domains for a project"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    domains = db.query(MessagingDomain).filter(
        MessagingDomain.project_id == project_id
    ).order_by(MessagingDomain.created_at.desc()).all()

    return domains


@router.post("/", response_model=MessagingDomainResponse, status_code=status.HTTP_201_CREATED)
def create_domain(
    project_id: int,
    data: MessagingDomainCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new messaging domain"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    # Check if domain already exists for this project
    existing = db.query(MessagingDomain).filter(
        MessagingDomain.project_id == project_id,
        MessagingDomain.domain == data.domain
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Domain already registered for this project"
        )

    # Generate write key and verification token
    write_key = key_generator.generate_write_key()
    verification_token = key_generator.generate_verification_token()

    domain = MessagingDomain(
        project_id=project_id,
        domain=data.domain,
        write_key=write_key,
        verification_token=verification_token,
        allowed_origins=data.allowed_origins
    )

    db.add(domain)
    db.commit()
    db.refresh(domain)

    return domain


@router.get("/{domain_id}", response_model=MessagingDomainResponse)
def get_domain(
    project_id: int,
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific messaging domain"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.id == domain_id,
        MessagingDomain.project_id == project_id
    ).first()

    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    return domain


@router.put("/{domain_id}", response_model=MessagingDomainResponse)
def update_domain(
    project_id: int,
    domain_id: int,
    data: MessagingDomainUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a messaging domain"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.id == domain_id,
        MessagingDomain.project_id == project_id
    ).first()

    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    # Update fields
    if data.domain is not None:
        # Check if new domain already exists
        existing = db.query(MessagingDomain).filter(
            MessagingDomain.project_id == project_id,
            MessagingDomain.domain == data.domain,
            MessagingDomain.id != domain_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Domain already exists")
        domain.domain = data.domain
        # Reset verification when domain changes
        domain.is_verified = False
        domain.verified_at = None
        domain.verification_token = key_generator.generate_verification_token()

    if data.allowed_origins is not None:
        domain.allowed_origins = data.allowed_origins

    if data.is_active is not None:
        domain.is_active = data.is_active

    db.commit()
    db.refresh(domain)

    return domain


@router.delete("/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(
    project_id: int,
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a messaging domain"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.id == domain_id,
        MessagingDomain.project_id == project_id
    ).first()

    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    db.delete(domain)
    db.commit()


@router.post("/{domain_id}/verify", response_model=MessagingDomainVerifyResponse)
def verify_domain(
    project_id: int,
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Verify domain ownership via DNS TXT record.

    The user must add a TXT record to their DNS:
    - Record name: _versya.{domain}
    - Record value: versya-verify={verification_token}

    Localhost and development domains (.local) skip verification.
    """
    from datetime import datetime
    from app.services.messaging.dns_verifier import dns_verifier

    get_project_or_404(db, project_id, current_user.workspace_id)

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.id == domain_id,
        MessagingDomain.project_id == project_id
    ).first()

    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    if domain.is_verified:
        return MessagingDomainVerifyResponse(
            verified=True,
            message="Domain is already verified"
        )

    # Verify via DNS TXT record
    verified, message, found_value = dns_verifier.verify_domain(
        domain.domain,
        domain.verification_token
    )

    if verified:
        domain.is_verified = True
        domain.verified_at = datetime.utcnow()
        db.commit()

    return MessagingDomainVerifyResponse(
        verified=verified,
        message=message
    )


@router.get("/{domain_id}/snippet", response_model=MessagingDomainSnippetResponse)
def get_snippet(
    project_id: int,
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the JavaScript SDK snippet for a domain"""
    get_project_or_404(db, project_id, current_user.workspace_id)

    domain = db.query(MessagingDomain).filter(
        MessagingDomain.id == domain_id,
        MessagingDomain.project_id == project_id
    ).first()

    if not domain:
        raise HTTPException(status_code=404, detail="Domain not found")

    sdk_endpoint = _sdk_endpoint_for_domain(db, project_id, domain.id)

    # Generate snippet - SDK served from API/proxy with cache-busting version param
    snippet = f'''<script>
(function(w,d,s,k,api){{
  w.versya=w.versya||{{_q:[],
    init:function(){{this._q.push(['init',arguments])}},
    identify:function(){{this._q.push(['identify',arguments])}},
    track:function(){{this._q.push(['track',arguments])}},
    page:function(){{this._q.push(['page',arguments])}},
    reset:function(){{this._q.push(['reset',arguments])}},
    getVisitorData:function(){{this._q.push(['getVisitorData',arguments])}}
  }};
  var f=d.getElementsByTagName(s)[0],j=d.createElement(s);
  j.async=1;j.src=api+'/sdk/versya-messaging.js?v={SDK_VERSION}';
  f.parentNode.insertBefore(j,f);
  w.versya.init(k,{{apiUrl:api}});
}})(window,document,'script','{domain.write_key}','{sdk_endpoint}');
</script>'''

    async_snippet = f'''<script async src="{sdk_endpoint}/sdk/versya-messaging.js?v={SDK_VERSION}"></script>
<script>
  window.versya = window.versya || {{ _q: [] }};
  versya.init('{domain.write_key}', {{apiUrl: '{sdk_endpoint}'}});
</script>'''

    return MessagingDomainSnippetResponse(
        domain_id=domain.id,
        write_key=domain.write_key,
        snippet=snippet,
        async_snippet=async_snippet,
        sdk_endpoint=sdk_endpoint,
        tracking_hostname=(
            sdk_endpoint.replace("https://", "").replace("http://", "").split("/")[0]
            if sdk_endpoint != API_BASE_URL.rstrip("/")
            else None
        )
    )
