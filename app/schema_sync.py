"""PostgreSQL: add columns present on SQLAlchemy models but missing from the database."""
from __future__ import annotations

import logging
from typing import Any, Iterator

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateColumn
from sqlalchemy.dialects import postgresql

logger = logging.getLogger(__name__)


def iter_missing_column_statements(engine: Engine, base: Any) -> Iterator[str]:
    if engine.dialect.name != "postgresql":
        return
    insp = inspect(engine)
    dialect = postgresql.dialect()
    for table in base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        db_cols = {c["name"].lower() for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name.lower() in db_cols:
                continue
            try:
                fragment = str(CreateColumn(col).compile(dialect=dialect))
            except Exception as exc:
                logger.warning("ORM column compile skip %s.%s: %s", table.name, col.name, exc)
                continue
            yield f'ALTER TABLE "{table.name}" ADD COLUMN IF NOT EXISTS {fragment}'


def apply_missing_columns_from_metadata(engine: Engine, base: Any, log: logging.Logger | None = None) -> None:
    """Execute one transaction per statement; log and continue on failure."""
    log = log or logger
    if engine.dialect.name != "postgresql":
        return
    for stmt in iter_missing_column_statements(engine, base):
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:
            log.warning("ORM column apply skip: %s | %s", exc, stmt[:220])
