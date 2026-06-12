"""
Shared alert eligibility gates.

Rules can still compute signals for every market, but Discord-worthy events
must pass these quality checks first.
"""

from dataclasses import dataclass
from datetime import datetime

from config import settings
from core import market_classifier
from market_providers.models import AnomalyType, MarketSnapshot
from storage import sqlite_storage as db


@dataclass(frozen=True)
class AlertQuality:
    passed: bool
    reason: str | None
    classification: market_classifier.MarketClassification


def check(snapshot: MarketSnapshot, anomaly_type: AnomalyType) -> AlertQuality:
    classification = market_classifier.classify_snapshot(snapshot)
    reason = _reject_reason(snapshot, anomaly_type, classification)
    return AlertQuality(
        passed=reason is None,
        reason=reason,
        classification=classification,
    )


def _reject_reason(
    snapshot: MarketSnapshot,
    anomaly_type: AnomalyType,
    classification: market_classifier.MarketClassification,
) -> str | None:
    if settings.ALERT_MATCH_WINNER_ONLY and not classification.is_actionable:
        return classification.reject_reason or f"market_type={classification.market_type}"

    # Pre-match gate — the strategy only trades pre-match waves, so alerts on
    # started (or imminently starting) matches are noise. Also catches stale
    # "zombie" markets whose match ended weeks ago but never closed.
    if settings.ALERT_PRE_MATCH_ONLY:
        start = snapshot.match_start_time
        if start is None:
            if settings.ALERT_REQUIRE_START_TIME:
                return "missing_match_start_time"
        else:
            lead_min = (start - datetime.utcnow()).total_seconds() / 60
            if lead_min < settings.ALERT_PRE_MATCH_MIN_LEAD_MINUTES:
                return f"match_started_or_imminent (lead={lead_min:.0f} min)"

    if snapshot.probability <= 0.005 or snapshot.probability >= 0.995:
        return "resolved_like_probability"

    if snapshot.probability < settings.ALERT_OPPORTUNITY_PROB_MIN:
        return "below_probability_floor"

    if snapshot.spread is not None and snapshot.spread > settings.ALERT_MAX_SPREAD:
        return f"wide_spread={snapshot.spread:.3f}"

    if snapshot.liquidity is not None and snapshot.liquidity < settings.ALERT_MIN_LIQUIDITY:
        return f"low_liquidity={snapshot.liquidity:.2f}"

    if snapshot.volume_total is not None and snapshot.volume_total < settings.ALERT_MIN_VOLUME:
        return f"low_volume={snapshot.volume_total:.2f}"

    history = db.count_snapshots(
        snapshot.market_id,
        snapshot.player_name,
        snapshot.source,
    )
    if history < settings.ALERT_MIN_HISTORY_SNAPSHOTS:
        return f"insufficient_history={history}"

    return None
