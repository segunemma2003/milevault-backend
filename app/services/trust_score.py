"""
Bondrova Trust Score — a credit-score-style rating (300–850) for every user, buyer and seller alike,
recalculated whenever something happens on their deals.

  Deal history   35%  completed deals (diminishing returns) + value traded
  Reliability    30%  sellers: on-time delivery, few revisions / invalid-delivery reports
                      buyers: funding before the deadline, not leaving work to auto-release
  Disputes       20%  disputes ruled against you pull it down sharply
  Reviews        10%  1–5 stars from counterparties after completed deals (smoothed)
  Verification    5%  ID, BVN, email, account age

New users start around the low 500s and climb as they complete deals — like building credit.
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

MIN_SCORE, MAX_SCORE = 300, 850
WEIGHTS = {"history": 0.35, "reliability": 0.30, "disputes": 0.20, "reviews": 0.10, "verification": 0.05}
BANDS = [(800, "Excellent"), (740, "Very good"), (670, "Good"), (580, "Fair"), (0, "Building")]
# What each level means, in plain words — shown wherever a score is shown.
BAND_MEANING = {
    "Building": "New on Bondrova or few completed deals so far. Not a red flag — there's just less history to go on, "
                "so a smaller first milestone is sensible.",
    "Fair": "Has completed some deals, but with a few problems such as late deliveries, revisions or a lost dispute. "
            "Check their reviews.",
    "Good": "A solid record of completed deals with few problems.",
    "Very good": "Consistently completes deals on time and gets good reviews.",
    "Excellent": "One of Bondrova's most reliable members — a long record, great reviews and no disputes lost.",
}
REVIEW_PRIOR_MEAN, REVIEW_PRIOR_WEIGHT = 3.5, 3

TIPS = {
    "history": "Complete more deals — each finished deal raises your score.",
    "reliability": "Deliver milestones on time and respond to deliveries quickly; fund deals before the deadline.",
    "disputes": "Resolve issues with your counterparty before they become disputes.",
    "reviews": "Ask your counterparties to rate you when a deal completes.",
    "verification": "Finish KYC (ID, face check and BVN) to max this out.",
}


def band(score: int | None) -> str:
    s = score or MIN_SCORE
    return next(name for floor, name in BANDS if s >= floor)


def meaning(score: int | None) -> str:
    return BAND_MEANING[band(score)]


def _to_usd(db: Session, amount: float, currency: str) -> float:
    if (currency or "USD").upper() == "USD":
        return float(amount or 0)
    try:
        from app.routers.transactions import _mid_rate
        return float(amount or 0) * _mid_rate(db, currency.upper(), "USD")
    except Exception:
        return 0.0


def compute(db: Session, user) -> tuple[int, dict]:
    from app.models.dispute import Dispute
    from app.models.transaction import Milestone, Transaction
    from app.models.trust import DealReview

    uid = user.id
    mine = (Transaction.buyer_id == uid) | (Transaction.seller_id == uid)

    # ── Deal history ────────────────────────────────────────────────────────
    completed = db.query(Transaction).filter(mine, Transaction.status == "completed").all()
    n_done = len(completed)
    volume = sum(_to_usd(db, t.amount, t.currency) for t in completed)
    history = 0.75 * (1 - math.exp(-n_done / 8)) + 0.25 * min(1.0, math.log10(1 + volume) / 5)

    # ── Reliability ─────────────────────────────────────────────────────────
    signals = []  # each 0..1
    sold = (db.query(Milestone).join(Transaction, Milestone.transaction_id == Transaction.id)
            .filter(Transaction.seller_id == uid, Milestone.delivered_at.isnot(None)).all())
    for m in sold:
        on_time = 1.0 if (not m.due_date or m.delivered_at <= m.due_date) else 0.4
        revisions = sum(1 for e in (m.milestone_action_logs or []) if isinstance(e, dict) and "revision" in str(e.get("action", "")))
        s = on_time - 0.15 * min(revisions, 3) - (0.4 if m.invalid_delivery_reported else 0)
        signals.append(max(0.0, s))
    bought = db.query(Transaction).filter(Transaction.buyer_id == uid,
                                          Transaction.status.notin_(("pending_approval", "pending_acceptance", "draft"))).all()
    for t in bought:
        if t.status == "cancelled" and not (t.funded_milestones or 0):
            signals.append(0.2)  # accepted but never funded before the deadline
        else:
            ms = t.milestones or []
            auto = sum(1 for m in ms if (m.feedback or "").startswith("Auto-released"))
            signals.append(max(0.3, 1.0 - 0.25 * auto) if ms else 0.8)
    reliability = (sum(signals) / len(signals)) if signals else 0.5
    if signals:  # little evidence → stay near neutral
        weight = min(1.0, len(signals) / 5)
        reliability = 0.5 * (1 - weight) + reliability * weight

    # ── Disputes ────────────────────────────────────────────────────────────
    disputes = (db.query(Dispute).join(Transaction, Dispute.transaction_id == Transaction.id).filter(mine).all())
    against = 0
    open_against = 0
    for d in disputes:
        role = "buyer" if d.transaction.buyer_id == uid else "seller"
        if d.status in ("resolved", "closed") and d.ruled_against == role:
            against += 1
        elif d.status in ("open", "in_review") and d.raised_by != uid:
            open_against += 1
    dispute_score = max(0.0, 1.0 - 0.35 * against - 0.05 * open_against)

    # ── Reviews ─────────────────────────────────────────────────────────────
    agg = db.query(func.count(DealReview.id), func.coalesce(func.sum(DealReview.rating), 0)).filter(DealReview.reviewee_id == uid).one()
    r_count, r_sum = int(agg[0] or 0), float(agg[1] or 0)
    r_avg = (r_sum + REVIEW_PRIOR_MEAN * REVIEW_PRIOR_WEIGHT) / (r_count + REVIEW_PRIOR_WEIGHT)
    reviews = (r_avg - 1) / 4

    # ── Verification & tenure ───────────────────────────────────────────────
    age_days = (datetime.utcnow() - (user.created_at or datetime.utcnow())).days
    verification = (0.45 * bool(user.is_kyc_verified) + 0.3 * (user.bvn_status == "verified")
                    + 0.1 * bool(user.is_email_verified) + 0.15 * min(1.0, age_days / 180))

    parts = {"history": history, "reliability": reliability, "disputes": dispute_score,
             "reviews": reviews, "verification": verification}
    total = sum(WEIGHTS[k] * max(0.0, min(1.0, v)) for k, v in parts.items())
    score = int(round(MIN_SCORE + (MAX_SCORE - MIN_SCORE) * total))
    breakdown = {
        "factors": {k: {"score": round(max(0.0, min(1.0, v)) * 100), "weight": int(WEIGHTS[k] * 100), "tip": TIPS[k]}
                    for k, v in parts.items()},
        "completed_deals": n_done, "volume_usd": round(volume, 2), "disputes_lost": against,
        "reviews_count": r_count, "reviews_average": round(r_sum / r_count, 2) if r_count else None,
    }
    return score, breakdown


def recompute(db: Session, user, reason: str = "") -> int:
    """Update the user's score (and star rating), log the change, notify on a band change. No commit."""
    from app.models.trust import DealReview, TrustScoreEvent
    if user is None:
        return 0
    old = user.trust_score
    score, breakdown = compute(db, user)
    user.trust_score, user.trust_breakdown, user.trust_updated_at = score, breakdown, datetime.utcnow()
    user.rating = breakdown["reviews_average"] or 0.0
    user.rating_count = breakdown["reviews_count"]
    if old is None or score != old:
        db.add(TrustScoreEvent(user_id=user.id, score=score, change=score - (old or score), reason=(reason or "Updated")[:200]))
    if old is not None and band(old) != band(score):
        from app.services.notification_service import create_notification
        up = score > old
        create_notification(db, user.id, f"Your Trust Score is now {band(score)}",
                            f"Your Bondrova Trust Score {'rose' if up else 'fell'} to {score} ({band(score)})"
                            + (f" — {reason}." if reason else "."), "account")
    return score


def recompute_ids(db: Session, user_ids, reason: str = "") -> None:
    from app.models.user import User
    for uid in {u for u in user_ids if u}:
        recompute(db, db.get(User, uid), reason)


def deal_cap_multiplier(score: int | None) -> float:
    """Share of the platform's max deal size a user may create, by score."""
    s = score or 0
    if s >= 740:
        return 1.0
    if s >= 670:
        return 0.8
    if s >= 580:
        return 0.5
    return 0.25


def summary_line(db: Session, user) -> str:
    """One line about a user for invitations: score, stars, deals, verification — or 'new member'."""
    if user.trust_score is None:
        recompute(db, user, "First calculation")
    b = user.trust_breakdown or {}
    deals = b.get("completed_deals", 0)
    parts = [f"Trust Score {user.trust_score} ({band(user.trust_score)})"]
    if user.rating_count:
        parts.append(f"{float(user.rating or 0):.1f}★ from {int(user.rating_count)} review{'s' if user.rating_count != 1 else ''}")
    parts.append(f"{deals} completed deal{'s' if deals != 1 else ''}" if deals else "new to Bondrova — no completed deals yet")
    parts.append("identity verified" if user.is_kyc_verified else "identity not yet verified")
    return " · ".join(parts) + f". What this means: {meaning(user.trust_score)}"


def public_profile_name(user) -> str:
    last = (user.last_name or "").strip()
    return f"{(user.first_name or '').strip()} {last[:1] + '.' if last else ''}".strip()


def public_profile(db: Session, user) -> dict:
    """What another user may see about this person before dealing with them."""
    if user.trust_score is None:
        recompute(db, user, "First calculation")
    b = user.trust_breakdown or {}
    last = (user.last_name or "").strip()
    return {
        "registered": True,
        "name": f"{(user.first_name or '').strip()} {last[:1] + '.' if last else ''}".strip(),
        "trust_score": user.trust_score, "band": band(user.trust_score), "band_meaning": meaning(user.trust_score),
        "rating": float(user.rating or 0), "rating_count": int(user.rating_count or 0),
        "completed_deals": b.get("completed_deals", 0), "disputes_lost": b.get("disputes_lost", 0),
        "is_kyc_verified": bool(user.is_kyc_verified), "bvn_verified": user.bvn_status == "verified",
        "member_since": user.created_at,
    }
