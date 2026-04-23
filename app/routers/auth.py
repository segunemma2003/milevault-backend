"""
Auth router — register, login, refresh, logout, change-password.
Tokens are set as HttpOnly cookies AND returned in the response body
for flexibility (mobile apps / API clients can use the body).
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.auth import RegisterRequest, LoginRequest, TokenResponse, RefreshRequest, ChangePasswordRequest
from app.schemas.user import UserOut
from app.models.user import User, UserSettings
from app.models.wallet import WalletBalance
from app.services.auth_service import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token
)
from app.dependencies import get_current_user, ACCESS_COOKIE, REFRESH_COOKIE
from app.config import settings

router = APIRouter(prefix="/auth", tags=["Auth"])

DEFAULT_CURRENCIES = ["USD", "EUR", "GBP"]

COOKIE_OPTS = dict(
    httponly=True,
    secure=settings.COOKIE_SECURE,
    samesite=settings.COOKIE_SAMESITE,
    domain=settings.COOKIE_DOMAIN or None,
)


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str):
    response.set_cookie(
        key=ACCESS_COOKIE,
        value=access_token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        **COOKIE_OPTS,
    )
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/api/v1/auth/refresh",   # Restrict refresh cookie to refresh endpoint
        **COOKIE_OPTS,
    )


def _clear_auth_cookies(response: Response):
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth/refresh")


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "EMAIL_ALREADY_REGISTERED",
                "message": f"The email '{payload.email}' is already registered. Please log in or use a different email.",
            },
        )

    if len(payload.password) < 8:
        raise HTTPException(
            status_code=422,
            detail={"error": "WEAK_PASSWORD", "message": "Password must be at least 8 characters long."},
        )

    user = User(
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        email=payload.email.lower().strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
        country_code=getattr(payload, "country_code", None),
    )
    db.add(user)
    db.flush()

    db.add(UserSettings(user_id=user.id))
    for currency in DEFAULT_CURRENCIES:
        db.add(WalletBalance(user_id=user.id, currency=currency, amount=0.0))

    db.commit()
    db.refresh(user)

    access_token = create_access_token({"sub": user.id})
    refresh_token = create_refresh_token({"sub": user.id})
    _set_auth_cookies(response, access_token, refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()

    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "INVALID_CREDENTIALS",
                "message": "The email or password you entered is incorrect. Please try again.",
            },
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "ACCOUNT_DEACTIVATED",
                "message": "Your account has been deactivated. Please contact support@milevault.com.",
            },
        )

    access_token = create_access_token({"sub": user.id})
    refresh_token = create_refresh_token({"sub": user.id})
    _set_auth_cookies(response, access_token, refresh_token)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenResponse)
def refresh_token(request: Request, response: Response, db: Session = Depends(get_db),
                  body: RefreshRequest = None):
    # Accept token from cookie OR request body
    token = request.cookies.get(REFRESH_COOKIE)
    if not token and body:
        token = body.refresh_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "MISSING_REFRESH_TOKEN", "message": "No refresh token provided."},
        )

    decoded = decode_token(token)
    if not decoded or decoded.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "INVALID_REFRESH_TOKEN", "message": "The refresh token is invalid or has expired. Please log in again."},
        )

    user = db.query(User).filter(User.id == decoded["sub"], User.is_active == True).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "USER_NOT_FOUND", "message": "Account associated with this token no longer exists."},
        )

    access_token = create_access_token({"sub": user.id})
    new_refresh = create_refresh_token({"sub": user.id})
    _set_auth_cookies(response, access_token, new_refresh)
    return TokenResponse(access_token=access_token, refresh_token=new_refresh)


@router.post("/logout")
def logout(response: Response):
    _clear_auth_cookies(response)
    return {"message": "Logged out successfully."}


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "WRONG_CURRENT_PASSWORD",
                "message": "The current password you entered is incorrect.",
            },
        )
    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code=422,
            detail={"error": "WEAK_PASSWORD", "message": "New password must be at least 8 characters long."},
        )
    if payload.new_password == payload.current_password:
        raise HTTPException(
            status_code=422,
            detail={"error": "SAME_PASSWORD", "message": "New password must differ from the current password."},
        )

    current_user.hashed_password = hash_password(payload.new_password)
    db.commit()

    # Force re-login by clearing cookies
    _clear_auth_cookies(response)
    return {"message": "Password updated successfully. Please log in with your new password."}
