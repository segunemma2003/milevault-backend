"""Per-user limits derived from reputation + platform caps (transaction size, withdrawal friction signals)."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.currency import PlatformSettings
from app.models.user import User


def _settings(db: Session) -> Optional[PlatformSettings]:
    return db.query(PlatformSettings).filter(PlatformSettings.id == "default").first()


def max_new_transaction_amount_for_user(db: Session, user: User) -> float:
    """
    Platform max_transaction_amount (if set) is scaled by trust:
    - High rating + low dispute rate: full cap
    - Low rating or high dispute rate: reduced cap
    """
    ps = _settings(db)
    base = float(ps.max_transaction_amount) if ps and ps.max_transaction_amount else 1_000_000.0
    base = max(base, 100.0)
    r = float(user.rating or 0)
    dr = float(user.dispute_rate or 0)
    if r >= 4.5 and dr < 8.0:
        mult = 1.0
    elif r < 3.0 or dr > 35.0:
        mult = 0.12
    elif dr > 18.0:
        mult = 0.45
    elif r < 4.0:
        mult = 0.65
    else:
        mult = 1.0
    return round(base * mult, 2)


def max_deal_amount_for_parties(db: Session, buyer: User, seller: User | None) -> float:
    """Stricter of buyer/seller caps when both exist."""
    b_cap = max_new_transaction_amount_for_user(db, buyer)
    if not seller:
        return b_cap
    s_cap = max_new_transaction_amount_for_user(db, seller)
    return round(min(b_cap, s_cap), 2)


def withdrawal_flagged_high_risk(user: User, amount: float) -> bool:
    """Heuristic for admin queue prioritization (all withdrawals remain pending admin)."""
    rs = float(user.risk_score or 0)
    dr = float(user.dispute_rate or 0)
    if amount >= 25_000:
        return True
    if rs >= 45:
        return True
    if dr > 22 and amount >= 3_000:
        return True
    if dr > 35 and amount >= 500:
        return True
    return False
