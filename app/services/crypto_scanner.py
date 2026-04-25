"""
Blockchain scanner — reads public explorer APIs to detect incoming transactions.
Uses only read-only public APIs. No private keys involved.

Chains:
  bitcoin    → Blockstream.info REST API (no key needed)
  erc20_usdt → Etherscan API  (ETHERSCAN_API_KEY optional, uses public endpoint)
  bep20_usdt → BscScan API    (BSCSCAN_API_KEY optional)
  trc20_usdt → TronGrid API   (no key needed)
  solana     → Solana RPC     (public mainnet)
"""

import logging
from typing import Optional
import httpx

logger = logging.getLogger(__name__)

USDT_ERC20_CONTRACT = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955"
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def _get(url: str, params: dict = None, timeout: int = 12) -> dict | list | None:
    try:
        r = httpx.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning(f"Scanner HTTP error {url}: {exc}")
        return None


def scan_bitcoin(address: str, last_tx_hash: Optional[str] = None) -> list[dict]:
    """Return new confirmed incoming BTC txs since last_tx_hash (exclusive)."""
    data = _get(f"https://blockstream.info/api/address/{address}/txs")
    if not data or not isinstance(data, list):
        return []

    results = []
    for tx in data:
        txid = tx.get("txid", "")
        if last_tx_hash and txid == last_tx_hash:
            break
        # Look for outputs to our address
        for vout in tx.get("vout", []):
            addrs = vout.get("scriptpubkey_address", "")
            if addrs == address:
                sats = vout.get("value", 0)
                if sats > 0:
                    status = tx.get("status", {})
                    results.append({
                        "tx_hash": txid,
                        "from_address": None,
                        "amount_crypto": sats / 1e8,
                        "currency": "BTC",
                        "confirmations": 1 if status.get("confirmed") else 0,
                        "block_number": str(status.get("block_height", "")),
                    })
    return results


def scan_erc20_usdt(address: str, last_block: Optional[str] = None, api_key: str = "") -> list[dict]:
    """Return new USDT ERC20 transfers to address since last_block."""
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": USDT_ERC20_CONTRACT,
        "address": address,
        "sort": "desc",
        "offset": 50,
        "page": 1,
    }
    if api_key:
        params["apikey"] = api_key
    if last_block:
        params["startblock"] = int(last_block) + 1

    data = _get("https://api.etherscan.io/api", params=params)
    if not data or data.get("status") == "0":
        return []

    results = []
    for tx in (data.get("result") or []):
        if tx.get("to", "").lower() != address.lower():
            continue
        value = int(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 6)))
        if value <= 0:
            continue
        results.append({
            "tx_hash": tx["hash"],
            "from_address": tx.get("from"),
            "amount_crypto": value,
            "currency": "USDT",
            "confirmations": int(tx.get("confirmations", 0)),
            "block_number": tx.get("blockNumber", ""),
        })
    return results


def scan_bep20_usdt(address: str, last_block: Optional[str] = None, api_key: str = "") -> list[dict]:
    """Return new USDT BEP20 transfers to address since last_block."""
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": USDT_BEP20_CONTRACT,
        "address": address,
        "sort": "desc",
        "offset": 50,
        "page": 1,
    }
    if api_key:
        params["apikey"] = api_key
    if last_block:
        params["startblock"] = int(last_block) + 1

    data = _get("https://api.bscscan.com/api", params=params)
    if not data or data.get("status") == "0":
        return []

    results = []
    for tx in (data.get("result") or []):
        if tx.get("to", "").lower() != address.lower():
            continue
        value = int(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
        if value <= 0:
            continue
        results.append({
            "tx_hash": tx["hash"],
            "from_address": tx.get("from"),
            "amount_crypto": value,
            "currency": "USDT",
            "confirmations": int(tx.get("confirmations", 0)),
            "block_number": tx.get("blockNumber", ""),
        })
    return results


def scan_trc20_usdt(address: str, last_tx_hash: Optional[str] = None) -> list[dict]:
    """Return new USDT TRC20 transfers to address."""
    params = {
        "contract_address": USDT_TRC20_CONTRACT,
        "to_address": address,
        "limit": 50,
        "order_by": "block_timestamp,desc",
    }
    data = _get("https://apilist.tronscan.org/api/token_trc20/transfers", params=params)
    if not data:
        return []

    results = []
    for tx in (data.get("token_transfers") or []):
        if last_tx_hash and tx.get("transaction_id") == last_tx_hash:
            break
        if tx.get("to_address") != address:
            continue
        decimals = int(tx.get("decimals", 6))
        value = int(tx.get("quant", 0)) / (10 ** decimals)
        if value <= 0:
            continue
        results.append({
            "tx_hash": tx["transaction_id"],
            "from_address": tx.get("from_address"),
            "amount_crypto": value,
            "currency": "USDT",
            "confirmations": 1,
            "block_number": str(tx.get("block", "")),
        })
    return results


def scan_solana(address: str, last_sig: Optional[str] = None) -> list[dict]:
    """Return new SOL transfers to address."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": 50, "until": last_sig}],
    }
    try:
        r = httpx.post("https://api.mainnet-beta.solana.com", json=payload, timeout=12)
        r.raise_for_status()
        sigs = r.json().get("result") or []
    except Exception as exc:
        logger.warning(f"Solana scan error: {exc}")
        return []

    results = []
    for s in sigs:
        sig = s.get("signature", "")
        # Fetch transaction detail
        tx_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
        }
        try:
            tr = httpx.post("https://api.mainnet-beta.solana.com", json=tx_payload, timeout=12)
            tx = tr.json().get("result")
        except Exception:
            continue
        if not tx:
            continue
        meta = tx.get("meta") or {}
        if meta.get("err"):
            continue
        pre = meta.get("preBalances", [])
        post = meta.get("postBalances", [])
        acct_keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
        for i, key in enumerate(acct_keys):
            addr = key if isinstance(key, str) else key.get("pubkey", "")
            if addr == address and i < len(pre) and i < len(post):
                diff = (post[i] - pre[i]) / 1e9
                if diff > 0:
                    results.append({
                        "tx_hash": sig,
                        "from_address": None,
                        "amount_crypto": diff,
                        "currency": "SOL",
                        "confirmations": 1,
                        "block_number": str(tx.get("slot", "")),
                    })
                    break
    return results


def scan_network(network: str, address: str, last_cursor: Optional[str], config: dict) -> list[dict]:
    """Dispatch to the correct scanner for a given network."""
    try:
        if network == "bitcoin":
            return scan_bitcoin(address, last_cursor)
        elif network == "erc20_usdt":
            return scan_erc20_usdt(address, last_cursor, api_key=config.get("etherscan_key", ""))
        elif network == "bep20_usdt":
            return scan_bep20_usdt(address, last_cursor, api_key=config.get("bscscan_key", ""))
        elif network == "trc20_usdt":
            return scan_trc20_usdt(address, last_cursor)
        elif network == "solana":
            return scan_solana(address, last_cursor)
    except Exception as exc:
        logger.error(f"scan_network({network}) error: {exc}")
    return []
