"""
Daily report — runs the read-only analyzers and writes a dated file to
reports\\, so the review routine is just reading the newest report there.
backtest_match_waves.py additionally writes its per-series CSV dataset
(match_waves_YYYY-MM-DD.csv) alongside the report.

Scheduled via Windows Task Scheduler ("TennisBot Daily Report"). Safe to run
any time: both analyzers open the database read-only (sqlite mode=ro) and the
live scanner uses WAL, so they never block each other.

Keeps the most recent 30 reports.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
REPORTS = BASE / "reports"
KEEP_REPORTS = 30
ANALYZERS = (
    "analyze_alert_outcomes.py",
    "analyze_pre_spike.py",
    "backtest_match_waves.py",
)


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    sections = [f"TennisBot daily report — generated {datetime.now():%Y-%m-%d %H:%M} (local)\n"]

    for script in ANALYZERS:
        proc = subprocess.run(
            [sys.executable, str(BASE / script)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=BASE,
            timeout=600,
        )
        sections.append(f"{'#' * 78}\n# {script}\n{'#' * 78}\n{proc.stdout}")
        if proc.returncode != 0 or proc.stderr.strip():
            sections.append(f"[stderr / exit {proc.returncode}]\n{proc.stderr}")

    out = REPORTS / f"report_{datetime.now():%Y-%m-%d}.txt"
    out.write_text("\n".join(sections), encoding="utf-8")

    for old in sorted(REPORTS.glob("report_*.txt"))[:-KEEP_REPORTS]:
        old.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
