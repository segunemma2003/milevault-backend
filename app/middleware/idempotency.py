"""
Idempotency for every mutating request that carries an Idempotency-Key header (the web app sends
one per user action: withdraw, pay, transfer, convert, deposit, create deal…).

  first request with a key   → claimed atomically in the database, runs, response stored
  same key again (done)      → the stored response is returned, nothing runs again
  same key while running     → 409 REQUEST_IN_PROGRESS (a double click / parallel retry)
  same key, different body   → 422 IDEMPOTENCY_KEY_REUSED
  error / crash              → claim released, so a retry can run (e.g. after topping up)

Keys are scoped to the signed-in user, and records expire after 48 hours.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)

IDEMPOTENT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SKIP_PATHS = ("/api/v1/auth/login", "/api/v1/auth/refresh", "/ws", "/api/v1/payments/webhooks", "/api/v1/wallet/webhook")
TTL = timedelta(hours=48)


def _user_key(request: Request) -> str:
    from app.dependencies import ACCESS_COOKIE
    from app.services.auth_service import decode_token
    token = None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    token = token or request.cookies.get(ACCESS_COOKIE)
    payload = decode_token(token) if token else None
    return str(payload.get("sub")) if payload and payload.get("sub") else "anon"


def _claim(user_key: str, key: str, method_path: str, body_hash: str):
    """('claimed', None) | ('replay', record) | ('busy', None) | ('mismatch', None)."""
    from app.database import SessionLocal
    from app.models.wallet import IdempotencyRecord
    db = SessionLocal()
    try:
        db.add(IdempotencyRecord(user_key=user_key, idem_key=key, method_path=method_path, body_hash=body_hash))
        db.commit()
        return "claimed", None
    except IntegrityError:
        db.rollback()
        rec = db.query(IdempotencyRecord).filter_by(user_key=user_key, idem_key=key).first()
        if not rec:
            return "busy", None
        if datetime.utcnow() - (rec.created_at or datetime.utcnow()) > TTL:
            db.delete(rec)  # expired: start over
            db.commit()
            return _claim(user_key, key, method_path, body_hash)
        if rec.method_path != method_path or rec.body_hash != body_hash:
            return "mismatch", None
        if rec.status != "done":
            return "busy", None
        return "replay", {"status": rec.response_status, "body": rec.response_body}
    finally:
        db.close()


def _finish(user_key: str, key: str, status: int | None, body: str | None) -> None:
    from app.database import SessionLocal
    from app.models.wallet import IdempotencyRecord
    db = SessionLocal()
    try:
        rec = db.query(IdempotencyRecord).filter_by(user_key=user_key, idem_key=key).first()
        if not rec:
            return
        if status is None or not (200 <= status < 300):
            db.delete(rec)  # didn't succeed (nothing changed): let a retry run
        else:
            rec.status, rec.response_status, rec.response_body = "done", status, body
        db.commit()
    finally:
        db.close()


class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        key = request.headers.get("Idempotency-Key")
        path = request.url.path
        if not key or request.method not in IDEMPOTENT_METHODS or path.startswith(SKIP_PATHS):
            return await call_next(request)
        if not (10 <= len(key) <= 128):
            return JSONResponse(status_code=422, content={"detail": {
                "error": "INVALID_IDEMPOTENCY_KEY", "message": "Idempotency-Key must be 10–128 characters."}})

        body = await request.body()
        user_key = _user_key(request)
        method_path = f"{request.method} {path}"
        body_hash = hashlib.sha256(body).hexdigest()
        outcome, rec = await run_in_threadpool(_claim, user_key, key, method_path, body_hash)
        if outcome == "replay":
            logger.info("Idempotent replay %s %s", method_path, key[:8])
            return Response(content=rec["body"] or "", status_code=rec["status"] or 200,
                            media_type="application/json", headers={"X-Idempotency-Replayed": "true"})
        if outcome == "busy":
            return JSONResponse(status_code=409, content={"detail": {
                "error": "REQUEST_IN_PROGRESS", "message": "This request is already being processed — please wait."}})
        if outcome == "mismatch":
            return JSONResponse(status_code=422, content={"detail": {
                "error": "IDEMPOTENCY_KEY_REUSED", "message": "This request key was already used for a different request."}})

        try:
            response = await call_next(request)
        except Exception:
            await run_in_threadpool(_finish, user_key, key, None, None)
            raise
        chunks = b""
        async for chunk in response.body_iterator:
            chunks += chunk
        await run_in_threadpool(_finish, user_key, key, response.status_code, chunks.decode("utf-8", "replace"))
        headers = {k: v for k, v in response.headers.items() if k.lower() != "content-length"}
        return Response(content=chunks, status_code=response.status_code, headers=headers, media_type=response.media_type)
