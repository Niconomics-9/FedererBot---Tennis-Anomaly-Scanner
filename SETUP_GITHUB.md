# Hosting the scanner on GitHub Actions (free, no credit card)

The bot runs as a scheduled GitHub Actions workflow instead of a local
process: every ~10 minutes a runner spins up, runs 2 poll cycles (120 s
apart) against the Supabase Postgres database (the durable store), and
exits. A second workflow produces the daily report. Public repos get
unlimited Actions minutes — that is what makes this free, and it is why the
repo must be **public**.

What stays private: the Discord webhook, the Kalshi key id, and the Kalshi
private key live in GitHub **Actions secrets**, never in code. What becomes
public: the source code, LEARNINGS.md/STATUS.md, and the daily reports.

## One-time setup (~10 minutes)

### 0. Pre-flight — remove the stray secrets file

`".env - Copy.example"` in this folder contains a REAL Kalshi private key.
It is gitignored as a belt-and-braces measure, but delete it anyway:

```powershell
Remove-Item ".env - Copy.example"
```

### 1. Create the repository

On github.com: **New repository** → name e.g. `tennis-odds-alert-bot` →
**Public** → do NOT add a README/.gitignore (the project has them) → Create.

### 2. Push the code

From this folder (`git init` + initial commit were already done; `.gitignore`
keeps `.env`, `*.pem`, `*.db`, and logs out):

```powershell
git config user.name "<your github username>"
git config user.email "<your github noreply email>"   # Settings → Emails
git remote add origin https://github.com/<YOUR_USERNAME>/tennis-odds-alert-bot.git
git push -u origin main
```

(The identity lines are cosmetic — they make future commits show as you
instead of the "TennisBot" placeholder used for the initial commit.)

### 3. Add the four secrets

Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret name | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the `DISCORD_WEBHOOK_URL=` value from local `.env` |
| `KALSHI_KEY_ID` | the `KALSHI_KEY_ID=` value from local `.env` |
| `KALSHI_PRIVATE_KEY_PEM` | the full contents of `kalshi_private_key.pem`, including the BEGIN/END lines |
| `SUPABASE_DB_URL` | the **Session pooler** connection string from Supabase Dashboard → Connect (port 5432). Not the direct `db.<ref>.supabase.co` string — that host is IPv6-only and Actions runners cannot reach it |

### 4. First run

Repo → **Actions** tab → enable workflows if prompted → select **Scanner** →
**Run workflow**. Watch the log: both providers should fetch (~1,450
Polymarket + ~630 Kalshi markets). The first runs are quiet by design — the
database starts empty and alerts require history/classification gates.

### 5. Cut over (avoid duplicate Discord alerts)

Once the Scanner workflow is green and running on schedule, stop the local
bot — running both means double alerts:

```powershell
Disable-ScheduledTask -TaskName "TennisBot Scanner"
Disable-ScheduledTask -TaskName "TennisBot Daily Report"
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process
```

(Re-enable with `Enable-ScheduledTask` + reboot to fall back to local.)

## How it differs from local hosting

| | Local (Task Scheduler) | GitHub Actions |
|---|---|---|
| Poll cadence | every 120 s continuously | 2 cycles per run, runs every ~10 min (GitHub can add minutes of jitter) |
| Uptime | dies with sleep/reboot | 24/7, no machine needed |
| Database | Supabase Postgres (shared) | Supabase Postgres (shared), snapshots pruned to 7 days (`SNAPSHOT_RETENTION_DAYS`) |
| Reports | `reports\` folder | committed to the repo daily — readable from any device |
| Signal semantics | velocity_5c spans ~10 min | velocity_5c spans bursts ~10 min apart — recalibrate thresholds against post-migration data before judging them |

Known trade-offs, accepted because waves develop over hours (median entry
~18 h before start, per the 2026-06-12 backtest): cron jitter and the
coarser cadence. The database lives in Supabase, so local runs and Actions
runs share one durable store — no cache eviction risk, no backup artifacts,
no re-alert noise when switching hosts.

## Day-to-day

Nothing to run. Alerts arrive in Discord; the daily report and the
`match_waves_*.csv` backtest dataset land in `reports/` in the repo
overnight (midnight EDT / 11 PM EST), ready to read in the morning. The Actions tab is the health dashboard — a wall of
green checkmarks every 10 minutes.
