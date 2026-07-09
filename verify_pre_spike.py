"""
Synthetic end-to-end check of the PRE_SPIKE scoring engine.

Runs against a throwaway SQLite database (no network, nothing sent to
Discord) and verifies:
  1. A coiled low-odds market with rising pressure + Kalshi confirmation
     scores high, persists watch_score, and emits an urgent event.
  2. Breakdown components are complete and sum to the total.
  3. Cooldown blocks repeat alerts; standard -> urgent escalation is allowed.
  4. Out-of-band markets are scored but never alert.
  5. score_history records the breakdown (JSON round-trip) and prunes old rows.

Usage:  python verify_pre_spike.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

# Windows consoles may default to cp1252, which cannot print alert emoji.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Must be set before config.settings is imported.
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.invalid/webhook")
_tmpdir = tempfile.mkdtemp(prefix="pre_spike_check_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "check.db")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)-7s %(name)s | %(message)s")
for noisy in ("urllib3",):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from core import anomaly_engine, pre_spike_engine
from market_providers.models import AlertRecord, AnomalyType, MarketSnapshot
from storage import supabase_storage as db

try:
    from alerts import discord_alerts
except ModuleNotFoundError:
    discord_alerts = None

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def snap(market_id, match, player, prob, source, ts, spread=None, vol=None, liq=None,
         start_in_min=120.0):
    bid = prob - (spread / 2) if spread is not None else None
    ask = prob + (spread / 2) if spread is not None else None
    return MarketSnapshot(
        market_id=market_id, match_name=match, player_name=player,
        probability=prob, source=source, market_url="https://example.com/m",
        timestamp=ts, bid_probability=bid, ask_probability=ask,
        spread=spread, volume_total=vol, liquidity=liq,
        match_start_time=datetime.utcnow() + timedelta(minutes=start_in_min),
    )


def pre_spike_events(events):
    return [e for e in events if e.anomaly_type == AnomalyType.PRE_SPIKE_CANDIDATE]


def main() -> int:
    db.init_db()
    now = datetime.utcnow()

    # ── Kalshi twin moves up first (cross-exchange confirmation source) ───────
    print("\n--- seeding kalshi twin (Doe J. rising) ---")
    for prob, secs in [(0.03, 300), (0.04, 180), (0.06, 60)]:
        anomaly_engine.evaluate(snap(
            "k1", "Nadal vs Doe", "Doe J.", prob, "kalshi", now - timedelta(seconds=secs)
        ))

    # ── Polymarket market: dips to a new low, then coils and pressures up ─────
    print("\n--- polymarket cycles 1-2 (dip to new low) ---")
    anomaly_engine.evaluate(snap(
        "p1", "Nadal vs Doe", "Doe J.", 0.050, "polymarket",
        now - timedelta(seconds=3), spread=0.03, vol=1000, liq=5000,
    ))
    anomaly_engine.evaluate(snap(
        "p1", "Nadal vs Doe", "Doe J.", 0.035, "polymarket",
        now - timedelta(seconds=2), spread=0.03, vol=1010, liq=5000,
    ))

    print("\n--- polymarket cycle 3 (pressure building) ---")
    events = anomaly_engine.evaluate(snap(
        "p1", "Nadal vs Doe", "Doe J.", 0.045, "polymarket",
        now - timedelta(seconds=1), spread=0.02, vol=1060, liq=5400,
    ))
    ps = pre_spike_events(events)

    print("\n--- assertions: scoring + standard alert ---")
    check("standard PRE_SPIKE event emitted", len(ps) == 1 and not ps[0].urgent,
          f"events={[(e.anomaly_type.value, getattr(e, 'urgent', None)) for e in events]}")
    if ps:
        ev = ps[0]
        check("score is 73 (42 pressure + 25 band setup + 6 kalshi + 0 external)",
              ev.watch_score == 73.0, f"got {ev.watch_score}")
        check("breakdown sums to total",
              abs(sum(ev.score_breakdown.values()) - ev.watch_score) < 1e-6)
        expected_keys = {
            "pressure.spread_tightening", "pressure.volume_surge",
            "pressure.liquidity_rising", "pressure.velocity_positive",
            "pressure.acceleration_positive", "pressure.sustained_move",
            "pressure.update_freq_rising",
            "low_odds.in_band", "low_odds.coiled_turning_up",
            "low_odds.recent_new_low", "cross.kalshi_confirmation",
            "external.momentum",
        }
        check("all 12 components present", set(ev.score_breakdown) == expected_keys,
              f"got {sorted(ev.score_breakdown)}")
        check("in-band membership earns only 5 points",
              ev.score_breakdown["low_odds.in_band"] == 5.0,
              f"got {ev.score_breakdown['low_odds.in_band']}")
        check("external stub contributes 0", ev.score_breakdown["external.momentum"] == 0.0)
        check("kalshi confirmation scored 6",
              ev.score_breakdown["cross.kalshi_confirmation"] == 6.0)

        if discord_alerts is not None:
            print("\n--- sample Discord message (not sent) ---")
            print(discord_alerts._build_message(ev))
        else:
            print("\n--- sample Discord message skipped (requests not installed) ---")

    signals = db.get_signals("p1", "Doe J.", "polymarket")
    check("watch_score persisted to market_signals",
          signals is not None and signals.watch_score == 73.0,
          f"got {signals.watch_score if signals else None}")
    check("volume_acceleration filled in (50 - 10 = 40)",
          signals is not None and signals.volume_acceleration == 40.0,
          f"got {signals.volume_acceleration if signals else None}")

    with db._connect() as conn:
        hist = conn.execute(
            "SELECT * FROM score_history WHERE market_id = 'p1' AND total_score = 73.0"
        ).fetchall()
    check("score_history row recorded for the 73-score cycle", len(hist) == 1,
          f"got {len(hist)} rows")
    if hist and ps:
        check("history components JSON round-trips to the event breakdown",
              json.loads(hist[0]["components_json"]) == ps[0].score_breakdown)

    # ── cooldown: record the standard alert, next qualifying cycle is silent ──
    print("\n--- cooldown after standard alert ---")
    db.save_alert(AlertRecord("p1", "Doe J.", "polymarket",
                              AnomalyType.PRE_SPIKE_CANDIDATE.value, 0.035, 0.045))
    events = anomaly_engine.evaluate(snap(
        "p1", "Nadal vs Doe", "Doe J.", 0.054, "polymarket",
        now, spread=0.015, vol=1150, liq=5900,
    ))
    check("no repeat alert while standard cooldown active",
          len(pre_spike_events(events)) == 0)
    signals = db.get_signals("p1", "Doe J.", "polymarket")
    check("watch_score still updated during cooldown",
          signals is not None and signals.watch_score is not None,
          f"got {signals.watch_score if signals else None}")

    # ── cooldown matrix: standard blocks standard, allows urgent escalation ───
    print("\n--- cooldown matrix (standard -> urgent escalation) ---")
    with db._connect() as conn:
        conn.execute("DELETE FROM alerts_sent")
    db.save_alert(AlertRecord("p1", "Doe J.", "polymarket",
                              AnomalyType.PRE_SPIKE_CANDIDATE.value, 0.035, 0.045))
    probe = snap("p1", "Nadal vs Doe", "Doe J.", 0.05, "polymarket", now)
    check("standard blocked by recent standard alert",
          pre_spike_engine._on_cooldown(probe, urgent=False) is True)
    check("urgent allowed after standard (escalation)",
          pre_spike_engine._on_cooldown(probe, urgent=True) is False)
    db.save_alert(AlertRecord("p1", "Doe J.", "polymarket",
                              pre_spike_engine.URGENT_ALERT_TYPE, 0.035, 0.05))
    check("urgent blocked by recent urgent alert",
          pre_spike_engine._on_cooldown(probe, urgent=True) is True)

    # ── out-of-band market: scored, never alerts ──────────────────────────────
    print("\n--- out-of-band market (50%) ---")
    for prob, secs in [(0.48, 2), (0.50, 1)]:
        events = anomaly_engine.evaluate(snap(
            "p2", "Alcaraz vs X", "Alcaraz C.", prob, "polymarket",
            now - timedelta(seconds=secs), spread=0.01, vol=9000, liq=20000,
        ))
    check("no PRE_SPIKE alert outside the eligibility band",
          len(pre_spike_events(events)) == 0)
    signals = db.get_signals("p2", "Alcaraz C.", "polymarket")
    check("out-of-band market still gets a persisted score",
          signals is not None and signals.watch_score is not None,
          f"got {signals.watch_score if signals else None}")

    # ── score_history pruning ──────────────────────────────────────────────────
    print("\n--- score_history retention pruning ---")
    db.save_score_history("old1", "Stale P.", "polymarket", 0.05, 33.0,
                          {"low_odds.in_band": 15.0}, now - timedelta(days=30))
    db.prune_score_history(14)
    with db._connect() as conn:
        old = conn.execute(
            "SELECT COUNT(*) AS c FROM score_history WHERE market_id = 'old1'"
        ).fetchone()["c"]
        fresh = conn.execute("SELECT COUNT(*) AS c FROM score_history").fetchone()["c"]
    check("prune removed the 30-day-old row", old == 0, f"got {old}")
    check("prune kept fresh rows", fresh > 0, f"got {fresh}")

    print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
    print(f"check DB kept at: {os.environ['DB_PATH']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
