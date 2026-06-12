"""
Scanner — the top-level coordinator for one poll cycle.

Responsibilities:
  1. Call each enabled provider to get fresh snapshots.
  2. Feed each snapshot through the anomaly engine.
  3. Dispatch a Discord alert the moment a snapshot produces events — a full
     cycle can take minutes at high market counts, and time-sensitive alerts
     lose their value if they wait for the end of the cycle.
  4. Return a summary count.
"""

import logging

from alerts import discord_alerts
from core import anomaly_engine
from market_providers.base_provider import BaseProvider

logger = logging.getLogger(__name__)


def run_cycle(providers: list[BaseProvider]) -> int:
    """
    Execute one full scan across all providers.
    Returns the total number of alerts sent.
    """
    detected = 0
    sent = 0

    for provider in providers:
        logger.info("[scanner] Polling %s...", provider.name)
        try:
            snapshots = provider.fetch_snapshots()
        except Exception as exc:
            logger.error("[scanner] Provider %s raised: %s", provider.name, exc)
            continue

        if not snapshots:
            logger.info("[scanner] %s returned no snapshots.", provider.name)
            continue

        for snapshot in snapshots:
            try:
                events = anomaly_engine.evaluate(snapshot)
            except Exception as exc:
                logger.error(
                    "[scanner] Error evaluating snapshot %s / %s: %s",
                    snapshot.market_id, snapshot.player_name, exc,
                )
                continue

            detected += len(events)
            for event in events:
                # Alert only on Polymarket markets — Kalshi data is still collected
                # and feeds signals/stats, but we only bet on Polymarket.
                if event.snapshot.source != "polymarket":
                    logger.debug(
                        "[scanner] Suppressed %s alert for %s (source=%s, not polymarket)",
                        event.anomaly_type.value, event.snapshot.player_name, event.snapshot.source,
                    )
                    continue
                if discord_alerts.send_alert(event):
                    sent += 1

    logger.info("[scanner] Cycle complete — %d event(s) detected, %d alert(s) sent.",
                detected, sent)
    return sent
