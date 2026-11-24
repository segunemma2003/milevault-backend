"""
Ledger reconciliation: rebuild every (user, currency, bucket) balance from ledger_entries
and compare with the stored wallet_balances. Any difference means money moved without a
ledger record (or vice-versa) and is reported to admins.

Provider-side checks (Quidax / Paystack balances vs. our totals) plug in here once those
integrations exist.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.wallet import LedgerEntry, WalletBalance
from app.services.ledger import BUCKETS, to_decimal

TOLERANCE = Decimal("0.00000001")


def compute_mismatches(db: Session) -> list[dict]:
    derived: dict[tuple[str, str, str], Decimal] = defaultdict(Decimal)
    for e in db.query(LedgerEntry).yield_per(1000):
        amt = to_decimal(e.amount)
        cur = (e.currency or "").upper()
        if e.credit_user_id and e.credit_account in BUCKETS:
            derived[(e.credit_user_id, cur, e.credit_account)] += amt
        if e.debit_user_id and e.debit_account in BUCKETS:
            derived[(e.debit_user_id, cur, e.debit_account)] -= amt

    stored: dict[tuple[str, str, str], Decimal] = {}
    for b in db.query(WalletBalance).all():
        cur = (b.currency or "").upper()
        for bucket, col in BUCKETS.items():
            stored[(b.user_id, cur, bucket)] = to_decimal(getattr(b, col) or 0)

    out = []
    for key in set(derived) | set(stored):
        s, d = stored.get(key, Decimal(0)), derived.get(key, Decimal(0))
        if abs(s - d) > TOLERANCE:
            user_id, currency, bucket = key
            out.append({"user_id": user_id, "currency": currency, "bucket": bucket,
                        "stored": str(s), "ledger": str(d), "difference": str(s - d)})
    out.sort(key=lambda r: abs(Decimal(r["difference"])), reverse=True)
    return out


def negative_balances(db: Session) -> list[dict]:
    rows = []
    for b in db.query(WalletBalance).all():
        for bucket, col in BUCKETS.items():
            v = to_decimal(getattr(b, col) or 0)
            if v < 0:
                rows.append({"user_id": b.user_id, "currency": b.currency, "bucket": bucket, "value": str(v)})
    return rows
