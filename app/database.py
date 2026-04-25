from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

# Railway Postgres requires SSL; add sslmode=require if not already present
_db_url = settings.DATABASE_URL
# Only add SSL for external/proxy hosts — internal Railway network doesn't need it
_needs_ssl = any(h in _db_url for h in ("rlwy.net", "render.com", "neon.tech", "supabase")) and \
             "railway.internal" not in _db_url
if _needs_ssl and "sslmode" not in _db_url:
    _db_url += ("&" if "?" in _db_url else "?") + "sslmode=require"

engine = create_engine(_db_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _incremental_schema_statements() -> list[str]:
    """Parse scripts/schema_updates_milevault_2026_04.sql into executable statements."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "schema_updates_milevault_2026_04.sql"
    if not path.is_file():
        return []
    raw = path.read_text()
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    clean = "\n".join(lines)
    return [s.strip() for s in clean.split(";") if s.strip()]


def create_tables():
    from app.models import user, transaction, wallet, dispute, message, kyc, notification  # noqa
    from app.models import agent, currency  # noqa
    Base.metadata.create_all(bind=engine)
    # create_all does not add new columns to existing tables — apply idempotent ALTERs
    # against the same DATABASE_URL the app uses (fixes Railway drift vs CLI migrations).
    if engine.dialect.name != "postgresql":
        return
    stmts = _incremental_schema_statements()
    if stmts:
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))
