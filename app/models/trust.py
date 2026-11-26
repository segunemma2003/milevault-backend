import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, text

from app.database import Base


class DealReview(Base):
    """A 1–5 star rating one party leaves the other — after each completed milestone (milestone_id set)
    and once for the whole deal (milestone_id NULL). One per reviewer per milestone / per deal."""
    __tablename__ = "deal_reviews"
    __table_args__ = (
        Index("uq_review_deal", "transaction_id", "reviewer_id", unique=True,
              postgresql_where=text("milestone_id IS NULL"), sqlite_where=text("milestone_id IS NULL")),
        Index("uq_review_milestone", "milestone_id", "reviewer_id", unique=True,
              postgresql_where=text("milestone_id IS NOT NULL"), sqlite_where=text("milestone_id IS NOT NULL")),
    )

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
    milestone_id = Column(String, ForeignKey("milestones.id", ondelete="CASCADE"), nullable=True, index=True)
    reviewer_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reviewee_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    reviewee_role = Column(String(10), nullable=False)  # buyer | seller
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TrustScoreEvent(Base):
    """History of a user's Trust Score — what changed it and by how much."""
    __tablename__ = "trust_score_events"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    change = Column(Integer, nullable=False)
    reason = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
