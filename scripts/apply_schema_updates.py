#!/usr/bin/env python3
"""
Apply incremental SQL (ALTERs / CREATE IF NOT EXISTS) to Postgres.

One command (from a Railway-linked directory):

  python3 scripts/apply_schema_updates.py --railway-service Postgres

Or set DATABASE_URL yourself (public / TCP URL, not *.railway.internal):

  python3 scripts/apply_schema_updates.py

Uses `psql` when available (no extra Python deps). Otherwise uses SQLAlchemy if installed.

To align *all* ORM columns with the database (recommended after model changes), use:

  python scripts/sync_model_columns_to_db.py --apply --railway-service Postgres

(requires app venv / same deps as the API.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv() -> None:
        return None


def _default_sql_path() -> Path:
    return Path(__file__).resolve().parent / "schema_updates_milevault_2026_04.sql"


def _split_sql(raw: str) -> list[str]:
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
    clean = "\n".join(lines)
    return [s.strip() for s in clean.split(";") if s.strip()]


def _apply_with_psql(url: str, sql_file: Path) -> None:
    subprocess.run(
        ["psql", url, "-v", "ON_ERROR_STOP=1", "-f", str(sql_file)],
        check=True,
    )


def _apply_with_sqlalchemy(url: str, stmts: list[str]) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url, pool_pre_ping=True)
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def _url_from_railway(service: str) -> str:
    raw = subprocess.check_output(
        ["railway", "variables", "--json", "-s", service],
        text=True,
    )
    data = json.loads(raw)
    return str(data.get("DATABASE_PUBLIC_URL") or data.get("DATABASE_URL") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run schema update SQL against DATABASE_URL.")
    parser.add_argument(
        "-f",
        "--file",
        type=Path,
        default=_default_sql_path(),
        help="SQL file to apply",
    )
    parser.add_argument(
        "--railway-service",
        metavar="NAME",
        help="Fetch DATABASE_PUBLIC_URL from `railway variables -s NAME` (e.g. Postgres). "
        "Wins over .env for this run.",
    )
    args = parser.parse_args()

    if args.railway_service:
        url = _url_from_railway(args.railway_service).strip()
        if not url:
            print("Railway returned no DATABASE_PUBLIC_URL / DATABASE_URL.", file=sys.stderr)
            return 1
        if "railway.internal" in url and os.environ.get("FORCE_INTERNAL_URL") != "1":
            print(
                "Railway variables only had *.railway.internal — use Postgres service variables "
                "or enable public TCP / DATABASE_PUBLIC_URL.",
                file=sys.stderr,
            )
            return 1
        os.environ["DATABASE_URL"] = url

    load_dotenv()  # type: ignore[misc]
    url = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
    if not url:
        print(
            "Missing DATABASE_URL (or DATABASE_PUBLIC_URL).\n"
            "Railway (from Mac): Postgres service → Connect → public URL, then:\n"
            "  export DATABASE_URL='postgresql://…'\n"
            "  python3 scripts/apply_schema_updates.py",
            file=sys.stderr,
        )
        return 1

    if "railway.internal" in url and os.environ.get("FORCE_INTERNAL_URL") != "1":
        print(
            "DATABASE_URL uses *.railway.internal — not reachable from your laptop.\n"
            "Use the public Postgres URL from Railway, or run SQL inside: railway ssh\n"
            "Override (inside Railway only): FORCE_INTERNAL_URL=1",
            file=sys.stderr,
        )
        return 1

    path: Path = args.file
    if not path.is_file():
        print(f"SQL file not found: {path}", file=sys.stderr)
        return 1

    try:
        if shutil.which("psql"):
            _apply_with_psql(url, path)
            print(f"OK — psql applied {path.name}")
            return 0
        stmts = _split_sql(path.read_text())
        _apply_with_sqlalchemy(url, stmts)
        print(f"OK — applied {len(stmts)} statements via SQLAlchemy from {path.name}")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"psql failed (exit {e.returncode})", file=sys.stderr)
        return 1
    except ModuleNotFoundError:
        print(
            "Neither `psql` nor SQLAlchemy is available.\n"
            "Install Postgres client (psql) or: pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        print(f"Apply failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
