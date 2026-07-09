"""
Signal engine — computes and persists basic market signals every cycle.

Called by anomaly_engine.evaluate() after each snapshot is saved and stats
are updated.  All signals are written to the market_signals table.

Signals computed
----------------
velocity_1c       prob delta vs 1 cycle ago (OFFSET 1 in desc snapshot history)
velocity_5c       prob delta vs 5 cycles ago (OFFSET 5)
acceleration      change in velocity_1c (requires 3 snapshots: curr, -1, -2)
spread_current    current bid-ask spread from snapshot (None if not provided)
spread_trend      spread delta vs prior cycle's spread (None if insufficient data)
distance_from_low current_prob - lowest_probability (from market_stats)
time_since_low_min minutes elapsed since the recorded lowest probability

Signals NOT computed here (owned by core.pre_spike_engine)
----------------------------------------------------------
volume_acceleration   stored None here; pre_spike_engine fills it in right
                      after this runs (needs two cycles of volume diffs)
update_freq_1h        computed here using count_price_changes_since()
watch_score           stored None here; pre_spike_engine writes the 0–100
                      score via db.update_score_fields() each cycle

Design notes
------------
- All computations are tolerant of missing data (returns None, not an error).
- Spread trend requires the prior market_signals row, which may not exist yet.
- Velocity requires at least 2 snapshots; acceleration requires 3.
- time_since_low_min requires the lowest probability timestamp, which is not
  currently stored in market_stats — we approximate by querying the snapshot
  table for when the lowest value was last observed.
"""

import logging
from datetime import datetime, timedelta

from market_providers.models import MarketSignals, MarketSnapshot, MarketStats
from storage import supabase_storage as db

logger = logging.getLogger(__name__)


def compute_and_save(snapshot: MarketSnapshot, stats: MarketStats) -> MarketSignals:
    """
    Compute all current signals for this snapshot and persist them.
    Returns the MarketSignals object for logging/inspection.
    """
    now = datetime.utcnow()
    curr = snapshot.probability

    # ── velocity_1c (need 1 prior snapshot) ───────────────────────────────────
    snap_1 = db.get_nth_previous_snapshot(
        snapshot.market_id, snapshot.player_name, snapshot.source, offset=1
    )
    velocity_1c: float | None = None
    if snap_1 is not None:
        velocity_1c = round(curr - snap_1.probability, 6)

    # ── velocity_5c (need 5 prior snapshots) ──────────────────────────────────
    snap_5 = db.get_nth_previous_snapshot(
        snapshot.market_id, snapshot.player_name, snapshot.source, offset=5
    )
    velocity_5c: float | None = None
    if snap_5 is not None:
        velocity_5c = round(curr - snap_5.probability, 6)

    # ── acceleration (need 2 prior snapshots to get two velocity readings) ────
    acceleration: float | None = None
    if snap_1 is not None:
        snap_2 = db.get_nth_previous_snapshot(
            snapshot.market_id, snapshot.player_name, snapshot.source, offset=2
        )
        if snap_2 is not None:
            prev_velocity = snap_1.probability - snap_2.probability
            if velocity_1c is not None:
                acceleration = round(velocity_1c - prev_velocity, 6)

    # ── spread_current ────────────────────────────────────────────────────────
    spread_current: float | None = snapshot.spread

    # ── spread_trend (delta vs prior cycle's spread) ──────────────────────────
    spread_trend: float | None = None
    if spread_current is not None and snap_1 is not None and snap_1.spread is not None:
        spread_trend = round(spread_current - snap_1.spread, 6)

    # ── distance_from_low ─────────────────────────────────────────────────────
    distance_from_low = round(curr - stats.lowest_probability, 6)

    # ── time_since_low_min ────────────────────────────────────────────────────
    # Find the most recent snapshot where the probability equals the recorded low.
    # This is an approximation — if the low was hit multiple times we use the latest.
    time_since_low_min: float | None = _time_since_low(
        snapshot.market_id,
        snapshot.player_name,
        snapshot.source,
        stats.lowest_probability,
        now,
    )

    # ── update_freq_1h ────────────────────────────────────────────────────────
    since_1h = now - timedelta(hours=1)
    update_freq_1h: int = db.count_price_changes_since(
        snapshot.market_id, snapshot.player_name, snapshot.source, since_1h
    )

    # ── volume_acceleration (owned by pre_spike_engine) ───────────────────────
    # Stored as None here; pre_spike_engine computes it from snapshot volume
    # diffs and updates the row via db.update_score_fields() each cycle.
    volume_acceleration: float | None = None

    signals = MarketSignals(
        market_id=snapshot.market_id,
        player_name=snapshot.player_name,
        source=snapshot.source,
        current_probability=curr,
        velocity_1c=velocity_1c,
        velocity_5c=velocity_5c,
        acceleration=acceleration,
        spread_current=spread_current,
        spread_trend=spread_trend,
        volume_acceleration=volume_acceleration,
        update_freq_1h=update_freq_1h if update_freq_1h is not None else 0,
        time_since_low_min=time_since_low_min,
        distance_from_low=distance_from_low,
        watch_score=None,  # written by pre_spike_engine after this returns
        score_updated_at=now,
    )

    db.upsert_signals(signals)

    logger.debug(
        "[signals] %s / %s | v1c=%s v5c=%s accel=%s spread=%s dist_low=%s t_low=%s freq1h=%s",
        snapshot.player_name,
        snapshot.match_name,
        _fmt(velocity_1c),
        _fmt(velocity_5c),
        _fmt(acceleration),
        _fmt(spread_current),
        _fmt(distance_from_low),
        _fmt(time_since_low_min),
        update_freq_1h,
    )

    return signals


# ── helpers ───────────────────────────────────────────────────────────────────

def _time_since_low(
    market_id: str,
    player_name: str,
    source: str,
    lowest_probability: float,
    now: datetime,
) -> float | None:
    """
    Minutes elapsed since the most recent snapshot at the recorded low
    (0.0001 float tolerance; if the low was hit multiple times, the latest
    touch is used).  Tolerant of any failure — returns None, never raises.
    """
    try:
        low_ts = db.get_low_touch_time(market_id, player_name, source, lowest_probability)
    except Exception:
        return None
    if low_ts is None:
        return None
    return round((now - low_ts).total_seconds() / 60, 2)


def _fmt(value: float | None) -> str:
    """Format a float signal value for debug logging."""
    if value is None:
        return "N/A"
    return f"{value:+.4f}"
