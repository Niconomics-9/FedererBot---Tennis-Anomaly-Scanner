"""
External / momentum signals.

Two signals are now implemented from data already in the SQLite DB
(no external API required):

  sustained_odds_drift  -- Has the market moved consistently upward over
                             the past 2 hours (not just the last cycle)?
                                                        Measured via two time anchors (2 h ago and
                                                                                   30 min ago) so a single repricing tick does
                                                                                                              not score; a genuine sustained wave does.
                                                                                                              
                                                                                                                volume_trend_long     -- Is trading volume rising across a 2-hour window
                                                                                                                                           (not just in the last cycle)? The 2-hour window
                                                                                                                                                                      is split into 4 half-hour buckets; the score is
                                                                                                                                                                                                 the fraction of consecutive bucket pairs where
                                                                                                                                                                                                                            volume inflow increased.
                                                                                                                                                                                                                            
                                                                                                                                                                                                                            The live-score, break-of-serve, news, and social fields remain stubs --
                                                                                                                                                                                                                            they define the interface so each source can be wired in later without
                                                                                                                                                                                                                            touching the scoring code.
                                                                                                                                                                                                                            
                                                                                                                                                                                                                            Contract
                                                                                                                                                                                                                            --------
                                                                                                                                                                                                                            - get_external_momentum() never raises and never blocks on network I/O for
                                                                                                                                                                                                                              longer than a poll cycle can afford.
                                                                                                                                                                                                                              - All fields are None when a source is unavailable; score_external() treats
                                                                                                                                                                                                                                None as "no evidence" (0 points), never as negative evidence.
                                                                                                                                                                                                                                - score_external() is deterministic and returns 0..10.
                                                                                                                                                                                                                                """

import logging
from dataclasses import dataclass
from datetime import timedelta

from market_providers.models import MarketSnapshot
from storage import sqlite_storage as db

logger = logging.getLogger(__name__)


@dataclass
class ExternalMomentum:
    """One reading of off-market momentum evidence for a player."""

  # -- Signals implemented from existing DB data (no API needed) -----------
    # Sustained directional drift measured over a 2-h window: 0..1
  # (0 = no drift or inconsistent direction, 1 = large clean upward move).
  sustained_odds_drift: float | None = None

  # Long-window volume trend measured over 2 h with 4 half-hour buckets:
  # fraction of consecutive bucket pairs where inflow rose (0, 0.33, 0.67, 1).
  volume_trend_long: float | None = None

  # -- Stubs: external sources (API integration needed) --------------------
  live_score_pressure: float | None = None  # 0-1: in-match momentum
  break_of_serve:      bool  | None = None  # player just broke serve
  news_buzz:           float | None = None  # 0-1: news velocity
  social_buzz:         float | None = None  # 0-1: social chatter velocity


# -- point allocation (bucket max = 10; cap applied in score_external) -----
# DB-backed signals (live today)
_WEIGHT_SUSTAINED_DRIFT = 7.0
_WEIGHT_VOLUME_LONG     = 3.0
# Stub signals (contribute 0 until integrated; weights kept for documentation)
_WEIGHT_LIVE_SCORE      = 4.0
_WEIGHT_BREAK           = 3.0
_WEIGHT_NEWS            = 2.0
_WEIGHT_SOCIAL          = 1.0

# Thresholds for sustained_odds_drift
_DRIFT_WINDOW_HOURS         = 2       # how far back to look for the early anchor
_DRIFT_MID_MINUTES          = 30      # the mid-point anchor
_DRIFT_MAX_PP               = 0.12    # prob delta that earns a full 1.0 score (12 pp)
_DRIFT_MIN_PP               = 0.005   # moves smaller than this score 0 (noise floor)
_DRIFT_CONSISTENCY_TOL      = -0.005  # one leg may dip this much without disqualifying

# Thresholds for volume_trend_long
_VOL_WINDOW_HOURS = 2   # total look-back
_VOL_BUCKETS      = 4   # number of equal-width sub-windows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_external_momentum(snapshot: MarketSnapshot) -> ExternalMomentum:
    """
      Compute external momentum signals from the SQLite snapshot history.
        Never raises; missing data gracefully returns None for the field.
          """
    now = snapshot.timestamp
    return ExternalMomentum(
      sustained_odds_drift=_compute_sustained_drift(snapshot, now),
      volume_trend_long=_compute_volume_trend(snapshot, now),
    )


def score_external(momentum: ExternalMomentum) -> float:
    """
      Convert an ExternalMomentum reading into 0..10 points, deterministically.
        None fields contribute nothing (no evidence != negative evidence).
          The bucket is hard-capped at 10 regardless of how many signals fire.
            """
    pts = 0.0

  # DB-backed signals (active now)
    if momentum.sustained_odds_drift is not None:
          pts += _WEIGHT_SUSTAINED_DRIFT * max(0.0, min(1.0, momentum.sustained_odds_drift))
        if momentum.volume_trend_long is not None:
              pts += _WEIGHT_VOLUME_LONG * max(0.0, min(1.0, momentum.volume_trend_long))

  # Stubs (contribute 0 until integrated)
  if momentum.live_score_pressure is not None:
        pts += _WEIGHT_LIVE_SCORE * max(0.0, min(1.0, momentum.live_score_pressure))
      if momentum.break_of_serve:
            pts += _WEIGHT_BREAK
          if momentum.news_buzz is not None:
                pts += _WEIGHT_NEWS * max(0.0, min(1.0, momentum.news_buzz))
              if momentum.social_buzz is not None:
                    pts += _WEIGHT_SOCIAL * max(0.0, min(1.0, momentum.social_buzz))

  return round(min(pts, 10.0), 2)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_sustained_drift(
    snapshot: MarketSnapshot,
    now,
) -> float | None:
    """
      Measures whether the market has moved consistently upward over 2 hours.

        Two time anchors are fetched from the DB:
            early  = oldest snapshot at or after (now - 2 h)
                mid    = oldest snapshot at or after (now - 30 min)

                  Then two legs are computed:
                      leg1 = prob_mid   - prob_early   (first ~1.5 h of the window)
                          leg2 = prob_now   - prob_mid     (last 30 min)

                            Both legs must point upward (within a small noise tolerance) for the move
                              to be classified as "sustained". Total drift is then normalised by
                                _DRIFT_MAX_PP and clipped to 0..1. A 20% bonus is applied when both legs
                                  are unambiguously positive.

                                    Returns None if there is insufficient history (< 2 h of snapshots).
                                      """
    try:
          since_early = now - timedelta(hours=_DRIFT_WINDOW_HOURS)
          since_mid   = now - timedelta(minutes=_DRIFT_MID_MINUTES)

      snap_early = db.get_snapshot_at_or_after(
              snapshot.market_id, snapshot.player_name, snapshot.source, since_early
      )
    snap_mid = db.get_snapshot_at_or_after(
            snapshot.market_id, snapshot.player_name, snapshot.source, since_mid
    )

    if snap_early is None or snap_mid is None:
            return None

    # Avoid comparing a snapshot to itself
    if abs((snap_early.timestamp - now).total_seconds()) < 60:
            return None
          if abs((snap_mid.timestamp - now).total_seconds()) < 30:
                  return None

    leg1 = snap_mid.probability   - snap_early.probability
    leg2 = snapshot.probability   - snap_mid.probability
    total = snapshot.probability  - snap_early.probability

    if total < _DRIFT_MIN_PP:
            return 0.0

    # Both legs must be non-negative within noise tolerance
    if leg1 < _DRIFT_CONSISTENCY_TOL or leg2 < _DRIFT_CONSISTENCY_TOL:
            # Inconsistent direction -- give fractional credit only
            return round(min(0.3, max(0.0, (total / _DRIFT_MAX_PP) * 0.4)), 4)

    raw = total / _DRIFT_MAX_PP
    if leg1 > 0 and leg2 > 0:
            raw *= 1.2  # consistency bonus
    return round(min(1.0, max(0.0, raw)), 4)

except Exception as exc:
    logger.debug(
            "[external] sustained_drift error for %s / %s: %s",
            snapshot.player_name, snapshot.market_id, exc,
    )
    return None


def _compute_volume_trend(
    snapshot: MarketSnapshot,
    now,
) -> float | None:
    """
      Measures whether trading volume has been consistently rising over 2 hours.

        The window is split into 4 equal half-hour buckets. The oldest snapshot at
          the start of each bucket is used as the volume baseline, and the current
            snapshot closes the final bucket. Inflow per bucket = volume_total delta.

              Score = (number of consecutive bucket pairs where inflow rose) / 3
                giving 0, 0.33, 0.67, or 1.0.

                  Returns None when volume_total is unavailable or history is too short.
                    """
  if snapshot.volume_total is None:
        return None

  try:
        bucket_h = _VOL_WINDOW_HOURS / _VOL_BUCKETS  # 0.5 h each

    # Build the 5 bucket-edge timestamps (start of each bucket + now)
        edges = [
                now - timedelta(hours=_VOL_WINDOW_HOURS - i * bucket_h)
                for i in range(_VOL_BUCKETS + 1)
        ]

    # Fetch oldest snapshot at or after each edge (except the last = now)
    bucket_snaps = [
            db.get_snapshot_at_or_after(
                      snapshot.market_id, snapshot.player_name, snapshot.source, edge
            )
            for edge in edges[:-1]
    ]

    # Require all 4 bucket anchors to be present
    if any(s is None or s.volume_total is None for s in bucket_snaps):
            return None

    # Build volume list: [v0, v1, v2, v3, v_now]
    volumes = [s.volume_total for s in bucket_snaps] + [snapshot.volume_total]

    # Inflow per bucket (volume_total is cumulative lifetime volume)
    inflows = [volumes[i + 1] - volumes[i] for i in range(_VOL_BUCKETS)]

    # Count consecutive rising bucket transitions
    rising = sum(
            1 for i in range(1, _VOL_BUCKETS) if inflows[i] > inflows[i - 1]
    )
    return round(rising / (_VOL_BUCKETS - 1), 4)

except Exception as exc:
    logger.debug(
            "[external] volume_trend_long error for %s / %s: %s",
            snapshot.player_name, snapshot.market_id, exc,
    )
    return None
