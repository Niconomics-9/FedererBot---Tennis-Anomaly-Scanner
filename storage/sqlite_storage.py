"""
SQLite storage layer.

Tables
------
market_snapshots
    Every individual probability reading, including microstructure fields.
    New columns are added via safe migration if the table already exists.

market_stats
    One row per (market_id, player_name, source).
    Rolling open / current / low / high probability + new_low flag.

market_signals
    One row per (market_id, player_name, source).
    Computed signals updated every cycle by the signal engine.

score_history
    Append-only PRE_SPIKE score trail (market_signals.watch_score is
    overwritten every cycle).  Used offline to calibrate scoring weights
    against subsequent price moves.  Pruned on startup.

alerts_sent
    Every Discord alert dispatched; used for cooldown deduplication.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from config import settings
from market_providers.models import (
    AlertRecord,
    MarketSignals,
    MarketSnapshot,
    MarketStats,
)

logger = logging.getLogger(__name__)


# ── connection ────────────────────────────────────────────────────────────────

_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    """
    Return the shared module-level connection, creating it on first use.
    The scanner is single-threaded; reusing one connection avoids re-opening
    the database for each of the tens of thousands of queries in a full scan
    cycle.  `with _connect() as conn:` blocks still commit per block — the
    connection context manager handles transactions, not connection lifetime.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.DB_PATH)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


# ── schema ────────────────────────────────────────────────────────────────────

# New nullable columns added to market_snapshots in this version.
# Each entry: (column_name, sqlite_type)
_SNAPSHOT_NEW_COLUMNS: list[tuple[str, str]] = [
    ("bid_probability",  "REAL"),
    ("ask_probability",  "REAL"),
    ("spread",           "REAL"),
    ("volume_total",     "REAL"),
    ("liquidity",        "REAL"),
    ("last_api_update",  "TEXT"),
    ("trade_count_1h",   "INTEGER"),
    ("match_start_time", "TEXT"),
]

_ALERT_NEW_COLUMNS: list[tuple[str, str]] = [
    ("match_key", "TEXT"),
]


def _migrate_market_snapshots(conn: sqlite3.Connection) -> None:
    """
    Add microstructure columns to market_snapshots if they are missing.
    SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS, so we
    check PRAGMA table_info first and only add what's absent.
    """
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
    for col_name, col_type in _SNAPSHOT_NEW_COLUMNS:
        if col_name not in existing_cols:
            conn.execute(
                f"ALTER TABLE market_snapshots ADD COLUMN {col_name} {col_type}"
            )
            logger.info("[migration] Added column market_snapshots.%s", col_name)


def _migrate_alerts_sent(conn: sqlite3.Connection) -> None:
    """Add alert-quality columns to alerts_sent if they are missing."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts_sent)")}
    for col_name, col_type in _ALERT_NEW_COLUMNS:
        if col_name not in existing_cols:
            conn.execute(f"ALTER TABLE alerts_sent ADD COLUMN {col_name} {col_type}")
            logger.info("[migration] Added column alerts_sent.%s", col_name)


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            -- ── raw snapshot history ──────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id        TEXT    NOT NULL,
                match_name       TEXT    NOT NULL,
                player_name      TEXT    NOT NULL,
                source           TEXT    NOT NULL,
                probability      REAL    NOT NULL,
                market_url       TEXT    NOT NULL,
                timestamp        TEXT    NOT NULL,
                -- microstructure (nullable — set by provider when available)
                bid_probability  REAL,
                ask_probability  REAL,
                spread           REAL,
                volume_total     REAL,
                liquidity        REAL,
                last_api_update  TEXT,
                trade_count_1h   INTEGER,
                match_start_time TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_snap_lookup
                ON market_snapshots (market_id, player_name, source, timestamp DESC);

            -- ── rolling stats ──────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS market_stats (
                market_id            TEXT    NOT NULL,
                player_name          TEXT    NOT NULL,
                source               TEXT    NOT NULL,
                opening_probability  REAL    NOT NULL,
                current_probability  REAL    NOT NULL,
                lowest_probability   REAL    NOT NULL,
                highest_probability  REAL    NOT NULL,
                new_low_alerted      INTEGER NOT NULL DEFAULT 0,
                last_updated         TEXT    NOT NULL,
                PRIMARY KEY (market_id, player_name, source)
            );

            -- ── computed signals (one row per market × player × source) ───────
            CREATE TABLE IF NOT EXISTS market_signals (
                market_id            TEXT    NOT NULL,
                player_name          TEXT    NOT NULL,
                source               TEXT    NOT NULL,
                current_probability  REAL    NOT NULL,
                velocity_1c          REAL,
                velocity_5c          REAL,
                acceleration         REAL,
                spread_current       REAL,
                spread_trend         REAL,
                volume_acceleration  REAL,
                update_freq_1h       INTEGER,
                time_since_low_min   REAL,
                distance_from_low    REAL,
                watch_score          REAL,
                score_updated_at     TEXT    NOT NULL,
                PRIMARY KEY (market_id, player_name, source)
            );

            -- ── PRE_SPIKE score trail (append-only, pruned on startup) ────────
            CREATE TABLE IF NOT EXISTS score_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id       TEXT    NOT NULL,
                player_name     TEXT    NOT NULL,
                source          TEXT    NOT NULL,
                probability     REAL    NOT NULL,
                total_score     REAL    NOT NULL,
                components_json TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_score_history
                ON score_history (market_id, player_name, source, created_at);

            -- ── alert history ─────────────────────────────────────────────────
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id    TEXT    NOT NULL,
                player_name  TEXT    NOT NULL,
                source       TEXT    NOT NULL,
                anomaly_type TEXT    NOT NULL,
                prev_prob    REAL    NOT NULL,
                curr_prob    REAL    NOT NULL,
                sent_at      TEXT    NOT NULL,
                match_key    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_alert_cooldown
                ON alerts_sent (market_id, player_name, source, anomaly_type, sent_at DESC);

            """
        )

        # Safe migration for databases created before microstructure columns existed
        _migrate_market_snapshots(conn)
        _migrate_alerts_sent(conn)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_alert_match_cooldown
                ON alerts_sent (source, match_key, sent_at DESC)
            """
        )

    prune_score_history(settings.PRE_SPIKE_HISTORY_RETENTION_DAYS)
    prune_snapshots(settings.SNAPSHOT_RETENTION_DAYS)

    logger.info("Database initialised at %s", settings.DB_PATH)


# ── snapshot writes / reads ───────────────────────────────────────────────────

def save_snapshot(snapshot: MarketSnapshot) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO market_snapshots
                (market_id, match_name, player_name, source, probability,
                 market_url, timestamp,
                 bid_probability, ask_probability, spread,
                 volume_total, liquidity, last_api_update, trade_count_1h,
                 match_start_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.market_id,
                snapshot.match_name,
                snapshot.player_name,
                snapshot.source,
                snapshot.probability,
                snapshot.market_url,
                snapshot.timestamp.isoformat(),
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


def _row_to_snapshot(row: sqlite3.Row) -> MarketSnapshot:
    """Reconstruct a MarketSnapshot from a database row."""
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
        timestamp=datetime.fromisoformat(row["timestamp"]),
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
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
            ORDER BY timestamp DESC
            LIMIT 1 OFFSET 1
            """,
            (market_id, player_name, source),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def get_nth_previous_snapshot(
    market_id: str, player_name: str, source: str, offset: int
) -> MarketSnapshot | None:
    """Return the snapshot N positions back (OFFSET n in DESC order). Used by signal engine."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
            ORDER BY timestamp DESC
            LIMIT 1 OFFSET ?
            """,
            (market_id, player_name, source, offset),
        ).fetchone()
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
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
              AND timestamp >= ?
            ORDER BY timestamp ASC
            LIMIT 1
            """,
            (market_id, player_name, source, since.isoformat()),
        ).fetchone()
    return _row_to_snapshot(row) if row else None


def count_snapshots(market_id: str, player_name: str, source: str) -> int:
    """Number of stored readings for a market/player/source triple."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
            """,
            (market_id, player_name, source),
        ).fetchone()
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
    with _connect() as conn:
        row = conn.execute(
            """
            WITH ordered AS (
                SELECT probability,
                       LAG(probability) OVER (ORDER BY timestamp) AS prev_prob
                FROM market_snapshots
                WHERE market_id = ? AND player_name = ? AND source = ?
                  AND timestamp >= ?
                ORDER BY timestamp
            )
            SELECT COUNT(*) AS changes
            FROM ordered
            WHERE prev_prob IS NOT NULL
              AND ABS(probability - prev_prob) > 0.0001
            """,
            (market_id, player_name, source, since.isoformat()),
        ).fetchone()
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
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT timestamp FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
              AND ABS(probability - ?) < 0.0001
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (market_id, player_name, source, probability),
        ).fetchone()
    if row is None:
        return None
    try:
        return datetime.fromisoformat(row["timestamp"])
    except ValueError:
        return None


# ── rolling stats ─────────────────────────────────────────────────────────────

def get_stats(
    market_id: str, player_name: str, source: str
) -> MarketStats | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM market_stats
            WHERE market_id = ? AND player_name = ? AND source = ?
            """,
            (market_id, player_name, source),
        ).fetchone()

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
        last_updated=datetime.fromisoformat(row["last_updated"]),
    )


def upsert_stats(stats: MarketStats) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO market_stats
                (market_id, player_name, source,
                 opening_probability, current_probability,
                 lowest_probability, highest_probability,
                 new_low_alerted, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                stats.last_updated.isoformat(),
            ),
        )


# ── market signals ────────────────────────────────────────────────────────────

def get_signals(
    market_id: str, player_name: str, source: str
) -> MarketSignals | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM market_signals
            WHERE market_id = ? AND player_name = ? AND source = ?
            """,
            (market_id, player_name, source),
        ).fetchone()

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
        update_freq_1h=row["update_freq_1h"],
        time_since_low_min=row["time_since_low_min"],
        distance_from_low=row["distance_from_low"],
        watch_score=row["watch_score"],
        score_updated_at=datetime.fromisoformat(row["score_updated_at"]),
    )


def upsert_signals(signals: MarketSignals) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO market_signals
                (market_id, player_name, source, current_probability,
                 velocity_1c, velocity_5c, acceleration,
                 spread_current, spread_trend, volume_acceleration,
                 update_freq_1h, time_since_low_min, distance_from_low,
                 watch_score, score_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                signals.score_updated_at.isoformat(),
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
    with _connect() as conn:
        conn.execute(
            """
            UPDATE market_signals
            SET watch_score = ?, volume_acceleration = ?, score_updated_at = ?
            WHERE market_id = ? AND player_name = ? AND source = ?
            """,
            (
                watch_score,
                volume_acceleration,
                score_updated_at.isoformat(),
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
    """Append one PRE_SPIKE scoring result to the calibration trail."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO score_history
                (market_id, player_name, source, probability,
                 total_score, components_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                player_name,
                source,
                probability,
                total_score,
                json.dumps(components),
                created_at.isoformat(),
            ),
        )


def prune_snapshots(retention_days: int) -> None:
    """Delete market_snapshots rows older than the retention window
    (0 = keep forever). Keeps the database bounded on hosts where it is
    persisted between runs (e.g. the GitHub Actions cache)."""
    if retention_days <= 0:
        return
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM market_snapshots WHERE timestamp < ?", (cutoff,)
        )
    if cursor.rowcount:
        logger.info("[prune] Removed %d snapshots older than %d days",
                    cursor.rowcount, retention_days)


def prune_score_history(retention_days: int) -> None:
    """Delete score_history rows older than the retention window."""
    cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM score_history WHERE created_at < ?", (cutoff,)
        )
    if cursor.rowcount:
        logger.info("[prune] Removed %d score_history rows older than %d days",
                    cursor.rowcount, retention_days)


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
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT market_id, player_name FROM market_snapshots
            WHERE source = ? AND timestamp >= ?
              AND LOWER(player_name) LIKE ?
            """,
            (source, since.isoformat(), pattern),
        ).fetchall()
    return [(row["market_id"], row["player_name"]) for row in rows]


# ── alert deduplication ───────────────────────────────────────────────────────

def save_alert(record: AlertRecord) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO alerts_sent
                (market_id, player_name, source, anomaly_type,
                 prev_prob, curr_prob, sent_at, match_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.market_id,
                record.player_name,
                record.source,
                record.anomaly_type,
                record.prev_prob,
                record.curr_prob,
                record.sent_at.isoformat(),
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
    cutoff = (datetime.utcnow() - timedelta(minutes=cooldown_minutes)).isoformat()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM alerts_sent
            WHERE market_id = ? AND player_name = ? AND source = ?
              AND anomaly_type = ? AND sent_at >= ?
            LIMIT 1
            """,
            (market_id, player_name, source, anomaly_type, cutoff),
        ).fetchone()
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
    cutoff = (datetime.utcnow() - timedelta(minutes=cooldown_minutes)).isoformat()
    with _connect() as conn:
        if anomaly_type is None:
            row = conn.execute(
                """
                SELECT 1 FROM alerts_sent
                WHERE source = ? AND match_key = ? AND sent_at >= ?
                LIMIT 1
                """,
                (source, match_key, cutoff),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT 1 FROM alerts_sent
                WHERE source = ? AND match_key = ?
                  AND anomaly_type = ? AND sent_at >= ?
                LIMIT 1
                """,
                (source, match_key, anomaly_type, cutoff),
            ).fetchone()
    return row is not None
