"""
Anomaly detection engine — evaluates all four rules against a fresh snapshot.

Rule evaluation order:
  1. LOW_ODDS_SPIKE      — longshot surges upward
  2. FAVORITE_RECOVERY   — fallen favourite bounces back
  3. FAST_MOVE           — rapid move within a short time window
  4. NEW_LOW_WATCH       — first time a player drops below the watch threshold
  5. PRE_SPIKE_CANDIDATE — pre_spike_engine score crosses the alert threshold

Each rule fires independently; a single snapshot can produce multiple anomalies.

Cooldown deduplication is enforced here before returning events, so the caller
(scanner.py) can trust that every returned AnomalyEvent should generate a
Discord alert.
"""

import logging
from datetime import datetime, timedelta

from config import settings
from core import (
    alert_quality,
    market_classifier,
    market_tracker,
    pre_spike_engine,
    signal_engine,
)
from market_providers.models import AnomalyEvent, AnomalyType, MarketSnapshot, MarketStats
from storage import sqlite_storage as db

logger = logging.getLogger(__name__)


def evaluate(snapshot: MarketSnapshot) -> list[AnomalyEvent]:
    """
    Process a single fresh snapshot:
      1. Ingest it into the database.
      2. Update rolling stats.
      3. Run all anomaly rules.
      4. Filter by cooldown.
      5. Return any triggered AnomalyEvent objects.
    """
    db.save_snapshot(snapshot)
    stats = market_tracker.update_stats(snapshot)

    # Compute and persist basic signals; kept separate from anomaly rules
    # so the signal table is always populated regardless of alert state.
    signals = None
    try:
        signals = signal_engine.compute_and_save(snapshot, stats)
    except Exception as exc:
        logger.warning("[signals] Computation failed for %s / %s: %s",
                       snapshot.player_name, snapshot.market_id, exc)

    prev_snapshot = db.get_previous_snapshot(
        snapshot.market_id, snapshot.player_name, snapshot.source
    )
    prev_prob: float | None = prev_snapshot.probability if prev_snapshot else None

    events: list[AnomalyEvent] = []

    events += _rule_low_odds_spike(snapshot, prev_prob, stats)
    events += _rule_favorite_recovery(snapshot, prev_prob, stats)
    events += _rule_fast_move(snapshot, stats)
    events += _rule_new_low_watch(snapshot, stats)

    # PRE_SPIKE scoring needs this cycle's signals; isolated so a scoring
    # failure never blocks the rule-based alerts above.
    if signals is not None:
        try:
            events += pre_spike_engine.evaluate(snapshot, stats, signals)
        except Exception as exc:
            logger.warning("[pre_spike] Scoring failed for %s / %s: %s",
                           snapshot.player_name, snapshot.market_id, exc)

    return _dedupe_events(events)


# ── Rule 1 — LOW_ODDS_SPIKE ───────────────────────────────────────────────────

def _rule_low_odds_spike(
    snapshot: MarketSnapshot,
    prev_prob: float | None,
    stats: MarketStats,
) -> list[AnomalyEvent]:
    if prev_prob is None:
        return []

    curr = snapshot.probability
    delta = curr - prev_prob

    if not (prev_prob <= settings.LOW_ODDS_THRESHOLD and delta >= settings.LOW_ODDS_SPIKE_DELTA):
        return []

    if curr > settings.ALERT_OPPORTUNITY_PROB_MAX:
        logger.debug(
            "[quality] LOW_ODDS_SPIKE skipped for %s / %s: above opportunity band %.3f",
            snapshot.player_name, snapshot.match_name, curr,
        )
        return []

    if not _passes_quality(snapshot, AnomalyType.LOW_ODDS_SPIKE):
        return []

    if _on_cooldown(snapshot, AnomalyType.LOW_ODDS_SPIKE):
        return []

    logger.info(
        "[LOW_ODDS_SPIKE] %s / %s: %.1f%% -> %.1f%% (+%.1f pp)",
        snapshot.match_name, snapshot.player_name,
        prev_prob * 100, curr * 100, delta * 100,
    )
    return [AnomalyEvent(
        anomaly_type=AnomalyType.LOW_ODDS_SPIKE,
        snapshot=snapshot,
        prev_probability=prev_prob,
        current_probability=curr,
        move=delta,
        time_window_minutes=None,
        stats=stats,
    )]


# ── Rule 2 — FAVORITE_RECOVERY ────────────────────────────────────────────────

def _rule_favorite_recovery(
    snapshot: MarketSnapshot,
    prev_prob: float | None,
    stats: MarketStats,
) -> list[AnomalyEvent]:
    curr = snapshot.probability
    low = stats.lowest_probability
    opening = stats.opening_probability
    recovery = curr - low

    if not (
        opening >= settings.FAVORITE_OPENING_THRESHOLD
        and low <= settings.FAVORITE_LOW_THRESHOLD
        and recovery >= settings.FAVORITE_RECOVERY_DELTA
    ):
        return []

    if curr > settings.ALERT_FAVORITE_RECOVERY_MAX:
        logger.debug(
            "[quality] FAVORITE_RECOVERY skipped for %s / %s: current %.3f above max",
            snapshot.player_name, snapshot.match_name, curr,
        )
        return []

    if not _passes_quality(snapshot, AnomalyType.FAVORITE_RECOVERY):
        return []

    if _on_cooldown(snapshot, AnomalyType.FAVORITE_RECOVERY):
        return []

    logger.info(
        "[FAVORITE_RECOVERY] %s / %s: open=%.1f%%, low=%.1f%%, now=%.1f%% (+%.1f pp from low)",
        snapshot.match_name, snapshot.player_name,
        opening * 100, low * 100, curr * 100, recovery * 100,
    )
    return [AnomalyEvent(
        anomaly_type=AnomalyType.FAVORITE_RECOVERY,
        snapshot=snapshot,
        prev_probability=low,      # "previous" = the recorded low
        current_probability=curr,
        move=recovery,
        time_window_minutes=None,
        stats=stats,
    )]


# ── Rule 3 — FAST_MOVE ────────────────────────────────────────────────────────

def _rule_fast_move(
    snapshot: MarketSnapshot,
    stats: MarketStats,
) -> list[AnomalyEvent]:
    window_minutes = settings.FAST_MOVE_WINDOW_MINUTES
    since = datetime.utcnow() - timedelta(minutes=window_minutes)

    baseline_snap = db.get_snapshot_at_or_after(
        snapshot.market_id, snapshot.player_name, snapshot.source, since
    )
    if baseline_snap is None:
        return []

    baseline_prob = baseline_snap.probability
    curr = snapshot.probability
    delta = curr - baseline_prob

    if delta < settings.FAST_MOVE_DELTA:
        return []

    if baseline_prob > settings.ALERT_FAST_MOVE_PREV_MAX:
        logger.debug(
            "[quality] FAST_MOVE skipped for %s / %s: baseline %.3f above max",
            snapshot.player_name, snapshot.match_name, baseline_prob,
        )
        return []

    if curr > settings.ALERT_FAST_MOVE_CURR_MAX:
        logger.debug(
            "[quality] FAST_MOVE skipped for %s / %s: current %.3f above max",
            snapshot.player_name, snapshot.match_name, curr,
        )
        return []

    if not _passes_quality(snapshot, AnomalyType.FAST_MOVE):
        return []

    if _on_cooldown(snapshot, AnomalyType.FAST_MOVE):
        return []

    logger.info(
        "[FAST_MOVE] %s / %s: %.1f%% -> %.1f%% (+%.1f pp in %d min)",
        snapshot.match_name, snapshot.player_name,
        baseline_prob * 100, curr * 100, delta * 100, window_minutes,
    )
    return [AnomalyEvent(
        anomaly_type=AnomalyType.FAST_MOVE,
        snapshot=snapshot,
        prev_probability=baseline_prob,
        current_probability=curr,
        move=delta,
        time_window_minutes=window_minutes,
        stats=stats,
    )]


# ── Rule 4 — NEW_LOW_WATCH ────────────────────────────────────────────────────

def _rule_new_low_watch(
    snapshot: MarketSnapshot,
    stats: MarketStats,
) -> list[AnomalyEvent]:
    # Only fire once per market — flag prevents re-triggering
    if stats.new_low_alerted:
        return []

    curr = snapshot.probability
    if curr >= settings.NEW_LOW_THRESHOLD:
        return []

    if not _passes_quality(snapshot, AnomalyType.NEW_LOW_WATCH):
        return []

    # Mark immediately so concurrent cycles don't double-fire
    market_tracker.mark_new_low_alerted(
        snapshot.market_id, snapshot.player_name, snapshot.source
    )

    logger.info(
        "[NEW_LOW_WATCH] %s / %s at %.1f%% — live match, first time below %.1f%%",
        snapshot.match_name, snapshot.player_name,
        curr * 100, settings.NEW_LOW_THRESHOLD * 100,
    )
    return [AnomalyEvent(
        anomaly_type=AnomalyType.NEW_LOW_WATCH,
        snapshot=snapshot,
        prev_probability=None,
        current_probability=curr,
        move=None,
        time_window_minutes=None,
        stats=stats,
    )]


# ── helpers ───────────────────────────────────────────────────────────────────

def _passes_quality(snapshot: MarketSnapshot, anomaly_type: AnomalyType) -> bool:
    quality = alert_quality.check(snapshot, anomaly_type)
    if quality.passed:
        return True
    logger.debug(
        "[quality] %s skipped for %s / %s: %s",
        anomaly_type.value,
        snapshot.player_name,
        snapshot.match_name,
        quality.reason,
    )
    return False


def _on_cooldown(snapshot: MarketSnapshot, anomaly_type: AnomalyType) -> bool:
    in_cooldown = db.was_alert_sent_recently(
        market_id=snapshot.market_id,
        player_name=snapshot.player_name,
        source=snapshot.source,
        anomaly_type=anomaly_type.value,
        cooldown_minutes=settings.ALERT_COOLDOWN_MINUTES,
    )
    if in_cooldown:
        logger.debug(
            "[cooldown] %s skipped for %s / %s",
            anomaly_type.value, snapshot.player_name, snapshot.match_name,
        )
        return True

    classification = market_classifier.classify_snapshot(snapshot)
    match_cooldown = db.was_match_alert_sent_recently(
        source=snapshot.source,
        match_key=classification.match_key,
        cooldown_minutes=settings.ALERT_MATCH_COOLDOWN_MINUTES,
    )
    if match_cooldown:
        logger.debug(
            "[cooldown] %s skipped for %s / %s (match cooldown)",
            anomaly_type.value, snapshot.player_name, snapshot.match_name,
        )
    return match_cooldown


def _dedupe_events(events: list[AnomalyEvent]) -> list[AnomalyEvent]:
    """Keep only the highest-priority event per match for one evaluation."""
    best_by_match: dict[str, AnomalyEvent] = {}
    for event in events:
        classification = market_classifier.classify_snapshot(event.snapshot)
        key = classification.match_key or event.snapshot.market_id
        existing = best_by_match.get(key)
        if existing is None or _event_priority(event) > _event_priority(existing):
            best_by_match[key] = event
    return list(best_by_match.values())


def _event_priority(event: AnomalyEvent) -> int:
    if event.anomaly_type == AnomalyType.PRE_SPIKE_CANDIDATE:
        return 60 if event.urgent else 45
    priorities = {
        AnomalyType.LOW_ODDS_SPIKE: 40,
        AnomalyType.FAVORITE_RECOVERY: 35,
        AnomalyType.FAST_MOVE: 25,
        AnomalyType.NEW_LOW_WATCH: 10,
    }
    return priorities.get(event.anomaly_type, 0)
