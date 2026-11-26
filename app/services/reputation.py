"""
Portable transaction reputation: a public, shareable summary of a user's track record.

Kept in three clearly separate groups so nobody mistakes one for another:
  • verified  — facts from Bondrova's own records (deals completed and their value, identity checks,
                time on the platform). Nobody can type these in.
  • ratings   — what counterparties said (subjective), split by role.
  • disputes  — how many were raised on their deals, how many are settled, and how many were
                decided against them.
No email, phone or amounts of individual deals are shown.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.dispute import Dispute
from app.models.transaction import Transaction
from app.models.trust import DealReview
from app.models.user import User


def public_profile(db: Session, user: User) -> dict:
    from app.services import trust_score

    completed = db.query(Transaction).filter(
        Transaction.status == "completed",
        or_(Transaction.buyer_id == user.id, Transaction.seller_id == user.id))
    as_buyer = completed.filter(Transaction.buyer_id == user.id).count()
    as_seller = completed.filter(Transaction.seller_id == user.id).count()
    value = completed.with_entities(func.coalesce(func.sum(Transaction.amount), 0.0)).scalar() or 0.0

    def ratings(role: str) -> dict:
        avg, n = (db.query(func.avg(DealReview.rating), func.count(DealReview.id))
                  .filter(DealReview.reviewee_id == user.id, DealReview.reviewee_role == role).one())
        return {"average": round(float(avg), 2) if avg is not None else None, "count": int(n or 0)}

    deal_ids = db.query(Transaction.id).filter(or_(Transaction.buyer_id == user.id, Transaction.seller_id == user.id))
    disputes = db.query(Dispute).filter(Dispute.transaction_id.in_(deal_ids.subquery().select()))
    total_disputes = disputes.count()
    settled = disputes.filter(Dispute.status.in_(("resolved", "closed"))).count()
    against = 0
    for d in disputes.filter(Dispute.ruled_against.in_(("buyer", "seller"))).all():
        tx = d.transaction
        if tx and ((d.ruled_against == "buyer" and tx.buyer_id == user.id) or
                   (d.ruled_against == "seller" and tx.seller_id == user.id)):
            against += 1

    created = user.created_at or datetime.utcnow()
    return {
        "id": user.id,
        "display_name": f"{user.first_name} {(user.last_name or '')[:1]}.".strip(),
        "business_name": getattr(user, "business_name", None),
        "avatar_url": user.avatar_url,
        "trust": {"score": user.trust_score, "band": trust_score.band(user.trust_score),
                  "meaning": trust_score.meaning(user.trust_score)},
        "verified": {
            "completed_deals": as_buyer + as_seller,
            "completed_as_buyer": as_buyer,
            "completed_as_seller": as_seller,
            "completed_value_usd": round(float(value), 2),
            "identity_verified": bool(user.is_kyc_verified),
            "bvn_verified": bool(getattr(user, "bvn_verified_at", None)),
            "email_verified": bool(user.is_email_verified),
            "member_since": created.date().isoformat(),
            "days_on_platform": max(0, (datetime.utcnow() - created).days),
        },
        "ratings": {"as_seller": ratings("seller"), "as_buyer": ratings("buyer")},
        "disputes": {"total": total_disputes, "settled": settled, "open": total_disputes - settled,
                     "decided_against": against},
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
