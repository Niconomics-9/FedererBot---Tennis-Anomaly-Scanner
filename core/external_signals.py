"""
External / momentum signals — STUB.

Live score feeds, break-of-serve detection, news, and social chatter are not
integrated yet.  This module defines the interface the PRE_SPIKE engine
consumes so each source can be wired in later without touching the scoring
code: implement a fetcher, populate the matching ExternalMomentum field, and
the points start flowing.

Contract
--------
- get_external_momentum() never raises and never blocks on network I/O for
  longer than a poll cycle can afford.  Today it returns an empty reading.
- All fields are None when a source is unavailable; score_external() treats
  None as "no evidence" (0 points), never as negative evidence.
- score_external() is deterministic and returns 0..max_points.
"""

import logging
from dataclasses import dataclass

from market_providers.models import MarketSnapshot

logger = logging.getLogger(__name__)


@dataclass
class ExternalMomentum:
    """One reading of off-market momentum evidence for a player."""

    live_score_pressure: float | None = None  # 0–1: in-match momentum (e.g. points won streak)
    break_of_serve:      bool | None = None   # player just broke opponent's serve
    news_buzz:           float | None = None  # 0–1: news velocity about the player
    social_buzz:         float | None = None  # 0–1: social chatter velocity


# Point allocation within the external bucket (sums to the bucket max of 10).
_WEIGHT_LIVE_SCORE = 4.0
_WEIGHT_BREAK      = 3.0
_WEIGHT_NEWS       = 2.0
_WEIGHT_SOCIAL     = 1.0


def get_external_momentum(snapshot: MarketSnapshot) -> ExternalMomentum:
    """
    Fetch external momentum evidence for this player.

    STUB: no sources are integrated, so every field is None and the bucket
    scores 0.  Future integrations replace pieces of this function.
    """
    return ExternalMomentum()


def score_external(momentum: ExternalMomentum) -> float:
    """
    Convert an ExternalMomentum reading into 0..10 points, deterministically.
    None fields contribute nothing.
    """
    points = 0.0
    if momentum.live_score_pressure is not None:
        points += _WEIGHT_LIVE_SCORE * max(0.0, min(1.0, momentum.live_score_pressure))
    if momentum.break_of_serve:
        points += _WEIGHT_BREAK
    if momentum.news_buzz is not None:
        points += _WEIGHT_NEWS * max(0.0, min(1.0, momentum.news_buzz))
    if momentum.social_buzz is not None:
        points += _WEIGHT_SOCIAL * max(0.0, min(1.0, momentum.social_buzz))
    return round(min(points, 10.0), 2)
