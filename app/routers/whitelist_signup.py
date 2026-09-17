"""
Whitelist Signup Router

Public API endpoint for collecting whitelist signups from the static marketing website.
Only accepts requests from the configured WEBSITE_URL origin.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from typing import List
import os

from app.database import get_db
from app import models, schemas
from app.routers.auth import get_current_user

router = APIRouter(prefix="/whitelist", tags=["whitelist"])

# Get allowed website URL from environment
WEBSITE_URL = os.getenv("WEBSITE_URL", "")


def verify_origin(request: Request) -> None:
    """
    Verify that the request comes from the allowed website origin.
    Raises HTTPException if origin is not allowed.
    """
    if not WEBSITE_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Whitelist signup service is not configured"
        )

    origin = request.headers.get("origin", "")
    referer = request.headers.get("referer", "")

    # Check origin header first (for CORS preflight)
    allowed_origins = [url.strip() for url in WEBSITE_URL.split(",")]

    origin_allowed = any(
        origin == allowed or origin.startswith(allowed.rstrip("/"))
        for allowed in allowed_origins
        if allowed
    )

    # Fallback to referer check
    referer_allowed = any(
        referer.startswith(allowed.rstrip("/"))
        for allowed in allowed_origins
        if allowed
    )

    if not origin_allowed and not referer_allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin not allowed"
        )


@router.post(
    "/signup",
    response_model=schemas.WhitelistSignupSuccessResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit whitelist signup",
    description="Public endpoint for collecting whitelist signups from the marketing website."
)
async def create_whitelist_signup(
    signup: schemas.WhitelistSignupCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Create a new whitelist signup entry.

    This endpoint is public but only accepts requests from the configured WEBSITE_URL.
    It captures:
    - Contact info: email, phone, country, name, company
    - Feedback: suggestion/comments
    - Tracking: IP, user agent, referrer, UTM parameters
    """
    # Verify origin
    verify_origin(request)

    # Check for duplicate email
    existing = db.query(models.WhitelistSignup).filter(
        models.WhitelistSignup.email == signup.email
    ).first()

    if existing:
        # Return success anyway to prevent email enumeration
        return schemas.WhitelistSignupSuccessResponse(
            success=True,
            message="Thank you for your interest! We'll be in touch soon.",
            signup_id=existing.id
        )

    # Extract tracking info from request
    ip_address = request.headers.get("x-forwarded-for", request.client.host if request.client else None)
    if ip_address and "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()

    user_agent = request.headers.get("user-agent", "")[:500]
    referer = request.headers.get("referer", "")[:500]

    # Create signup entry
    db_signup = models.WhitelistSignup(
        email=signup.email,
        phone=signup.phone,
        country=signup.country,
        name=signup.name,
        company=signup.company,
        suggestion=signup.suggestion,
        ip_address=ip_address,
        user_agent=user_agent,
        referrer=referer,
        utm_source=signup.utm_source,
        utm_medium=signup.utm_medium,
        utm_campaign=signup.utm_campaign
    )

    db.add(db_signup)
    db.commit()
    db.refresh(db_signup)

    return schemas.WhitelistSignupSuccessResponse(
        success=True,
        message="Thank you for your interest! We'll be in touch soon.",
        signup_id=db_signup.id
    )


# ==========================================
# Admin endpoints (authenticated)
# ==========================================

@router.get(
    "/signups",
    response_model=List[schemas.WhitelistSignupFullResponse],
    summary="List all whitelist signups",
    description="Admin endpoint to list all whitelist signups with full tracking data."
)
async def list_whitelist_signups(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    List all whitelist signups (admin only).
    Returns full tracking data including IP, user agent, UTM params.
    """
    # Check if user is admin
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    signups = db.query(models.WhitelistSignup).order_by(
        models.WhitelistSignup.created_at.desc()
    ).offset(skip).limit(limit).all()

    return signups


@router.get(
    "/signups/{signup_id}",
    response_model=schemas.WhitelistSignupFullResponse,
    summary="Get whitelist signup details",
    description="Admin endpoint to get full details of a whitelist signup."
)
async def get_whitelist_signup(
    signup_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Get a specific whitelist signup by ID (admin only).
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    signup = db.query(models.WhitelistSignup).filter(
        models.WhitelistSignup.id == signup_id
    ).first()

    if not signup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Signup not found"
        )

    return signup


@router.delete(
    "/signups/{signup_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete whitelist signup",
    description="Admin endpoint to delete a whitelist signup."
)
async def delete_whitelist_signup(
    signup_id: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Delete a whitelist signup (admin only).
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    signup = db.query(models.WhitelistSignup).filter(
        models.WhitelistSignup.id == signup_id
    ).first()

    if not signup:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Signup not found"
        )

    db.delete(signup)
    db.commit()

    return None


@router.get(
    "/signups/stats/summary",
    summary="Get whitelist signup statistics",
    description="Admin endpoint to get summary statistics of whitelist signups."
)
async def get_whitelist_stats(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user)
):
    """
    Get summary statistics for whitelist signups (admin only).
    """
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )

    from sqlalchemy import func

    total = db.query(func.count(models.WhitelistSignup.id)).scalar()
    verified = db.query(func.count(models.WhitelistSignup.id)).filter(
        models.WhitelistSignup.is_verified == True
    ).scalar()

    # Get signups by country
    by_country = db.query(
        models.WhitelistSignup.country,
        func.count(models.WhitelistSignup.id).label("count")
    ).group_by(models.WhitelistSignup.country).all()

    # Get signups by UTM source
    by_source = db.query(
        models.WhitelistSignup.utm_source,
        func.count(models.WhitelistSignup.id).label("count")
    ).filter(
        models.WhitelistSignup.utm_source.isnot(None)
    ).group_by(models.WhitelistSignup.utm_source).all()

    return {
        "total_signups": total,
        "verified_signups": verified,
        "by_country": [{"country": c or "Unknown", "count": cnt} for c, cnt in by_country],
        "by_utm_source": [{"source": s or "Direct", "count": cnt} for s, cnt in by_source]
    }
