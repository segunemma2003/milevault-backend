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
    currency = Column(String(10), nullable=False, default="NGN")
    type = Column(String(20), nullable=False, default="milestone")  # one_time | milestone
    status = Column(String(30), nullable=False, default="pending_acceptance")
    # State machine:
    # draft → pending_acceptance → funding_in_progress → partially_funded
    # → active → in_progress → delivered → under_review → completed
    # → disputed → refunded | cancelled
    buyer_id = Column(String, ForeignKey("users.id"), nullable=False)
    seller_id = Column(String, ForeignKey("users.id"), nullable=True)
    # User who created the invitation (accept/decline is the other party; cancel is initiator)
    initiated_by_user_id = Column(String, ForeignKey("users.id"), nullable=True)
    counterparty_email = Column(String(255), nullable=True)
    supporting_url = Column(String, nullable=True)
    contract_signed = Column(Boolean, default=False)
    terms_accepted = Column(Boolean, default=False)
    expected_completion_date = Column(DateTime, nullable=True)
    funding_deadline = Column(DateTime, nullable=True)   # auto-cancel if no milestone funded
    service_fee_payment = Column(String(10), default="buyer")  # buyer | seller | split
    buyer_fee_ratio = Column(Float, default=100.0)
    seller_fee_ratio = Column(Float, default=0.0)
    notes = Column(Text, nullable=True)
    project_url = Column(String, nullable=True)
    milestones_count = Column(Integer, default=0)
    funded_milestones = Column(Integer, default=0)
    completed_milestones = Column(Integer, default=0)
    # Exchange rate locked at creation
    locked_exchange_rate = Column(Float, nullable=True)
    locked_rate_from = Column(String(10), nullable=True)
    locked_rate_to = Column(String(10), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stale_activity_warn_sent_at = Column(DateTime, nullable=True)
    last_reminded_at = Column(DateTime, nullable=True)

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
    status = Column(String(20), default="pending")
    # pending → partially_funded → funded → in_progress → under_review → completed
    # delivered (legacy) | revision_requested | disputed
    funded_amount = Column(Float, default=0.0)    # how much has been escrowed for this milestone
    is_funded = Column(Boolean, default=False)    # True when funded_amount >= amount
    funding_deadline = Column(DateTime, nullable=True)
    due_date = Column(DateTime, nullable=True)
    completed_date = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    auto_release_at = Column(DateTime, nullable=True)  # auto-approve if buyer inactive
    delivery_title = Column(String(300), nullable=True)
    delivery_note = Column(Text, nullable=True)         # seller's delivery description (required with proof)
    delivery_attachments = Column(JSON, default=list)   # file keys / URLs from upload flow
    delivery_external_links = Column(JSON, default=list)  # e.g. GitHub, Drive
    delivery_version_notes = Column(Text, nullable=True)
    invalid_delivery_reported = Column(Boolean, default=False)
    invalid_delivery_report_note = Column(Text, nullable=True)
    milestone_action_logs = Column(JSON, default=list)   # append-only audit: {at, user_id, action, detail}
    expectations = Column(Text, nullable=True)
    feedback = Column(Text, nullable=True)
    revision_note = Column(Text, nullable=True)
    percentage_of_total = Column(Float, nullable=True)
    attachments = Column(JSON, default=list)
    supporting_documents = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    transaction = relationship("Transaction", back_populates="milestones")
