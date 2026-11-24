"""
Quidax client: sub-accounts (one per user), deposit addresses, deposits, withdrawals.

Every endpoint path lives in this one file. They follow Quidax's public API docs
(https://docs.quidax.io) — confirm them against the sandbox when the keys arrive; nothing
else in the app depends on the exact URL shapes.

Money model: deposits land in the user's sub-account, are credited to our ledger, then
swept to the master account ("me"). Withdrawals are paid from the master account.
"""
from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx

from app.config import settings


class QuidaxError(Exception):
    pass


def enabled() -> bool:
    return bool(settings.QUIDAX_SECRET_KEY)


def rails() -> list[tuple[str, str]]:
    """Configured (currency, network) pairs, e.g. [("USDT","trc20"), ("BTC","btc")]."""
    out = []
    for part in (settings.CRYPTO_RAILS or "").split(","):
        if ":" in part:
            cur, net = part.split(":", 1)
            if cur.strip() and net.strip():
                out.append((cur.strip().upper(), net.strip().lower()))
    return out


def _request(method: str, path: str, *, json: Optional[dict] = None, params: Optional[dict] = None) -> Any:
    if not enabled():
        raise QuidaxError("Quidax is not configured (QUIDAX_SECRET_KEY missing).")
    url = settings.QUIDAX_BASE_URL.rstrip("/") + path
    with httpx.Client(timeout=20) as client:
        resp = client.request(method, url, json=json, params=params, headers={
            "Authorization": f"Bearer {settings.QUIDAX_SECRET_KEY}",
            "Content-Type": "application/json", "Accept": "application/json",
        })
    try:
        body = resp.json()
    except ValueError:
        raise QuidaxError(f"Quidax returned HTTP {resp.status_code}")
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get("status") == "error"):
        msg = body.get("message") if isinstance(body, dict) else None
        raise QuidaxError(msg or f"Quidax error (HTTP {resp.status_code})")
    return body.get("data") if isinstance(body, dict) and "data" in body else body


# ─── Sub-accounts & addresses ─────────────────────────────────────────────────

def master_account_id() -> str:
    data = _request("GET", "/users/me")
    return str(data["id"])


def create_sub_account(*, email: str, first_name: str, last_name: str, phone: Optional[str]) -> str:
    data = _request("POST", "/users", json={
        "email": email, "first_name": first_name, "last_name": last_name, "phone_number": phone or "",
    })
    return str(data["id"])


def create_deposit_address(*, quidax_user_id: str, currency: str, network: str) -> Dict[str, Any]:
    """Creates (or returns) the permanent deposit address for this sub-account/currency/network."""
    data = _request("POST", f"/users/{quidax_user_id}/wallets/{currency.lower()}/addresses",
                    params={"network": network.lower()})
    return {"address": data.get("address"), "destination_tag": data.get("destination_tag"), "id": data.get("id")}


def fetch_deposit_address(*, quidax_user_id: str, currency: str, network: str) -> Dict[str, Any]:
    data = _request("GET", f"/users/{quidax_user_id}/wallets/{currency.lower()}/address",
                    params={"network": network.lower()})
    return {"address": data.get("address"), "destination_tag": data.get("destination_tag")}


# ─── Deposits ─────────────────────────────────────────────────────────────────

def fetch_deposit(*, quidax_user_id: str, deposit_id: str) -> Dict[str, Any]:
    """Source of truth for a deposit — the webhook body is only a hint."""
    d = _request("GET", f"/users/{quidax_user_id}/deposits/{deposit_id}")
    return {
        "id": str(d.get("id")),
        "currency": (d.get("currency") or "").upper(),
        "amount": Decimal(str(d.get("amount") or "0")),
        "status": (d.get("status") or "").lower(),       # accepted / submitted / pending / rejected ...
        "txid": d.get("txid"),
        "network": ((d.get("network") or (d.get("payment_address") or {}).get("network")) or "").lower(),
        "address": (d.get("payment_address") or {}).get("address"),
    }


DEPOSIT_DONE_STATES = {"accepted", "done", "completed", "successful", "success", "confirmed"}


# ─── Withdrawals & internal transfers ─────────────────────────────────────────

def create_withdrawal(*, from_user: str, currency: str, amount, address: str, network: Optional[str],
                      reference: str, note: str = "", destination_tag: Optional[str] = None) -> Dict[str, Any]:
    """`reference` is OUR withdrawal id; Quidax rejects a duplicate reference, so a retry can't double-send."""
    payload = {
        "currency": currency.lower(), "amount": str(amount), "fund_uid": address,
        "transaction_note": note[:100], "narration": note[:100], "reference": reference,
    }
    if network:
        payload["network"] = network.lower()
    if destination_tag:
        payload["fund_uid2"] = destination_tag
    data = _request("POST", f"/users/{from_user}/withdraws", json=payload)
    return {"id": str(data.get("id")), "status": (data.get("status") or "").lower(), "reference": data.get("reference")}


def fetch_withdrawal_by_reference(*, from_user: str, reference: str) -> Dict[str, Any]:
    d = _request("GET", f"/users/{from_user}/withdraws/reference/{reference}")
    return {"id": str(d.get("id")), "status": (d.get("status") or "").lower(), "txid": d.get("txid"),
            "reason": d.get("reason")}


WITHDRAWAL_DONE_STATES = {"done", "completed", "successful", "success"}
WITHDRAWAL_FAILED_STATES = {"failed", "rejected", "cancelled", "canceled"}


def sweep_to_master(*, quidax_user_id: str, currency: str, amount, reference: str) -> Dict[str, Any]:
    """Internal sub-account → master transfer (fund_uid = master account id)."""
    return create_withdrawal(from_user=quidax_user_id, currency=currency, amount=amount,
                             address=master_account_id(), network=None, reference=reference,
                             note="Bondrova sweep")


# ─── Webhooks ─────────────────────────────────────────────────────────────────

def verify_webhook(raw_body: bytes, signature_header: Optional[str]) -> bool:
    """
    Accepts Quidax's "t=<ts>,s=<hmac>" header (HMAC-SHA256 of "<ts>.<body>") or a plain
    HMAC-SHA256 hex of the body. Fails closed when no secret is configured.
    """
    secret = settings.QUIDAX_WEBHOOK_SECRET
    if not secret or not signature_header:
        return False
    sig = signature_header.strip()
    parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
    if "t" in parts and "s" in parts:
        signed = f"{parts['t']}.".encode() + raw_body
        expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parts["s"])
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
