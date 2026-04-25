#!/usr/bin/env python3
"""
Align PostgreSQL with SQLAlchemy models: create missing tables, add missing columns.

From `milevault-backend` with app dependencies installed (venv + `pip install -r requirements.txt`):

  python scripts/sync_model_columns_to_db.py                    # print ALTERs only (dry-run)
  python scripts/sync_model_columns_to_db.py --apply
  python scripts/sync_model_columns_to_db.py --apply --railway-service Postgres

Set DATABASE_URL in the environment, or use --railway-service / --database-url.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def _url_from_railway(service: str) -> str:
    raw = subprocess.check_output(
        ["railway", "variables", "--json", "-s", service],
        text=True,
    )
    data = json.loads(raw)
    return str(data.get("DATABASE_PUBLIC_URL") or data.get("DATABASE_URL") or "").strip()


def _ensure_ssl_database_url(url: str) -> str:
    if not url:
        return url
    if any(h in url for h in ("rlwy.net", "render.com", "neon.tech", "supabase")) and "railway.internal" not in url:
        if "sslmode" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _prepare_database_url(args: argparse.Namespace) -> None:
    if args.railway_service:
        url = _url_from_railway(args.railway_service)
        if not url:
            print("Railway returned no DATABASE_PUBLIC_URL / DATABASE_URL.", file=sys.stderr)
            sys.exit(1)
        if "railway.internal" in url and os.environ.get("FORCE_INTERNAL_URL") != "1":
            print(
                "Railway URL is internal-only. Use Postgres service variables (public TCP).",
                file=sys.stderr,
            )
            sys.exit(1)
        os.environ["DATABASE_URL"] = _ensure_ssl_database_url(url)
    elif args.database_url:
        os.environ["DATABASE_URL"] = _ensure_ssl_database_url(args.database_url.strip())


def _import_base_and_engine():
    from app.database import Base, engine

    return Base, engine


def _load_all_models() -> None:
    import app.models.user  # noqa: F401
    import app.models.transaction  # noqa: F401
    import app.models.wallet  # noqa: F401
    import app.models.dispute  # noqa: F401
    import app.models.message  # noqa: F401
    import app.models.kyc  # noqa: F401
    import app.models.notification  # noqa: F401
    import app.models.agent  # noqa: F401
    import app.models.currency  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync DB schema to SQLAlchemy models (PostgreSQL).")
    parser.add_argument("--apply", action="store_true", help="Execute create_all + ALTER ADD COLUMN")
    parser.add_argument("--railway-service", metavar="NAME", help="e.g. Postgres — sets DATABASE_URL from Railway")
    parser.add_argument("--database-url", dest="database_url", help="Override DATABASE_URL for this run")
    args = parser.parse_args()
    dry_run = not args.apply

    _prepare_database_url(args)

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)

    Base, engine = _import_base_and_engine()
    _load_all_models()

    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateColumn
    from sqlalchemy.dialects import postgresql

    dialect = postgresql.dialect()
    alter_stmts: list[str] = []

    if not dry_run:
        Base.metadata.create_all(bind=engine)

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            if dry_run:
                print(f"-- Missing table (would be created with --apply): {table.name}", file=sys.stderr)
            continue

        db_cols = {c["name"].lower() for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name.lower() in db_cols:
                continue
            try:
                fragment = str(CreateColumn(col).compile(dialect=dialect))
            except Exception as e:
                print(
                    f"-- SKIP {table.name}.{col.name}: compile failed ({e})",
                    file=sys.stderr,
                )
                continue
            qtable = f'"{table.name}"'
            alter_stmts.append(f"ALTER TABLE {qtable} ADD COLUMN IF NOT EXISTS {fragment}")

    for stmt in alter_stmts:
        print(stmt)

    if dry_run:
        if alter_stmts:
            print(
                f"\n{len(alter_stmts)} ALTER(s) above not executed; pass --apply to run.",
                file=sys.stderr,
            )
        else:
            print("\nNo missing columns on existing tables (tables may still be missing: use --apply).", file=sys.stderr)
        return 0

    if alter_stmts:
        with engine.begin() as conn:
            for stmt in alter_stmts:
                conn.execute(text(stmt))
        print(f"Applied {len(alter_stmts)} ADD COLUMN statement(s).", file=sys.stderr)
    else:
        print("No ADD COLUMN statements needed.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
