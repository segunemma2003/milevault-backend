import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Boolean, DateTime, Text, ForeignKey, JSON, Integer
from sqlalchemy.orm import relationship
from app.database import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    type = Column(String(20), nullable=False, default="one_time")  # one_time | milestone
    status = Column(String(30), nullable=False, default="pending_approval")
    # draft | pending_approval | approved | in_progress | completed | cancelled | disputed
    buyer_id = Column(String, ForeignKey("users.id"), nullable=False)
    seller_id = Column(String, ForeignKey("users.id"), nullable=True)
    counterparty_email = Column(String(255), nullable=True)
    supporting_url = Column(String, nullable=True)
    contract_signed = Column(Boolean, default=False)
    expected_completion_date = Column(DateTime, nullable=True)
    service_fee_payment = Column(String(10), default="buyer")  # buyer | seller | split
    buyer_fee_ratio = Column(Float, default=100.0)
    seller_fee_ratio = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    project_url = Column(String, nullable=True)
    milestones_count = Column(Integer, default=0)
    completed_milestones = Column(Integer, default=0)
    terms_accepted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    buyer = relationship("User", foreign_keys=[buyer_id], back_populates="transactions_as_buyer")
    seller = relationship("User", foreign_keys=[seller_id], back_populates="transactions_as_seller")
    milestones = relationship("Milestone", back_populates="transaction", cascade="all, delete-orphan")
    disputes = relationship("Dispute", back_populates="transaction", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="transaction", cascade="all, delete-orphan")
    wallet_transactions = relationship("WalletTransaction", back_populates="linked_transaction")
    agent_request = relationship("AgentRequest", back_populates="transaction", uselist=False, cascade="all, delete-orphan")


class Milestone(Base):
    __tablename__ = "milestones"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=True)
    status = Column(String(20), default="pending")  # pending | in_progress | completed | disputed
    due_date = Column(DateTime, nullable=True)
    completed_date = Column(DateTime, nullable=True)
    expectations = Column(Text, nullable=True)
    feedback = Column(Text, nullable=True)
    percentage_of_total = Column(Float, nullable=True)
    attachments = Column(JSON, default=list)
    supporting_documents = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="milestones")
