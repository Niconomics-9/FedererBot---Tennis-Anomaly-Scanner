"""
Offline outcome report for sent alerts — measures whether alerts "worked"
in wave-chasing terms: after the alert, how far did the odds move?

An alert is judged by the forward price path, not by who won the match:
  peak    max probability within the window minus probability at alert time
          (the wave you could have ridden)
  dip     min probability within the window minus probability at alert time
          (the drawdown you would have sat through)

Report sections
---------------
1. Per anomaly type: count, median/max peak, % reaching +3 pp and +5 pp
   within each window, median dip.
2. Pre-match vs in-play/unknown split (match_start_time vs alert time) —
   evidence for the pre-match-only strategy. Alerts sent before 2026-06-11
   predate start-time capture and show as "unknown".

Standalone on purpose: opens the database read-only via sqlite URI and does
not import project modules, so it needs no .env and can never write.

Usage:
    python analyze_alert_outcomes.py [path\\to\\tennis_scanner.db]
    (default: DB_PATH env var, else tennis_scanner.db in the cwd)
"""

import os
import sqlite3
import statistics
import sys
from bisect import bisect_right
from datetime import datetime, timedelta

# Windows consoles may default to cp1252, which cannot print the dividers.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WINDOWS_MIN = (30, 60, 120)
WAVE_LEVELS = (0.03, 0.05)      # +3 pp / +5 pp forward moves worth reporting


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "tennis_scanner.db")
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    alerts = conn.execute(
        "SELECT * FROM alerts_sent ORDER BY sent_at"
    ).fetchall()
    if not alerts:
        print("alerts_sent is empty — let the scanner run first.")
        return 0

    now_iso = datetime.utcnow().isoformat()
    results: list[dict] = []
    for alert in alerts:
        key = (alert["market_id"], alert["player_name"], alert["source"])
        snaps = conn.execute(
            """
            SELECT probability, timestamp, match_start_time FROM market_snapshots
            WHERE market_id = ? AND player_name = ? AND source = ?
            ORDER BY timestamp
            """,
            key,
        ).fetchall()
        if not snaps:
            continue
        timestamps = [s["timestamp"] for s in snaps]
        probs = [s["probability"] for s in snaps]

        t0 = alert["sent_at"]
        entry = {
            "type": alert["anomaly_type"],
            "entry_prob": alert["curr_prob"],
            "phase": _phase(snaps, t0),
        }
        for window in WINDOWS_MIN:
            t_end = (datetime.fromisoformat(t0) + timedelta(minutes=window)).isoformat()
            lo, hi = bisect_right(timestamps, t0), bisect_right(timestamps, t_end)
            forward = probs[lo:hi]
            if forward:
                entry[f"peak_{window}"] = round(max(forward) - alert["curr_prob"], 6)
                entry[f"dip_{window}"] = round(min(forward) - alert["curr_prob"], 6)
            else:
                entry[f"peak_{window}"] = None
                entry[f"dip_{window}"] = None
            entry[f"complete_{window}"] = t_end <= now_iso and bool(forward)
        results.append(entry)

    for window in WINDOWS_MIN:
        _print_type_report(results, window)
    _print_phase_report(results, 60)
    return 0


def _phase(snaps: list[sqlite3.Row], sent_at: str) -> str:
    """pre_match / in_play / unknown at alert time, from the latest snapshot
    at or before the alert that carries a match_start_time."""
    start = None
    for s in snaps:
        if s["timestamp"] > sent_at:
            break
        if s["match_start_time"]:
            start = s["match_start_time"]
    if start is None:
        return "unknown"
    return "pre_match" if sent_at < start else "in_play"


def _print_type_report(results: list[dict], window: int) -> None:
    types = sorted({r["type"] for r in results})
    print(f"\n{'=' * 86}")
    print(f"Alert outcomes by type — forward window {window} min "
          f"(peak = best rideable move from alert price)")
    print(f"{'=' * 86}")
    print(f"{'type':<28} {'n':>5} {'med peak':>9} {'max peak':>9} "
          + " ".join(f">={lvl * 100:.0f}pp" for lvl in WAVE_LEVELS)
          + f" {'med dip':>9}")
    for atype in types:
        rows = [r for r in results if r["type"] == atype and r[f"complete_{window}"]]
        pending = sum(1 for r in results if r["type"] == atype and not r[f"complete_{window}"])
        if not rows:
            print(f"{atype:<28} {0:>5}" + (f"   ({pending} pending)" if pending else ""))
            continue
        peaks = [r[f"peak_{window}"] for r in rows]
        dips = [r[f"dip_{window}"] for r in rows]
        level_rates = " ".join(
            f"{sum(1 for p in peaks if p >= lvl) / len(peaks):>5.0%}" for lvl in WAVE_LEVELS
        )
        print(f"{atype:<28} {len(rows):>5} {statistics.median(peaks) * 100:>+8.2f}p "
              f"{max(peaks) * 100:>+8.2f}p {level_rates} "
              f"{statistics.median(dips) * 100:>+8.2f}p"
              + (f"   ({pending} pending)" if pending else ""))


def _print_phase_report(results: list[dict], window: int) -> None:
    print(f"\n{'=' * 86}")
    print(f"Pre-match vs in-play at alert time — forward window {window} min")
    print(f"{'=' * 86}")
    print(f"{'phase':<12} {'n':>5} {'med peak':>9} "
          + " ".join(f">={lvl * 100:.0f}pp" for lvl in WAVE_LEVELS)
          + f" {'med dip':>9}")
    for phase in ("pre_match", "in_play", "unknown"):
        rows = [r for r in results if r["phase"] == phase and r[f"complete_{window}"]]
        if not rows:
            print(f"{phase:<12} {0:>5}")
            continue
        peaks = [r[f"peak_{window}"] for r in rows]
        dips = [r[f"dip_{window}"] for r in rows]
        level_rates = " ".join(
            f"{sum(1 for p in peaks if p >= lvl) / len(peaks):>5.0%}" for lvl in WAVE_LEVELS
        )
        print(f"{phase:<12} {len(rows):>5} {statistics.median(peaks) * 100:>+8.2f}p "
              f"{level_rates} {statistics.median(dips) * 100:>+8.2f}p")


if __name__ == "__main__":
    sys.exit(main())
