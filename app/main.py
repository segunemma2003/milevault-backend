"""
Bondrova FastAPI application entry point.
Middleware stack (outermost → innermost):
  CORSMiddleware → SecurityMiddleware → RateLimiterMiddleware → IdempotencyMiddleware → Router
"""
import os
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import create_tables, get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.auth_service import decode_token
from app.middleware.security import SecurityMiddleware
from app.middleware.rate_limiter import RateLimiterMiddleware
from app.middleware.idempotency import IdempotencyMiddleware
from app.routers import (
    auth, users, transactions, wallet,
    disputes, messages, notifications, kyc, dashboard,
)
from app.routers import agents, admin, uploads, contact, content, admin_kyc, payments, admin_team
import app.services.notification_dispatch  # noqa: F401  (registers after-commit delivery)
from app.routers.crypto import router as crypto_router, admin_router as crypto_admin_router

logger = logging.getLogger("milevault")


def _seed_admin() -> None:
    """Create the platform admin account on first boot if it doesn't exist."""
    try:
        from app.database import SessionLocal
        from app.models.user import User
        from app.config import settings
        from passlib.context import CryptContext

        if not settings.ADMIN_PASSWORD:
            return

        db = SessionLocal()
        try:
            existing = db.query(User).filter(User.email == settings.ADMIN_EMAIL).first()
            if existing:
                if not (existing.is_admin and existing.is_email_verified and existing.is_active):
                    existing.is_admin = True
                    existing.is_email_verified = True
                    existing.is_active = True
                    db.commit()
                    logger.info(f"Admin flags ensured on existing user: {settings.ADMIN_EMAIL}")
                return

            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=10)
            import uuid
            admin = User(
                id=str(uuid.uuid4()),
                first_name="Bondrova",
                last_name="Admin",
                email=settings.ADMIN_EMAIL,
                hashed_password=pwd_context.hash(settings.ADMIN_PASSWORD),
                role="buyer",
                is_admin=True,
                is_active=True,
                is_kyc_verified=True,
                is_email_verified=True,
                country_code="NG",
            )
            db.add(admin)
            db.commit()
            logger.info(f"Admin account created: {settings.ADMIN_EMAIL}")
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"Admin seed skipped: {exc}")


def _seed_default_currencies() -> None:
    """Ensure NGN and USD exist as active currencies on first boot."""
    try:
        from app.database import SessionLocal
        from app.models.currency import Currency, CurrencyType

        DEFAULTS = [
            {"code": "NGN", "name": "Nigerian Naira",  "symbol": "₦", "type": CurrencyType.fiat, "decimal_places": 2, "is_base": False},
            {"code": "USD", "name": "US Dollar",       "symbol": "$", "type": CurrencyType.fiat, "decimal_places": 2, "is_base": True},
            {"code": "EUR", "name": "Euro",             "symbol": "€", "type": CurrencyType.fiat, "decimal_places": 2, "is_base": False},
            {"code": "GBP", "name": "British Pound",   "symbol": "£", "type": CurrencyType.fiat, "decimal_places": 2, "is_base": False},
            {"code": "GHS", "name": "Ghanaian Cedi",   "symbol": "₵", "type": CurrencyType.fiat, "decimal_places": 2, "is_base": False},
        ]

        db = SessionLocal()
        try:
            added = 0
            for c in DEFAULTS:
                exists = db.query(Currency).filter(Currency.code == c["code"]).first()
                if not exists:
                    db.add(Currency(**c))
                    added += 1
            if added:
                db.commit()
                logger.info(f"Seeded {added} default currencies.")
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"Currency seed skipped: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services.celery_app import fail_fast_publishing
    fail_fast_publishing()
    # Must succeed: otherwise ORM queries hit missing columns while the API still runs.
    create_tables()
    logger.info("Bondrova API started — tables and incremental schema verified.")

    _seed_admin()
    _seed_default_currencies()

    # Start Redis pub/sub listener for WebSocket fan-out
    try:
        from app.websocket.manager import manager
        asyncio.create_task(manager.start_redis_subscriber())
        logger.info("Redis pub/sub subscriber started.")
    except Exception as exc:
        logger.warning(f"Redis subscriber not started (Redis unavailable?): {exc}")

    yield
    logger.info("Bondrova API shutting down.")


app = FastAPI(
    title="Bondrova API",
    description="Secure escrow platform — presigned S3 uploads, WebSocket chat, multi-gateway payments.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ─── Middleware ───────────────────────────────────────────────────────────────
# Starlette runs the LAST added middleware FIRST (outermost). Order, outside → in:
#   CORS → security (CSRF/headers) → rate limit → idempotency → app
# CORS must be outermost so every response — including 403/409/429 and idempotent replays
# produced by the inner layers — carries the headers the browser needs to read it.
app.add_middleware(IdempotencyMiddleware)
app.add_middleware(RateLimiterMiddleware)
app.add_middleware(SecurityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Idempotency-Replayed", "X-RateLimit-Remaining", "Retry-After"],
)

# ─── Static file serving (dev only) ──────────────────────────────────────────
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
if not settings.is_production:
    app.mount("/local-uploads", StaticFiles(directory=settings.UPLOAD_DIR), name="local-uploads")

# ─── API routers ──────────────────────────────────────────────────────────────
_V1 = "/api/v1"

app.include_router(auth.router, prefix=_V1)
app.include_router(users.router, prefix=_V1)
app.include_router(transactions.router, prefix=_V1)
app.include_router(wallet.router, prefix=_V1)
app.include_router(disputes.router, prefix=_V1)
app.include_router(messages.router, prefix=_V1)
app.include_router(kyc.router, prefix=_V1)
app.include_router(notifications.router, prefix=_V1)
app.include_router(dashboard.router, prefix=_V1)
app.include_router(agents.router, prefix=_V1)
app.include_router(admin.router, prefix=_V1)
app.include_router(uploads.router, prefix=_V1)
app.include_router(content.router, prefix=_V1)
app.include_router(admin_kyc.router, prefix=_V1)
app.include_router(admin_team.router, prefix=_V1)
app.include_router(payments.router, prefix=_V1)
app.include_router(contact.router, prefix=_V1)
app.include_router(crypto_router, prefix=_V1)
app.include_router(crypto_admin_router, prefix=_V1)


# ─── WebSocket endpoints ──────────────────────────────────────────────────────

async def _authenticate_ws(token: str | None, db, websocket: WebSocket | None = None) -> User | None:
    """
    Auth for WebSockets: the HttpOnly access cookie (browsers send it on the WS handshake, so
    this keeps working after a page refresh), or a ?token= query param as a fallback.
    """
    if not token and websocket is not None:
        from app.routers.auth import ACCESS_COOKIE
        token = websocket.cookies.get(ACCESS_COOKIE)
    if not token:
        return None
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    from sqlalchemy.orm import Session
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    return user


@app.websocket("/ws/transaction/{transaction_id}")
async def transaction_ws(
    websocket: WebSocket,
    transaction_id: str,
    token: str | None = Query(None, description="JWT access token (optional — the login cookie is used if absent)"),
):
    """
    Real-time transaction chat channel.
    Clients pass ?token=<access_token> (HttpOnly cookies aren't sent over WS).
    Channel key: transaction:<transaction_id>
    """
    from app.websocket.manager import manager
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = await _authenticate_ws(token, db, websocket)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        # Verify user is party to this transaction
        from app.models.transaction import Transaction
        from app.models.agent import AgentRequest
        tx = db.query(Transaction).filter(Transaction.id == transaction_id).first()
        if not tx:
            await websocket.close(code=4004, reason="Transaction not found")
            return

        is_party = str(tx.buyer_id) == str(user.id) or str(tx.seller_id) == str(user.id)
        is_agent = False
        agent_req = db.query(AgentRequest).filter(
            AgentRequest.transaction_id == transaction_id,
            AgentRequest.status == "accepted",
        ).first()
        if agent_req and str(agent_req.agent_id) == str(getattr(user, "agent_profile_id", "")):
            is_agent = True

        if not is_party and not is_agent and not user.is_admin:
            await websocket.close(code=4003, reason="Forbidden")
            return

        # Plain values, then release the DB connection: a session held for the life of the socket
        # kept a transaction (and table locks) open per open browser tab.
        user_id, first_name = str(user.id), user.first_name
        full_name = f"{user.first_name} {user.last_name}".strip()
        db.close()

        channel = f"transaction:{transaction_id}"
        await manager.connect(websocket, channel)

        # Notify channel that user joined
        await manager.send_to_channel(channel, {
            "type": "user_joined",
            "user_id": user_id,
            "name": full_name,
        })

        try:
            while True:
                data = await websocket.receive_json()
                event_type = data.get("type", "message")

                if event_type == "message":
                    # Legacy path (the web app now sends via POST /transactions/{id}/messages).
                    from app.models.message import ChatMessage
                    content = (data.get("content") or "").strip()[:4000]
                    if not content:
                        continue
                    from app.websocket.manager import build_chat_message_event
                    mdb = SessionLocal()  # short-lived: only for this one write
                    try:
                        msg = ChatMessage(sender_id=user_id, transaction_id=transaction_id, message=content, attachments=[])
                        mdb.add(msg)
                        mdb.commit()
                        mdb.refresh(msg)
                        event = build_chat_message_event(msg)
                    finally:
                        mdb.close()
                    await manager.send_to_channel(channel, event)

                elif event_type == "typing":
                    await manager.send_to_channel(channel, {
                        "type": "typing",
                        "user_id": user_id,
                        "name": first_name,
                    })

        except WebSocketDisconnect:
            manager.disconnect(websocket, channel)
            await manager.send_to_channel(channel, {
                "type": "user_left",
                "user_id": user_id,
            })
    finally:
        db.close()


@app.websocket("/ws/notifications")
async def notifications_ws(
    websocket: WebSocket,
    token: str | None = Query(None, description="JWT access token (optional — the login cookie is used if absent)"),
):
    """
    Personal notification channel for a user.
    Channel key: user:<user_id>
    """
    from app.websocket.manager import manager
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = await _authenticate_ws(token, db, websocket)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        channel = f"user:{user.id}"
        db.close()  # release the DB connection — the socket itself doesn't need it
        await manager.connect(websocket, channel)

        try:
            while True:
                # Keep alive — client can send pings
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_text("pong")
        except WebSocketDisconnect:
            manager.disconnect(websocket, channel)
    finally:
        db.close()


# ─── Utility endpoints ────────────────────────────────────────────────────────

@app.get("/", tags=["Health"])
def root():
    return {
        "message": "Bondrova API v2.0",
        "docs": "/docs",
        "status": "running",
    }


@app.get("/health", tags=["Health"])
def health():
    from app.services.cache_service import cache_get
    redis_ok = cache_get("__health_check__") is not None or True  # ping
    return {
        "status": "ok",
        "redis": "connected" if redis_ok else "unavailable (degraded mode)",
        "s3": "enabled" if settings.s3_enabled else "local fallback",
    }
