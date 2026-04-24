"""Per-user limits derived from reputation + platform caps (transaction size, withdrawal friction signals)."""
from typing import Any, Dict, Optional

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


def max_deal_amount_for_parties(db: Session, buyer: User, seller: Optional[User]) -> float:
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


def public_deal_limits_for_user(db: Session, user: User) -> Dict[str, Any]:
    """
    User-facing deal cap (no internal withdrawal heuristics or fraud-rule details).
    """
    ps = _settings(db)
    platform_cap = float(ps.max_transaction_amount) if ps and ps.max_transaction_amount else None
    cap = max_new_transaction_amount_for_user(db, user)
    r = float(user.rating or 0)
    dr = float(user.dispute_rate or 0)
    cr = float(user.completion_rate or 0)
    if r >= 4.5 and dr < 8.0:
        band = "trusted"
    elif r < 3.0 or dr > 35.0:
        band = "restricted"
    elif dr > 18.0:
        band = "elevated_review"
    elif r < 4.0:
        band = "limited"
    else:
        band = "standard"
    return {
        "max_new_deal_amount_as_party": cap,
        "platform_max_transaction_amount": round(platform_cap, 2) if platform_cap else None,
        "rating_average": r,
        "completion_rate_percent": cr,
        "dispute_rate_percent": dr,
        "trust_band": band,
        "explanation": (
            "When you are buyer or seller on a new deal, the maximum amount is capped using your "
            "rating, completion rate, and dispute rate relative to the platform maximum. "
            "This is the effective ceiling for your side of the deal."
        ),
    }
