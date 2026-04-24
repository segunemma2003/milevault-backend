import os
import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.user import UserOut, UpdateProfileRequest, UserSettingsOut, UpdateSettingsRequest
from app.models.user import User, UserSettings
from app.dependencies import get_current_user
from app.config import settings

router = APIRouter(prefix="/users", tags=["users"])


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
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    allowed_types = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    ext = file.filename.split(".")[-1] if "." in file.filename else "jpg"
    filename = f"avatar_{current_user.id}_{uuid.uuid4().hex}.{ext}"
    upload_path = os.path.join(settings.UPLOAD_DIR, "avatars")
    os.makedirs(upload_path, exist_ok=True)

    file_path = os.path.join(upload_path, filename)
    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    current_user.avatar_url = f"/uploads/avatars/{filename}"
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
