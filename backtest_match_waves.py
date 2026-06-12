"""
Retrospective wave backtest — labels every saved match with whether pre-match
wave-chasing WOULD have worked, so tuning can be judged against all matches,
not only the ones that happened to alert.

For each Polymarket match-winner series (market x player) with a known start
time, replay the pre-match price path and find the best entry -> peak move:

  entry   any snapshot taken >= 10 minutes before the match start
          (mirrors ALERT_PRE_MATCH_MIN_LEAD_MINUTES — the actionability rule)
  peak    the highest later snapshot still before the match start
  wave    peak - entry, in probability points

A series "would have worked" when wave >= +3 pp (the success bar from
LEARNINGS.md; +5 pp reported as the stronger tier). The match result is
irrelevant — this measures rideable pre-match moves only. Kalshi is excluded
because its snapshots carry no start time.

Output
------
1. stdout summary — saved-data inventory, wave rates, entry-band breakdown,
   wave timing, alert hit/miss cross-reference, and a watch_score replay
   (max pre-match PRE_SPIKE score for wave vs non-wave series, with
   precision/recall at the live alert threshold).
2. reports\\match_waves_YYYY-MM-DD.csv — one labelled row per series: the
   per-match "would it have worked" dataset (keeps the most recent 30 files).

Read-only on the database (sqlite mode=ro); needs no .env. The only project
import is core.market_classifier (pure stdlib), so match-winner filtering
matches the live scanner exactly instead of drifting.

Usage:
    python backtest_match_waves.py [path\\to\\tennis_scanner.db]
    (default: DB_PATH env var, else tennis_scanner.db in the cwd)
"""

import csv
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path

from core.market_classifier import MarketType, classify_market

# Windows consoles may default to cp1252, which cannot print the dividers.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MIN_LEAD_MINUTES = 10          # entry must be at least this far before start
MIN_PRE_SNAPSHOTS = 5          # fewer pre-match readings -> insufficient_history
WAVE_WORKED = 0.03             # +3 pp: "would have worked" (LEARNINGS.md bar)
WAVE_STRONG = 0.05             # +5 pp: the stronger tier
SCORE_ALERT_THRESHOLD = 45.0   # mirrors settings.PRE_SPIKE_ALERT_SCORE (kept
                               # as a constant: this script must run without .env)
KEEP_CSVS = 30

ENTRY_BANDS = (
    (0.00, 0.02, "<2%"),
    (0.02, 0.12, "2-12%"),
    (0.12, 0.30, "12-30%"),
    (0.30, 0.60, "30-60%"),
    (0.60, 1.01, "60%+"),
)

CSV_FIELDS = [
    "source", "market_id", "player_name", "match_name", "match_key",
    "match_start_time", "status", "label", "would_have_worked",
    "n_pre_snapshots", "pre_span_minutes",
    "best_wave_pp", "entry_time", "entry_lead_minutes", "entry_prob",
    "peak_prob", "peak_time", "peak_lead_minutes",
    "move_at_start_pp", "max_dip_after_entry_pp",
    "entry_spread", "entry_volume", "entry_velocity5_pp", "entry_band",
    "max_prematch_watch_score", "n_prematch_score_rows",
    "alerted_prematch", "first_prematch_alert_type", "first_prematch_alert_at",
    "alerted_any",
]


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DB_PATH", "tennis_scanner.db")
    if not os.path.exists(db_path):
        print(f"Database not found: {db_path}")
        return 1

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    _print_inventory(conn)

    alerts = _load_alerts(conn)
    scores = _load_scores(conn)

    rows = conn.execute(
        """
        SELECT market_id, player_name, match_name, probability, timestamp,
               match_start_time, spread, volume_total
        FROM market_snapshots
        WHERE source = 'polymarket'
        ORDER BY market_id, player_name, timestamp
        """
    ).fetchall()

    skipped = {"not_match_winner": 0, "no_start_time": 0}
    results: list[dict] = []
    for (market_id, player_name), group in groupby(
        rows, key=lambda r: (r["market_id"], r["player_name"])
    ):
        snaps = list(group)
        classification = classify_market(snaps[-1]["match_name"], player_name)
        if classification.market_type != MarketType.MATCH_WINNER:
            skipped["not_match_winner"] += 1
            continue

        start_iso = None
        for s in snaps:
            if s["match_start_time"]:
                start_iso = s["match_start_time"]   # latest reading wins
        if start_iso is None:
            skipped["no_start_time"] += 1
            continue

        results.append(_evaluate_series(
            market_id, player_name, snaps, start_iso, now_iso,
            classification.match_key, alerts, scores,
        ))

    if not results:
        print("\nNo backtestable series yet — let the scanner run first.")
        return 0

    csv_path = _write_csv(results)
    _print_summary(results, skipped, csv_path)
    return 0


# ── data loading ──────────────────────────────────────────────────────────────

def _load_alerts(conn: sqlite3.Connection) -> dict[tuple, list]:
    """(market_id, player_name) -> [(sent_at, anomaly_type), ...] sorted."""
    out: dict[tuple, list] = {}
    for r in conn.execute(
        """
        SELECT market_id, player_name, anomaly_type, sent_at
        FROM alerts_sent WHERE source = 'polymarket' ORDER BY sent_at
        """
    ):
        out.setdefault((r["market_id"], r["player_name"]), []).append(
            (r["sent_at"], r["anomaly_type"])
        )
    return out


def _load_scores(conn: sqlite3.Connection) -> dict[tuple, list]:
    """(market_id, player_name) -> [(created_at, total_score), ...] sorted."""
    out: dict[tuple, list] = {}
    for r in conn.execute(
        """
        SELECT market_id, player_name, total_score, created_at
        FROM score_history WHERE source = 'polymarket' ORDER BY created_at
        """
    ):
        out.setdefault((r["market_id"], r["player_name"]), []).append(
            (r["created_at"], r["total_score"])
        )
    return out


# ── per-series evaluation ─────────────────────────────────────────────────────

def _evaluate_series(
    market_id: str,
    player_name: str,
    snaps: list[sqlite3.Row],
    start_iso: str,
    now_iso: str,
    match_key: str,
    alerts: dict[tuple, list],
    scores: dict[tuple, list],
) -> dict:
    start_dt = datetime.fromisoformat(start_iso)
    status = "completed" if start_iso <= now_iso else "upcoming"

    pre = [s for s in snaps if s["timestamp"] < start_iso]
    row: dict = {
        "source": "polymarket",
        "market_id": market_id,
        "player_name": player_name,
        "match_name": snaps[-1]["match_name"],
        "match_key": match_key,
        "match_start_time": start_iso,
        "status": status,
        "n_pre_snapshots": len(pre),
        "would_have_worked": 0,
    }
    if pre:
        span = datetime.fromisoformat(pre[-1]["timestamp"]) - datetime.fromisoformat(pre[0]["timestamp"])
        row["pre_span_minutes"] = round(span.total_seconds() / 60, 1)

    _join_alerts(row, alerts.get((market_id, player_name)), start_iso)
    _join_scores(row, scores.get((market_id, player_name)), start_iso)

    if len(pre) < MIN_PRE_SNAPSHOTS:
        row["label"] = "insufficient_history"
        return row

    best = _best_wave(pre, start_dt)
    if best is None:
        row["label"] = "no_valid_entry"
        return row

    entry_i, peak_i, wave = best
    entry, peak = pre[entry_i], pre[peak_i]
    entry_dt = datetime.fromisoformat(entry["timestamp"])
    peak_dt = datetime.fromisoformat(peak["timestamp"])
    dip = min(s["probability"] for s in pre[entry_i:peak_i + 1]) - entry["probability"]

    if wave >= WAVE_STRONG:
        row["label"] = "wave_5pp"
    elif wave >= WAVE_WORKED:
        row["label"] = "wave_3pp"
    elif wave >= 0.01:
        row["label"] = "small_move"
    else:
        row["label"] = "flat"
    row["would_have_worked"] = int(wave >= WAVE_WORKED and status == "completed")

    row.update({
        "best_wave_pp": round(wave * 100, 2),
        "entry_time": entry["timestamp"],
        "entry_lead_minutes": round((start_dt - entry_dt).total_seconds() / 60, 1),
        "entry_prob": round(entry["probability"], 4),
        "peak_prob": round(peak["probability"], 4),
        "peak_time": peak["timestamp"],
        "peak_lead_minutes": round((start_dt - peak_dt).total_seconds() / 60, 1),
        "move_at_start_pp": round((pre[-1]["probability"] - entry["probability"]) * 100, 2),
        "max_dip_after_entry_pp": round(dip * 100, 2),
        "entry_spread": entry["spread"],
        "entry_volume": entry["volume_total"],
        "entry_band": _band(entry["probability"]),
    })
    if entry_i >= 5:
        row["entry_velocity5_pp"] = round(
            (entry["probability"] - pre[entry_i - 5]["probability"]) * 100, 2
        )
    return row


def _best_wave(pre: list[sqlite3.Row], start_dt: datetime) -> tuple[int, int, float] | None:
    """
    (entry_index, peak_index, wave) maximising forward rise. Entry needs
    >= MIN_LEAD_MINUTES before start; the peak may be any later pre-match
    snapshot (a position can be exited right up to the start). Earliest entry
    wins ties so reported lead times are conservative-realistic.
    """
    entry_cutoff = (start_dt - timedelta(minutes=MIN_LEAD_MINUTES)).isoformat()
    n = len(pre)

    # best_after[i] = (max probability, its earliest index) over j > i
    best_after: list[tuple[float, int] | None] = [None] * n
    cur_val, cur_idx = -1.0, -1
    for i in range(n - 1, -1, -1):
        best_after[i] = (cur_val, cur_idx) if cur_idx >= 0 else None
        if pre[i]["probability"] >= cur_val:
            cur_val, cur_idx = pre[i]["probability"], i

    best: tuple[int, int, float] | None = None
    for i in range(n - 1):
        if pre[i]["timestamp"] > entry_cutoff:
            break
        if best_after[i] is None:
            continue
        peak_val, peak_idx = best_after[i]
        wave = peak_val - pre[i]["probability"]
        if best is None or wave > best[2]:
            best = (i, peak_idx, wave)
    return best


def _join_alerts(row: dict, sent: list | None, start_iso: str) -> None:
    row["alerted_any"] = int(bool(sent))
    prematch = [a for a in (sent or []) if a[0] < start_iso]
    row["alerted_prematch"] = int(bool(prematch))
    if prematch:
        row["first_prematch_alert_at"] = prematch[0][0]
        row["first_prematch_alert_type"] = prematch[0][1]


def _join_scores(row: dict, hist: list | None, start_iso: str) -> None:
    prematch = [s[1] for s in (hist or []) if s[0] < start_iso]
    row["n_prematch_score_rows"] = len(prematch)
    if prematch:
        row["max_prematch_watch_score"] = round(max(prematch), 1)


def _band(prob: float) -> str:
    for lo, hi, name in ENTRY_BANDS:
        if lo <= prob < hi:
            return name
    return "?"


# ── output ────────────────────────────────────────────────────────────────────

def _write_csv(results: list[dict]) -> Path:
    reports = Path(__file__).resolve().parent / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / f"match_waves_{datetime.now():%Y-%m-%d}.csv"
    ordered = sorted(
        results,
        key=lambda r: (r["status"], -(r.get("best_wave_pp") or -999)),
    )
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in CSV_FIELDS} for r in ordered)
    for old in sorted(reports.glob("match_waves_*.csv"))[:-KEEP_CSVS]:
        old.unlink()
    return path


def _print_inventory(conn: sqlite3.Connection) -> None:
    lo, hi = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM market_snapshots").fetchone()
    print(f"\n{'=' * 86}")
    print(f"Saved data inventory — snapshots from {lo} to {hi} (UTC)")
    print(f"{'=' * 86}")
    print(f"{'source':<12} {'snapshots':>10} {'matches':>8} {'series':>8}")
    for r in conn.execute(
        """
        SELECT source, COUNT(*) AS n, COUNT(DISTINCT match_name) AS matches,
               COUNT(DISTINCT market_id || '|' || player_name) AS series
        FROM market_snapshots GROUP BY source ORDER BY n DESC
        """
    ):
        print(f"{r['source']:<12} {r['n']:>10,} {r['matches']:>8,} {r['series']:>8,}")
    alerts = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT anomaly_type) FROM alerts_sent"
    ).fetchone()
    scores = conn.execute("SELECT COUNT(*) FROM score_history").fetchone()[0]
    print(f"alerts sent: {alerts[0]}  |  score_history rows: {scores:,}")


def _print_summary(results: list[dict], skipped: dict, csv_path: Path) -> None:
    completed = [r for r in results if r["status"] == "completed"]
    upcoming = [r for r in results if r["status"] == "upcoming"]
    scored = [r for r in completed if r["label"] not in ("insufficient_history", "no_valid_entry")]

    print(f"\n{'=' * 86}")
    print("Backtest universe — Polymarket match-winner series with a known start time")
    print(f"{'=' * 86}")
    print(f"series found: {len(results)}  (completed {len(completed)}, upcoming {len(upcoming)})")
    print(f"skipped: {skipped['not_match_winner']} prop/future/unknown series, "
          f"{skipped['no_start_time']} match-winner series without start time")
    for label in ("insufficient_history", "no_valid_entry"):
        n = sum(1 for r in completed if r["label"] == label)
        if n:
            print(f"completed but {label}: {n}")

    if not scored:
        print("\nNo completed series with enough pre-match history yet.")
        return

    print(f"\n{'=' * 86}")
    print(f"Would it have worked? — best pre-match entry->peak move, {len(scored)} series")
    print(f"(worked = wave >= +{WAVE_WORKED * 100:.0f} pp; entry >= {MIN_LEAD_MINUTES} min before start)")
    print(f"{'=' * 86}")
    print(f"{'label':<22} {'n':>6} {'share':>7} {'med wave':>9} {'med entry lead':>15}")
    for label in ("wave_5pp", "wave_3pp", "small_move", "flat"):
        rows = [r for r in scored if r["label"] == label]
        if not rows:
            continue
        waves = [r["best_wave_pp"] for r in rows]
        leads = [r["entry_lead_minutes"] for r in rows]
        print(f"{label:<22} {len(rows):>6} {len(rows) / len(scored):>7.1%} "
              f"{statistics.median(waves):>+8.2f}p {statistics.median(leads):>13.0f}m")

    worked = [r for r in scored if r["would_have_worked"]]
    matches_all = {r["match_key"] for r in scored}
    matches_worked = {r["match_key"] for r in worked}
    print(f"\nwould have worked: {len(worked)}/{len(scored)} series ({len(worked) / len(scored):.1%})"
          f" — {len(matches_worked)}/{len(matches_all)} distinct matches"
          f" ({len(matches_worked) / len(matches_all):.1%})")

    print(f"\n{'-' * 86}")
    print("By entry band (belief 3: are 30-60% entries pulling their weight?)")
    print(f"{'band':<10} {'n':>6} {'worked':>7} {'rate':>7} {'med wave':>9} {'med dip':>9}")
    for _, _, name in ENTRY_BANDS:
        rows = [r for r in scored if r.get("entry_band") == name]
        if not rows:
            continue
        hits = [r for r in rows if r["would_have_worked"]]
        waves = [r["best_wave_pp"] for r in rows]
        dips = [r["max_dip_after_entry_pp"] for r in rows]
        print(f"{name:<10} {len(rows):>6} {len(hits):>7} {len(hits) / len(rows):>7.1%} "
              f"{statistics.median(waves):>+8.2f}p {statistics.median(dips):>+8.2f}p")

    if worked:
        print(f"\n{'-' * 86}")
        print("Wave timing (worked series) — how far before the start do waves begin/peak?")
        entry_leads = sorted(r["entry_lead_minutes"] for r in worked)
        peak_leads = sorted(r["peak_lead_minutes"] for r in worked)
        for name, vals in (("entry lead", entry_leads), ("peak lead", peak_leads)):
            q1, q3 = _quartiles(vals)
            print(f"{name}: median {statistics.median(vals):.0f} min "
                  f"(IQR {q1:.0f}-{q3:.0f} min before start)")

    print(f"\n{'-' * 86}")
    print("Did the live bot catch them? (pre-match alerts on this series)")
    caught = [r for r in worked if r["alerted_prematch"]]
    false_alarms = [r for r in scored if r["alerted_prematch"] and not r["would_have_worked"]]
    alerted = [r for r in scored if r["alerted_prematch"]]
    print(f"waves caught: {len(caught)}/{len(worked)}"
          f" ({len(caught) / len(worked):.1%} recall)" if worked else "no waves to catch")
    if alerted:
        print(f"pre-match alerts on completed series: {len(alerted)}, "
              f"of which on waves: {len(alerted) - len(false_alarms)} "
              f"({(len(alerted) - len(false_alarms)) / len(alerted):.1%} precision)")
    else:
        print("no pre-match alerts on completed series yet")

    with_scores = [r for r in scored if r["n_prematch_score_rows"]]
    if with_scores:
        print(f"\n{'-' * 86}")
        print(f"watch_score replay — max pre-match PRE_SPIKE score, {len(with_scores)} series with scores")
        wave_scores = [r["max_prematch_watch_score"] for r in with_scores if r["would_have_worked"]]
        flat_scores = [r["max_prematch_watch_score"] for r in with_scores if not r["would_have_worked"]]
        if wave_scores:
            print(f"median max score — waves: {statistics.median(wave_scores):.0f}"
                  + (f", non-waves: {statistics.median(flat_scores):.0f}" if flat_scores else ""))
        waves_scored = [r for r in with_scores if r["would_have_worked"]]
        print(f"threshold sweep ({len(waves_scored)} wave series have score history; "
              f"live alert threshold is {SCORE_ALERT_THRESHOLD:.0f}):")
        print(f"{'threshold':<10} {'fired':>6} {'hit':>5} {'precision':>10} {'recall':>8}")
        for thr in (30, 40, 50, 60, SCORE_ALERT_THRESHOLD):
            fired = [r for r in with_scores if r["max_prematch_watch_score"] >= thr]
            hits = [r for r in fired if r["would_have_worked"]]
            precision = f"{len(hits) / len(fired):>9.1%}" if fired else f"{'—':>9}"
            recall = f"{len(hits) / len(waves_scored):>7.1%}" if waves_scored else f"{'—':>7}"
            print(f">= {thr:<7.0f} {len(fired):>6} {len(hits):>5} {precision} {recall}")

    print(f"\nPer-series dataset written to {csv_path}")


def _quartiles(sorted_vals: list[float]) -> tuple[float, float]:
    n = len(sorted_vals)
    return sorted_vals[n // 4], sorted_vals[(3 * n) // 4]


if __name__ == "__main__":
    sys.exit(main())
