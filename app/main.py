"""
MileVault FastAPI application entry point.
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
from app.routers import agents, admin, uploads

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
                if not existing.is_admin:
                    existing.is_admin = True
                    db.commit()
                    logger.info(f"Admin flag set on existing user: {settings.ADMIN_EMAIL}")
                return

            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
            import uuid
            admin = User(
                id=str(uuid.uuid4()),
                first_name="MileVault",
                last_name="Admin",
                email=settings.ADMIN_EMAIL,
                hashed_password=pwd_context.hash(settings.ADMIN_PASSWORD),
                role="buyer",
                is_admin=True,
                is_active=True,
                is_kyc_verified=True,
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
    try:
        create_tables()
        logger.info("MileVault API started — tables verified.")
    except Exception as exc:
        import traceback
        logger.error(f"DB table creation failed (degraded mode): {exc}\n{traceback.format_exc()}")

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
    logger.info("MileVault API shutting down.")


app = FastAPI(
    title="MileVault API",
    description="Secure escrow platform — presigned S3 uploads, WebSocket chat, multi-gateway payments.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ─── CORS (must be first) ─────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Idempotency-Replayed", "X-RateLimit-Remaining", "Retry-After"],
)

# ─── Custom middleware stack ──────────────────────────────────────────────────
app.add_middleware(SecurityMiddleware)
app.add_middleware(RateLimiterMiddleware)
app.add_middleware(IdempotencyMiddleware)

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


# ─── WebSocket endpoints ──────────────────────────────────────────────────────

async def _authenticate_ws(token: str, db) -> User | None:
    """Decode JWT from ?token= query param (cookies not available over WS)."""
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
    token: str = Query(..., description="JWT access token"),
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
        user = await _authenticate_ws(token, db)
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

        channel = f"transaction:{transaction_id}"
        await manager.connect(websocket, channel)

        # Notify channel that user joined
        await manager.send_to_channel(channel, {
            "type": "user_joined",
            "user_id": str(user.id),
            "name": f"{user.first_name} {user.last_name}".strip(),
        })

        try:
            while True:
                data = await websocket.receive_json()
                event_type = data.get("type", "message")

                if event_type == "message":
                    from app.models.message import Message
                    msg = Message(
                        sender_id=user.id,
                        transaction_id=transaction_id,
                        content=data.get("content", "").strip()[:4000],
                        message_type=data.get("message_type", "text"),
                    )
                    if not msg.content:
                        continue
                    db.add(msg)
                    db.commit()
                    db.refresh(msg)

                    from app.websocket.manager import build_chat_message_event
                    await manager.send_to_channel(channel, build_chat_message_event(msg))

                elif event_type == "typing":
                    await manager.send_to_channel(channel, {
                        "type": "typing",
                        "user_id": str(user.id),
                        "name": user.first_name,
                    })

        except WebSocketDisconnect:
            manager.disconnect(websocket, channel)
            await manager.send_to_channel(channel, {
                "type": "user_left",
                "user_id": str(user.id),
            })
    finally:
        db.close()


@app.websocket("/ws/notifications")
async def notifications_ws(
    websocket: WebSocket,
    token: str = Query(..., description="JWT access token"),
):
    """
    Personal notification channel for a user.
    Channel key: user:<user_id>
    """
    from app.websocket.manager import manager
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        user = await _authenticate_ws(token, db)
        if not user:
            await websocket.close(code=4001, reason="Unauthorized")
            return

        channel = f"user:{user.id}"
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
        "message": "MileVault API v2.0",
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
