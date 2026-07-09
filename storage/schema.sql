-- FedererBot Supabase schema
-- Run once in Supabase Dashboard -> SQL Editor (already applied as
-- migration `federerbot_initial_schema`). Committed for reference.
-- Translated from the SQLite CREATE TABLE statements in sqlite_storage.py.

-- ── market_snapshots ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_snapshots (
    id               BIGSERIAL PRIMARY KEY,
    market_id        TEXT      NOT NULL,
    match_name       TEXT      NOT NULL,
    player_name      TEXT      NOT NULL,
    source           TEXT      NOT NULL,
    probability      REAL      NOT NULL,
    market_url       TEXT      NOT NULL,
    timestamp        TIMESTAMPTZ NOT NULL,
    -- microstructure (nullable)
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

-- ── market_stats ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_stats (
    market_id            TEXT  NOT NULL,
    player_name          TEXT  NOT NULL,
    source               TEXT  NOT NULL,
    opening_probability  REAL  NOT NULL,
    current_probability  REAL  NOT NULL,
    lowest_probability   REAL  NOT NULL,
    highest_probability  REAL  NOT NULL,
    new_low_alerted      INTEGER NOT NULL DEFAULT 0,
    last_updated         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market_id, player_name, source)
);

-- ── market_signals ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS market_signals (
    market_id           TEXT  NOT NULL,
    player_name         TEXT  NOT NULL,
    source              TEXT  NOT NULL,
    current_probability REAL  NOT NULL,
    velocity_1c         REAL,
    velocity_5c         REAL,
    acceleration        REAL,
    spread_current      REAL,
    spread_trend        REAL,
    volume_acceleration REAL,
    update_freq_1h      REAL,
    time_since_low_min  REAL,
    distance_from_low   REAL,
    watch_score         REAL,
    score_updated_at    TIMESTAMPTZ,
    PRIMARY KEY (market_id, player_name, source)
);

-- ── score_history ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS score_history (
    id          BIGSERIAL PRIMARY KEY,
    market_id   TEXT  NOT NULL,
    player_name TEXT  NOT NULL,
    source      TEXT  NOT NULL,
    probability REAL  NOT NULL,
    total_score REAL  NOT NULL,
    components  JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_score_history_lookup
    ON score_history (market_id, player_name, source, created_at DESC);

-- ── alerts_sent ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts_sent (
    id           BIGSERIAL PRIMARY KEY,
    market_id    TEXT  NOT NULL,
    player_name  TEXT  NOT NULL,
    source       TEXT  NOT NULL,
    anomaly_type TEXT  NOT NULL,
    prev_prob    REAL  NOT NULL,
    curr_prob    REAL  NOT NULL,
    sent_at      TIMESTAMPTZ NOT NULL,
    match_key    TEXT
);

CREATE INDEX IF NOT EXISTS idx_alert_cooldown
    ON alerts_sent (market_id, player_name, source, anomaly_type, sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_match_cooldown
    ON alerts_sent (source, match_key, sent_at DESC);
