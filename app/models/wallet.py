import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class WalletBalance(Base):
    __tablename__ = "wallet_balances"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    currency = Column(String(10), nullable=False)
    amount = Column(Float, default=0.0)
    pending_amount = Column(Float, default=0.0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="wallet_balances")


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(20), nullable=False)
    # deposit | withdrawal | payment | refund | conversion | escrow_in | escrow_out
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False)
    status = Column(String(20), default="pending")  # pending | completed | failed | cancelled | processing
    description = Column(String(500), nullable=True)
    transaction_id = Column(String, ForeignKey("transactions.id", ondelete="SET NULL"), nullable=True)
    method = Column(String(50), nullable=True)  # bank | card | paypal | crypto
    reference = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="wallet_transactions")
    linked_transaction = relationship("Transaction", back_populates="wallet_transactions")
