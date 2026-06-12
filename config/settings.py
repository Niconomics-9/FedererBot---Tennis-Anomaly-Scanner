"""
Central configuration — loaded once at startup from .env.

All thresholds are expressed as decimals (0.0–1.0) matching the
probability values used throughout the codebase.
"""

import os

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

# ── helpers (carried over from original config.py) ────────────────────────────

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            "Check your .env file."
        )
    return val


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        raise EnvironmentError(f"'{key}' must be a float.")


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        raise EnvironmentError(f"'{key}' must be an integer.")


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes")


# ── Required ──────────────────────────────────────────────────────────────────

DISCORD_WEBHOOK_URL: str = _require("DISCORD_WEBHOOK_URL")

# ── Provider API keys (optional per-provider) ─────────────────────────────────

# Kalshi RSA key authentication
KALSHI_KEY_ID: str = os.getenv("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")

# Polymarket is public — no key required; set POLYMARKET_ENABLED=false to skip
POLYMARKET_ENABLED: bool = _bool("POLYMARKET_ENABLED", True)
KALSHI_ENABLED: bool = _bool("KALSHI_ENABLED", bool(KALSHI_KEY_ID))

# ── Anomaly detection thresholds ─────────────────────────────────────────────

# Rule 1 — LOW_ODDS_SPIKE
# Fire when a longshot's probability was at or below this...
LOW_ODDS_THRESHOLD: float = _float("LOW_ODDS_THRESHOLD", 0.10)
# ...and increased by at least this many percentage points
LOW_ODDS_SPIKE_DELTA: float = _float("LOW_ODDS_SPIKE_DELTA", 0.03)

# Rule 2 — FAVORITE_RECOVERY
# Opening probability must have been at or above this to qualify
FAVORITE_OPENING_THRESHOLD: float = _float("FAVORITE_OPENING_THRESHOLD", 0.60)
# The market must have dropped to at or below this at some point
FAVORITE_LOW_THRESHOLD: float = _float("FAVORITE_LOW_THRESHOLD", 0.25)
# Current probability must be at least this far above the recorded low
FAVORITE_RECOVERY_DELTA: float = _float("FAVORITE_RECOVERY_DELTA", 0.07)

# Rule 3 — FAST_MOVE
# Probability increase within FAST_MOVE_WINDOW_MINUTES that triggers an alert
FAST_MOVE_DELTA: float = _float("FAST_MOVE_DELTA", 0.05)
FAST_MOVE_WINDOW_MINUTES: int = _int("FAST_MOVE_WINDOW_MINUTES", 10)

# Rule 4 — NEW_LOW_WATCH
# Alert the first time a player's probability drops below this level
NEW_LOW_THRESHOLD: float = _float("NEW_LOW_THRESHOLD", 0.08)

# Alert quality gates
# These gates reduce Discord noise while raw snapshots/signals still collect.
ALERT_MATCH_WINNER_ONLY: bool = _bool("ALERT_MATCH_WINNER_ONLY", True)
ALERT_OPPORTUNITY_PROB_MIN: float = _float("ALERT_OPPORTUNITY_PROB_MIN", 0.02)
ALERT_OPPORTUNITY_PROB_MAX: float = _float("ALERT_OPPORTUNITY_PROB_MAX", 0.35)
ALERT_FAST_MOVE_PREV_MAX: float = _float("ALERT_FAST_MOVE_PREV_MAX", 0.25)
ALERT_FAST_MOVE_CURR_MAX: float = _float("ALERT_FAST_MOVE_CURR_MAX", 0.35)
ALERT_FAVORITE_RECOVERY_MAX: float = _float("ALERT_FAVORITE_RECOVERY_MAX", 0.45)
ALERT_MAX_SPREAD: float = _float("ALERT_MAX_SPREAD", 0.08)
ALERT_MIN_LIQUIDITY: float = _float("ALERT_MIN_LIQUIDITY", 100.0)
ALERT_MIN_VOLUME: float = _float("ALERT_MIN_VOLUME", 100.0)
ALERT_MIN_HISTORY_SNAPSHOTS: int = _int("ALERT_MIN_HISTORY_SNAPSHOTS", 3)
ALERT_MATCH_COOLDOWN_MINUTES: int = _int("ALERT_MATCH_COOLDOWN_MINUTES", 60)

# Pre-match gate — the strategy is to catch odds waves BEFORE the match, so
# alerts are suppressed once a match has started (or starts too soon to act).
# In-play comeback swings are exactly the noise this removes.
ALERT_PRE_MATCH_ONLY: bool = _bool("ALERT_PRE_MATCH_ONLY", True)
# Minimum minutes between alert time and match start for the alert to be
# actionable (enter and have time to ride/exit before the match begins).
ALERT_PRE_MATCH_MIN_LEAD_MINUTES: int = _int("ALERT_PRE_MATCH_MIN_LEAD_MINUTES", 10)
# Polymarket supplies gameStartTime on every match market (verified live), so
# a match-winner market without one is malformed or stale — suppress it.
ALERT_REQUIRE_START_TIME: bool = _bool("ALERT_REQUIRE_START_TIME", True)

# ── PRE_SPIKE scoring engine ──────────────────────────────────────────────────

# Eligibility band — alerts only fire for probabilities inside it.
# Wide on purpose: the target is odds LIKELY TO MOVE, not low odds per se.
# Below 2% spreads dominate any wave; above 30% the relative payoff of a
# pre-match move shrinks and mid-range drift is mostly noise.
PRE_SPIKE_PROB_MIN: float = _float("PRE_SPIKE_PROB_MIN", 0.02)
PRE_SPIKE_PROB_MAX: float = _float("PRE_SPIKE_PROB_MAX", 0.30)

# Score thresholds (0–100 scale). 70 with the rebalanced weights demands
# MORE movement evidence than the old 75 did, because band membership now
# contributes 5 points instead of 15. Realistic max while the external bucket
# is a stub is 83 (52 pressure + 25 band + 6 cross), so urgent at 80 means
# "essentially everything is firing, including cross-exchange confirmation".
PRE_SPIKE_ALERT_SCORE: float = _float("PRE_SPIKE_ALERT_SCORE", 70.0)
PRE_SPIKE_URGENT_SCORE: float = _float("PRE_SPIKE_URGENT_SCORE", 80.0)

# Dedicated cooldown — one standard alert per market per window; a single
# escalation to urgent is allowed inside the window if the score crosses
# PRE_SPIKE_URGENT_SCORE after a standard alert already fired.
PRE_SPIKE_COOLDOWN_MINUTES: int = _int("PRE_SPIKE_COOLDOWN_MINUTES", 45)

# Cross-exchange confirmation: how recent a Kalshi reading must be to count.
PRE_SPIKE_KALSHI_FRESHNESS_MINUTES: int = _int("PRE_SPIKE_KALSHI_FRESHNESS_MINUTES", 15)

# Component breakdowns log at INFO once total score reaches this floor
# (everything still logs at DEBUG regardless).
PRE_SPIKE_LOG_SCORE_FLOOR: float = _float("PRE_SPIKE_LOG_SCORE_FLOOR", 40.0)

# Score-history trail (for offline weight calibration via analyze_pre_spike.py).
# A row is recorded when score >= this floor OR probability is in the band;
# rows older than the retention window are pruned at startup.
PRE_SPIKE_HISTORY_MIN_SCORE: float = _float("PRE_SPIKE_HISTORY_MIN_SCORE", 20.0)
PRE_SPIKE_HISTORY_RETENTION_DAYS: int = _int("PRE_SPIKE_HISTORY_RETENTION_DAYS", 14)

# ── Polling ───────────────────────────────────────────────────────────────────

POLL_SECONDS: int = _int("POLL_SECONDS", 60)

# 0 = run forever (local service). N > 0 = run N scan cycles and exit — used
# by the GitHub Actions runner, where each scheduled job does a short burst
# and the workflow schedule provides the long-running cadence.
MAX_SCAN_CYCLES: int = _int("MAX_SCAN_CYCLES", 0)

# ── Alert deduplication ───────────────────────────────────────────────────────

ALERT_COOLDOWN_MINUTES: int = _int("ALERT_COOLDOWN_MINUTES", 30)

# ── Storage ───────────────────────────────────────────────────────────────────

DB_PATH: str = os.getenv("DB_PATH", "tennis_scanner.db")

# Snapshot retention: 0 = keep forever. N > 0 prunes market_snapshots rows
# older than N days at startup. Used on GitHub Actions to keep the cached
# database small; labelled outcomes live on in the match_waves backtest CSVs.
SNAPSHOT_RETENTION_DAYS: int = _int("SNAPSHOT_RETENTION_DAYS", 0)

# ── Demo / test mode ──────────────────────────────────────────────────────────

DEMO_MODE: bool = _bool("DEMO_MODE", False)
