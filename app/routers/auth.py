"""
Auth router — register, login, refresh, logout, change-password.
Tokens are set as HttpOnly cookies AND returned in the response body
for flexibility (mobile apps / API clients can use the body).
"""
from datetime import datetime, timedelta
import hashlib
import secrets
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.auth import (
    RegisterRequest, LoginRequest, TokenResponse, RefreshRequest, ChangePasswordRequest,
    RegisterResponse, VerifyEmailRequest, ResendVerificationRequest,
    ForgotPasswordRequest, ResetPasswordRequest,
)
from app.schemas.user import UserOut
from app.models.user import User, UserSettings
from app.models.wallet import WalletBalance
from app.models.currency import PlatformSettings
from app.services.auth_service import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token
)
from app.dependencies import get_current_user, ACCESS_COOKIE, REFRESH_COOKIE
from app.config import settings

router = APIRouter(prefix="/auth", tags=["Auth"])

DEFAULT_CURRENCIES = ["USD", "EUR", "GBP"]
EMAIL_VERIFICATION_TTL_MINUTES = 15

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


def _hash_email_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_email_verification_token(user: User) -> str:
    raw = secrets.token_urlsafe(32)
    user.email_verification_token = _hash_email_token(raw)
    user.email_verification_expires_at = datetime.utcnow() + timedelta(minutes=EMAIL_VERIFICATION_TTL_MINUTES)
    return raw


def _issue_password_reset_token(user: User) -> str:
    raw = secrets.token_urlsafe(32)
    user.password_reset_token = _hash_email_token(raw)
    user.password_reset_expires_at = datetime.utcnow() + timedelta(hours=1)
    return raw


def _send_verification_email(user: User, raw_token: str) -> None:
    verify_url = f"{settings.FRONTEND_URL}/verify-email?token={raw_token}"
    body_html = (
        f"<h2>Verify your MileVault email</h2>"
        f"<p>Hi {user.first_name},</p>"
        f"<p>Click the button below to verify your email address and activate your account access.</p>"
        f"<p><a href='{verify_url}' style='display:inline-block;padding:10px 16px;background:#111827;color:#fff;text-decoration:none;border-radius:6px;'>Verify Email</a></p>"
        f"<p>Or copy this link:</p><p>{verify_url}</p>"
        f"<p>This link expires in {EMAIL_VERIFICATION_TTL_MINUTES} minutes.</p>"
    )
    try:
        from app.services.tasks import send_notification_email
        send_notification_email.delay(
            to_email=user.email,
            subject="Verify your MileVault email",
            body_html=body_html,
        )
    except Exception:
        # Non-blocking: account is created even if mail worker is temporarily unavailable.
        pass


def _email_verification_required(db: Session) -> bool:
    settings_row = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if not settings_row:
        return True
    return bool(getattr(settings_row, "require_email_verification", True))


def _delete_if_unverified_expired(db: Session, user: User) -> bool:
    if user.is_email_verified:
        return False
    if not user.email_verification_expires_at:
        return False
    if user.email_verification_expires_at >= datetime.utcnow():
        return False
    db.delete(user)
    db.commit()
    return True


def _send_password_reset_email(user: User, raw_token: str) -> None:
    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"
    body_html = (
        f"<h2>Reset your MileVault password</h2>"
        f"<p>Hi {user.first_name},</p>"
        f"<p>Click below to set a new password:</p>"
        f"<p><a href='{reset_url}' style='display:inline-block;padding:10px 16px;background:#111827;color:#fff;text-decoration:none;border-radius:6px;'>Reset Password</a></p>"
        f"<p>This link expires in 1 hour.</p>"
    )
    try:
        from app.services.tasks import send_notification_email
        send_notification_email.delay(
            to_email=user.email,
            subject="Reset your MileVault password",
            body_html=body_html,
        )
    except Exception:
        pass


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
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

    require_email_verification = _email_verification_required(db)
    user = User(
        first_name=payload.first_name.strip(),
        last_name=payload.last_name.strip(),
        email=payload.email.lower().strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
        country_code=getattr(payload, "country_code", None),
        is_email_verified=not require_email_verification,
    )
    raw_token = _issue_email_verification_token(user) if require_email_verification else None
    db.add(user)
    db.flush()

    db.add(UserSettings(user_id=user.id))
    for currency in DEFAULT_CURRENCIES:
        db.add(WalletBalance(user_id=user.id, currency=currency, amount=0.0))

    db.commit()
    db.refresh(user)
    if require_email_verification and raw_token:
        _send_verification_email(user, raw_token)
        try:
            from app.services.notification_service import create_notification
            create_notification(
                db,
                user.id,
                "Verification Email Sent",
                "We've sent a verification email to your inbox. Verify within 15 minutes to activate your account.",
                "account",
            )
            db.commit()
        except Exception:
            pass
    _clear_auth_cookies(response)
    return RegisterResponse(
        message=(
            "Account created. Please verify your email to sign in."
            if require_email_verification
            else "Account created successfully. You can now sign in."
        ),
        email=user.email,
        requires_email_verification=require_email_verification,
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, response: Response, request: Request, db: Session = Depends(get_db)):
    require_email_verification = _email_verification_required(db)
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
    if require_email_verification and _delete_if_unverified_expired(db, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "EMAIL_NOT_VERIFIED_ACCOUNT_DELETED",
                "message": "Email was not verified within 15 minutes, so this account was deleted. Please register again.",
            },
        )
    if require_email_verification and not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "EMAIL_NOT_VERIFIED",
                "message": "Please verify your email before signing in. Check your inbox for a verification link.",
            },
        )

    access_token = create_access_token({"sub": user.id})
    refresh_token = create_refresh_token({"sub": user.id})
    _set_auth_cookies(response, access_token, refresh_token)

    # Security alert: notify user of new login
    try:
        from app.services.notification_service import create_notification
        ip = request.client.host if request.client else "unknown"
        create_notification(
            db, user.id,
            "New Login Detected",
            f"Your account was accessed from IP {ip}. If this wasn't you, change your password immediately.",
            "security",
        )
        db.commit()
    except Exception:
        pass

    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/verify-email")
def verify_email(payload: VerifyEmailRequest, db: Session = Depends(get_db)):
    if not _email_verification_required(db):
        return {"message": "Email verification is currently disabled by admin."}
    hashed = _hash_email_token(payload.token.strip())
    user = db.query(User).filter(User.email_verification_token == hashed).first()
    if not user:
        raise HTTPException(
            status_code=400,
            detail={"error": "INVALID_VERIFICATION_TOKEN", "message": "Verification link is invalid."},
        )
    if user.is_email_verified:
        return {"message": "Email already verified."}
    if not user.email_verification_expires_at or user.email_verification_expires_at < datetime.utcnow():
        db.delete(user)
        db.commit()
        raise HTTPException(
            status_code=400,
            detail={
                "error": "VERIFICATION_TOKEN_EXPIRED_ACCOUNT_DELETED",
                "message": "Verification link expired and the unverified account was deleted. Please register again.",
            },
        )
    user.is_email_verified = True
    user.email_verification_token = None
    user.email_verification_expires_at = None
    db.commit()
    return {"message": "Email verified successfully. You can now sign in."}


@router.post("/resend-verification")
def resend_verification(payload: ResendVerificationRequest, db: Session = Depends(get_db)):
    if not _email_verification_required(db):
        return {"message": "Email verification is currently disabled by admin."}
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if not user:
        # Do not leak account existence.
        return {"message": "If an account exists for this email, a verification link has been sent."}
    if _delete_if_unverified_expired(db, user):
        return {"message": "Your previous unverified account expired and was deleted. Please register again."}
    if user.is_email_verified:
        return {"message": "Email is already verified. Please sign in."}
    raw_token = _issue_email_verification_token(user)
    db.commit()
    _send_verification_email(user, raw_token)
    try:
        from app.services.notification_service import create_notification
        create_notification(
            db,
            user.id,
            "Verification Email Sent",
            "A new verification email has been sent. Verify within 15 minutes to keep your account active.",
            "account",
        )
        db.commit()
    except Exception:
        pass
    return {"message": "Verification email sent. Check your inbox."}


@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower().strip()).first()
    if user and user.is_active:
        raw = _issue_password_reset_token(user)
        db.commit()
        _send_password_reset_email(user, raw)
    return {"message": "If an account exists for this email, a reset link has been sent."}


@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, response: Response, db: Session = Depends(get_db)):
    if len(payload.new_password) < 8:
        raise HTTPException(
            status_code=422,
            detail={"error": "WEAK_PASSWORD", "message": "New password must be at least 8 characters long."},
        )
    hashed = _hash_email_token(payload.token.strip())
    user = db.query(User).filter(User.password_reset_token == hashed).first()
    if not user:
        raise HTTPException(status_code=400, detail={"error": "INVALID_RESET_TOKEN", "message": "Reset token is invalid."})
    if not user.password_reset_expires_at or user.password_reset_expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail={"error": "RESET_TOKEN_EXPIRED", "message": "Reset token has expired."})
    user.hashed_password = hash_password(payload.new_password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    db.commit()
    _clear_auth_cookies(response)
    return {"message": "Password reset successful. Please sign in."}


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
