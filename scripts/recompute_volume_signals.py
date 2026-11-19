"""Recompute saved volume-spike signals from cached market bars."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(__import__("os").environ.get("DENNY888_DB_PATH") or ROOT / "portfolio.db")
RECENT_DAYS = 5
BASELINE_DAYS = 60
MINIMUM_PRICE_BARS = 35


def recompute(db_path=DB_PATH):
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with sqlite3.connect(db_path, timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        snapshots = db.execute(
            "SELECT session_date,results,missing FROM daily_market_scans ORDER BY session_date"
        ).fetchall()
        reports = []
        required = BASELINE_DAYS + RECENT_DAYS
        for snapshot in snapshots:
            results = json.loads(snapshot["results"])
            missing = json.loads(snapshot["missing"])
            insufficient = 0
            for symbol, row in list(results.items()):
                bars = db.execute(
                    "SELECT day,volume FROM market_bars WHERE symbol=? AND day<=? ORDER BY day DESC LIMIT ?",
                    (symbol, snapshot["session_date"], required),
                ).fetchall()[::-1]
                if len(bars) < MINIMUM_PRICE_BARS:
                    results.pop(symbol)
                    missing[symbol] = f"daily history insufficient ({len(bars)}/{required} bars)"
                    insufficient += 1
                    continue
                if len(bars) < required:
                    row["vol_pct"] = 0.0
                    row["spike"] = False
                    row["spike_dates"] = []
                    continue
                baseline = [float(bar["volume"]) for bar in bars[:BASELINE_DAYS]]
                recent = bars[BASELINE_DAYS:]
                baseline_volume = sum(baseline) / BASELINE_DAYS
                highest = max(recent, key=lambda bar: float(bar["volume"]))
                highest_volume = float(highest["volume"])
                spike = baseline_volume > 0 and highest_volume > 1.5 * baseline_volume
                row["vol_pct"] = (highest_volume / baseline_volume - 1) * 100 if baseline_volume else 0.0
                row["spike"] = spike
                row["spike_dates"] = [highest["day"]] if spike else []
            db.execute(
                "UPDATE daily_market_scans SET published_at=?,results=?,missing=? WHERE session_date=?",
                (updated_at, json.dumps(results, allow_nan=False), json.dumps(missing), snapshot["session_date"]),
            )
            reports.append((snapshot["session_date"], len(results), insufficient))
    return reports


if __name__ == "__main__":
    for day, count, insufficient in recompute():
        print(f"{day}: {count} rows; {insufficient} excluded for fewer than {MINIMUM_PRICE_BARS} price bars")
