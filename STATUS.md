# Tennis Prediction Market Anomaly Scanner — Status

**Last updated:** 2026-06-11
**State:** 🟢 LIVE — self-running via Windows Task Scheduler, polling every 120 seconds

## Hands-off operation (2026-06-11)

Nothing needs to be run manually anymore:

| Task | Schedule | What it does |
|---|---|---|
| **TennisBot Scanner** | At logon + keepalive retry every 15 min | Starts `pythonw run_live.py`; a socket lock (port 47831) in run_live.py guarantees a single instance, so retries are harmless no-ops while the bot is alive and an auto-restart when it isn't |
| **TennisBot Daily Report** | Daily 6:00 AM Pacific (9:00 AM Eastern) | Runs `run_daily_report.py` → writes `reports\report_YYYY-MM-DD.txt` (alert outcomes + PRE_SPIKE calibration + full-universe wave backtest, read-only; keeps last 30) and `reports\match_waves_YYYY-MM-DD.csv` (per-match "would it have worked" dataset) |

**Routine:** Discord alerts arrive in real time. Daily/weekly, skim the newest file in `reports\` and check it against the beliefs in [LEARNINGS.md](LEARNINGS.md). Manage tasks via `taskschd.msc` or `Get-ScheduledTask -TaskName "TennisBot*"`.

---

## What it does

Monitors tennis prediction markets on **Polymarket** and **Kalshi**, detects odds likely to move, and sends alerts to **Discord**. No betting, no trade execution — alerts only.

**Strategy (2026-06-11): pre-match wave chasing.** Alerts fire only BEFORE the match starts (≥ 10 min lead). The target is odds that are about to move — not low odds per se, and not in-play comebacks (by the time a comeback shows in the odds, the wave already happened). See [LEARNINGS.md](LEARNINGS.md) for what works and the tuning log.

## Live data coverage

| Provider | Markets | Source |
|---|---|---|
| Polymarket | ~1,450 live tennis markets | Gamma API `/events?tag_slug=tennis` (ATP, WTA, ITF, Challengers, Grand Slam futures) |
| Kalshi | ~630 live tennis markets | `/events?series_ticker=...` → `/markets?event_ticker=...` across 25 tennis series (KXATPMATCH, KXWTAMATCH, KXITFMATCH, KXITFWMATCH, Challenger series, slam futures) |

Both providers include **ITF men's and women's matches** (M15/W15/W50 etc.).

## Alert rules

| Rule | Trigger | Purpose |
|---|---|---|
| 👀 **NEW_LOW_WATCH** | A **live match** ("X vs Y" only, futures excluded) first seen with a player ≤ **10%** | Entry opportunity — low odds that may recover (e.g. 5% → 20%) |
| 🚀 **LOW_ODDS_SPIKE** | Player was ≤ 10%, jumps ≥ 3 pp | Longshot surging |
| 💪 **FAVORITE_RECOVERY** | Opened ≥ 60%, fell ≤ 25%, recovered ≥ 7 pp | Fallen favourite bouncing back |
| ⚡ **FAST_MOVE** | ≥ 5 pp move within 10 minutes | Rapid market movement |

- **Pre-match only:** every alert type is suppressed once a match has started or starts within 10 minutes (`gameStartTime` from Polymarket — verified present on 100% of match markets). In-play rules (NEW_LOW_WATCH, FAVORITE_RECOVERY) effectively stop firing — intended. Stale never-closed markets are suppressed by the same gate.
- Discord alerts are **match-winner focused by default**.
- Completed matches, totals, handicaps, set winners, tournament futures, malformed markets, 0.5% / 99.5% resolved-looking prices, wide spreads, thin liquidity/volume, and markets with too little snapshot history are suppressed.
- Alert cooldown: **30 minutes** per market/rule plus **60 minutes** per match across related markets.
- PRE_SPIKE scoring is live: band **2–30%**, alert ≥ **70**, urgent ≥ **80**, rebalanced so points come from movement evidence (volume surge, spread tightening, sustained drift) rather than band membership.

## Configuration (`.env`)

| Setting | Value |
|---|---|
| Poll interval | 120 s |
| NEW_LOW_THRESHOLD | 0.10 |
| LOW_ODDS_SPIKE | prev ≤ 10%, +3 pp |
| FAST_MOVE | +5 pp / 10 min |
| FAVORITE_RECOVERY | open ≥ 60%, low ≤ 25%, +7 pp |
| Cooldown | 30 min |
| Match cooldown | 60 min |
| Alert market type | match winners only |
| Alert history gate | 3 snapshots |
| Alert spread gate | ≤ 8 pp when spread is available |
| Alert liquidity / volume gate | ≥ 100 when available |
| Pre-match gate | on — ≥ 10 min lead, missing start time rejects |
| PRE_SPIKE band / alert / urgent | 2–30% / 70 / 80 |
| Demo mode | off |

Credentials configured: Discord webhook ✅ · Kalshi RSA key (`kalshi_private_key.pem` — never commit) ✅ · Polymarket needs no key.

## Architecture

```
main.py                      — entry point, poll loop
config/settings.py           — env-driven config
market_providers/
  models.py                  — MarketSnapshot (+7 microstructure fields), MarketSignals
  polymarket_provider.py     — Gamma API events endpoint, resolved-market filter (0.5%–99.5%)
  kalshi_provider.py         — RSA-signed requests, two-stage series→events→markets fetch
core/
  scanner.py                 — orchestrates providers per cycle
  anomaly_engine.py          — 4 rules + cooldown dedup
  market_classifier.py       — match-winner vs noisy-market classification
  alert_quality.py           — spread/liquidity/history/type alert gates
  market_tracker.py          — rolling stats (open/low/high)
  signal_engine.py           — 6 microstructure signals per snapshot
  pre_spike_engine.py        — conservative watch_score scoring
alerts/discord_alerter.py    — webhook formatting
storage/supabase_storage.py  — Supabase Postgres (5 tables; storage/schema.sql)
storage/report_db.py         — read-only access for the analyzer scripts
```

## Data being collected and scored

Every cycle, per market: probability, bid/ask, spread, volume, liquidity, velocity (1-cycle & 5-cycle), acceleration, spread trend, distance from low, update frequency, volume acceleration, and PRE_SPIKE `watch_score`. Scores are stored for calibration even when Discord alerts are suppressed.

## Known limitations

- Kalshi fetch makes many API calls (25 series × events × markets) paced at ~6.7 req/s to stay under Kalshi's ~10 req/s limit, so a Kalshi poll takes ~45 s of the cycle.
- ~50/50 new matches produce no alert by design — alerts require low odds, movement, and quality gates.
- First run after a DB reset is quieter than before because alerts require market classification and minimum history.

## Recent changes (2026-07-09) — Supabase Postgres cutover completed

Storage moved from a SQLite file juggled through the Actions cache to a
Supabase Postgres project (`federerbot`, us-east-1) as the single durable
store. The storage layer itself (`storage/supabase_storage.py`) and the
schema migration (`federerbot_initial_schema`, applied) already existed;
this change finished the cutover:

- Analyzers (`analyze_alert_outcomes/pre_spike/missed_waves`,
  `backtest_match_waves`) ported from sqlite to read-only Postgres via new
  `storage/report_db.py` (timestamps normalised back to ISO strings so the
  analysis logic is unchanged).
- Verify scripts run against a disposable `verify_<hex>` Postgres schema
  (new `storage/verify_env.py` + `SUPABASE_SCHEMA` setting) — they no longer
  need a local db and can never touch live tables.
- Workflows: cache restore/save and db backup-artifact steps removed; both
  workflows now need the `SUPABASE_DB_URL` Actions secret (Session pooler
  string — the direct db host is IPv6-only and unreachable from runners).
- `.env.example` / SETUP_GITHUB.md updated accordingly.

**Cutover checklist:** add the `SUPABASE_DB_URL` repo secret, then trigger
the Scanner workflow manually and check both providers write snapshots.

## Recent changes (2026-06-12d) — PRE_SPIKE opened up to where waves live

Missed-wave post-mortem (`analyze_missed_waves.py`, now part of the daily
report): 30 of 31 real pre-match waves produced no alert — 80% were killed
by the hard 2–30% band gate (waves actually enter at 31–55%, coin-flip
matches steaming toward a favorite), the rest by the unreachable threshold
70 (wave scores top out at 64 with the external bucket stubbed). Adopted:
band cap 0.30 → 0.60, alert score 70 → 45, urgent 80 → 60 (replay over the
widened universe: ~61% precision / 64% recall, ~9 fires/day). Details and
caveats in [LEARNINGS.md](LEARNINGS.md).

## Recent changes (2026-06-12c) — GitHub Actions hosting LIVE

Cutover completed: code pushed to
[Niconomics-9/FedererBot---Tennis-Anomaly-Scanner](https://github.com/Niconomics-9/FedererBot---Tennis-Anomaly-Scanner),
the three Actions secrets set, Scanner workflow green on schedule, Daily
report workflow verified end-to-end (report committed + db backup artifact).
Local Task Scheduler tasks ("TennisBot Scanner", "TennisBot Daily Report")
are **disabled** — re-enable them only if moving back off Actions.
First hosted run lost most Kalshi markets to 429 rate limits (runners issue
requests faster than Kalshi's ~10 req/s cap); fixed by pacing requests
150 ms apart and retrying 429s with backoff in `kalshi_provider.py`.

## Recent changes (2026-06-12b) — GitHub Actions hosting prepared

Free 24/7 cloud hosting (no credit card) via scheduled GitHub Actions runs —
see [SETUP_GITHUB.md](SETUP_GITHUB.md) for the 10-minute setup + cutover.
Code changes: `MAX_SCAN_CYCLES` burst mode in main.py, `SNAPSHOT_RETENTION_DAYS`
pruning in storage, `.github/workflows/` (scanner every ~10 min + daily report),
`.gitignore`, local git repo initialised (push pending — needs the GitHub repo).
Local Task Scheduler hosting keeps working unchanged until cutover.

## Recent changes (2026-06-12) — retrospective wave backtest

1. New `backtest_match_waves.py` — labels every saved Polymarket match-winner
   series with whether pre-match wave-chasing would have worked (best
   entry→peak ≥ +3 pp, entry ≥ 10 min before start), cross-references alerts
   (caught/missed) and replays watch_score with a threshold sweep. Writes
   `reports\match_waves_YYYY-MM-DD.csv`; added to the daily report. First
   findings (waves start ~18 h early; score threshold 70 never fires
   pre-match) recorded in [LEARNINGS.md](LEARNINGS.md).

## Recent changes (2026-06-11) — pre-match pivot

1. `match_start_time` captured from Polymarket `gameStartTime`, stored on every snapshot (safe DB migration)
2. Hard pre-match gate in `alert_quality` for all alert types (pre-match only, ≥ 10 min lead)
3. PRE_SPIKE rebalanced for wave detection: in-band 15 → 5 pts, new `pressure.sustained_move` (velocity_5c), band 2–12% → 2–30%, alert 75 → 70, urgent 85 → 80
4. Discord alerts lead with a match-start headline in **US Eastern time** (EST/EDT, no tzdata dependency) plus a live Discord countdown tag; ET + UTC + lead time repeated inside the detail block
5. New `analyze_alert_outcomes.py` — measures forward odds move after each alert (peak/dip), pre-match vs in-play
6. `LEARNINGS.md` added — hypotheses, evidence, tuning log

## Recent fixes (2026-06-09/10)

1. Polymarket returned 0 tennis → switched `/markets` to `/events?tag_slug=tennis` (~1,450 markets found)
2. Added resolved-market filter (skip prob < 0.5% or > 99.5%)
3. Kalshi returned 0 tennis → replaced full-market pagination with two-stage series→events→markets fetch (630 markets)
4. NEW_LOW_WATCH restricted to live matches only; threshold 8% → 10%; cleared stale flags so live matches re-trigger
5. Added alert-quality gates, match-level cooldown, cleaner player parsing, and conservative PRE_SPIKE retuning

## Pending / next steps

- [x] Windows Task Scheduler — done 2026-06-11 (see "Hands-off operation" above; the bot may show as two python processes — WindowsApps shim + real interpreter — that's one instance)
- [ ] ~2026-06-18: review a week of pre-match-only data in `reports\` against LEARNINGS.md beliefs
- [ ] Continue PRE_SPIKE calibration after more completed score_history rows
- [ ] External signals stub (news/social) — likely the biggest source of pre-match edge
- [ ] ~~Optional: speed up Kalshi fetch (batch/parallel requests)~~ — not viable: Kalshi caps at ~10 req/s, requests are now deliberately paced (2026-06-12)
