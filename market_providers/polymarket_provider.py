"""
Polymarket provider — fetches tennis prediction markets via the Polymarket
Gamma API.

No API key required.

Docs: https://docs.polymarket.com/api-reference/introduction

API used: https://gamma-api.polymarket.com/markets
  - Returns active binary markets with bid, ask, volume, liquidity directly
  - Paginated via offset parameter

Market structure
----------------
Binary YES/NO markets. Relevant fields per market:
  conditionId     — unique market identifier
  question        — human-readable question text
  slug            — URL slug
  outcomePrices   — JSON string ["yes_price", "no_price"] in [0,1]
  bestBid         — best bid on YES token [0,1]
  bestAsk         — best ask on YES token [0,1]
  spread          — ask - bid (pre-computed by Polymarket)
  lastTradePrice  — last execution price [0,1]
  volume          — total lifetime volume USD
  liquidityNum    — order book depth USD
  updatedAt       — ISO timestamp of last update
  volume24hr      — 24-hour volume (useful for activity signal)

Tennis market identification
----------------------------
We paginate all active+open markets and filter locally on question/slug text.
The Gamma API keyword search does not filter server-side reliably.
"""

import logging
from datetime import datetime, timezone

import requests

from core.market_classifier import extract_selection_name
from market_providers.base_provider import BaseProvider
from market_providers.models import MarketSnapshot

logger = logging.getLogger(__name__)

_BASE = "https://gamma-api.polymarket.com"

_EVENTS_PAGE_SIZE = 100


class PolymarketProvider(BaseProvider):

    @property
    def name(self) -> str:
        return "polymarket"

    def fetch_snapshots(self) -> list[MarketSnapshot]:
        try:
            markets = self._fetch_tennis_markets()
            snapshots = self._parse_markets(markets)
            logger.info("[polymarket] Fetched %d snapshot(s).", len(snapshots))
            return snapshots
        except Exception as exc:
            logger.exception("[polymarket] Unexpected error: %s", exc)
            return []

    # ── internal ─────────────────────────────────────────────────────────────

    def _fetch_tennis_markets(self) -> list[dict]:
        """
        Fetch tennis events via /events?tag_slug=tennis&active=true&closed=false.
        Each event contains a nested 'markets' list with the binary markets.
        Offset-paginate until exhausted.
        """
        tennis: list[dict] = []
        offset = 0

        while True:
            try:
                resp = requests.get(
                    f"{_BASE}/events",
                    params={
                        "limit":    _EVENTS_PAGE_SIZE,
                        "offset":   offset,
                        "active":   "true",
                        "closed":   "false",
                        "tag_slug": "tennis",
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                events: list[dict] = resp.json()
            except requests.RequestException as exc:
                logger.error("[polymarket] HTTP error: %s", exc)
                break

            if not events:
                break

            for event in events:
                for market in event.get("markets") or []:
                    # carry event-level slug for URL construction if market lacks one
                    if not market.get("slug"):
                        market["slug"] = event.get("slug", "")
                    tennis.append(market)

            offset += len(events)
            if len(events) < _EVENTS_PAGE_SIZE:
                break

        logger.debug("[polymarket] Found %d tennis market(s) from events.", len(tennis))
        return tennis

    def _parse_markets(self, markets: list[dict]) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        now = datetime.utcnow()

        for market in markets:
            market_id: str = market.get("conditionId") or market.get("id", "")
            question:  str = market.get("question", "Unknown match")
            slug:      str = market.get("slug", "")
            market_url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

            # ── probability from outcomePrices[0] (YES outcome) ───────────────
            outcome_prices = market.get("outcomePrices")
            try:
                import json as _json
                prices = _json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                probability = float(prices[0])
            except (TypeError, ValueError, IndexError, KeyError):
                # Fall back to lastTradePrice
                ltp = market.get("lastTradePrice")
                if ltp is None:
                    continue
                try:
                    probability = float(ltp)
                except (ValueError, TypeError):
                    continue

            probability = max(0.0, min(1.0, probability))

            # Skip resolved markets (settled at 0 or 1)
            if probability < 0.005 or probability > 0.995:
                continue

            # ── microstructure ────────────────────────────────────────────────
            bid_prob  = _parse_float(market.get("bestBid"))
            ask_prob  = _parse_float(market.get("bestAsk"))
            spread    = _parse_float(market.get("spread"))

            if bid_prob is not None:
                bid_prob = max(0.0, min(1.0, bid_prob))
            if ask_prob is not None:
                ask_prob = max(0.0, min(1.0, ask_prob))

            # Compute spread ourselves if the API value is missing
            if spread is None and bid_prob is not None and ask_prob is not None:
                spread = round(ask_prob - bid_prob, 6)

            volume_total = _parse_float(market.get("volume"))
            liquidity    = _parse_float(market.get("liquidityNum") or market.get("liquidity"))
            last_api_update = _parse_ts(market.get("updatedAt") or market.get("startDate"))

            # Scheduled match start. Present on every match market (verified
            # live: 2,782/2,782); absent on tournament futures, which the
            # classifier rejects anyway. Event startDate is the market's
            # creation date, NOT the match start — never fall back to it.
            match_start_time = _parse_ts(market.get("gameStartTime"))

            player_name = _extract_player_name(question)

            snapshots.append(
                MarketSnapshot(
                    market_id=market_id,
                    match_name=question,
                    player_name=player_name,
                    probability=probability,
                    source=self.name,
                    market_url=market_url,
                    timestamp=now,
                    bid_probability=bid_prob,
                    ask_probability=ask_prob,
                    spread=spread,
                    volume_total=volume_total,
                    liquidity=liquidity,
                    last_api_update=last_api_update,
                    trade_count_1h=None,
                    match_start_time=match_start_time,
                )
            )

        return snapshots


# ── helpers ───────────────────────────────────────────────────────────────────

def _parse_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_ts(value) -> datetime | None:
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        pass
    try:
        return datetime.utcfromtimestamp(float(value))
    except (ValueError, TypeError):
        return None


def _extract_player_name(question: str) -> str:
    """
    Best-effort extraction of a player name from a Polymarket question string.

    Examples:
      "Will Djokovic win Wimbledon 2024?"     -> "Djokovic"
      "Carlos Alcaraz to win Roland Garros?"  -> "Carlos Alcaraz"
      "Iga Swiatek - French Open Winner"      -> "Iga Swiatek"
    """
    selection = extract_selection_name(question)
    if selection:
        return selection

    q = question.strip()

    if q.lower().startswith("will "):
        rest = q[5:]
        for sep in (" win ", " to win ", " beat ", " defeat "):
            idx = rest.lower().find(sep)
            if idx != -1:
                return rest[:idx].strip()
        words = rest.split()
        return " ".join(words[:2]) if len(words) >= 2 else rest

    for sep in (" to win ", " win ", " - "):
        idx = q.lower().find(sep)
        if idx != -1:
            return q[:idx].strip()

    return q[:40]
