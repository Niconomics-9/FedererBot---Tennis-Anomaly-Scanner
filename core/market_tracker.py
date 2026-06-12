"""
Maintains per-player rolling stats (opening / current / lowest / highest
probability) by reading from and writing to the database after each
snapshot is ingested.
"""

import logging
from datetime import datetime

from market_providers.models import MarketSnapshot, MarketStats
from storage import sqlite_storage as db

logger = logging.getLogger(__name__)


def update_stats(snapshot: MarketSnapshot) -> MarketStats:
    """
    Load existing stats for this (market_id, player_name, source), update them
    with the new probability reading, persist, and return the updated stats.

    If no prior stats exist this snapshot is treated as the opening reading.
    """
    existing = db.get_stats(snapshot.market_id, snapshot.player_name, snapshot.source)

    if existing is None:
        stats = MarketStats(
            market_id=snapshot.market_id,
            player_name=snapshot.player_name,
            source=snapshot.source,
            opening_probability=snapshot.probability,
            current_probability=snapshot.probability,
            lowest_probability=snapshot.probability,
            highest_probability=snapshot.probability,
            new_low_alerted=False,
            last_updated=datetime.utcnow(),
        )
        logger.debug(
            "[tracker] New market entry: %s / %s @ %.1f%%",
            snapshot.match_name,
            snapshot.player_name,
            snapshot.probability * 100,
        )
    else:
        stats = MarketStats(
            market_id=existing.market_id,
            player_name=existing.player_name,
            source=existing.source,
            opening_probability=existing.opening_probability,
            current_probability=snapshot.probability,
            lowest_probability=min(existing.lowest_probability, snapshot.probability),
            highest_probability=max(existing.highest_probability, snapshot.probability),
            new_low_alerted=existing.new_low_alerted,
            last_updated=datetime.utcnow(),
        )

    db.upsert_stats(stats)
    return stats


def mark_new_low_alerted(
    market_id: str, player_name: str, source: str
) -> None:
    """Flip the new_low_alerted flag so the NEW_LOW_WATCH fires only once."""
    stats = db.get_stats(market_id, player_name, source)
    if stats is not None:
        stats.new_low_alerted = True
        db.upsert_stats(stats)
