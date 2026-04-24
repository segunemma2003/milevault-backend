"""Funding deadline and buyer review (auto-release) windows — admin-configurable with env fallbacks."""
from sqlalchemy.orm import Session
from app.config import settings
from app.models.currency import PlatformSettings


def get_funding_deadline_days(db: Session) -> int:
    ps = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if ps is not None and getattr(ps, "funding_deadline_days", None) is not None:
        return max(1, min(366, int(ps.funding_deadline_days)))
    return max(1, min(366, int(settings.FUNDING_DEADLINE_DAYS)))


def get_auto_release_days(db: Session) -> int:
    ps = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if ps is not None and getattr(ps, "auto_release_days", None) is not None:
        return max(3, min(7, int(ps.auto_release_days)))
    return settings.auto_release_days_clamped


def get_invite_expiry_days(db: Session) -> int:
    ps = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if ps is not None and getattr(ps, "invite_expiry_days", None) is not None:
        return max(7, min(180, int(ps.invite_expiry_days)))
    return 30


def get_stale_activity_warn_days(db: Session) -> int:
    ps = db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()
    if ps is not None and getattr(ps, "stale_activity_warn_days", None) is not None:
        return max(30, min(730, int(ps.stale_activity_warn_days)))
    return 90
