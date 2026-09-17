from fastapi import APIRouter, HTTPException, Depends, status, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app import models, schemas
import requests
import jwt as pyjwt
from jwt import PyJWKClient
from datetime import datetime, timedelta
import hashlib
import logging
import os
from typing import Dict, Any, Optional
import uuid

router = APIRouter()
logger = logging.getLogger(__name__)

# Environment variables
KEYCLOAK_URL = os.getenv("KEYCLOAK_URL", "http://localhost:8006")
KEYCLOAK_REALM = os.getenv("KEYCLOAK_REALM", "autoflow")
KEYCLOAK_CLIENT_ID = os.getenv("KEYCLOAK_CLIENT_ID", "autoflow-frontend")
KEYCLOAK_JWKS_URL = os.getenv("KEYCLOAK_JWKS_URL")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
JWT_SECRET = os.getenv("JWT_SECRET", "your-secret-key")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 120  # 2 hours
REFRESH_TOKEN_EXPIRE_DAYS = 30
COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

_keycloak_jwks_client_cache: dict[str, PyJWKClient] = {}

def create_access_token(data: dict, expires_delta: timedelta = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = pyjwt.encode(to_encode, JWT_SECRET, algorithm=JWT_ALGORITHM)
    return encoded_jwt

def _hash_token(raw_token: str) -> str:
    """SHA-256 hash of a raw refresh token."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _create_refresh_token(user_id: int, db: Session, device_info: str = None) -> str:
    """Generate a refresh token, store its hash in DB, return the raw token."""
    raw_token = str(uuid.uuid4())
    token_hash = _hash_token(raw_token)
    rt = models.RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        device_info=device_info,
    )
    db.add(rt)
    db.commit()
    return raw_token


def _revoke_refresh_token(raw_token: str, db: Session) -> None:
    """Revoke a refresh token by its raw value."""
    token_hash = _hash_token(raw_token)
    rt = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash,
    ).first()
    if rt:
        rt.is_revoked = True
        db.commit()


def set_refresh_token_cookie(response: Response, token: str) -> None:
    """Set the refresh token as an HttpOnly cookie on the response."""
    is_secure = ENVIRONMENT != "development"
    response.set_cookie(
        key="refresh_token",
        value=token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/",
        domain=COOKIE_DOMAIN,
        max_age=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
    )


def clear_refresh_token_cookie(response: Response) -> None:
    """Delete the refresh token cookie."""
    is_secure = ENVIRONMENT != "development"
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=is_secure,
        samesite="lax",
        path="/",
        domain=COOKIE_DOMAIN,
    )


def get_refresh_token_from_request(request: Request, body: Optional[schemas.RefreshTokenRequest] = None) -> Optional[str]:
    """Read refresh token from cookie first, then fall back to request body."""
    token = request.cookies.get("refresh_token")
    if token:
        return token
    if body and body.refresh_token:
        return body.refresh_token
    return None


def verify_google_id_token(id_token: str) -> Dict[str, Any]:
    """Verify Google ID token and return user info"""
    try:
        # Verify token with Google
        response = requests.get(
            f"https://www.googleapis.com/oauth2/v3/tokeninfo?id_token={id_token}",
            timeout=10
        )
        
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google token"
            )
        
        user_info = response.json()
        
        # Verify the token is for our client
        if GOOGLE_CLIENT_ID and user_info.get("aud") != GOOGLE_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token audience mismatch"
            )
        
        return user_info
        
    except requests.RequestException:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify Google token"
        )


def _keycloak_issuer() -> str:
    return f"{KEYCLOAK_URL.rstrip('/')}/realms/{KEYCLOAK_REALM}"


def _keycloak_jwks_client() -> PyJWKClient:
    jwks_url = KEYCLOAK_JWKS_URL or f"{_keycloak_issuer()}/protocol/openid-connect/certs"
    if jwks_url not in _keycloak_jwks_client_cache:
        _keycloak_jwks_client_cache[jwks_url] = PyJWKClient(
            jwks_url,
            headers={
                "Accept": "application/json",
                "User-Agent": "VersyaBackend/1.0",
            },
        )
    return _keycloak_jwks_client_cache[jwks_url]


def verify_keycloak_id_token(id_token: str) -> Dict[str, Any]:
    """Verify a Keycloak ID token and return its claims."""
    try:
        signing_key = _keycloak_jwks_client().get_signing_key_from_jwt(id_token)
        return pyjwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256", "RS384", "RS512"],
            audience=KEYCLOAK_CLIENT_ID,
            issuer=_keycloak_issuer(),
        )
    except pyjwt.PyJWTError as exc:
        logger.warning(
            "Invalid Keycloak token: error_type=%s detail=%s issuer=%s audience=%s",
            exc.__class__.__name__,
            str(exc),
            _keycloak_issuer(),
            KEYCLOAK_CLIENT_ID,
        )
        raise HTTPException(
            status_code=401,
            detail=f"Invalid Keycloak token: {exc.__class__.__name__}",
        )


def _token_response_for_user(user: models.User, db: Session, response: Response = None) -> Dict[str, Any]:
    token_data = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "workspace_id": user.workspace_id,
    }
    access_token = create_access_token(token_data)
    refresh_token = _create_refresh_token(user.id, db)
    if response is not None:
        set_refresh_token_cookie(response, refresh_token)
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "is_verified": user.is_verified,
        "keycloak_id": user.keycloak_id,
    }


def _ensure_workspace(db: Session, user: models.User) -> None:
    if user.workspace_id:
        return
    workspace = models.Workspace(
        name=f"{user.name}'s Workspace",
        owner_id=user.id,
        is_active=True,
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    user.workspace_id = workspace.id
    db.add(user)
    db.commit()
    db.refresh(user)


def _link_or_create_keycloak_user(db: Session, claims: Dict[str, Any]) -> models.User:
    keycloak_sub = claims.get("sub")
    email = claims.get("email")
    name = claims.get("name") or claims.get("preferred_username") or email
    email_verified = bool(claims.get("email_verified", False))

    if not keycloak_sub:
        raise HTTPException(status_code=401, detail="Keycloak token is missing subject")
    if not email:
        raise HTTPException(status_code=400, detail="Keycloak token is missing email")

    user = db.query(models.User).filter(models.User.keycloak_id == str(keycloak_sub)).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == str(email)).first()

    if not user:
        user = models.User(
            email=str(email),
            name=str(name or email),
            keycloak_id=str(keycloak_sub),
            social_provider="keycloak",
            is_active=True,
            is_verified=email_verified,
            role="user",
            last_login=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _ensure_workspace(db, user)
        return user

    if not user.is_active:
        raise HTTPException(status_code=401, detail="User is inactive")

    existing_sub_user = (
        db.query(models.User)
        .filter(models.User.keycloak_id == str(keycloak_sub), models.User.id != user.id)
        .first()
    )
    if existing_sub_user:
        raise HTTPException(status_code=409, detail="Keycloak identity is already linked to another user")

    user.last_login = datetime.utcnow()
    user.name = user.name or str(name or email)
    user.social_provider = "keycloak"
    if email_verified and not user.is_verified:
        user.is_verified = True
    if not user.keycloak_id or str(user.keycloak_id).startswith("google_"):
        user.keycloak_id = str(keycloak_sub)
    db.add(user)
    db.commit()
    db.refresh(user)
    _ensure_workspace(db, user)
    return user

@router.post("/auth/social-login")
async def social_login(
    login_data: schemas.SocialLoginRequest,
    response: Response = None,
    db: Session = Depends(get_db),
):
    """Handle social login (Google, etc.) and create/login user"""
    
    print(f"DEBUG: Social login attempt with provider: {login_data.provider}")
    print(f"DEBUG: Token length: {len(login_data.token) if login_data.token else 'None'}")
    
    if login_data.provider != "google":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Google login is currently supported"
        )
    
    # Verify Google ID token
    try:
        google_user_info = verify_google_id_token(login_data.token)
        print(f"DEBUG: Google user info received: {google_user_info.get('email', 'No email')}")
    except Exception as e:
        print(f"DEBUG: Google token verification failed: {str(e)}")
        raise
    
    # Extract user information
    email = google_user_info.get("email")
    name = google_user_info.get("name", "")
    google_id = google_user_info.get("sub")
    
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email not provided by Google"
        )
    
    # Find or create user
    user = db.query(models.User).filter(models.User.email == email).first()
    
    if not user:
        # Create new user
        user = models.User(
            email=email,
            name=name,
            keycloak_id=f"google_{google_id}",  # Use Google ID as Keycloak ID for now
            social_provider="google",
            is_active=True,
            is_verified=True,  # Social logins are considered verified
            role="user",
            last_login=datetime.utcnow()
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        
        # Create personal workspace
        workspace = models.Workspace(
            name=f"{name}'s Workspace",
            owner_id=user.id,
            is_active=True
        )
        db.add(workspace)
        db.commit()
        db.refresh(workspace)
        
        # Assign user to their workspace
        user.workspace_id = workspace.id
        db.add(user)
        db.commit()
        db.refresh(user)
        
    else:
        # Update existing user
        user.social_provider = "google"
        user.last_login = datetime.utcnow()
        if not user.is_verified:
            user.is_verified = True
        if not user.keycloak_id:
            user.keycloak_id = f"google_{google_id}"
        
        # Ensure user has a workspace
        if not user.workspace_id:
            workspace = models.Workspace(
                name=f"{user.name}'s Workspace",
                owner_id=user.id,
                is_active=True
            )
            db.add(workspace)
            db.commit()
            db.refresh(workspace)
            
            user.workspace_id = workspace.id
        
        db.add(user)
        db.commit()
        db.refresh(user)
    
    token_response = _token_response_for_user(user, db, response)
    token_response["google_user_info"] = google_user_info
    return token_response


@router.post("/auth/keycloak-callback")
async def keycloak_callback(
    callback_data: schemas.KeycloakCallbackRequest,
    response: Response = None,
    db: Session = Depends(get_db),
):
    """Exchange a Keycloak authorization code and link/login the Versya user."""
    token_url = f"{KEYCLOAK_URL.rstrip('/')}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token"
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": KEYCLOAK_CLIENT_ID,
        "code": callback_data.code,
        "redirect_uri": callback_data.redirect_uri,
    }
    if callback_data.code_verifier:
        token_payload["code_verifier"] = callback_data.code_verifier

    try:
        token_response = requests.post(
            token_url,
            data=token_payload,
            timeout=10,
        )
    except requests.RequestException:
        logger.exception("Failed to contact Keycloak token endpoint")
        raise HTTPException(status_code=502, detail="Failed to contact Keycloak")

    if token_response.status_code != 200:
        try:
            keycloak_error = token_response.json()
        except ValueError:
            keycloak_error = {
                "error": "non_json_response",
                "error_description": token_response.text[:500],
            }

        error_code = keycloak_error.get("error") or "unknown_error"
        error_description = keycloak_error.get("error_description") or ""
        logger.warning(
            "Keycloak authorization code exchange failed: status=%s error=%s description=%s redirect_uri=%s has_code_verifier=%s",
            token_response.status_code,
            error_code,
            error_description,
            callback_data.redirect_uri,
            bool(callback_data.code_verifier),
        )

        detail = f"Keycloak authorization code exchange failed: {error_code}"
        if error_description:
            detail = f"{detail} - {error_description}"
        raise HTTPException(status_code=401, detail=detail)

    keycloak_tokens = token_response.json()
    id_token = keycloak_tokens.get("id_token")
    if not id_token:
        logger.warning(
            "Keycloak code exchange succeeded but no ID token was returned: keys=%s",
            sorted(keycloak_tokens.keys()),
        )
        raise HTTPException(status_code=401, detail="Keycloak did not return an ID token")

    try:
        claims = verify_keycloak_id_token(id_token)
    except HTTPException:
        logger.warning("Keycloak callback failed while validating returned ID token")
        raise

    user = _link_or_create_keycloak_user(db, claims)
    return _token_response_for_user(user, db, response)

def get_current_user_from_token(token: str, db: Session):
    """Extract user from JWT token"""
    try:
        payload = pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        user = db.query(models.User).filter(models.User.id == int(user_id)).first()
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        
        return user
    except pyjwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")

@router.post("/auth/refresh")
async def refresh_token(
    request: Request,
    response: Response,
    body: Optional[schemas.RefreshTokenRequest] = None,
    db: Session = Depends(get_db),
):
    """Exchange a valid refresh token for a new access + refresh token pair.

    The refresh token is read from the HttpOnly cookie first, falling back
    to the JSON body for backward compatibility.
    """
    raw_token = get_refresh_token_from_request(request, body)
    if not raw_token:
        raise HTTPException(status_code=401, detail="No refresh token provided")

    token_hash = _hash_token(raw_token)
    rt = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == token_hash,
    ).first()

    if not rt or rt.is_revoked or rt.expires_at < datetime.utcnow():
        # Return JSONResponse directly so the Set-Cookie (clear) header is preserved
        resp = JSONResponse(status_code=401, content={"detail": "Invalid or expired refresh token"})
        clear_refresh_token_cookie(resp)
        return resp

    user = db.query(models.User).filter(models.User.id == rt.user_id).first()
    if not user or not user.is_active:
        resp = JSONResponse(status_code=401, content={"detail": "User not found or inactive"})
        clear_refresh_token_cookie(resp)
        return resp

    # Revoke the old refresh token (rotation)
    rt.is_revoked = True
    db.commit()

    # Issue new tokens
    token_data = {
        "sub": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "workspace_id": user.workspace_id,
    }
    new_access_token = create_access_token(token_data)
    new_refresh_token = _create_refresh_token(user.id, db, device_info=rt.device_info)

    set_refresh_token_cookie(response, new_refresh_token)

    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "is_verified": user.is_verified,
    }


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    authorization: str = Header(None),
    body: Optional[schemas.RefreshTokenRequest] = None,
    db: Session = Depends(get_db),
):
    """Revoke the refresh token on logout and clear the cookie."""
    raw_token = get_refresh_token_from_request(request, body)
    if raw_token:
        _revoke_refresh_token(raw_token, db)

    clear_refresh_token_cookie(response)

    return {"success": True, "message": "Logged out successfully"}


@router.get("/auth/me")
async def get_current_user_info(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
):
    """Get current user information."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    token = authorization.split(" ")[1]
    user = get_current_user_from_token(token, db)
    
    # Get workspace info
    workspace = None
    if user.workspace_id:
        workspace = db.query(models.Workspace).filter(models.Workspace.id == user.workspace_id).first()
    
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "is_verified": user.is_verified,
        "workspace_id": user.workspace_id,
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "owner_id": workspace.owner_id,
            "is_active": workspace.is_active,
            "created_at": workspace.created_at.isoformat(),
            "updated_at": workspace.updated_at.isoformat()
        } if workspace else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None
    }

# Dependency function for authentication
async def get_current_user(
    authorization: str = Header(None),
    db: Session = Depends(get_db)
) -> models.User:
    """Dependency to get current authenticated user"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    
    token = authorization.split(" ")[1]
    return get_current_user_from_token(token, db)
