"""Publish preliminary and confirmation scans after the official US equity close."""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


NEW_YORK = ZoneInfo("America/New_York")
PRELIMINARY_DELAY = timedelta(minutes=20)
CONFIRMATION_DELAY = timedelta(minutes=60)


def _latest_due_close(now: datetime) -> datetime:
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    cutoff = now.astimezone(timezone.utc) - PRELIMINARY_DELAY
    market = xcals.get_calendar("XNYS")
    session = market.date_to_session(cutoff.astimezone(NEW_YORK).date(), direction="previous")
    if market.session_close(session) > cutoff:
        session = market.previous_session(session)
    return market.session_close(session)


def latest_due_session(now: datetime) -> date:
    """Return the most recent session eligible for its preliminary scan."""
    return _latest_due_close(now).date()


def run_if_due(db_path: Path, now: datetime):
    from market_scan_store import needs_scheduled_scan, run_scan

    close = _latest_due_close(now)
    target = close.date()
    phase = "confirmation" if now.astimezone(timezone.utc) >= close + CONFIRMATION_DELAY else "preliminary"
    if not needs_scheduled_scan(db_path, target, phase=phase):
        return None
    return run_scan(db_path, target, phase=phase)


if __name__ == "__main__":
    db_path = Path(os.environ.get("DENNY888_DB_PATH") or Path(__file__).with_name("portfolio.db"))
    result = run_if_due(db_path, datetime.now(timezone.utc))
    if result is not None:
        print(f"Market scan {result['as_of']} {result['phase']} {result['status']} "
              f"({result['count']}/{result['target_count']} symbols)", flush=True)
