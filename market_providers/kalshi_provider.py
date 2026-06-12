"""
Kalshi provider — fetches tennis prediction markets via the Kalshi REST API.

Kalshi is a CFTC-regulated prediction market exchange (US).
Docs: https://trading-api.readme.io/reference/getmarkets

Authentication: RSA signing (not a simple token)
-------------------------------------------------
Each request must include three headers:
  KALSHI-ACCESS-KEY        — the key ID (UUID)
  KALSHI-ACCESS-TIMESTAMP  — current time in milliseconds (string)
  KALSHI-ACCESS-SIGNATURE  — base64( RSA-SHA256( timestamp + METHOD + path ) )

The private key is loaded once at init from the PEM file path in settings.

Market structure
----------------
Each Kalshi market is a binary YES/NO contract.
Prices are in cents (0–99 for yes_bid / yes_ask).

Fields collected
----------------
  yes_bid / yes_ask → bid_probability / ask_probability (÷100)
  spread            = ask_probability - bid_probability
  probability       = mid-price (bid+ask)/2 ÷ 100
  volume            → volume_total
  open_interest     → liquidity (closest semantic match)
  last_trade_time   → last_api_update
  trade_count_1h    → not in /markets endpoint → None
"""

import base64
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import settings
from core.market_classifier import extract_selection_name
from market_providers.base_provider import BaseProvider
from market_providers.models import MarketSnapshot

logger = logging.getLogger(__name__)

_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Known Kalshi series tickers for active tennis match markets.
# Match-level series (live/upcoming individual matches) come first.
# Tournament-winner futures are included for long-horizon signals.
_TENNIS_SERIES = [
    # Match markets
    "KXATPMATCH",           # ATP match winner
    "KXWTAMATCH",           # WTA match winner
    "KXCHALLENGERMATCH",    # ATP Challenger match
    "KXATPCHALLENGERMATCH", # ATP Challenger match (alt)
    "KXWTACHALLENGERMATCH", # WTA Challenger match
    "KXITFMATCH",           # ITF men's match
    "KXITFWMATCH",          # ITF women's match
    "KXATPEXACTMATCH",      # ATP exact match
    "KXWTAEXACTMATCH",      # WTA exact match
    # Set/game markets
    "KXATPSETWINNER",
    "KXWTASETWINNER",
    "KXATPS1GWINNER",
    "KXATPS2GWINNER",
    "KXATPS3GWINNER",
    "KXATPS4GWINNER",
    "KXATPS5GWINNER",
    # Tournament winner futures
    "KXWIMMEN",             # Wimbledon men's singles
    "KXWIMWOMEN",           # Wimbledon women's singles
    "KXAOMEN",              # Australian Open men's
    "KXAOWOMEN",            # Australian Open women's
    "KXFOMENSINGLES",       # French Open men's
    "KXFOWOMENSINGLES",     # French Open women's
    "KXUSOMENSINGLES",      # US Open men's
    "KXUSOWOMENSINGLES",    # US Open women's
]


class KalshiProvider(BaseProvider):

    # Kalshi's basic tier allows ~10 requests/second; GitHub Actions runners
    # are fast enough to exceed that, so every request is paced and 429s are
    # retried with backoff instead of dropping the series/event.
    _MIN_REQUEST_INTERVAL = 0.15
    _MAX_ATTEMPTS = 4

    def __init__(self) -> None:
        self._key_id = settings.KALSHI_KEY_ID
        self._private_key = self._load_private_key()
        self._last_request = 0.0

    @property
    def name(self) -> str:
        return "kalshi"

    def fetch_snapshots(self) -> list[MarketSnapshot]:
        if not self._key_id or self._private_key is None:
            logger.warning("[kalshi] Missing key ID or private key — provider disabled.")
            return []
        try:
            markets = self._fetch_tennis_markets()
            snapshots = self._parse_markets(markets)
            logger.info("[kalshi] Fetched %d snapshot(s).", len(snapshots))
            return snapshots
        except Exception as exc:
            logger.exception("[kalshi] Unexpected error: %s", exc)
            return []

    # ── auth ─────────────────────────────────────────────────────────────────

    def _load_private_key(self):
        path = Path(settings.KALSHI_PRIVATE_KEY_PATH)
        if not path.exists():
            logger.error("[kalshi] Private key file not found: %s", path)
            return None
        try:
            key_bytes = path.read_bytes()
            return serialization.load_pem_private_key(key_bytes, password=None)
        except Exception as exc:
            logger.error("[kalshi] Failed to load private key: %s", exc)
            return None

    def _signed_headers(self, method: str, path: str) -> dict:
        """
        Build the three Kalshi RSA auth headers for a given method + path.
        Signature covers: timestamp_ms (str) + METHOD + /path
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode()
        signature = self._private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
        return {
            "KALSHI-ACCESS-KEY":       self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    # ── internal ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict) -> dict:
        """Signed GET against the v2 API with pacing and 429 retry/backoff."""
        for attempt in range(self._MAX_ATTEMPTS):
            wait = self._MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            resp = requests.get(
                f"{_BASE}{path}",
                params=params,
                headers=self._signed_headers("GET", f"/trade-api/v2{path}"),
                timeout=15,
            )
            if resp.status_code == 429 and attempt < self._MAX_ATTEMPTS - 1:
                try:
                    delay = float(resp.headers.get("Retry-After", ""))
                except ValueError:
                    delay = float(2 ** attempt)
                logger.warning("[kalshi] 429 rate-limited, retrying in %.1fs", delay)
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        raise requests.RequestException("unreachable")  # loop always returns/raises

    def _fetch_tennis_markets(self) -> list[dict]:
        """
        Two-stage fetch:
          1. For each known tennis series, page through open events.
          2. For each event, page through its markets.
        Events don't embed markets in the list response, so the second
        call to /markets?event_ticker=X is required.
        """
        tennis: list[dict] = []

        for series_ticker in _TENNIS_SERIES:
            # Stage 1: collect event tickers
            event_tickers: list[str] = []
            cursor: str | None = None
            while True:
                params: dict = {"limit": 100, "series_ticker": series_ticker, "status": "open"}
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self._get("/events", params)
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 401:
                        logger.error("[kalshi] Authentication failed (401). Check key ID and private key.")
                        return tennis
                    logger.error("[kalshi] HTTP error fetching events for %s: %s", series_ticker, exc)
                    break
                except requests.RequestException as exc:
                    logger.error("[kalshi] HTTP error fetching events for %s: %s", series_ticker, exc)
                    break

                events = data.get("events", [])
                event_tickers.extend(e["event_ticker"] for e in events if "event_ticker" in e)
                cursor = data.get("cursor")
                if not cursor or not events:
                    break

            # Stage 2: fetch markets for each event
            for event_ticker in event_tickers:
                ecursor: str | None = None
                while True:
                    eparams: dict = {"limit": 100, "event_ticker": event_ticker, "status": "open"}
                    if ecursor:
                        eparams["cursor"] = ecursor
                    try:
                        edata = self._get("/markets", eparams)
                    except requests.RequestException as exc:
                        logger.error("[kalshi] HTTP error fetching markets for event %s: %s", event_ticker, exc)
                        break

                    markets = edata.get("markets", [])
                    tennis.extend(markets)
                    ecursor = edata.get("cursor")
                    if not ecursor or not markets:
                        break

        logger.debug("[kalshi] Found %d tennis market(s).", len(tennis))
        return tennis

    def _parse_markets(self, markets: list[dict]) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        now = datetime.utcnow()

        for market in markets:
            ticker: str = market.get("ticker", "")
            title: str  = market.get("title", "Unknown match")
            market_url  = f"https://kalshi.com/markets/{ticker}" if ticker else "https://kalshi.com"

            # ── bid / ask (new API uses _dollars fields, already in [0.0, 1.0]) ─
            bid_prob = _to_float(market.get("yes_bid_dollars"))
            ask_prob = _to_float(market.get("yes_ask_dollars"))

            if bid_prob is not None:
                bid_prob = max(0.0, min(1.0, bid_prob))
            if ask_prob is not None:
                ask_prob = max(0.0, min(1.0, ask_prob))

            # ── mid-price → primary probability ───────────────────────────────
            if bid_prob is not None and ask_prob is not None:
                probability = (bid_prob + ask_prob) / 2
            elif ask_prob is not None:
                probability = ask_prob
            elif bid_prob is not None:
                probability = bid_prob
            else:
                last = market.get("last_price_dollars")
                if last is None:
                    continue
                probability = max(0.0, min(1.0, float(last)))

            # ── spread ────────────────────────────────────────────────────────
            spread: float | None = None
            if bid_prob is not None and ask_prob is not None:
                spread = round(ask_prob - bid_prob, 6)

            # ── volume / liquidity (new field names) ──────────────────────────
            volume_total: float | None = _to_float(market.get("volume_fp"))
            liquidity:    float | None = _to_float(market.get("liquidity_dollars"))

            # ── last price update timestamp ───────────────────────────────────
            last_api_update = _parse_ts(
                market.get("updated_time") or market.get("close_time")
            )

            player_name = _extract_player_name(title)

            snapshots.append(
                MarketSnapshot(
                    market_id=ticker,
                    match_name=title,
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
                )
            )

        return snapshots


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_float(value) -> float | None:
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


def _extract_player_name(title: str) -> str:
    selection = extract_selection_name(title)
    if selection:
        return selection

    t = title.strip()
    if t.lower().startswith("will "):
        rest = t[5:]
        for sep in (" win ", " to win "):
            idx = rest.lower().find(sep)
            if idx != -1:
                return rest[:idx].strip()
        words = rest.split()
        return " ".join(words[:2]) if len(words) >= 2 else rest
    for sep in (" to win ", " win ", " - "):
        idx = t.lower().find(sep)
        if idx != -1:
            return t[:idx].strip()
    return t[:40]
