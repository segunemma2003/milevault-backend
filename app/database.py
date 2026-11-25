import re
import logging
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
_schema_log = logging.getLogger(__name__)


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


_ADD_COLUMN = re.compile(r'\s*ALTER\s+TABLE\s+"?(\w+)"?\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"?(\w+)"?', re.I)


def _run_ddl(stmt: str, attempts: int = 4) -> None:
    """Run one DDL statement without ever hanging startup: wait at most 5s for its table lock,
    then retry (another container's transaction usually finishes within seconds)."""
    import time
    for i in range(attempts):
        try:
            with engine.begin() as conn:
                conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                conn.execute(text(stmt))
            return
        except Exception as exc:
            if "lock timeout" not in str(exc).lower() or i == attempts - 1:
                raise
            _schema_log.warning("Schema: table busy, retrying (%s/%s): %s", i + 1, attempts, stmt[:120])
            time.sleep(2)


def create_tables():
    from app.models import user, transaction, wallet, dispute, message, kyc, notification  # noqa
    from app.models import agent, currency, crypto  # noqa
    from app.models import content  # noqa
    from app.models import payments  # noqa
    from app.models import support  # noqa
    Base.metadata.create_all(bind=engine)
    # create_all does not add new columns to existing tables — apply idempotent ALTERs
    # against the same DATABASE_URL the app uses (fixes Railway drift vs CLI migrations).
    if engine.dialect.name != "postgresql":
        return
    stmts = _incremental_schema_statements()
    with engine.connect() as conn:
        existing = {(r[0], r[1]) for r in conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"))}
    # One transaction per statement: if a late CREATE fails, PostgreSQL must not roll back
    # earlier ALTER ADD COLUMN (e.g. transactions.funding_deadline).
    for stmt in stmts:
        head = stmt.lstrip()[:20].upper()
        m = _ADD_COLUMN.match(stmt)
        if m and (m.group(1).lower(), m.group(2).lower()) in existing:
            # Already there. Skipping matters: even "ADD COLUMN IF NOT EXISTS" takes an ACCESS
            # EXCLUSIVE lock, and waiting for it behind the previous container's open
            # transactions froze startup past Railway's health check.
            continue
        try:
            _run_ddl(stmt)
        except Exception as exc:
            if head.startswith("CREATE TABLE") or head.startswith("CREATE INDEX"):
                _schema_log.warning(
                    "Incremental schema optional DDL failed (continuing): %s | %s",
                    exc,
                    stmt[:200],
                )
                continue
            raise

    # Second pass: anything the SQL file missed or failed to compile — matches live ORM models.
    from app.schema_sync import apply_missing_columns_from_metadata

    apply_missing_columns_from_metadata(engine, Base, log=_schema_log)

    _apply_money_precision()


# Money columns stored as exact NUMERIC(28,8) (8 dp covers satoshi; NGN/USD are quantised
# to 2 dp and USDT/USDC to 6 dp by app/services/ledger.py). The ORM type stays Float so
# existing call sites keep working; values round-trip exactly at this scale.
_MONEY_COLUMNS = [
    ("wallet_balances", "amount"),
    ("wallet_balances", "escrow_amount"),
    ("wallet_balances", "pending_amount"),
    ("wallet_transactions", "amount"),
    ("ledger_entries", "amount"),
    ("transactions", "amount"),
    ("milestones", "amount"),
    ("milestones", "funded_amount"),
    ("crypto_pending_deposits", "amount_crypto"),
    ("crypto_pending_deposits", "fiat_amount"),
]


def _apply_money_precision() -> None:
    """Idempotent: converts double-precision money columns to NUMERIC(28,8). Never blocks startup."""
    if engine.dialect.name != "postgresql":
        return
    if not settings.MONEY_PRECISION_MIGRATION:
        _schema_log.warning("Money precision migration is OFF (set MONEY_PRECISION_MIGRATION=true after a DB backup).")
    for table, column in (_MONEY_COLUMNS if settings.MONEY_PRECISION_MIGRATION else []):
        try:
            with engine.begin() as conn:
                dtype = conn.execute(
                    text(
                        "SELECT data_type FROM information_schema.columns "
                        "WHERE table_name = :t AND column_name = :c"
                    ),
                    {"t": table, "c": column},
                ).scalar()
                if dtype == "double precision":
                    conn.execute(text(
                        f'ALTER TABLE "{table}" ALTER COLUMN "{column}" '
                        f'TYPE NUMERIC(28,8) USING ROUND("{column}"::numeric, 8)'
                    ))
                    _schema_log.warning("Money precision: %s.%s converted to NUMERIC(28,8)", table, column)
        except Exception as exc:
            _schema_log.warning("Money precision: could not convert %s.%s: %s", table, column, exc)

    # One balance row per (user, currency). Skipped (with a warning) if duplicates exist.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_wallet_balances_user_currency "
                "ON wallet_balances (user_id, currency)"
            ))
    except Exception as exc:
        _schema_log.warning("Could not create unique index on wallet_balances(user_id, currency): %s", exc)
