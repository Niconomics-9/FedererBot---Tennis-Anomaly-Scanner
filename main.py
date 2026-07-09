"""
Tennis Prediction Market Anomaly Scanner
Entry point — bootstraps providers, initialises storage, runs polling loop.

Usage:
    python main.py

Set DEMO_MODE=true in .env to run synthetic test data without any API keys.
"""

import logging
import sys
import time
from datetime import datetime

from config import settings
from core import scanner
from market_providers.base_provider import BaseProvider
from market_providers.kalshi_provider import KalshiProvider
from market_providers.polymarket_provider import PolymarketProvider
from storage import supabase_storage as db

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
        logging.FileHandler("scanner.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ── demo / test mode ──────────────────────────────────────────────────────────

def _build_demo_providers() -> list[BaseProvider]:
    """
    Returns a fake provider that emits synthetic snapshots designed to
    trigger every anomaly rule within two poll cycles.  No API keys needed.
    """
    from datetime import timedelta
    from market_providers.models import MarketSnapshot

    class _DemoProvider(BaseProvider):
        _cycle = 0

        @property
        def name(self) -> str:
            return "demo"

        def fetch_snapshots(self) -> list[MarketSnapshot]:
            _DemoProvider._cycle += 1
            now = datetime.utcnow()
            url = "https://example.com/demo-market"

            if _DemoProvider._cycle == 1:
                # Seed cycle: establish opening probabilities
                return [
                    # LOW_ODDS_SPIKE candidate: longshot at 5%
                    MarketSnapshot("m1", "Nadal vs Doe", "Doe J.", 0.05, "demo", url, now),
                    # FAVORITE_RECOVERY candidate: favourite at 70%, will fall then recover
                    MarketSnapshot("m2", "Swiatek vs X", "Swiatek I.", 0.70, "demo", url, now),
                    # NEW_LOW_WATCH candidate: favourite at 65%
                    MarketSnapshot("m3", "Djokovic vs Y", "Djokovic N.", 0.65, "demo", url, now),
                ]

            if _DemoProvider._cycle == 2:
                # Drive Swiatek and Djokovic down; keep longshot steady
                return [
                    MarketSnapshot("m1", "Nadal vs Doe", "Doe J.", 0.06, "demo", url, now),
                    MarketSnapshot("m2", "Swiatek vs X", "Swiatek I.", 0.20, "demo", url, now),
                    MarketSnapshot("m3", "Djokovic vs Y", "Djokovic N.", 0.06, "demo", url, now),
                ]

            # Cycle 3+: trigger LOW_ODDS_SPIKE, FAVORITE_RECOVERY, FAST_MOVE
            return [
                # LOW_ODDS_SPIKE: was 5% → now 12% (+7 pp)
                MarketSnapshot("m1", "Nadal vs Doe", "Doe J.", 0.12, "demo", url, now),
                # FAVORITE_RECOVERY: was 70% open, fell to 20%, now 30% (+10 pp from low)
                MarketSnapshot("m2", "Swiatek vs X", "Swiatek I.", 0.30, "demo", url, now),
                # NEW_LOW already fired on cycle 2; now recovering — will show FAST_MOVE
                MarketSnapshot("m3", "Djokovic vs Y", "Djokovic N.", 0.12, "demo", url, now),
            ]

    return [_DemoProvider()]


# ── provider factory ──────────────────────────────────────────────────────────

def _build_providers() -> list[BaseProvider]:
    providers: list[BaseProvider] = []

    if settings.POLYMARKET_ENABLED:
        providers.append(PolymarketProvider())
        logger.info("Provider enabled: polymarket")
    else:
        logger.info("Provider disabled: polymarket (POLYMARKET_ENABLED=false)")

    if settings.KALSHI_ENABLED:
        providers.append(KalshiProvider())
        logger.info("Provider enabled: kalshi")
    else:
        logger.info("Provider disabled: kalshi (no KALSHI_API_KEY set)")

    if not providers:
        raise RuntimeError(
            "No market providers are enabled. "
            "Set POLYMARKET_ENABLED=true or provide KALSHI_API_KEY."
        )

    return providers


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=" * 60)
    logger.info("Tennis Prediction Market Anomaly Scanner")
    logger.info("Poll interval  : %d seconds", settings.POLL_SECONDS)
    logger.info("LOW_ODDS       : prev <= %.0f%%, delta >= %.0f pp",
                settings.LOW_ODDS_THRESHOLD * 100, settings.LOW_ODDS_SPIKE_DELTA * 100)
    logger.info("FAV_RECOVERY   : open >= %.0f%%, low <= %.0f%%, delta >= %.0f pp",
                settings.FAVORITE_OPENING_THRESHOLD * 100,
                settings.FAVORITE_LOW_THRESHOLD * 100,
                settings.FAVORITE_RECOVERY_DELTA * 100)
    logger.info("FAST_MOVE      : delta >= %.0f pp within %d min",
                settings.FAST_MOVE_DELTA * 100, settings.FAST_MOVE_WINDOW_MINUTES)
    logger.info("NEW_LOW_WATCH  : first drop below %.0f%%",
                settings.NEW_LOW_THRESHOLD * 100)
    logger.info("PRE_SPIKE      : band %.0f%%-%.0f%%, alert >= %.0f, urgent >= %.0f, cooldown %d min",
                settings.PRE_SPIKE_PROB_MIN * 100, settings.PRE_SPIKE_PROB_MAX * 100,
                settings.PRE_SPIKE_ALERT_SCORE, settings.PRE_SPIKE_URGENT_SCORE,
                settings.PRE_SPIKE_COOLDOWN_MINUTES)
    logger.info("Alert cooldown : %d minutes", settings.ALERT_COOLDOWN_MINUTES)
    if settings.DEMO_MODE:
        logger.info("*** DEMO MODE — using synthetic data ***")
    logger.info("=" * 60)

    db.init_db()

    providers = _build_demo_providers() if settings.DEMO_MODE else _build_providers()
    cycle = 0

    while True:
        cycle += 1
        logger.info(
            "--- Cycle #%d — %s ---",
            cycle,
            datetime.utcnow().strftime("%H:%M:%S UTC"),
        )
        try:
            alerts_sent = scanner.run_cycle(providers)
            logger.info("Cycle #%d complete — %d alert(s) sent.", cycle, alerts_sent)
        except Exception as exc:
            logger.exception("Unhandled error in cycle #%d: %s", cycle, exc)

        if settings.MAX_SCAN_CYCLES and cycle >= settings.MAX_SCAN_CYCLES:
            logger.info("MAX_SCAN_CYCLES=%d reached — exiting.", settings.MAX_SCAN_CYCLES)
            break

        logger.info("Sleeping %d seconds...", settings.POLL_SECONDS)
        time.sleep(settings.POLL_SECONDS)


if __name__ == "__main__":
    main()
