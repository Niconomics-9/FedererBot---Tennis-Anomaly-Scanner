"""
Disposable-schema scaffolding for the verify_* scripts.

The engines write through storage.supabase_storage, which targets the live
Supabase database — synthetic test traffic must never land in the real
tables. setup() points SUPABASE_SCHEMA at a fresh throwaway schema BEFORE
config.settings is imported, so every read and write the engines make goes
there instead (search_path is part of the connection options, so even a
reconnect keeps the override). create_tables() builds the project tables
inside it and teardown() drops the whole schema afterwards.

Usage (order matters — setup() before any project import):

    from storage import verify_env
    SCHEMA = verify_env.setup()
    from core import anomaly_engine            # imports settings AFTER setup
    ...
    verify_env.create_tables()
    try:
        ...checks...
    finally:
        verify_env.teardown()
"""

import os
import uuid
from pathlib import Path

_schema: str | None = None


def setup() -> str:
    """Pick a unique throwaway schema name and export SUPABASE_SCHEMA.
    Must run before config.settings (or anything importing it) is imported."""
    global _schema
    _schema = f"verify_{uuid.uuid4().hex[:12]}"
    os.environ["SUPABASE_SCHEMA"] = _schema
    return _schema


def create_tables() -> None:
    """Create the throwaway schema and the project tables inside it."""
    from storage import supabase_storage as db

    ddl = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
    conn = db._connect()
    with conn, conn.cursor() as cur:
        cur.execute(f'CREATE SCHEMA "{_schema}"')
        cur.execute(ddl)


def teardown() -> None:
    """Drop the throwaway schema and everything in it. Best-effort: a failed
    drop is reported but never masks the failure that got us here."""
    from storage import supabase_storage as db

    if _schema is None:
        return
    try:
        conn = db._connect()
        conn.rollback()  # clear any aborted transaction left by a failing check
        with conn, conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{_schema}" CASCADE')
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not drop schema {_schema}: {exc}")
