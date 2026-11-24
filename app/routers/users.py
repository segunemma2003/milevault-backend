import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.user import UserOut, UpdateProfileRequest, UserSettingsOut, UpdateSettingsRequest
from app.models.user import User, UserSettings
from app.dependencies import get_current_user
from app.config import settings

router = APIRouter(prefix="/users", tags=["users"])


def _public_api_base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    host = request.url.hostname or ""
    if base.startswith("http://") and host not in ("localhost", "127.0.0.1"):
        base = "https://" + base[len("http://"):]  # Railway terminates TLS at the proxy
    return base


async def _read_upload_request(request: Request):
    """Accept either JSON {"s3_key": ...} (file already PUT to the bucket) or a multipart file."""
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        body = await request.json()
        return "key", body
    form = await request.form()
    return "file", form


@router.get("/me", response_model=UserOut)
def get_profile(current_user: User = Depends(get_current_user)):
    return current_user


@router.get("/me/deal-limits")
def get_my_deal_limits(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Max deal size when you are a party, from reputation + platform cap (no internal fraud-rule detail)."""
    from app.services.reputation_limits import public_deal_limits_for_user

    return public_deal_limits_for_user(db, current_user)


@router.put("/me", response_model=UserOut)
def update_profile(
    payload: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(current_user, field, value)
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/me/avatar", response_model=UserOut)
async def upload_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set the avatar from an s3_key (browser already uploaded it) or from a multipart `file`."""
    from app.services.s3_service import owns_key, put_bytes, _build_s3_key

    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    kind, data = await _read_upload_request(request)
    if kind == "key":
        s3_key = str(data.get("s3_key") or "")
        if not owns_key(s3_key, "avatars", str(current_user.id)):
            raise HTTPException(status_code=400, detail="Invalid avatar upload key")
    else:
        file = data.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="No file provided")
        if file.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Only image files are allowed")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image too large (max 10MB)")
        s3_key = _build_s3_key("avatars", file.filename or "avatar.jpg", str(current_user.id))
        put_bytes(s3_key, content, file.content_type)

    if settings.s3_enabled:
        current_user.avatar_url = f"{_public_api_base(request)}/api/v1/uploads/avatar/{s3_key}"
    else:
        current_user.avatar_url = f"{_public_api_base(request)}/api/v1/uploads/local/{s3_key}"
    db.commit()
    db.refresh(current_user)
    return current_user


@router.get("/me/settings", response_model=UserSettingsOut)
def get_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.settings:
        s = UserSettings(user_id=current_user.id)
        db.add(s)
        db.commit()
        db.refresh(current_user)
    return current_user.settings


@router.put("/me/settings", response_model=UserSettingsOut)
def update_settings(
    payload: UpdateSettingsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.settings:
        s = UserSettings(user_id=current_user.id)
        db.add(s)
        db.flush()
        db.refresh(current_user)

    for field, value in payload.model_dump(exclude_none=True).items():
        setattr(current_user.settings, field, value)
    db.commit()
    db.refresh(current_user.settings)
    return current_user.settings
