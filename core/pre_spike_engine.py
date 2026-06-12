"""
PRE_SPIKE scoring engine — flags markets whose odds are likely to MOVE soon,
from microstructure pressure rather than the price move itself.

The goal is wave-chasing, not winner-picking: an alert is good when the price
moves afterwards (entry → ride → exit pre-match), regardless of who wins the
match. Being a low odd earns almost nothing by itself — points come from
evidence of building pressure.

Deterministic 0–100 score from four buckets (no ML, no execution):

  1. Market pressure        0–52   spread tightening, volume surge, liquidity
                                   rising, positive velocity/acceleration,
                                   sustained multi-cycle drift, update
                                   frequency rising
  2. Band setup             0–25   small eligibility credit (the PRE_SPIKE
                                   band from settings), price coiled near its
                                   low and turning upward, low touched recently
  3. Cross-exchange         0–6    equivalent Kalshi market already moving up
                                   (placeholder matching — see cross_exchange)
  4. External momentum      0–10   news / social / schedule context — stub,
                                   contributes 0 until integrated

An alert therefore requires broad, simultaneous movement evidence. The
probability band and alert-quality gates (including the pre-match-only gate)
are enforced before Discord is touched.

The total is persisted to market_signals.watch_score every cycle (alert or
not), volume_acceleration is filled in (it was reserved by signal_engine),
and every component is logged for explainability.

Alerting: PRE_SPIKE_CANDIDATE at score >= PRE_SPIKE_ALERT_SCORE, flagged
urgent at >= PRE_SPIKE_URGENT_SCORE.  Cooldown allows one standard alert per
market per window plus a single escalation to urgent inside that window.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from config import settings
from core import alert_quality, cross_exchange, external_signals, market_classifier
from market_providers.models import (
    AnomalyEvent,
    AnomalyType,
    MarketSignals,
    MarketSnapshot,
    MarketStats,
)
from storage import sqlite_storage as db

logger = logging.getLogger(__name__)

# Alert-history type string for urgent records; lets the cooldown distinguish
# escalations without adding a second AnomalyType.
URGENT_ALERT_TYPE = AnomalyType.PRE_SPIKE_CANDIDATE.value + "_URGENT"

# ── bucket 1: market pressure (max 52) ────────────────────────────────────────
_PTS_SPREAD_STRONG, _PTS_SPREAD_WEAK = 6.0, 3.0     # spread_trend <= -0.005 / < 0
_SPREAD_STRONG_DELTA = -0.005
_PTS_VOLUME_SURGE, _PTS_VOLUME_UP = 12.0, 5.0       # accelerating / merely rising
_VOLUME_SURGE_RATIO = 1.5                           # this cycle's inflow vs prior cycle's
_PTS_LIQ_STRONG, _PTS_LIQ_WEAK = 5.0, 2.0           # +5% depth / any increase
_LIQ_STRONG_RATIO = 0.05
_PTS_VEL_STRONG, _PTS_VEL_WEAK = 8.0, 4.0           # velocity_1c >= 0.01 / > 0
_VEL_STRONG = 0.01
_PTS_ACCEL_STRONG, _PTS_ACCEL_WEAK = 5.0, 2.0       # acceleration >= 0.005 / > 0
_ACCEL_STRONG = 0.005
_PTS_SUSTAINED_STRONG, _PTS_SUSTAINED_WEAK = 10.0, 5.0  # velocity_5c >= 0.02 / > 0.005
_SUSTAINED_STRONG, _SUSTAINED_WEAK = 0.02, 0.005    # multi-cycle drift, not one tick
_PTS_FREQ_STRONG, _PTS_FREQ_WEAK = 6.0, 3.0         # rising with >= 2 changes / rising
_FREQ_WINDOW_MINUTES = 30                           # recent window vs the one before it

# ── bucket 2: band setup (max 25) ─────────────────────────────────────────────
# In-band membership is mostly an eligibility fact, not movement evidence —
# it earns a token 5 so breakdowns stay explainable, no more.
_PTS_IN_BAND = 5.0
_PTS_COILED_STRONG, _PTS_COILED_WEAK = 12.0, 6.0    # near low + turning up
_COILED_STRONG_DIST, _COILED_WEAK_DIST = 0.02, 0.04
_PTS_NEW_LOW_STRONG, _PTS_NEW_LOW_WEAK = 8.0, 4.0   # low touched recently
_NEW_LOW_STRONG_MIN, _NEW_LOW_WEAK_MIN = 60.0, 180.0

# ── bucket 3: cross-exchange confirmation (max 6) ─────────────────────────────
_PTS_KALSHI_STRONG, _PTS_KALSHI_WEAK = 6.0, 3.0
_KALSHI_STRONG_V5C, _KALSHI_STRONG_V1C = 0.02, 0.01


@dataclass
class PreSpikeScore:
    """Full scoring result — every component kept for logs and alerts."""
    total:               float
    components:          dict[str, float]
    notes:               dict[str, str] = field(default_factory=dict)
    volume_acceleration: float | None = None


def evaluate(
    snapshot: MarketSnapshot,
    stats: MarketStats,
    signals: MarketSignals,
) -> list[AnomalyEvent]:
    """
    Score this snapshot, persist the score, log the breakdown, and return a
    PRE_SPIKE_CANDIDATE event when the alert conditions and cooldown allow.
    Called by anomaly_engine after signal_engine has run for this cycle.
    """
    now = datetime.utcnow()
    score = compute_score(snapshot, stats, signals, now)

    db.update_score_fields(
        snapshot.market_id,
        snapshot.player_name,
        snapshot.source,
        watch_score=score.total,
        volume_acceleration=score.volume_acceleration,
        score_updated_at=now,
    )
    _record_history(snapshot, score, now)
    _log_breakdown(snapshot, score)

    # Hard eligibility gate: PRE_SPIKE only targets the tradeable band.
    # (Also enforced structurally — see module docstring — but explicit here.)
    if not (settings.PRE_SPIKE_PROB_MIN <= snapshot.probability <= settings.PRE_SPIKE_PROB_MAX):
        return []

    if score.total < settings.PRE_SPIKE_ALERT_SCORE:
        return []

    quality = alert_quality.check(snapshot, AnomalyType.PRE_SPIKE_CANDIDATE)
    if not quality.passed:
        logger.debug(
            "[quality] PRE_SPIKE_CANDIDATE skipped for %s / %s: %s",
            snapshot.player_name, snapshot.match_name, quality.reason,
        )
        return []

    urgent = score.total >= settings.PRE_SPIKE_URGENT_SCORE
    if _on_cooldown(snapshot, urgent):
        return []

    prev_prob = (
        round(snapshot.probability - signals.velocity_1c, 6)
        if signals.velocity_1c is not None
        else None
    )
    logger.info(
        "[PRE_SPIKE_CANDIDATE%s] %s / %s at %.1f%% — score %.1f",
        " URGENT" if urgent else "",
        snapshot.match_name, snapshot.player_name,
        snapshot.probability * 100, score.total,
    )
    return [AnomalyEvent(
        anomaly_type=AnomalyType.PRE_SPIKE_CANDIDATE,
        snapshot=snapshot,
        prev_probability=prev_prob,
        current_probability=snapshot.probability,
        move=signals.velocity_1c,
        time_window_minutes=None,
        stats=stats,
        watch_score=score.total,
        score_breakdown=score.components,
        urgent=urgent,
    )]


def compute_score(
    snapshot: MarketSnapshot,
    stats: MarketStats,
    signals: MarketSignals,
    now: datetime,
) -> PreSpikeScore:
    """Deterministic 0–100 score. Tolerant of missing data (None scores 0)."""
    components: dict[str, float] = {}
    notes: dict[str, str] = {}

    snap_1 = db.get_nth_previous_snapshot(
        snapshot.market_id, snapshot.player_name, snapshot.source, offset=1
    )
    snap_2 = db.get_nth_previous_snapshot(
        snapshot.market_id, snapshot.player_name, snapshot.source, offset=2
    )

    volume_acceleration = _score_market_pressure(
        snapshot, signals, snap_1, snap_2, now, components
    )
    _score_low_odds_setup(snapshot, signals, components)
    _score_cross_exchange(snapshot, components, notes)
    _score_external(snapshot, components)

    total = round(min(sum(components.values()), 100.0), 2)
    return PreSpikeScore(
        total=total,
        components=components,
        notes=notes,
        volume_acceleration=volume_acceleration,
    )


# ── bucket 1: market pressure ─────────────────────────────────────────────────

def _score_market_pressure(
    snapshot: MarketSnapshot,
    signals: MarketSignals,
    snap_1: MarketSnapshot | None,
    snap_2: MarketSnapshot | None,
    now: datetime,
    components: dict[str, float],
) -> float | None:
    """Fill pressure components; returns volume_acceleration for persistence."""

    # spread tightening
    pts = 0.0
    if signals.spread_trend is not None and signals.spread_trend < 0:
        pts = _PTS_SPREAD_STRONG if signals.spread_trend <= _SPREAD_STRONG_DELTA else _PTS_SPREAD_WEAK
    components["pressure.spread_tightening"] = pts

    # volume surge — compare this cycle's volume inflow to the prior cycle's
    vol_accel: float | None = None
    vol_delta_now: float | None = None
    vol_delta_prev: float | None = None
    if snapshot.volume_total is not None and snap_1 is not None and snap_1.volume_total is not None:
        vol_delta_now = snapshot.volume_total - snap_1.volume_total
        if snap_2 is not None and snap_2.volume_total is not None:
            vol_delta_prev = snap_1.volume_total - snap_2.volume_total
            vol_accel = round(vol_delta_now - vol_delta_prev, 6)
    pts = 0.0
    if vol_delta_now is not None and vol_delta_now > 0:
        surging = (
            vol_delta_prev is not None
            and vol_delta_now > _VOLUME_SURGE_RATIO * max(vol_delta_prev, 0.0)
        )
        pts = _PTS_VOLUME_SURGE if surging else _PTS_VOLUME_UP
    components["pressure.volume_surge"] = pts

    # liquidity increase
    pts = 0.0
    if snapshot.liquidity is not None and snap_1 is not None and snap_1.liquidity is not None:
        liq_delta = snapshot.liquidity - snap_1.liquidity
        if liq_delta > 0:
            strong = snap_1.liquidity > 0 and liq_delta >= _LIQ_STRONG_RATIO * snap_1.liquidity
            pts = _PTS_LIQ_STRONG if strong else _PTS_LIQ_WEAK
    components["pressure.liquidity_rising"] = pts

    # velocity positive
    pts = 0.0
    if signals.velocity_1c is not None and signals.velocity_1c > 0:
        pts = _PTS_VEL_STRONG if signals.velocity_1c >= _VEL_STRONG else _PTS_VEL_WEAK
    components["pressure.velocity_positive"] = pts

    # acceleration positive
    pts = 0.0
    if signals.acceleration is not None and signals.acceleration > 0:
        pts = _PTS_ACCEL_STRONG if signals.acceleration >= _ACCEL_STRONG else _PTS_ACCEL_WEAK
    components["pressure.acceleration_positive"] = pts

    # sustained move — drift held across 5 cycles, the clearest wave precursor
    # pre-match (velocity_1c alone can be a single repricing tick)
    pts = 0.0
    if signals.velocity_5c is not None and signals.velocity_5c > _SUSTAINED_WEAK:
        pts = _PTS_SUSTAINED_STRONG if signals.velocity_5c >= _SUSTAINED_STRONG else _PTS_SUSTAINED_WEAK
    components["pressure.sustained_move"] = pts

    # update frequency rising — price changes in the last 30 min vs the 30 min before
    recent = db.count_price_changes_since(
        snapshot.market_id, snapshot.player_name, snapshot.source,
        now - timedelta(minutes=_FREQ_WINDOW_MINUTES),
    )
    both_windows = db.count_price_changes_since(
        snapshot.market_id, snapshot.player_name, snapshot.source,
        now - timedelta(minutes=2 * _FREQ_WINDOW_MINUTES),
    )
    prior = both_windows - recent
    pts = 0.0
    if recent > prior:
        pts = _PTS_FREQ_STRONG if recent >= 2 else _PTS_FREQ_WEAK
    components["pressure.update_freq_rising"] = pts

    return vol_accel


# ── bucket 2: low-odds setup ──────────────────────────────────────────────────

def _score_low_odds_setup(
    snapshot: MarketSnapshot,
    signals: MarketSignals,
    components: dict[str, float],
) -> None:
    in_band = settings.PRE_SPIKE_PROB_MIN <= snapshot.probability <= settings.PRE_SPIKE_PROB_MAX

    # Outside the band the whole bucket is moot — zero it for clean breakdowns.
    if not in_band:
        components["low_odds.in_band"] = 0.0
        components["low_odds.coiled_turning_up"] = 0.0
        components["low_odds.recent_new_low"] = 0.0
        return

    components["low_odds.in_band"] = _PTS_IN_BAND

    # coiled near the low and turning upward
    pts = 0.0
    dist = signals.distance_from_low
    turning_up = signals.velocity_1c is not None and signals.velocity_1c > 0
    if dist is not None and turning_up:
        if dist <= _COILED_STRONG_DIST:
            pts = _PTS_COILED_STRONG
        elif dist <= _COILED_WEAK_DIST:
            pts = _PTS_COILED_WEAK
    components["low_odds.coiled_turning_up"] = pts

    # recently touched a new low
    pts = 0.0
    t_low = signals.time_since_low_min
    if t_low is not None:
        if t_low <= _NEW_LOW_STRONG_MIN:
            pts = _PTS_NEW_LOW_STRONG
        elif t_low <= _NEW_LOW_WEAK_MIN:
            pts = _PTS_NEW_LOW_WEAK
    components["low_odds.recent_new_low"] = pts


# ── bucket 3: cross-exchange confirmation ─────────────────────────────────────

def _score_cross_exchange(
    snapshot: MarketSnapshot,
    components: dict[str, float],
    notes: dict[str, str],
) -> None:
    confirmation = cross_exchange.find_confirmation(snapshot)
    notes["cross_exchange"] = confirmation.note

    pts = 0.0
    if confirmation.matched:
        v1 = confirmation.kalshi_velocity_1c
        v5 = confirmation.kalshi_velocity_5c
        strong = (v5 is not None and v5 >= _KALSHI_STRONG_V5C) or (
            v1 is not None and v1 >= _KALSHI_STRONG_V1C
        )
        rising = (v5 is not None and v5 > 0) or (v1 is not None and v1 > 0)
        if strong:
            pts = _PTS_KALSHI_STRONG
        elif rising:
            pts = _PTS_KALSHI_WEAK
        notes["cross_exchange"] += (
            f" (kalshi {confirmation.kalshi_market_id} v1c={v1} v5c={v5})"
        )
    components["cross.kalshi_confirmation"] = pts


# ── bucket 4: external momentum (stub) ────────────────────────────────────────

def _score_external(snapshot: MarketSnapshot, components: dict[str, float]) -> None:
    momentum = external_signals.get_external_momentum(snapshot)
    components["external.momentum"] = external_signals.score_external(momentum)


# ── history / cooldown / logging ──────────────────────────────────────────────

def _record_history(snapshot: MarketSnapshot, score: PreSpikeScore, now: datetime) -> None:
    """
    Append to the score_history calibration trail when the row is interesting:
    score above the floor, or any in-band longshot (so near-misses are kept).
    Never allowed to break the scoring cycle.
    """
    in_band = settings.PRE_SPIKE_PROB_MIN <= snapshot.probability <= settings.PRE_SPIKE_PROB_MAX
    if not in_band and score.total < settings.PRE_SPIKE_HISTORY_MIN_SCORE:
        return
    try:
        db.save_score_history(
            snapshot.market_id,
            snapshot.player_name,
            snapshot.source,
            probability=snapshot.probability,
            total_score=score.total,
            components=score.components,
            created_at=now,
        )
    except Exception as exc:
        logger.warning("[pre_spike] score_history write failed for %s / %s: %s",
                       snapshot.player_name, snapshot.market_id, exc)

def _on_cooldown(snapshot: MarketSnapshot, urgent: bool) -> bool:
    """
    Standard alerts respect any recent pre-spike alert (standard or urgent).
    Urgent alerts are only blocked by a recent *urgent* alert, so a market can
    escalate standard → urgent once inside the cooldown window.
    """
    cooldown = settings.PRE_SPIKE_COOLDOWN_MINUTES

    urgent_recent = db.was_alert_sent_recently(
        market_id=snapshot.market_id,
        player_name=snapshot.player_name,
        source=snapshot.source,
        anomaly_type=URGENT_ALERT_TYPE,
        cooldown_minutes=cooldown,
    )
    if urgent:
        blocked = urgent_recent
    else:
        blocked = urgent_recent or db.was_alert_sent_recently(
            market_id=snapshot.market_id,
            player_name=snapshot.player_name,
            source=snapshot.source,
            anomaly_type=AnomalyType.PRE_SPIKE_CANDIDATE.value,
            cooldown_minutes=cooldown,
        )
        if not blocked:
            classification = market_classifier.classify_snapshot(snapshot)
            blocked = db.was_match_alert_sent_recently(
                source=snapshot.source,
                match_key=classification.match_key,
                cooldown_minutes=settings.ALERT_MATCH_COOLDOWN_MINUTES,
            )
    if blocked:
        logger.debug(
            "[cooldown] PRE_SPIKE_CANDIDATE%s skipped for %s / %s",
            " (urgent)" if urgent else "",
            snapshot.player_name, snapshot.match_name,
        )
    return blocked


def _log_breakdown(snapshot: MarketSnapshot, score: PreSpikeScore) -> None:
    """Log every component every cycle — INFO above the floor, else DEBUG."""
    parts = " ".join(f"{name}={pts:g}" for name, pts in score.components.items())
    note = score.notes.get("cross_exchange", "")
    level = (
        logging.INFO
        if score.total >= settings.PRE_SPIKE_LOG_SCORE_FLOOR
        else logging.DEBUG
    )
    logger.log(
        level,
        "[pre_spike] %s / %s (%s) | prob=%.3f score=%.1f | %s | cross: %s",
        snapshot.player_name, snapshot.match_name, snapshot.source,
        snapshot.probability, score.total, parts, note,
    )
