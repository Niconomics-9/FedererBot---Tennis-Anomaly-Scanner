# Tennis Prediction Market Anomaly Scanner

A local Python bot that monitors tennis prediction markets (Polymarket, Kalshi),
stores a full microstructure history in SQLite, computes market signals every
cycle, and fires Discord alerts when unusual probability movements are detected.

**Alerting and data collection only. No bets. No trades. No wallet integration.**

---

## Current Status

| Layer | Status |
|---|---|
| Market providers (Polymarket, Kalshi) | ✅ Live — microstructure fields collected |
| Raw snapshot storage | ✅ All readings persisted with bid/ask/spread/volume/liquidity |
| Rolling stats (open/low/high/current prob) | ✅ Maintained per market × player × source |
| Basic signal computation | ✅ velocity_1c, velocity_5c, acceleration, spread, distance_from_low, time_since_low, update_freq_1h |
| Alert-quality gates | ✅ Match-winner focus, prop/future/completed-market suppression, spread/liquidity/history checks |
| Anomaly alerts (4 rules) | ✅ LOW_ODDS_SPIKE, FAVORITE_RECOVERY, FAST_MOVE, NEW_LOW_WATCH after quality gates |
| PRE_SPIKE scoring engine | ✅ Conservative 0-100 watch_score with no-trade Discord alerts |

---

## Architecture

```
main.py                          Entry point — polling loop + provider setup
│
├── market_providers/
│   ├── base_provider.py         Abstract interface all providers implement
│   ├── polymarket_provider.py   Polymarket CLOB API — no key required
│   ├── kalshi_provider.py       Kalshi REST API — requires KALSHI_API_KEY
│   └── models.py                MarketSnapshot, MarketStats, MarketSignals,
│                                AnomalyEvent, AlertRecord, AnomalyType
│
├── core/
│   ├── scanner.py               Orchestrates one full poll cycle
│   ├── anomaly_engine.py        Runs 4 detection rules per snapshot
│   ├── market_classifier.py     Classifies match winners vs props/futures/completed markets
│   ├── alert_quality.py         Shared Discord eligibility gates
│   ├── market_tracker.py        Updates rolling open/low/high/current stats
│   ├── signal_engine.py         Computes 6 basic signals, writes market_signals
│   └── pre_spike_engine.py      Scores low-odds candidates before a spike
│
├── storage/
│   └── sqlite_storage.py        All SQLite reads, writes, and safe migrations
│
├── alerts/
│   └── discord_alerts.py        Formats and sends Discord webhook messages
│
└── config/
    └── settings.py              Loads all environment variables via python-dotenv
```

---

## Data Flow

```
1. main.py starts the loop
2. scanner.run_cycle(providers)
     └─ for each provider:
          provider.fetch_snapshots()
            → HTTP call to Polymarket / Kalshi
            → normalise to list[MarketSnapshot] (with microstructure fields)
          for each snapshot:
            anomaly_engine.evaluate(snapshot)
              ├─ db.save_snapshot()            persist full snapshot incl. microstructure
              ├─ market_tracker.update_stats() update open/low/high/current
              ├─ signal_engine.compute_and_save()
              │    ├─ velocity_1c / velocity_5c / acceleration (from snapshot history)
              │    ├─ spread_current / spread_trend
              │    ├─ distance_from_low / time_since_low_min
              │    ├─ update_freq_1h (price changes in last 60 min)
              │    └─ upsert_signals() → market_signals table
              └─ run rules 1–4
                   └─ alert_quality gates:
                        match-winner only by default,
                        no completed/totals/handicaps/futures,
                        probability/spread/liquidity/volume/history checks,
                        match-level cooldown
                   if triggered and not in cooldown:
                     discord_alerts.send_alert()
                     db.save_alert()
3. Sleep POLL_SECONDS, repeat
```

---

## What Data Is Now Being Collected

### Per snapshot (market_snapshots table)

| Field | Source | Notes |
|---|---|---|
| probability | Both | Mid-price, always present |
| bid_probability | Polymarket token best_bid / Kalshi yes_bid÷100 | None if unavailable |
| ask_probability | Polymarket token best_ask / Kalshi yes_ask÷100 | None if unavailable |
| spread | ask − bid | None if either side missing |
| volume_total | Polymarket market.volume / Kalshi market.volume | Total lifetime volume |
| liquidity | Polymarket market.liquidity / Kalshi open_interest | Order book depth proxy |
| last_api_update | Polymarket last_trade_ts / Kalshi last_trade_time | When provider last changed price |
| trade_count_1h | Not yet available in /markets endpoints | Always None for now |

### Per market, continuously maintained (market_stats table)

opening_probability, current_probability, lowest_probability, highest_probability, new_low_alerted

### Per market, computed each cycle (market_signals table)

| Signal | Description |
|---|---|
| velocity_1c | Probability change since the prior cycle |
| velocity_5c | Probability change over the last 5 cycles |
| acceleration | Change in velocity_1c (is the move speeding up?) |
| spread_current | Current bid-ask spread in probability space |
| spread_trend | Spread delta vs prior cycle (tightening = negative) |
| distance_from_low | current_probability − lowest_probability |
| time_since_low_min | Minutes elapsed since the recorded low was reached |
| update_freq_1h | Number of distinct price changes detected in the last hour |
| volume_acceleration | Deferred — stored as None until PRE_SPIKE engine |
| watch_score | Reserved — stored as None until PRE_SPIKE engine |

---

## Detection Rules (Anomaly Alerts)

| Rule | Trigger |
|---|---|
| LOW_ODDS_SPIKE | prev prob ≤ 10% AND increase ≥ 3 pp |
| FAVORITE_RECOVERY | opening ≥ 60%, fell to ≤ 25%, recovered ≥ 7 pp from low |
| FAST_MOVE | increase ≥ 5 pp within 10 minutes |
| NEW_LOW_WATCH | first time a player drops below 8% (fires once per player per market) |

All thresholds configurable via `.env`.

### Alert Quality Gates

Discord alerts are now intentionally conservative. Raw snapshots and scores are
still collected for every market, but alerts pass through these gates first:

| Gate | Default |
|---|---|
| Market type | Match winners only (`ALERT_MATCH_WINNER_ONLY=true`) |
| Suppressed markets | Completed matches, totals, handicaps, set winners, tournament futures, malformed markets |
| Opportunity probability band | 2% to 35% for standard opportunity alerts |
| FAST_MOVE band | Previous <= 25%, current <= 35% |
| Spread | `ALERT_MAX_SPREAD=0.08` when spread is available |
| Liquidity / volume | `ALERT_MIN_LIQUIDITY=100`, `ALERT_MIN_VOLUME=100` when available |
| History | At least `ALERT_MIN_HISTORY_SNAPSHOTS=3` readings |
| Match cooldown | `ALERT_MATCH_COOLDOWN_MINUTES=60` across related markets |

---

## Discord Alert Format

```
📈 TENNIS ANOMALY — LOW ODDS SPIKE
┌───────────────────────────────────────────────────┐
│ Alert Type:           LOW ODDS SPIKE               │
│ Match:                Nadal vs Doe J.              │
│ Player:               Doe J.                       │
│ Source:               polymarket                   │
│ Current Probability:  12.0%                        │
│ Previous Probability: 5.0%                         │
│ Move:                 ▲ 7.0 pp                     │
│ Time Window:          N/A                          │
│ Market URL:           https://polymarket.com/...   │
│ ---                                                │
│ Opening Prob:         5.0%                         │
│ Lowest Prob:          5.0%                         │
│ Highest Prob:         12.0%                        │
│ Timestamp:            2024-06-09 14:32 UTC         │
└───────────────────────────────────────────────────┘
```

---

## Database Schema

```sql
market_snapshots
  id, market_id, match_name, player_name, source, probability, market_url,
  timestamp,
  -- microstructure (nullable):
  bid_probability, ask_probability, spread,
  volume_total, liquidity, last_api_update, trade_count_1h

market_stats
  market_id, player_name, source (PK),
  opening_probability, current_probability,
  lowest_probability, highest_probability,
  new_low_alerted, last_updated

market_signals
  market_id, player_name, source (PK),
  current_probability,
  velocity_1c, velocity_5c, acceleration,
  spread_current, spread_trend, volume_acceleration,
  update_freq_1h, time_since_low_min, distance_from_low,
  watch_score, score_updated_at

alerts_sent
  market_id, player_name, source, anomaly_type,
  prev_prob, curr_prob, sent_at, match_key
```

Migration is safe: the init_db() function uses PRAGMA table_info to add
missing columns to existing databases without data loss.

---

## Setup

```bash
cd tennis-odds-alert-bot
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
# fill in DISCORD_WEBHOOK_URL at minimum
python main.py
```

## Demo Mode (no API keys needed)

```env
DEMO_MODE=true
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/real/webhook
```

Triggers all four anomaly rules across 3 poll cycles.

---

## PRE_SPIKE Scoring

`core/pre_spike_engine.py` now writes `market_signals.watch_score` on every
cycle. It still logs and stores scores for calibration, but Discord alerts must
also pass the alert-quality gates above.

Current tuning is conservative:

1. Alerts only fire inside the low-odds PRE_SPIKE band.
2. Default alert threshold is `PRE_SPIKE_ALERT_SCORE=75`.
3. Volume surge and positive velocity carry more weight than before.
4. Naive Kalshi surname confirmation is capped at 6 points until proper
   cross-exchange market mapping exists.
5. External momentum remains a stub worth 0 points until live score/news/social
   sources are integrated.

Use `analyze_pre_spike.py` after enough completed rows accumulate to tune from
real outcomes instead of guesses.

## Verification

```bash
python verify_alert_quality.py   # throwaway DB, no Discord import/send
python verify_pre_spike.py       # throwaway DB, Discord preview optional
python analyze_pre_spike.py      # read-only report against tennis_scanner.db
```

---

## Adding a New Provider

1. Create `market_providers/myprovider_provider.py`
2. Subclass `BaseProvider`, implement `name` and `fetch_snapshots()`
3. Return `list[MarketSnapshot]` — populate as many microstructure fields as available
4. Register it in `main.py` `_build_providers()`

The signal engine, storage layer, and alerting layer require no changes.
