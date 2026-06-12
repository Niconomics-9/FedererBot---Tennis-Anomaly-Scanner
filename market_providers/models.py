"""
Shared data model that every market provider normalises into.

All providers — regardless of their internal format — must produce
MarketSnapshot objects.  Probabilities are always in [0.0, 1.0].

Microstructure fields (bid_probability, ask_probability, spread,
volume_total, liquidity, last_api_update, trade_count_1h) are optional.
Providers set what they can; everything else stays None.  The signal engine
and future scoring engine degrade gracefully on None values.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class AnomalyType(Enum):
    LOW_ODDS_SPIKE      = "LOW_ODDS_SPIKE"
    FAVORITE_RECOVERY   = "FAVORITE_RECOVERY"
    FAST_MOVE           = "FAST_MOVE"
    NEW_LOW_WATCH       = "NEW_LOW_WATCH"
    PRE_SPIKE_CANDIDATE = "PRE_SPIKE_CANDIDATE"


@dataclass
class MarketSnapshot:
    """A single probability reading for one player in one market."""

    # ── core identity (required) ───────────────────────────────────────────────
    market_id:   str    # provider-specific unique market identifier
    match_name:  str    # e.g. "Nadal vs Djokovic"
    player_name: str    # the specific player this probability belongs to
    probability: float  # implied win probability in [0.0, 1.0]
    source:      str    # "polymarket" | "kalshi"
    market_url:  str    # direct link to the market
    timestamp:   datetime = field(default_factory=datetime.utcnow)

    # ── microstructure (optional — None when provider doesn't supply) ──────────
    bid_probability:  float | None = None   # best bid price as probability [0,1]
    ask_probability:  float | None = None   # best ask price as probability [0,1]
    spread:           float | None = None   # ask - bid in probability space
    volume_total:     float | None = None   # total lifetime volume (USD / contracts)
    liquidity:        float | None = None   # order-book depth (USD)
    last_api_update:  datetime | None = None  # when provider last changed this price
    trade_count_1h:   int | None = None     # number of trades in the last hour
    match_start_time: datetime | None = None  # scheduled match start (UTC, naive);
                                              # gates pre-match-only alerting


@dataclass
class MarketStats:
    """
    Aggregated lifetime stats for a (market_id, player_name, source) triple.
    Maintained in the database and updated on every snapshot.
    """
    market_id:            str
    player_name:          str
    source:               str
    opening_probability:  float
    current_probability:  float
    lowest_probability:   float
    highest_probability:  float
    new_low_alerted:      bool = False
    last_updated:         datetime = field(default_factory=datetime.utcnow)


@dataclass
class MarketSignals:
    """
    Computed signals for a (market_id, player_name, source) triple.
    Updated every cycle by the signal engine.
    watch_score is reserved for the future PRE_SPIKE scoring engine (stored as
    None until that module is implemented).
    """
    market_id:           str
    player_name:         str
    source:              str
    current_probability: float

    # ── velocity / momentum ───────────────────────────────────────────────────
    velocity_1c:         float | None = None   # prob delta vs 1 cycle ago
    velocity_5c:         float | None = None   # prob delta vs 5 cycles ago
    acceleration:        float | None = None   # change in velocity_1c

    # ── microstructure ────────────────────────────────────────────────────────
    spread_current:      float | None = None   # current bid-ask spread
    spread_trend:        float | None = None   # spread delta vs prior reading

    # ── volume / activity ─────────────────────────────────────────────────────
    volume_acceleration: float | None = None   # change in volume rate
    update_freq_1h:      int | None = None     # price changes detected in last hour

    # ── distance / position ───────────────────────────────────────────────────
    time_since_low_min:  float | None = None   # minutes since lowest prob recorded
    distance_from_low:   float | None = None   # current_prob - lowest_prob

    # ── scoring ───────────────────────────────────────────────────────────────
    watch_score:         float | None = None   # 0–100, set by the PRE_SPIKE engine
    score_updated_at:    datetime = field(default_factory=datetime.utcnow)


@dataclass
class AnomalyEvent:
    """Represents a detected anomaly, ready to be sent as a Discord alert."""
    anomaly_type:        AnomalyType
    snapshot:            MarketSnapshot
    prev_probability:    float | None
    current_probability: float
    move:                float | None
    time_window_minutes: int | None
    stats:               MarketStats

    # ── PRE_SPIKE_CANDIDATE only (defaults keep other rules unchanged) ────────
    watch_score:         float | None = None              # total score, 0–100
    score_breakdown:     dict[str, float] | None = None   # per-component points
    urgent:              bool = False                     # score >= urgent threshold


@dataclass
class AlertRecord:
    """Stored in SQLite to enforce cooldown deduplication."""
    market_id:    str
    player_name:  str
    source:       str
    anomaly_type: str
    prev_prob:    float
    curr_prob:    float
    sent_at:      datetime = field(default_factory=datetime.utcnow)
    match_key:    str | None = None
