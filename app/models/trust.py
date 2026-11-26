import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database import Base


class DealReview(Base):
    """A 1–5 star rating one party leaves the other after a completed deal (one each way per deal)."""
    __tablename__ = "deal_reviews"
    __table_args__ = (UniqueConstraint("transaction_id", "reviewer_id", name="uq_review_per_party"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False, index=True)
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
