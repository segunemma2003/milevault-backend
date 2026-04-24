import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, ForeignKey, JSON
from sqlalchemy.orm import relationship
from app.database import Base


class Dispute(Base):
    __tablename__ = "disputes"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    milestone_id = Column(String, ForeignKey("milestones.id", ondelete="SET NULL"), nullable=True)
    raised_by = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    evidence_urls = Column(JSON, default=list)  # submitted at creation (S3 keys or https URLs)
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=False)
    reason = Column(String(50), nullable=False)
    # not_as_described | quality_issues | incomplete_delivery | deadline_missed
    # communication_issues | terms_violation | other
    suggested_resolution = Column(Text, nullable=True)
    status = Column(String(20), default="open")  # open | in_review | resolved | closed
    resolution = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="disputes")
    raised_by_user = relationship("User", back_populates="disputes")
    documents = relationship("DisputeDocument", back_populates="dispute", cascade="all, delete-orphan")


class DisputeDocument(Base):
    __tablename__ = "dispute_documents"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dispute_id = Column(String, ForeignKey("disputes.id", ondelete="CASCADE"), nullable=False)
    file_url = Column(String, nullable=False)
    file_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    dispute = relationship("Dispute", back_populates="documents")
