"""
Paystack rails: Dedicated Virtual Accounts (NGN in), bank resolution + Transfers (NGN out).
All amounts cross the boundary in kobo (integers) and are converted exactly with Decimal.
Docs: https://paystack.com/docs/payments/dedicated-virtual-accounts , /docs/transfers
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

BASE = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.PAYSTACK_SECRET_KEY)


def to_kobo(amount) -> int:
    from app.services.ledger import quantize
    return int(quantize(amount, "NGN") * 100)


def from_kobo(kobo) -> Decimal:
    return (Decimal(int(kobo)) / Decimal(100)).quantize(Decimal("0.01"))


def _request(method: str, path: str, *, json: Optional[dict] = None, params: Optional[dict] = None) -> Dict[str, Any]:
    if not enabled():
        raise PaystackError("Paystack is not configured (PAYSTACK_SECRET_KEY missing).")
    with httpx.Client(timeout=15) as client:
        resp = client.request(
            method, f"{BASE}{path}", json=json, params=params,
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"},
        )
    try:
        data = resp.json()
    except ValueError:
        raise PaystackError(f"Paystack returned HTTP {resp.status_code}")
    if not data.get("status"):
        raise PaystackError(data.get("message") or f"Paystack error (HTTP {resp.status_code})")
    return data.get("data") or {}


# ─── Customers & Dedicated Virtual Accounts ───────────────────────────────────

def create_customer(*, email: str, first_name: str, last_name: str, phone: Optional[str]) -> str:
    data = _request("POST", "/customer", json={
        "email": email, "first_name": first_name, "last_name": last_name, "phone": phone or None,
    })
    return data["customer_code"]


def create_dedicated_account(*, customer_code: str) -> Dict[str, Any]:
    """Returns {id, account_number, account_name, bank_name}. Requires DVA enabled on the Paystack business."""
    data = _request("POST", "/dedicated_account", json={
        "customer": customer_code, "preferred_bank": settings.PAYSTACK_DVA_PREFERRED_BANK,
    })
    return {
        "id": str(data.get("id") or ""),
        "account_number": data.get("account_number"),
        "account_name": data.get("account_name"),
        "bank_name": (data.get("bank") or {}).get("name"),
    }


# ─── Charges (deposits) ───────────────────────────────────────────────────────

def verify_transaction(reference: str) -> Dict[str, Any]:
    """Source of truth for a charge — never trust webhook bodies alone."""
    tx = _request("GET", f"/transaction/verify/{reference}")
    return {
        "status": tx.get("status"),                         # success | failed | abandoned | ...
        "amount": from_kobo(tx.get("amount") or 0),
        "currency": (tx.get("currency") or "NGN").upper(),
        "reference": tx.get("reference"),
        "channel": tx.get("channel"),
        "customer_code": (tx.get("customer") or {}).get("customer_code"),
        "customer_email": (tx.get("customer") or {}).get("email"),
        "metadata": tx.get("metadata") or {},
    }


# ─── Banks, recipients, transfers (payouts) ───────────────────────────────────

def list_banks() -> List[Dict[str, Any]]:
    data = _request("GET", "/bank", params={"country": "nigeria", "currency": "NGN", "perPage": 200})
    return [{"name": b.get("name"), "code": b.get("code")} for b in (data if isinstance(data, list) else [])
            if b.get("active", True)]


def resolve_account(*, account_number: str, bank_code: str) -> str:
    """Returns the account holder name as registered at the bank."""
    data = _request("GET", "/bank/resolve", params={"account_number": account_number, "bank_code": bank_code})
    return data.get("account_name") or ""


def create_recipient(*, name: str, account_number: str, bank_code: str) -> str:
    data = _request("POST", "/transferrecipient", json={
        "type": "nuban", "name": name, "account_number": account_number, "bank_code": bank_code, "currency": "NGN",
    })
    return data["recipient_code"]


def initiate_transfer(*, amount, recipient_code: str, reference: str, reason: str) -> Dict[str, Any]:
    """`reference` is OUR withdrawal id — Paystack rejects a duplicate reference, so retries can't double-pay."""
    data = _request("POST", "/transfer", json={
        "source": "balance", "amount": to_kobo(amount), "currency": "NGN",
        "recipient": recipient_code, "reason": reason[:100], "reference": reference,
    })
    return {"transfer_code": data.get("transfer_code"), "status": data.get("status"), "reference": data.get("reference")}


def verify_transfer(reference: str) -> Dict[str, Any]:
    data = _request("GET", f"/transfer/verify/{reference}")
    return {
        "status": data.get("status"),          # success | failed | reversed | pending | otp | processing
        "amount": from_kobo(data.get("amount") or 0),
        "reference": data.get("reference"),
        "transfer_code": data.get("transfer_code"),
        "reason": data.get("reason") or data.get("failures"),
    }
