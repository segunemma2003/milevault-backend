import uuid
from datetime import datetime
from sqlalchemy import Column, String, Boolean, DateTime, Text, Float, Integer, ForeignKey, UniqueConstraint
from app.database import Base


NETWORK_LABELS = {
    "bitcoin":    "Bitcoin (BTC)",
    "erc20_usdt": "USDT – ERC20 (Ethereum)",
    "bep20_usdt": "USDT – BEP20 (BSC)",
    "trc20_usdt": "USDT – TRC20 (TRON)",
    "solana":     "Solana (SOL)",
}

NETWORK_CURRENCIES = {
    "bitcoin":    "BTC",
    "erc20_usdt": "USDT",
    "bep20_usdt": "USDT",
    "trc20_usdt": "USDT",
    "solana":     "SOL",
}


class CryptoDepositAddress(Base):
    """Admin-owned deposit address per network. Users send here."""
    __tablename__ = "crypto_deposit_addresses"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    network = Column(String(30), nullable=False, unique=True)
    address = Column(String(300), nullable=False)
    label = Column(String(100), nullable=True)           # human-readable name
    is_active = Column(Boolean, default=True)
    min_confirmations = Column(Integer, default=1)
    last_scanned_at = Column(DateTime, nullable=True)
    last_scanned_cursor = Column(String(300), nullable=True)  # last tx hash / block
    created_by = Column(String, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CryptoPendingDeposit(Base):
    """A detected on-chain incoming transaction awaiting admin approval."""
    __tablename__ = "crypto_pending_deposits"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id"), nullable=True)   # matched after admin approves
    network = Column(String(30), nullable=False)
    tx_hash = Column(String(300), nullable=False, unique=True)
    from_address = Column(String(300), nullable=True)
    to_address = Column(String(300), nullable=False)
    amount_crypto = Column(Float, nullable=False)
    currency = Column(String(20), nullable=False)        # BTC | USDT | SOL
    fiat_currency = Column(String(10), default="NGN")
    fiat_amount = Column(Float, nullable=True)           # admin fills when approving
    status = Column(String(20), default="detected")      # detected | confirmed | approved | rejected
    confirmations = Column(Integer, default=0)
    block_number = Column(String(100), nullable=True)
    detected_at = Column(DateTime, default=datetime.utcnow)
    approved_at = Column(DateTime, nullable=True)
    approved_by = Column(String, ForeignKey("users.id"), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)


class UserCryptoAddress(Base):
    """User-saved withdrawal address per network."""
    __tablename__ = "user_crypto_addresses"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    network = Column(String(30), nullable=False)
    address = Column(String(300), nullable=False)
    label = Column(String(100), nullable=True)           # e.g. "My Binance wallet"
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "network", "address", name="uq_user_crypto_addr"),
    )
