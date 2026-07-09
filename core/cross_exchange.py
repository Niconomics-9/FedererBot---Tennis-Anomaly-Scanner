"""
Cross-exchange confirmation — PLACEHOLDER-QUALITY matching.

Goal: when a Polymarket longshot starts pressuring upward, check whether the
equivalent Kalshi market moved up first (smart money often hits the thinner
book before the move propagates).

Matching limitation
-------------------
There is no shared market identifier between exchanges, and player name
formats differ (Polymarket extracts names from the market question; Kalshi
uses its own ticker naming).  Until proper market mapping exists, we match
naively by surname: take the longest token of the Polymarket player name and
look for recent Kalshi snapshots whose player_name contains it.  False
positives are possible (siblings on tour, common surnames), so this bucket is
deliberately capped at 15/100 points.

Upgrade path: replace find_confirmation()'s lookup with a persistent
market-mapping table (polymarket_market_id <-> kalshi_market_id) populated by
a dedicated matcher; the dataclass returned here already carries the fields
that table would provide.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from config import settings
from core import market_classifier
from market_providers.models import MarketSnapshot
from storage import supabase_storage as db

logger = logging.getLogger(__name__)


@dataclass
class CrossExchangeConfirmation:
    """Result of looking for a Kalshi market confirming a Polymarket move."""

    matched:            bool
    kalshi_market_id:   str | None = None
    kalshi_player_name: str | None = None
    kalshi_velocity_1c: float | None = None
    kalshi_velocity_5c: float | None = None
    note:               str = ""


def find_confirmation(snapshot: MarketSnapshot) -> CrossExchangeConfirmation:
    """
    Look for a fresh Kalshi reading of (heuristically) the same player and
    return its stored velocity signals. Never raises — on any failure returns
    an unmatched result with an explanatory note.
    """
    if snapshot.source == "kalshi":
        return CrossExchangeConfirmation(matched=False, note="snapshot is already kalshi")

    classification = market_classifier.classify_snapshot(snapshot)
    if not classification.is_actionable:
        return CrossExchangeConfirmation(
            matched=False,
            note=classification.reject_reason or f"market_type={classification.market_type}",
        )

    surname = _surname(classification.selection or snapshot.player_name)
    if not surname or len(surname) < 3:
        return CrossExchangeConfirmation(matched=False, note="no usable surname")

    since = datetime.utcnow() - timedelta(minutes=settings.PRE_SPIKE_KALSHI_FRESHNESS_MINUTES)
    try:
        candidates = db.get_recent_player_markets("kalshi", surname, since)
    except Exception as exc:
        logger.debug("[cross_exchange] lookup failed for %s: %s", surname, exc)
        return CrossExchangeConfirmation(matched=False, note="lookup error")

    if not candidates:
        return CrossExchangeConfirmation(matched=False, note=f"no fresh kalshi market for '{surname}'")

    # Pick the candidate showing the strongest sustained upward move (v5c,
    # falling back to v1c) — that is the confirmation we are looking for.
    best: CrossExchangeConfirmation | None = None
    for market_id, player_name in candidates:
        signals = db.get_signals(market_id, player_name, "kalshi")
        if signals is None:
            continue
        confirmation = CrossExchangeConfirmation(
            matched=True,
            kalshi_market_id=market_id,
            kalshi_player_name=player_name,
            kalshi_velocity_1c=signals.velocity_1c,
            kalshi_velocity_5c=signals.velocity_5c,
            note=f"surname match '{surname}'",
        )
        if best is None or _strength(confirmation) > _strength(best):
            best = confirmation

    if best is None:
        return CrossExchangeConfirmation(matched=False, note=f"kalshi match for '{surname}' has no signals yet")
    return best


def _strength(confirmation: CrossExchangeConfirmation) -> float:
    v5 = confirmation.kalshi_velocity_5c or 0.0
    v1 = confirmation.kalshi_velocity_1c or 0.0
    return max(v5, v1)


def _surname(player_name: str) -> str:
    """
    Longest alphabetic token of the player name, lowercased.
    Works for "Djokovic N.", "Novak Djokovic", and "N. Djokovic" alike.
    """
    tokens = ["".join(ch for ch in tok if ch.isalpha()) for tok in player_name.split()]
    tokens = [tok for tok in tokens if tok]
    if not tokens:
        return ""
    return max(tokens, key=len).lower()
