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
