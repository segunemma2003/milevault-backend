"""
Auth dependencies — supports BOTH cookie-based and Bearer token auth.
Priority: Cookie > Bearer header (for backward compat with mobile/API clients).
"""
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.auth_service import decode_token
from app.models.user import User

bearer_scheme = HTTPBearer(auto_error=False)

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


def _extract_token(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials],
) -> Optional[str]:
    """Try cookie first, then Authorization header."""
    cookie_token = request.cookies.get(ACCESS_COOKIE)
    if cookie_token:
        return cookie_token
    if credentials and credentials.credentials:
        return credentials.credentials
    return None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    token = _extract_token(request, credentials)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired or the token is invalid. Please log in again.",
        )
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type. Use the access token, not the refresh token.",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token — missing subject.")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Account not found.")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Contact support at support@milevault.com.",
        )
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required. This action is restricted to platform administrators.",
        )
    return current_user


def get_current_approved_agent(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_agent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approved agent access required. Your agent account may be pending or not yet registered.",
        )
    return current_user


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Returns user if authenticated, None otherwise (for public endpoints)."""
    try:
        return get_current_user(request, credentials, db)
    except HTTPException:
        return None
