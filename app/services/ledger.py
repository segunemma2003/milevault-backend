"""
Central money service — every wallet balance change goes through here.

Guarantees (per call, inside the caller's DB transaction):
  * Row locks:   balance rows are SELECT ... FOR UPDATE'd in a deterministic order,
                 so concurrent requests serialise instead of double-spending.
  * Exact maths: amounts are Decimal, quantised to the currency's precision
                 (NGN/USD = 2 dp, USDT/USDC = 6 dp, BTC/ETH = 8 dp). The DB stores
                 NUMERIC(28,8), so nothing drifts.
  * No overdraft: a move that would make any bucket negative raises InsufficientFunds.
  * Exactly-once: pass an idempotency key; it is claimed in processed_events in the
                 same transaction. A repeat returns False and changes nothing.
  * Audit:       every movement writes a double-entry LedgerEntry.

The caller owns the transaction (db.commit()/rollback()).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.wallet import LedgerEntry, ProcessedEvent, WalletBalance

BUCKETS = {"available": "amount", "escrow": "escrow_amount", "pending": "pending_amount"}

# Fallback precision when the currency table has no row for a code.
DEFAULT_DECIMALS = {
    "NGN": 2, "USD": 2, "EUR": 2, "GBP": 2, "GHS": 2, "KES": 2, "ZAR": 2, "CAD": 2, "AUD": 2,
    "USDT": 6, "USDC": 6, "BTC": 8, "ETH": 8, "SOL": 8, "BNB": 8, "TRX": 6,
}
MAX_DECIMALS = 8  # column scale


class LedgerError(Exception):
    pass


class InsufficientFunds(LedgerError):
    def __init__(self, user_id: str, currency: str, bucket: str, have: Decimal, need: Decimal):
        self.user_id, self.currency, self.bucket, self.have, self.need = user_id, currency, bucket, have, need
        super().__init__(f"Insufficient {bucket} {currency}: have {have}, need {need}")


# ─── Amount helpers ───────────────────────────────────────────────────────────

def to_decimal(value) -> Decimal:
    """Float-safe conversion (uses str() so 0.1 stays 0.1, not 0.1000000000000000055…)."""
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def currency_decimals(db: Optional[Session], currency: str) -> int:
    code = (currency or "").upper()
    if db is not None:
        try:
            from app.models.currency import Currency
            row = db.query(Currency.decimal_places).filter(Currency.code == code).first()
            if row and row[0] is not None:
                return max(0, min(int(row[0]), MAX_DECIMALS))
        except Exception:
            pass
    return DEFAULT_DECIMALS.get(code, MAX_DECIMALS)


def quantize(value, currency: str, db: Optional[Session] = None) -> Decimal:
    places = currency_decimals(db, currency)
    return to_decimal(value).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _store(d: Decimal) -> float:
    # Columns are NUMERIC(28,8); the ORM type is Float so existing call sites keep working.
    # float(quantised Decimal) round-trips exactly through NUMERIC(28,8).
    return float(d)


# ─── Exactly-once guard ───────────────────────────────────────────────────────

def claim_event(db: Session, source: str, event_id: str) -> bool:
    """Record (source, event_id). Returns False if it was already processed."""
    if not event_id:
        raise LedgerError("claim_event requires an event_id")
    try:
        with db.begin_nested():
            db.add(ProcessedEvent(source=source[:60], event_id=str(event_id)[:255]))
            db.flush()
        return True
    except IntegrityError:
        return False


def is_processed(db: Session, source: str, event_id: str) -> bool:
    return db.query(ProcessedEvent.id).filter(
        ProcessedEvent.source == source[:60], ProcessedEvent.event_id == str(event_id)[:255]
    ).first() is not None


# ─── Locking ──────────────────────────────────────────────────────────────────

def lock_balance(db: Session, user_id: str, currency: str) -> WalletBalance:
    """Get-or-create the user's balance row for `currency`, locked FOR UPDATE."""
    currency = currency.upper()
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        # Serialise get-or-create for this (user, currency) even without a unique index.
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"wallet:{user_id}:{currency}"})
    bal = (
        db.query(WalletBalance)
        .filter(WalletBalance.user_id == user_id, WalletBalance.currency == currency)
        .with_for_update()
        .first()
    )
    if bal is None:
        bal = WalletBalance(user_id=user_id, currency=currency, amount=0.0, escrow_amount=0.0, pending_amount=0.0)
        db.add(bal)
        db.flush()
    return bal


def _lock_many(db: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], WalletBalance]:
    out = {}
    for user_id, currency in sorted(set((u, c.upper()) for u, c in keys)):  # fixed order → no deadlocks
        out[(user_id, currency)] = lock_balance(db, user_id, currency)
    return out


def _get(bal: WalletBalance, bucket: str) -> Decimal:
    return to_decimal(getattr(bal, BUCKETS[bucket]) or 0)


def _set(bal: WalletBalance, bucket: str, value: Decimal) -> None:
    setattr(bal, BUCKETS[bucket], _store(value))


def _check_bucket(bucket: str) -> None:
    if bucket not in BUCKETS:
        raise LedgerError(f"Unknown bucket '{bucket}'")


def _positive(amount, currency: str, db: Session) -> Decimal:
    amt = quantize(amount, currency, db)
    if amt <= 0:
        raise LedgerError(f"Amount must be positive (got {amount})")
    return amt


# ─── Operations ───────────────────────────────────────────────────────────────

def transfer(
    db: Session,
    *,
    currency: str,
    amount,
    from_user_id: Optional[str],
    from_bucket: Optional[str],
    to_user_id: Optional[str],
    to_bucket: Optional[str],
    reference_type: str,
    reference_id: Optional[str] = None,
    description: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    external_account: str = "external",
) -> bool:
    """
    Move `amount` from (from_user, from_bucket) to (to_user, to_bucket).
    Use from_user_id=None for money entering from outside (deposit) and
    to_user_id=None for money leaving (payout). Returns False if the
    idempotency key was already processed (nothing changed).
    """
    currency = currency.upper()
    if from_user_id is None and to_user_id is None:
        raise LedgerError("A transfer needs at least one user side")
    for uid, bucket in ((from_user_id, from_bucket), (to_user_id, to_bucket)):
        if uid is not None:
            _check_bucket(bucket)

    amt = _positive(amount, currency, db)

    # Lock first, then claim: a concurrent duplicate waits on the lock and then sees the
    # claim, and a repeat of an already-applied event reports "duplicate" (False) instead
    # of a misleading InsufficientFunds. If anything below raises, the caller's rollback
    # also releases the claim, so a corrected retry with the same key still works.
    keys = [(u, currency) for u in (from_user_id, to_user_id) if u is not None]
    locked = _lock_many(db, keys)

    if idempotency_key and not claim_event(db, reference_type, idempotency_key):
        return False

    if from_user_id is not None:
        src = locked[(from_user_id, currency)]
        have = _get(src, from_bucket)
        if have < amt:
            raise InsufficientFunds(from_user_id, currency, from_bucket, have, amt)
        _set(src, from_bucket, have - amt)
    if to_user_id is not None:
        dst = locked[(to_user_id, currency)]
        _set(dst, to_bucket, _get(dst, to_bucket) + amt)

    db.add(LedgerEntry(
        debit_user_id=from_user_id,
        credit_user_id=to_user_id,
        debit_account=from_bucket if from_user_id is not None else external_account,
        credit_account=to_bucket if to_user_id is not None else external_account,
        amount=_store(amt),
        currency=currency,
        reference_type=reference_type,
        reference_id=reference_id,
        description=description,
    ))
    db.flush()
    return True


def move(db: Session, *, user_id: str, currency: str, amount, from_bucket: str, to_bucket: str, **kw) -> bool:
    """Same user, between buckets (e.g. available → escrow)."""
    return transfer(db, currency=currency, amount=amount, from_user_id=user_id, from_bucket=from_bucket,
                    to_user_id=user_id, to_bucket=to_bucket, **kw)


def credit(db: Session, *, user_id: str, currency: str, amount, to_bucket: str = "available", **kw) -> bool:
    """Money arriving from outside the platform (verified deposit)."""
    return transfer(db, currency=currency, amount=amount, from_user_id=None, from_bucket=None,
                    to_user_id=user_id, to_bucket=to_bucket, **kw)


def debit(db: Session, *, user_id: str, currency: str, amount, from_bucket: str = "pending", **kw) -> bool:
    """Money leaving the platform (payout confirmed)."""
    return transfer(db, currency=currency, amount=amount, from_user_id=user_id, from_bucket=from_bucket,
                    to_user_id=None, to_bucket=None, **kw)
