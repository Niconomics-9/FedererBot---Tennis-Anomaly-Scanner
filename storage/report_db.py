"""
Read-only Supabase (Postgres) access for the standalone analyzer scripts.

The analyzers were written against SQLite, where timestamps are ISO-8601 TEXT
values that compare lexicographically. To keep their logic unchanged, every
datetime fetched here is converted back to a naive-UTC ISO string before the
row is handed over — the same shape sqlite rows had.

Connections are opened with default_transaction_read_only=on, so these
scripts can never write — the guarantee sqlite's mode=ro used to give.
"""

import os
from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor


def connect_readonly(dsn: str | None = None):
    """Open a read-only connection to `dsn` or $SUPABASE_DB_URL (or .env)."""
    dsn = dsn or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ModuleNotFoundError:
            pass
        dsn = os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise SystemExit(
            "SUPABASE_DB_URL is not set — export it or add it to .env "
            "(Supabase Dashboard → Connect → Session pooler string)."
        )
    options = "-c default_transaction_read_only=on -c timezone=utc"
    schema = os.environ.get("SUPABASE_SCHEMA", "")
    if schema:
        options += f" -c search_path={schema}"
    return psycopg2.connect(dsn, options=options)


def _clean(value):
    """timestamptz → naive-UTC ISO string (the sqlite TEXT convention)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat()
    return value


def rows(conn, sql: str, params=()) -> list[dict]:
    """Run a query, return dict rows with datetimes as naive-UTC ISO strings."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        return [{k: _clean(v) for k, v in r.items()} for r in cur.fetchall()]


def one(conn, sql: str, params=()) -> dict | None:
    """Like rows() but return only the first row (or None)."""
    result = rows(conn, sql, params)
    return result[0] if result else None
