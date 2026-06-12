"""
Missed-wave post-mortem — for every pre-match wave the bot did NOT alert on,
attribute WHY using the saved score trail, in gate order:

  1. never_in_band      no pre-match snapshot inside the PRE_SPIKE 2-30% band
                        -> the hard band gate made an alert impossible
  2. in_band_no_scores  band-eligible at some point but no score_history rows
                        pre-match (score stayed under the history floor)
  3. score_too_low      scored while eligible but max score < alert threshold
                        -> component deficit breakdown shows what was missing
  4. gated_after_score  a row crossed the threshold yet no alert
                        -> alert_quality gates or cooldown swallowed it

Reuses backtest_match_waves' labelling so "wave" means the same thing
(+3 pp best entry->peak, entry >= 10 min pre-start). Read-only on the DB.

Usage:
    python analyze_missed_waves.py [path\\to\\tennis_scanner.db]
"""

import json
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from itertools import groupby

import backtest_match_waves as bt
from config import settings
from core.market_classifier import MarketType, classify_market
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BAND_MIN = settings.PRE_SPIKE_PROB_MIN
BAND_MAX = settings.PRE_SPIKE_PROB_MAX
THRESHOLD = settings.PRE_SPIKE_ALERT_SCORE

# max points per component (from pre_spike_engine constants)
COMPONENT_MAX = {
    "pressure.spread_tightening": 6, "pressure.volume_surge": 12,
    "pressure.liquidity_rising": 5, "pressure.velocity_positive": 8,
    "pressure.acceleration_positive": 5, "pressure.sustained_move": 10,
    "pressure.update_freq_rising": 6,
    "low_odds.in_band": 5, "low_odds.coiled_turning_up": 12,
    "low_odds.recent_new_low": 8,
    "cross.kalshi_confirmation": 6, "external.momentum": 10,
}


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "tennis_scanner.db")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    alerts = bt._load_alerts(conn)
    scores = bt._load_scores(conn)

    snaps_by_series: dict[tuple, list] = {}
    rows = conn.execute(
        """
        SELECT market_id, player_name, match_name, probability, timestamp,
               match_start_time, spread, volume_total
        FROM market_snapshots
        WHERE source = 'polymarket'
        ORDER BY market_id, player_name, timestamp
        """
    ).fetchall()

    results = []
    for (market_id, player_name), group in groupby(rows, key=lambda r: (r["market_id"], r["player_name"])):
        snaps = list(group)
        classification = classify_market(snaps[-1]["match_name"], player_name)
        if classification.market_type != MarketType.MATCH_WINNER:
            continue
        start_iso = None
        for s in snaps:
            if s["match_start_time"]:
                start_iso = s["match_start_time"]
        if start_iso is None:
            continue
        r = bt._evaluate_series(
            market_id, player_name, snaps, start_iso, now_iso,
            classification.match_key, alerts, scores,
        )
        snaps_by_series[(market_id, player_name)] = snaps
        results.append(r)

    waves = [r for r in results if r.get("would_have_worked")]
    missed = [r for r in waves if not r["alerted_prematch"]]
    caught = [r for r in waves if r["alerted_prematch"]]
    print(f"waves: {len(waves)}  caught: {len(caught)}  missed: {len(missed)}\n")

    # full score rows (with components) per series
    def score_rows(mid, player, before_iso):
        return conn.execute(
            """
            SELECT probability, total_score, components_json, created_at
            FROM score_history
            WHERE source='polymarket' AND market_id=? AND player_name=? AND created_at < ?
            ORDER BY created_at
            """,
            (mid, player, before_iso),
        ).fetchall()

    buckets = defaultdict(list)
    deficits = defaultdict(list)   # component -> [pts at best eligible row]
    best_scores = []

    print(f"{'failure mode':<20} {'wave':>6} {'entry%':>7} {'peak%':>7} {'maxScore':>9}  series")
    for r in sorted(missed, key=lambda x: -x["best_wave_pp"]):
        key = (r["market_id"], r["player_name"])
        start_iso = r["match_start_time"]
        pre_snaps = [s for s in snaps_by_series[key] if s["timestamp"] < start_iso]
        in_band_ever = any(BAND_MIN <= s["probability"] <= BAND_MAX for s in pre_snaps)

        srows = score_rows(*key, start_iso)
        eligible = [s for s in srows if BAND_MIN <= s["probability"] <= BAND_MAX]
        best_elig = max(eligible, key=lambda s: s["total_score"], default=None)

        if not in_band_ever:
            mode = "never_in_band"
        elif not eligible:
            mode = "in_band_no_scores"
        elif best_elig["total_score"] < THRESHOLD:
            mode = "score_too_low"
            best_scores.append(best_elig["total_score"])
            for comp, pts in json.loads(best_elig["components_json"]).items():
                deficits[comp].append(pts)
        else:
            mode = "gated_after_score"

        buckets[mode].append(r)
        ms = f"{best_elig['total_score']:.0f}" if best_elig else "—"
        print(f"{mode:<20} {r['best_wave_pp']:>+5.1f}p {r['entry_prob']*100:>6.1f} "
              f"{r['peak_prob']*100:>6.1f} {ms:>9}  {r['player_name']} | {r['match_name'][:45]}")

    print(f"\n{'=' * 80}\nattribution: " + ", ".join(f"{k}: {len(v)}" for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))))

    if best_scores:
        print(f"\nscore_too_low cases — best eligible score: median {statistics.median(best_scores):.0f}, "
              f"max {max(best_scores):.0f} (threshold {THRESHOLD:.0f})")
        print(f"\ncomponent points at each case's best row (max possible | mean | hit rate):")
        for comp, vals in sorted(deficits.items(), key=lambda kv: -(COMPONENT_MAX.get(kv[0], 0) - statistics.mean(kv[1]))):
            mx = COMPONENT_MAX.get(comp, 0)
            nz = sum(1 for v in vals if v > 0)
            print(f"  {comp:<32} {mx:>3} | {statistics.mean(vals):>5.1f} | {nz}/{len(vals)} nonzero")

    # the caught one + false alarms, for contrast
    for r in caught:
        print(f"\ncaught wave: {r['player_name']} | {r['match_name'][:50]} "
              f"({r['first_prematch_alert_type']}, wave {r['best_wave_pp']:+.1f}p)")
    fa = [r for r in results if r["status"] == "completed" and r["alerted_prematch"] and not r["would_have_worked"]]
    print(f"false alarms (pre-match alert, no wave): {len(fa)}")
    for r in fa:
        print(f"  {r['player_name']} | {r['match_name'][:50]} ({r['first_prematch_alert_type']}, "
              f"label={r['label']}, wave {r.get('best_wave_pp', 0) or 0:+.1f}p)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
