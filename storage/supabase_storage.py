"""
Supabase (PostgreSQL) storage layer — drop-in replacement for sqlite_storage.

Exposes exactly the same public API as sqlite_storage.py, backed by a
Supabase Postgres database over psycopg2. Tables are created once via
storage/schema.sql in the Supabase SQL editor (or the committed migration) —
init_db() only verifies the connection and runs the startup prunes.

Datetime convention
-------------------
The whole codebase uses NAIVE UTC datetimes (datetime.utcnow()). Postgres
TIMESTAMPTZ columns return timezone-aware datetimes, so every read converts
back to naive UTC via _as_naive_utc() to keep comparisons in the engines
working unchanged. The connection session timezone is pinned to UTC so naive
datetimes written as parameters are interpreted as UTC.

last_api_update / match_start_time remain TEXT (ISO strings), exactly as in
the SQLite schema.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extensions
from psycopg2.extras import Json, RealDictCursor

from config import settings
from market_providers.models import (
    AlertRecord,
    MarketSignals,
    MarketSnapshot,
    MarketStats,
)

logger = logging.getLogger(__name__)


# ── connection ────────────────────────────────────────────────────────────────

_conn: psycopg2.extensions.connection | None = None


def _connect() -> psycopg2.extensions.connection:
    """
    Return the shared module-level connection, creating (or re-creating) it
    on first use or after a drop. The scanner is single-threaded; one
    persistent connection avoids a TLS handshake per query. Transactions are
    managed per call site with `with conn:` (commit on success, rollback on
    exception — note this differs from sqlite3, whose context manager never
    rolls back the connection object itself).
    """
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            settings.SUPABASE_DB_URL,
            options="-c timezone=utc",
        )
        _conn.autocommit = False
    return _conn


def _as_naive_utc(dt: datetime | None) -> datetime | None:
    """Convert an aware datetime from TIMESTAMPTZ to the codebase's naive-UTC convention."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _cursor(conn):
    return conn.cursor(cursor_factory=RealDictCursor)


# ── schema / startup ──────────────────────────────────────────────────────────

def init_db() -> None:
    """
    Verify the Supabase connection is alive, then run the startup prunes.
    Tables are NOT created here — run storage/schema.sql in the Supabase SQL
    editor once (see Plans/supabase-migration.md, Step 1).
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute("SELECT 1")
    logger.info("Supabase connection verified")

    prune_score_history(settings.PRE_SPIKE_HISTORY_RETENTION_DAYS)
    prune_snapshots(settings.SNAPSHOT_RETENTION_DAYS)


# ── snapshot writes / reads ───────────────────────────────────────────────────

def save_snapshot(snapshot: MarketSnapshot) -> None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO market_snapshots
                (market_id, match_name, player_name, source, probability,
                 market_url, timestamp,
                 bid_probability, ask_probability, spread,
                 volume_total, liquidity, last_api_update, trade_count_1h,
                 match_start_time)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                snapshot.market_id,
                snapshot.match_name,
                snapshot.player_name,
                snapshot.source,
                snapshot.probability,
                snapshot.market_url,
                snapshot.timestamp,
                snapshot.bid_probability,
                snapshot.ask_probability,
                snapshot.spread,
                snapshot.volume_total,
                snapshot.liquidity,
                snapshot.last_api_update.isoformat() if snapshot.last_api_update else None,
                snapshot.trade_count_1h,
                snapshot.match_start_time.isoformat() if snapshot.match_start_time else None,
            ),
        )


def _row_to_snapshot(row) -> MarketSnapshot:
    """Reconstruct a MarketSnapshot from a database row (RealDictCursor dict)."""
    last_api_update = None
    if row["last_api_update"]:
        try:
            last_api_update = datetime.fromisoformat(row["last_api_update"])
        except ValueError:
            pass

    match_start_time = None
    if row["match_start_time"]:
        try:
            match_start_time = datetime.fromisoformat(row["match_start_time"])
        except ValueError:
            pass

    return MarketSnapshot(
        market_id=row["market_id"],
        match_name=row["match_name"],
        player_name=row["player_name"],
        probability=row["probability"],
        source=row["source"],
        market_url=row["market_url"],
        timestamp=_as_naive_utc(row["timestamp"]),
        bid_probability=row["bid_probability"],
        ask_probability=row["ask_probability"],
        spread=row["spread"],
        volume_total=row["volume_total"],
        liquidity=row["liquidity"],
        last_api_update=last_api_update,
        trade_count_1h=row["trade_count_1h"],
        match_start_time=match_start_time,
    )


def get_previous_snapshot(
    market_id: str, player_name: str, source: str
) -> MarketSnapshot | None:
    """Return the second-most-recent snapshot (the reading before this cycle)."""
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
            ORDER BY timestamp DESC
            LIMIT 1 OFFSET 1
            """,
            (market_id, player_name, source),
        )
        row = cur.fetchone()
    return _row_to_snapshot(row) if row else None


def get_nth_previous_snapshot(
    market_id: str, player_name: str, source: str, offset: int
) -> MarketSnapshot | None:
    """Return the snapshot N positions back (OFFSET n in DESC order). Used by signal engine."""
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
            ORDER BY timestamp DESC
            LIMIT 1 OFFSET %s
            """,
            (market_id, player_name, source, offset),
        )
        row = cur.fetchone()
    return _row_to_snapshot(row) if row else None


def get_snapshot_at_or_after(
    market_id: str,
    player_name: str,
    source: str,
    since: datetime,
) -> MarketSnapshot | None:
    """
    Return the oldest snapshot at or after `since`.
    Used by the FAST_MOVE rule.
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
              AND timestamp >= %s
            ORDER BY timestamp ASC
            LIMIT 1
            """,
            (market_id, player_name, source, since),
        )
        row = cur.fetchone()
    return _row_to_snapshot(row) if row else None


def get_snapshots_in_window(
    market_id: str,
    player_name: str,
    source: str,
    since: datetime,
) -> list[MarketSnapshot]:
    """
    Return all snapshots for this market/player/source in the time window
    [since, now], ordered oldest-first.
    Used by external_signals.py to avoid N serial remote queries per market.
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
              AND timestamp >= %s
            ORDER BY timestamp ASC
            """,
            (market_id, player_name, source, since),
        )
        rows = cur.fetchall()
    return [_row_to_snapshot(row) for row in rows if row]


def count_snapshots(market_id: str, player_name: str, source: str) -> int:
    """Number of stored readings for a market/player/source triple."""
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
            """,
            (market_id, player_name, source),
        )
        row = cur.fetchone()
    return row["c"] if row else 0


def count_price_changes_since(
    market_id: str,
    player_name: str,
    source: str,
    since: datetime,
) -> int:
    """
    Count how many times the probability actually changed in the last N minutes.
    Uses a window function to compare each row's probability to the prior row.
    This is the update_freq_1h signal — distinct price movements, not poll count.
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            WITH ordered AS (
                SELECT probability,
                       LAG(probability) OVER (ORDER BY timestamp) AS prev_prob
                FROM market_snapshots
                WHERE market_id = %s AND player_name = %s AND source = %s
                  AND timestamp >= %s
            )
            SELECT COUNT(*) AS changes
            FROM ordered
            WHERE prev_prob IS NOT NULL
              AND ABS(probability - prev_prob) > 0.0001
            """,
            (market_id, player_name, source, since),
        )
        row = cur.fetchone()
    return row["changes"] if row else 0


def get_low_touch_time(
    market_id: str,
    player_name: str,
    source: str,
    probability: float,
) -> datetime | None:
    """
    Timestamp of the most recent snapshot whose probability equals the given
    value (±0.0001 float tolerance).  Used for the time_since_low signal.
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT timestamp FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
              AND ABS(probability - %s) < 0.0001
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (market_id, player_name, source, probability),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return _as_naive_utc(row["timestamp"])


# ── rolling stats ─────────────────────────────────────────────────────────────

def get_stats(
    market_id: str, player_name: str, source: str
) -> MarketStats | None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_stats
            WHERE market_id = %s AND player_name = %s AND source = %s
            """,
            (market_id, player_name, source),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return MarketStats(
        market_id=row["market_id"],
        player_name=row["player_name"],
        source=row["source"],
        opening_probability=row["opening_probability"],
        current_probability=row["current_probability"],
        lowest_probability=row["lowest_probability"],
        highest_probability=row["highest_probability"],
        new_low_alerted=bool(row["new_low_alerted"]),
        last_updated=_as_naive_utc(row["last_updated"]),
    )


def upsert_stats(stats: MarketStats) -> None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO market_stats
                (market_id, player_name, source,
                 opening_probability, current_probability,
                 lowest_probability, highest_probability,
                 new_low_alerted, last_updated)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(market_id, player_name, source) DO UPDATE SET
                current_probability = excluded.current_probability,
                lowest_probability  = excluded.lowest_probability,
                highest_probability = excluded.highest_probability,
                new_low_alerted     = excluded.new_low_alerted,
                last_updated        = excluded.last_updated
            """,
            (
                stats.market_id,
                stats.player_name,
                stats.source,
                stats.opening_probability,
                stats.current_probability,
                stats.lowest_probability,
                stats.highest_probability,
                int(stats.new_low_alerted),
                stats.last_updated,
            ),
        )


# ── market signals ────────────────────────────────────────────────────────────

def get_signals(
    market_id: str, player_name: str, source: str
) -> MarketSignals | None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT * FROM market_signals
            WHERE market_id = %s AND player_name = %s AND source = %s
            """,
            (market_id, player_name, source),
        )
        row = cur.fetchone()

    if row is None:
        return None

    return MarketSignals(
        market_id=row["market_id"],
        player_name=row["player_name"],
        source=row["source"],
        current_probability=row["current_probability"],
        velocity_1c=row["velocity_1c"],
        velocity_5c=row["velocity_5c"],
        acceleration=row["acceleration"],
        spread_current=row["spread_current"],
        spread_trend=row["spread_trend"],
        volume_acceleration=row["volume_acceleration"],
        update_freq_1h=int(row["update_freq_1h"]) if row["update_freq_1h"] is not None else None,
        time_since_low_min=row["time_since_low_min"],
        distance_from_low=row["distance_from_low"],
        watch_score=row["watch_score"],
        score_updated_at=_as_naive_utc(row["score_updated_at"]),
    )


def upsert_signals(signals: MarketSignals) -> None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO market_signals
                (market_id, player_name, source, current_probability,
                 velocity_1c, velocity_5c, acceleration,
                 spread_current, spread_trend, volume_acceleration,
                 update_freq_1h, time_since_low_min, distance_from_low,
                 watch_score, score_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(market_id, player_name, source) DO UPDATE SET
                current_probability = excluded.current_probability,
                velocity_1c         = excluded.velocity_1c,
                velocity_5c         = excluded.velocity_5c,
                acceleration        = excluded.acceleration,
                spread_current      = excluded.spread_current,
                spread_trend        = excluded.spread_trend,
                volume_acceleration = excluded.volume_acceleration,
                update_freq_1h      = excluded.update_freq_1h,
                time_since_low_min  = excluded.time_since_low_min,
                distance_from_low   = excluded.distance_from_low,
                watch_score         = excluded.watch_score,
                score_updated_at    = excluded.score_updated_at
            """,
            (
                signals.market_id,
                signals.player_name,
                signals.source,
                signals.current_probability,
                signals.velocity_1c,
                signals.velocity_5c,
                signals.acceleration,
                signals.spread_current,
                signals.spread_trend,
                signals.volume_acceleration,
                signals.update_freq_1h,
                signals.time_since_low_min,
                signals.distance_from_low,
                signals.watch_score,
                signals.score_updated_at,
            ),
        )


def update_score_fields(
    market_id: str,
    player_name: str,
    source: str,
    watch_score: float,
    volume_acceleration: float | None,
    score_updated_at: datetime,
) -> None:
    """
    Targeted update written by the PRE_SPIKE engine after the signal engine
    has upserted the row for this cycle. Keeps the two engines decoupled:
    signal_engine owns every other column, this owns the score fields.
    """
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            UPDATE market_signals
            SET watch_score = %s, volume_acceleration = %s, score_updated_at = %s
            WHERE market_id = %s AND player_name = %s AND source = %s
            """,
            (
                watch_score,
                volume_acceleration,
                score_updated_at,
                market_id,
                player_name,
                source,
            ),
        )


def save_score_history(
    market_id: str,
    player_name: str,
    source: str,
    probability: float,
    total_score: float,
    components: dict[str, float],
    created_at: datetime,
) -> None:
    """Append one PRE_SPIKE scoring result to the calibration trail (JSONB)."""
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO score_history
                (market_id, player_name, source, probability,
                 total_score, components, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                market_id,
                player_name,
                source,
                probability,
                total_score,
                Json(components),
                created_at,
            ),
        )


def prune_snapshots(retention_days: int) -> None:
    """Delete market_snapshots rows older than the retention window
    (0 = keep forever). Keeps the database bounded."""
    if retention_days <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute("DELETE FROM market_snapshots WHERE timestamp < %s", (cutoff,))
        rowcount = cur.rowcount
    if rowcount:
        logger.info("[prune] Removed %d snapshots older than %d days",
                    rowcount, retention_days)


def prune_score_history(retention_days: int) -> None:
    """Delete score_history rows older than the retention window."""
    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute("DELETE FROM score_history WHERE created_at < %s", (cutoff,))
        rowcount = cur.rowcount
    if rowcount:
        logger.info("[prune] Removed %d score_history rows older than %d days",
                    rowcount, retention_days)


def get_recent_player_markets(
    source: str,
    player_name_like: str,
    since: datetime,
) -> list[tuple[str, str]]:
    """
    Distinct (market_id, player_name) pairs from `source` whose player name
    contains the given substring (case-insensitive) and which have at least
    one snapshot at or after `since`. Used by cross-exchange matching.
    """
    pattern = "%" + player_name_like.lower().replace("%", "").replace("_", "") + "%"
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT DISTINCT market_id, player_name FROM market_snapshots
            WHERE source = %s AND timestamp >= %s
              AND LOWER(player_name) LIKE %s
            """,
            (source, since, pattern),
        )
        rows = cur.fetchall()
    return [(row["market_id"], row["player_name"]) for row in rows]


# ── alert deduplication ───────────────────────────────────────────────────────

def save_alert(record: AlertRecord) -> None:
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            INSERT INTO alerts_sent
                (market_id, player_name, source, anomaly_type,
                 prev_prob, curr_prob, sent_at, match_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record.market_id,
                record.player_name,
                record.source,
                record.anomaly_type,
                record.prev_prob,
                record.curr_prob,
                record.sent_at,
                record.match_key,
            ),
        )


def was_alert_sent_recently(
    market_id: str,
    player_name: str,
    source: str,
    anomaly_type: str,
    cooldown_minutes: int,
) -> bool:
    cutoff = datetime.utcnow() - timedelta(minutes=cooldown_minutes)
    conn = _connect()
    with conn, _cursor(conn) as cur:
        cur.execute(
            """
            SELECT 1 FROM alerts_sent
            WHERE market_id = %s AND player_name = %s AND source = %s
              AND anomaly_type = %s AND sent_at >= %s
            LIMIT 1
            """,
            (market_id, player_name, source, anomaly_type, cutoff),
        )
        row = cur.fetchone()
    return row is not None


def was_match_alert_sent_recently(
    source: str,
    match_key: str,
    cooldown_minutes: int,
    anomaly_type: str | None = None,
) -> bool:
    """
    Match-level cooldown across related markets. If anomaly_type is None, any
    recent alert for the match blocks another Discord alert.
    """
    if not match_key:
        return False
    cutoff = datetime.utcnow() - timedelta(minutes=cooldown_minutes)
    conn = _connect()
    with conn, _cursor(conn) as cur:
        if anomaly_type is None:
            cur.execute(
                """
                SELECT 1 FROM alerts_sent
                WHERE source = %s AND match_key = %s AND sent_at >= %s
                LIMIT 1
                """,
                (source, match_key, cutoff),
            )
        else:
            cur.execute(
                """
                SELECT 1 FROM alerts_sent
                WHERE source = %s AND match_key = %s
                  AND anomaly_type = %s AND sent_at >= %s
                LIMIT 1
                """,
                (source, match_key, anomaly_type, cutoff),
            )
        row = cur.fetchone()
    return row is not None
