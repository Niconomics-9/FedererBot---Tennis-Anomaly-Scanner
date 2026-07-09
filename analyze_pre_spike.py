"""
Offline calibration report for the PRE_SPIKE scoring engine.

Joins score_history rows against subsequent market_snapshots to answer:
did high scores actually precede upward moves?  Run after the scanner has
collected a few days of live data, then tune scoring weights from evidence.

Report sections
---------------
1. Score buckets: count, hit-rate (forward move >= +3 pp within 60 min,
   matching the LOW_ODDS_SPIKE_DELTA default), median and max forward move.
2. Component signal: average points per component among hits vs misses —
   shows which components actually separate the two when tuning.

Read-only on purpose: the connection is opened with
default_transaction_read_only=on, so this script can never write.

Usage:
    python analyze_pre_spike.py [postgres-connection-url]
    (default: SUPABASE_DB_URL env var, or .env)
"""

import statistics
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta

from storage.report_db import connect_readonly, rows

# Windows consoles may default to cp1252, which cannot print the dividers.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WINDOWS_MIN = (30, 60)        # forward-looking windows
HIT_MOVE = 0.03               # +3 pp within 60 min counts as a hit
BUCKETS = [(0, 40), (40, 55), (55, 70), (70, 85), (85, 100.01)]


def main() -> int:
    conn = connect_readonly(sys.argv[1] if len(sys.argv) > 1 else None)

    history = rows(
        conn,
        "SELECT * FROM score_history ORDER BY market_id, player_name, source, created_at",
    )
    if not history:
        print("score_history is empty — let the scanner run first.")
        return 0

    # Group history rows per market so each market's snapshots load only once.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in history:
        groups[(row["market_id"], row["player_name"], row["source"])].append(row)

    now_iso = datetime.utcnow().isoformat()
    results: list[dict] = []
    for key, group_rows in groups.items():
        snaps = rows(
            conn,
            """
            SELECT probability, timestamp FROM market_snapshots
            WHERE market_id = %s AND player_name = %s AND source = %s
            ORDER BY timestamp
            """,
            key,
        )
        timestamps = [s["timestamp"] for s in snaps]
        probs = [s["probability"] for s in snaps]

        for row in group_rows:
            t0 = row["created_at"]
            entry = {
                "score": row["total_score"],
                "components": row["components"],   # JSONB arrives parsed
            }
            for window in WINDOWS_MIN:
                t_end = (datetime.fromisoformat(t0) + timedelta(minutes=window)).isoformat()
                lo, hi = bisect_right(timestamps, t0), bisect_right(timestamps, t_end)
                forward = probs[lo:hi]
                entry[f"move_{window}"] = (
                    round(max(forward) - row["probability"], 6) if forward else None
                )
                # Window still open (or no data yet) — exclude from hit-rates.
                entry[f"complete_{window}"] = t_end <= now_iso and bool(forward)
            results.append(entry)

    _print_bucket_report(results)
    _print_component_report(results)
    return 0


def _print_bucket_report(results: list[dict]) -> None:
    print(f"\n{'=' * 72}")
    print(f"Score buckets — hit = forward move >= +{HIT_MOVE * 100:.0f} pp within 60 min")
    print(f"{'=' * 72}")
    print(f"{'bucket':<10} {'n':>6} {'hits':>6} {'hit-rate':>9} {'median mv60':>12} {'max mv60':>10}")
    for lo, hi in BUCKETS:
        rows = [r for r in results if lo <= r["score"] < hi and r["complete_60"]]
        pending = sum(1 for r in results if lo <= r["score"] < hi and not r["complete_60"])
        label = f"{lo:g}-{min(hi, 100):g}"
        if not rows:
            print(f"{label:<10} {0:>6} {'-':>6} {'-':>9} {'-':>12} {'-':>10}"
                  + (f"   ({pending} pending)" if pending else ""))
            continue
        moves = [r["move_60"] for r in rows]
        hits = sum(1 for m in moves if m >= HIT_MOVE)
        print(f"{label:<10} {len(rows):>6} {hits:>6} {hits / len(rows):>8.0%} "
              f"{statistics.median(moves) * 100:>+11.2f}pp {max(moves) * 100:>+9.2f}pp"
              + (f"   ({pending} pending)" if pending else ""))
    total_complete = sum(1 for r in results if r["complete_60"])
    if total_complete < 50:
        print(f"\nNote: only {total_complete} completed rows — treat rates as indicative only.")


def _print_component_report(results: list[dict]) -> None:
    complete = [r for r in results if r["complete_60"]]
    hits = [r for r in complete if r["move_60"] >= HIT_MOVE]
    misses = [r for r in complete if r["move_60"] < HIT_MOVE]
    if not hits or not misses:
        print("\nComponent report skipped — need at least one hit and one miss.")
        return

    keys = sorted({k for r in complete for k in r["components"]})
    print(f"\n{'=' * 72}")
    print(f"Component signal — avg points among hits (n={len(hits)}) vs misses (n={len(misses)})")
    print(f"{'=' * 72}")
    print(f"{'component':<34} {'avg hit':>9} {'avg miss':>9} {'edge':>7}")
    for key in keys:
        avg_hit = statistics.mean(r["components"].get(key, 0.0) for r in hits)
        avg_miss = statistics.mean(r["components"].get(key, 0.0) for r in misses)
        print(f"{key:<34} {avg_hit:>9.2f} {avg_miss:>9.2f} {avg_hit - avg_miss:>+7.2f}")


if __name__ == "__main__":
    sys.exit(main())
