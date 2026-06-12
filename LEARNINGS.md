# Learnings — what works, what doesn't

Working journal for the wave-chasing strategy. Update this whenever a tuning
change is made or evidence comes in. Evidence sources:

- `python analyze_alert_outcomes.py` — did odds move *after* each alert (the
  definition of a good alert), per type and pre-match vs in-play
- `python analyze_pre_spike.py` — which score components separate hits from
  misses, for re-weighting
- `python backtest_match_waves.py` — full-universe retrospective: labels EVERY
  saved Polymarket match-winner series with whether a pre-match wave would
  have worked (best entry→peak ≥ +3 pp, entry ≥ 10 min before start), plus
  caught/missed vs alerts and a watch_score threshold sweep. Per-series
  dataset accumulates in `reports\match_waves_YYYY-MM-DD.csv`

## The goal (set by user, 2026-06-11)

**Alerts must fire BEFORE the match starts.** We are not trying to win bets
or find every low odd — we are chasing **waves**: odds that are likely to
move, where one could enter and ride the move pre-match. A good alert is one
where the price moves afterwards, regardless of the match result.

## Evidence so far

### 2026-06-11 — baseline before the pre-match pivot
- 350 alerts in ~2 days: 228 NEW_LOW_WATCH, 103 FAST_MOVE, 12 LOW_ODDS_SPIKE,
  4 FAVORITE_RECOVERY, 3 PRE_SPIKE. The big two are dominated by **in-play**
  matches (e.g. FAST_MOVE 11% → 34.5% is a mid-match swing) — the alert fires
  *after* the wave, which is exactly wrong for this strategy. This motivated
  the hard pre-match gate.
- Verified live: **2,782 / 2,782** Polymarket tennis match markets carry
  `gameStartTime`; tournament futures don't (already filtered). Some "active"
  markets are months stale (match long over, never closed) — the pre-match
  gate kills those too.
- PRE_SPIKE fired only 3 times in 2 days at threshold 75 with the old
  weights — too strict to learn from. Rebalanced (below) to trade a lower
  threshold for harder evidence requirements.

### 2026-06-11 — first outcome report (legacy alerts baseline)
From `reports/report_2026-06-11.txt`, covering the ~210 completed legacy
(pre-pivot, phase "unknown") alerts: **median forward peak was +0.00 pp at 30,
60 and 120 min** — the typical alert was followed by no upward move at all.
Only ~12% reached +3 pp, ~11% reached +5 pp. The big maxima (e.g. +93 pp on a
NEW_LOW_WATCH) are in-play comebacks — rare lottery tickets, not a tradeable
pattern. **This is the baseline the pre-match strategy must beat**: success =
median peak meaningfully above zero and a >=3 pp rate well above 12%.

### 2026-06-12 — first full-universe wave backtest (all matches, not just alerts)

From `backtest_match_waves.py` over 2.7 days of data: 210 completed
Polymarket match-winner series with start times, 111 with enough (≥5)
pre-match snapshots to judge.

- **Base rate: 20.7% of series (23/111) had a ≥3 pp pre-match wave**, and
  17.1% had ≥5 pp with a median wave of **+16.5 pp** among them. Pre-match
  waves are real, big, and typically rode with zero drawdown (median dip
  0.0 pp between entry and peak).
- **Waves start EARLY**: median best entry ~18 h before start (IQR 9–40 h),
  median peak ~15 h before. The final pre-match hours are NOT where the
  moves are — coverage hours before start matters more than the 10-min lead
  edge case.
- **Band evidence (belief 3)**: waves concentrated at 12–60% entries
  (12–30%: 4/14 worked; 30–60%: 19/74). Zero waves at 60%+; almost no
  series at 2–12%. The PRE_SPIKE band cap of 30% sits *below* where most
  waves live (30–60%).
- **Catch rate**: the live bot caught 1/23 waves — expected, since the
  pre-match gate only went live 2026-06-11.
- **watch_score replay**: median max pre-match score 44 for wave series vs
  30 for non-waves — the score separates. Threshold sweep: **≥40 → 60.9%
  precision / 63.6% recall**; ≥50 → 60% / 13.6%; the live threshold 70
  **never fired pre-match**. Suggests 70 → ~40–45 plus band widening, but
  n = 22 wave series with score history — wait for ~a week of data
  (2026-06-18 review) before retuning.
- Caveats: hindsight-optimal entries (live recall will be lower), short
  window, and 99 completed series had too little pre-match history (bot
  often first sees a market late).

## Current beliefs (revise with data)

| # | Belief | Status |
|---|--------|--------|
| 1 | In-play swings are unusable for this strategy — by the time a comeback shows in odds, the wave already happened. | Adopted as a hard gate, 2026-06-11 |
| 2 | Membership in a low-odds band is not evidence a price will move; pressure signals (volume surge, spread tightening, sustained drift, update frequency) are. | Adopted: in_band 15 → 5 pts, sustained_move added at 10 pts |
| 3 | Pre-match waves are not confined to sub-12% odds; steam/news moves happen across the band. | Adopted: PRE_SPIKE band 2–12% → 2–30%. Watch whether 12–30% alerts have worse peak/dip ratios |
| 4 | Sustained multi-cycle drift (velocity_5c) beats single-cycle velocity as a wave precursor, since one tick is often just a repricing. | Untested — check component report after ~1 week |
| 5 | Alerts need lead time to be actionable; <10 min before start is too late to ride a wave. | Adopted as ALERT_PRE_MATCH_MIN_LEAD_MINUTES=10 |
| 6 | Cross-exchange (Kalshi twin moving first) is a strong confirm but rarely matches; don't make alerts depend on it. | Unchanged at max 6 pts |

## Tuning log

### 2026-06-11 — pre-match pivot
- Added `match_start_time` capture (Polymarket `gameStartTime`) + storage.
- Hard pre-match gate in `alert_quality` for ALL alert types:
  `ALERT_PRE_MATCH_ONLY=true`, min lead 10 min, missing start time rejects
  (safe: 100% coverage on match markets).
- PRE_SPIKE rebalance: in_band 15→5; new `pressure.sustained_move`
  (velocity_5c ≥ 0.02 → 10, > 0.005 → 5); band max 0.12→0.30;
  alert score 75→70 (more evidence required despite lower number);
  urgent 85→80 (85 was unreachable with the external-signals stub at 0).
- Expected side effects: NEW_LOW_WATCH and FAVORITE_RECOVERY (in-play
  patterns) should nearly stop firing — that is intended. Total alert volume
  drops sharply; what remains should be pre-match steam.
- **Check around 2026-06-18:** run both analyze scripts. Questions:
  (a) do pre-match alerts show positive median peak at 60–120 min?
  (b) does `pressure.sustained_move` separate hits from misses?
  (c) are 12–30% band alerts pulling their weight (belief 3)?
  (d) is PRE_SPIKE producing at least a handful of alerts per day? If zero,
      consider threshold 70→65 before touching weights.

## Open questions / next ideas

- Volume on pre-match tennis markets is thin until a few hours before start;
  consider whether pressure thresholds need scaling by time-to-start.
- External signals stub (news/social) is still 0 — the most likely source of
  genuine pre-match edge once integrated.
- Two python processes for the bot is normal on this machine: the
  WindowsApps `pythonw.exe` alias is a shim that spawns the real interpreter
  as a child — it is ONE bot instance, not a duplicate-alert risk.
