"""
Per-user payment rails:
  * VirtualAccount       — Paystack Dedicated Virtual Account (NGN bank account in the user's name)
  * UserDepositAddress   — Quidax sub-account deposit address per currency + network
  * PayoutDestination    — the user's verified withdrawal targets (bank account or crypto address)
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, Text, UniqueConstraint

from app.database import Base


class VirtualAccount(Base):
    __tablename__ = "virtual_accounts"
    __table_args__ = (UniqueConstraint("user_id", "provider", "currency", name="uq_virtual_account_user_provider_currency"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(30), nullable=False, default="paystack")
    currency = Column(String(10), nullable=False, default="NGN")
    status = Column(String(20), nullable=False, default="pending")  # pending | active | failed
    provider_customer_code = Column(String(100), nullable=True, index=True)
    provider_account_id = Column(String(100), nullable=True)
    account_number = Column(String(30), nullable=True, index=True)
    account_name = Column(String(255), nullable=True)
    bank_name = Column(String(120), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProviderCustomer(Base):
    """Our user ↔ provider account mapping (e.g. Quidax sub-account id)."""
    __tablename__ = "provider_customers"
    __table_args__ = (UniqueConstraint("user_id", "provider", name="uq_provider_customer_user_provider"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(30), nullable=False)
    provider_customer_id = Column(String(120), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class UserDepositAddress(Base):
    __tablename__ = "user_deposit_addresses"
    __table_args__ = (UniqueConstraint("user_id", "currency", "network", name="uq_deposit_address_user_currency_network"),)

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    provider = Column(String(30), nullable=False, default="quidax")
    currency = Column(String(10), nullable=False)   # USDT | USDC | BTC | ETH
    network = Column(String(20), nullable=False)    # trc20 | bep20 | erc20 | btc
    address = Column(String(300), nullable=True, index=True)
    destination_tag = Column(String(100), nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending | active | failed
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PayoutDestination(Base):
    """
    A withdrawal target. Lifecycle (Bybit-style address book):
      pending_confirmation → (email link clicked) → cooling_down → (24h) → usable
    Withdrawals are only allowed to a confirmed destination whose cooldown has passed.
    """
    __tablename__ = "payout_destinations"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind = Column(String(10), nullable=False)            # bank | crypto
    currency = Column(String(10), nullable=False)        # NGN | USDT | ...
    label = Column(String(100), nullable=True)
    # bank
    bank_code = Column(String(20), nullable=True)
    bank_name = Column(String(120), nullable=True)
    account_number = Column(String(30), nullable=True)
    account_name = Column(String(255), nullable=True)    # as resolved by the bank — must match KYC name
    provider_recipient_code = Column(String(100), nullable=True)
    # crypto
    network = Column(String(20), nullable=True)
    address = Column(String(300), nullable=True)
    destination_tag = Column(String(100), nullable=True)
    # lifecycle
    status = Column(String(30), nullable=False, default="pending_confirmation")  # pending_confirmation | confirmed | removed
    confirm_token_hash = Column(String(128), nullable=True, index=True)
    confirm_expires_at = Column(DateTime, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    usable_from = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def is_usable(self) -> bool:
        return self.status == "confirmed" and self.usable_from is not None and datetime.utcnow() >= self.usable_from
