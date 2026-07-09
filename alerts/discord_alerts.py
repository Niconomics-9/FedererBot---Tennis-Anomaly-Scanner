"""
Discord webhook alerting.

One send_alert() entry point handles all four anomaly types.
Each type gets a distinct emoji prefix so alerts are scannable at a glance.
"""

import logging
from datetime import datetime, timedelta, timezone

import requests

from config import settings
from core import market_classifier
from market_providers.models import AlertRecord, AnomalyEvent, AnomalyType
from storage import supabase_storage as db

logger = logging.getLogger(__name__)

_TYPE_EMOJI = {
    AnomalyType.LOW_ODDS_SPIKE:      "📈",
    AnomalyType.FAVORITE_RECOVERY:   "♻️",
    AnomalyType.FAST_MOVE:           "⚡",
    AnomalyType.NEW_LOW_WATCH:       "👁️",
    AnomalyType.PRE_SPIKE_CANDIDATE: "🔮",
}

_TYPE_LABEL = {
    AnomalyType.LOW_ODDS_SPIKE:      "LOW ODDS SPIKE",
    AnomalyType.FAVORITE_RECOVERY:   "FAVORITE RECOVERY",
    AnomalyType.FAST_MOVE:           "FAST MOVE",
    AnomalyType.NEW_LOW_WATCH:       "NEW LOW — WATCH",
    AnomalyType.PRE_SPIKE_CANDIDATE: "PRE-SPIKE CANDIDATE",
}


def send_alert(event: AnomalyEvent) -> bool:
    """
    Post a formatted Discord message for the given anomaly event.
    Records the alert in the database on success (enables cooldown logic).
    Returns True if the webhook call succeeded.
    """
    message = _build_message(event)

    try:
        resp = requests.post(
            settings.DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.HTTPError as exc:
        logger.error("[discord] HTTP error: %s", exc)
        return False
    except requests.RequestException as exc:
        logger.error("[discord] Network error: %s", exc)
        return False

    _record_alert(event)
    logger.info(
        "[discord] Alert sent — %s | %s / %s",
        event.anomaly_type.value,
        event.snapshot.player_name,
        event.snapshot.match_name,
    )
    return True


# ── message builder ───────────────────────────────────────────────────────────

def _build_message(event: AnomalyEvent) -> str:
    snap = event.snapshot
    classification = market_classifier.classify_snapshot(snap)
    atype = event.anomaly_type
    emoji = _TYPE_EMOJI[atype]
    label = _TYPE_LABEL[atype]
    if event.urgent:
        emoji = "🚨"
        label += " — URGENT"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Probability fields
    curr_pct = f"{event.current_probability * 100:.1f}%"
    prev_pct = (
        f"{event.prev_probability * 100:.1f}%"
        if event.prev_probability is not None
        else "N/A"
    )

    # Move field
    if event.move is not None:
        direction = "▲" if event.move > 0 else "▼"
        move_str = f"{direction} {abs(event.move) * 100:.1f} pp"
    else:
        move_str = "N/A"

    # Time window (FAST_MOVE only)
    if event.time_window_minutes is not None:
        window_str = f"{event.time_window_minutes} minutes"
    else:
        window_str = "N/A"

    # Match start — pre-match lead time is the actionable runway. Shown as a
    # headline above the code block (Eastern time + a Discord <t:..:R> tag
    # that renders a live countdown) and as precise fields inside it.
    if snap.match_start_time is not None:
        eastern_dt, tz_label = _to_eastern(snap.match_start_time)
        eastern_str = _fmt_eastern(eastern_dt, tz_label)
        lead_min = (snap.match_start_time - datetime.utcnow()).total_seconds() / 60
        lead_str = _fmt_lead(lead_min)
        unix_ts = int(snap.match_start_time.replace(tzinfo=timezone.utc).timestamp())
        start_headline = f"⏰ **{eastern_str}** — starts <t:{unix_ts}:R>\n"
        start_block = (
            f"Match Start (ET):     {eastern_str}\n"
            f"Match Start (UTC):    {snap.match_start_time.strftime('%Y-%m-%d %H:%M')}\n"
            f"Starts In:            {lead_str}\n"
        )
    else:
        start_headline = ""
        start_block = "Match Start:          unknown\n"

    # Rolling stats context block
    stats = event.stats
    stats_block = (
        f"Opening Prob:  {stats.opening_probability * 100:.1f}%\n"
        f"Lowest Prob:   {stats.lowest_probability * 100:.1f}%\n"
        f"Highest Prob:  {stats.highest_probability * 100:.1f}%\n"
    )

    # PRE_SPIKE score block — total plus per-component breakdown
    score_block = ""
    if event.watch_score is not None:
        score_block = f"Watch Score:          {event.watch_score:.1f} / 100\n"
        if event.score_breakdown:
            score_block += "Score Components:\n"
            for name, pts in event.score_breakdown.items():
                score_block += f"  {name:<32} {pts:g}\n"
        score_block += "---\n"

    return (
        f"{emoji} **TENNIS ANOMALY — {label}**\n"
        f"{start_headline}"
        "```\n"
        f"Alert Type:           {label}\n"
        f"Match:                {snap.match_name}\n"
        f"Player:               {classification.selection or snap.player_name}\n"
        f"Market Type:          {classification.market_type}\n"
        f"Source:               {snap.source}\n"
        f"Current Probability:  {curr_pct}\n"
        f"Previous Probability: {prev_pct}\n"
        f"Move:                 {move_str}\n"
        f"Time Window:          {window_str}\n"
        f"{start_block}"
        f"Market URL:           {snap.market_url}\n"
        "---\n"
        f"{score_block}"
        f"{stats_block}"
        f"Timestamp:            {timestamp}\n"
        "```"
    )


# ── US Eastern conversion ─────────────────────────────────────────────────────
# Windows Python ships no IANA tzdata, so zoneinfo("America/New_York") fails.
# US DST has been fixed since 2007 (2nd Sunday of March 02:00 local = 07:00 UTC
# through 1st Sunday of November 02:00 local = 06:00 UTC), so the conversion is
# deterministic without a dependency.

def _nth_sunday(year: int, month: int, n: int) -> datetime:
    first = datetime(year, month, 1)
    first_sunday = 1 + (6 - first.weekday()) % 7
    return datetime(year, month, first_sunday + 7 * (n - 1))


def _to_eastern(dt_utc: datetime) -> tuple[datetime, str]:
    """Naive-UTC datetime → (US Eastern datetime, 'EST'|'EDT')."""
    dst_start = _nth_sunday(dt_utc.year, 3, 2) + timedelta(hours=7)
    dst_end = _nth_sunday(dt_utc.year, 11, 1) + timedelta(hours=6)
    if dst_start <= dt_utc < dst_end:
        return dt_utc - timedelta(hours=4), "EDT"
    return dt_utc - timedelta(hours=5), "EST"


def _fmt_eastern(dt: datetime, tz_label: str) -> str:
    """'Thu Jun 12, 7:00 PM EDT' — 12-hour clock without strftime's zero pad."""
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{dt.strftime('%a %b')} {dt.day}, {hour12}:{dt.minute:02d} {ampm} {tz_label}"


def _fmt_lead(minutes: float) -> str:
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{int(minutes // 60)}h {int(minutes % 60):02d}m"


# ── record keeping ────────────────────────────────────────────────────────────

def _record_alert(event: AnomalyEvent) -> None:
    # Urgent pre-spike alerts are recorded under a distinct type string so the
    # cooldown can allow one standard -> urgent escalation per window.
    anomaly_type = event.anomaly_type.value
    if event.urgent:
        anomaly_type += "_URGENT"

    classification = market_classifier.classify_snapshot(event.snapshot)
    record = AlertRecord(
        market_id=event.snapshot.market_id,
        player_name=event.snapshot.player_name,
        source=event.snapshot.source,
        anomaly_type=anomaly_type,
        prev_prob=event.prev_probability if event.prev_probability is not None else 0.0,
        curr_prob=event.current_probability,
        sent_at=datetime.utcnow(),
        match_key=classification.match_key,
    )
    db.save_alert(record)
