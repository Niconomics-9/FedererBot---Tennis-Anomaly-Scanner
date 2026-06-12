"""
Synthetic verification for alert-quality gates.

Runs against a throwaway SQLite database. It does not import the Discord
sender and cannot send webhooks.

Usage:
    python verify_alert_quality.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://example.invalid/webhook")
tmpdir = tempfile.mkdtemp(prefix="alert_quality_check_")
os.environ["DB_PATH"] = os.path.join(tmpdir, "check.db")
os.environ["ALERT_MATCH_WINNER_ONLY"] = "true"
os.environ["ALERT_MIN_HISTORY_SNAPSHOTS"] = "3"
os.environ["ALERT_MIN_VOLUME"] = "100"
os.environ["ALERT_MIN_LIQUIDITY"] = "100"
os.environ["ALERT_MAX_SPREAD"] = "0.08"
os.environ["ALERT_MATCH_COOLDOWN_MINUTES"] = "60"
os.environ["ALERT_PRE_MATCH_ONLY"] = "true"
os.environ["ALERT_PRE_MATCH_MIN_LEAD_MINUTES"] = "10"
os.environ["ALERT_REQUIRE_START_TIME"] = "true"

from core import anomaly_engine, market_classifier
from market_providers.models import AlertRecord, AnomalyType, MarketSnapshot
from storage import sqlite_storage as db

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


def snap(
    market_id: str,
    match: str,
    player: str,
    prob: float,
    ts: datetime,
    *,
    spread: float = 0.02,
    vol: float = 1000.0,
    liq: float = 5000.0,
    start_in_min: float | None = 120.0,
) -> MarketSnapshot:
    return MarketSnapshot(
        market_id=market_id,
        match_name=match,
        player_name=player,
        probability=prob,
        source="polymarket",
        market_url="https://example.com/market",
        timestamp=ts,
        bid_probability=max(0.0, prob - spread / 2),
        ask_probability=min(1.0, prob + spread / 2),
        spread=spread,
        volume_total=vol,
        liquidity=liq,
        match_start_time=(
            datetime.utcnow() + timedelta(minutes=start_in_min)
            if start_in_min is not None
            else None
        ),
    )


def events_for(sequence: list[MarketSnapshot]) -> list:
    events = []
    for snapshot in sequence:
        events = anomaly_engine.evaluate(snapshot)
    return events


def event_types(events) -> list[str]:
    return [event.anomaly_type.value for event in events]


def main() -> int:
    db.init_db()
    now = datetime.utcnow()

    print("\n--- classifier ---")
    match = market_classifier.classify_market("Ilkley: Rafael Nadal vs Novak Djokovic", "Nadal")
    will_beat = market_classifier.classify_market("Will Rafael Nadal beat Novak Djokovic?", "Nadal")
    total = market_classifier.classify_market("Nadal vs Djokovic: Match O/U 21.5", "Over")
    handicap = market_classifier.classify_market("Set Handicap: Nadal (-1.5) vs Djokovic (+1.5)", "Nadal")
    future = market_classifier.classify_market("Will Nadal win Wimbledon 2026?", "Nadal")
    completed = market_classifier.classify_market(
        "ITF Tokyo: Completed Match: Hikaru Sato vs Kurea Hayasaka",
        "Hikaru Sato",
    )
    check("match winner is actionable", match.is_actionable and match.market_type == "match_winner")
    check("will-beat match winner is actionable",
          will_beat.is_actionable and will_beat.market_type == "match_winner")
    check("total is rejected", not total.is_actionable and total.market_type == "total")
    check("handicap is rejected", not handicap.is_actionable and handicap.market_type == "handicap")
    check("future is rejected", not future.is_actionable and future.market_type == "tournament_future")
    check("completed match is rejected", not completed.is_actionable and completed.market_type == "completed")

    print("\n--- clean match-winner low-odds spike ---")
    events = events_for([
        snap("p1", "Nadal vs Doe", "Doe", 0.050, now - timedelta(minutes=4), vol=1000),
        snap("p1", "Nadal vs Doe", "Doe", 0.055, now - timedelta(minutes=2), vol=1025),
        snap("p1", "Nadal vs Doe", "Doe", 0.120, now, vol=1100),
    ])
    check("low-odds spike alerts on clean match winner",
          event_types(events) == [AnomalyType.LOW_ODDS_SPIKE.value],
          f"got {event_types(events)}")

    print("\n--- noisy market suppression ---")
    events = events_for([
        snap("p_total", "Nadal vs Doe: Match O/U 21.5", "Over 21.5", 0.050, now, vol=1000),
        snap("p_total", "Nadal vs Doe: Match O/U 21.5", "Over 21.5", 0.055, now, vol=1025),
        snap("p_total", "Nadal vs Doe: Match O/U 21.5", "Over 21.5", 0.120, now, vol=1100),
    ])
    check("totals do not alert", events == [], f"got {event_types(events)}")

    events = events_for([
        snap("p_done", "Completed Match: Nadal vs Doe", "Doe", 0.050, now, vol=1000),
        snap("p_done", "Completed Match: Nadal vs Doe", "Doe", 0.055, now, vol=1025),
        snap("p_done", "Completed Match: Nadal vs Doe", "Doe", 0.120, now, vol=1100),
    ])
    check("completed matches do not alert", events == [], f"got {event_types(events)}")

    print("\n--- resolved/history gates ---")
    events = events_for([
        snap("p_low", "Nadal vs Doe", "Doe", 0.005, now, vol=1000),
        snap("p_low", "Nadal vs Doe", "Doe", 0.005, now, vol=1025),
        snap("p_low", "Nadal vs Doe", "Doe", 0.005, now, vol=1100),
    ])
    check("resolved-looking 0.5% market does not alert", events == [], f"got {event_types(events)}")

    first = anomaly_engine.evaluate(
        snap("p_hist", "Nadal vs Roe", "Roe", 0.050, now, vol=1000)
    )
    second = anomaly_engine.evaluate(
        snap("p_hist", "Nadal vs Roe", "Roe", 0.049, now, vol=1025)
    )
    third = anomaly_engine.evaluate(
        snap("p_hist", "Nadal vs Roe", "Roe", 0.048, now, vol=1100)
    )
    check("history gate blocks first low reading", first == [], f"got {event_types(first)}")
    check("history gate blocks second low reading", second == [], f"got {event_types(second)}")
    check("new-low can alert once history exists",
          event_types(third) == [AnomalyType.NEW_LOW_WATCH.value],
          f"got {event_types(third)}")

    print("\n--- pre-match gate ---")
    events = events_for([
        snap("p_live", "Garcia vs Lopez", "Lopez", 0.050, now - timedelta(minutes=4),
             vol=1000, start_in_min=-30),
        snap("p_live", "Garcia vs Lopez", "Lopez", 0.055, now - timedelta(minutes=2),
             vol=1025, start_in_min=-30),
        snap("p_live", "Garcia vs Lopez", "Lopez", 0.120, now, vol=1100, start_in_min=-30),
    ])
    check("started match does not alert", events == [], f"got {event_types(events)}")

    events = events_for([
        snap("p_soon", "Kim vs Park", "Park", 0.050, now - timedelta(minutes=4),
             vol=1000, start_in_min=5),
        snap("p_soon", "Kim vs Park", "Park", 0.055, now - timedelta(minutes=2),
             vol=1025, start_in_min=5),
        snap("p_soon", "Kim vs Park", "Park", 0.120, now, vol=1100, start_in_min=5),
    ])
    check("match starting inside the lead window does not alert",
          events == [], f"got {event_types(events)}")

    events = events_for([
        snap("p_nost", "Silva vs Costa", "Costa", 0.050, now - timedelta(minutes=4),
             vol=1000, start_in_min=None),
        snap("p_nost", "Silva vs Costa", "Costa", 0.055, now - timedelta(minutes=2),
             vol=1025, start_in_min=None),
        snap("p_nost", "Silva vs Costa", "Costa", 0.120, now, vol=1100, start_in_min=None),
    ])
    check("missing start time does not alert", events == [], f"got {event_types(events)}")

    print("\n--- match-level cooldown ---")
    classification = market_classifier.classify_market("Nadal vs Doe", "Doe")
    db.save_alert(AlertRecord(
        "p1",
        "Doe",
        "polymarket",
        AnomalyType.LOW_ODDS_SPIKE.value,
        0.055,
        0.120,
        match_key=classification.match_key,
    ))
    events = events_for([
        snap("p1_alt", "Doe vs Nadal", "Doe", 0.050, now, vol=1000),
        snap("p1_alt", "Doe vs Nadal", "Doe", 0.055, now, vol=1025),
        snap("p1_alt", "Doe vs Nadal", "Doe", 0.120, now, vol=1100),
    ])
    check("same match cooldown blocks related market", events == [], f"got {event_types(events)}")

    print(f"\n{'=' * 40}\n{PASS} passed, {FAIL} failed")
    print(f"check DB kept at: {os.environ['DB_PATH']}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
