"""Run the latest US equity session's scan once its official close is ten minutes old."""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


NEW_YORK = ZoneInfo("America/New_York")
AFTER_CLOSE = timedelta(minutes=10)


def latest_due_session(now: datetime) -> date:
    """Return the most recent session eligible for publication at this instant."""
    if now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    cutoff = now.astimezone(timezone.utc) - AFTER_CLOSE
    market = xcals.get_calendar("XNYS")
    session = market.date_to_session(cutoff.astimezone(NEW_YORK).date(), direction="previous")
    if market.session_close(session) > cutoff:
        session = market.previous_session(session)
    return session.date()


def run_if_due(db_path: Path, now: datetime):
    from market_scan_store import needs_scheduled_scan, run_scan

    target = latest_due_session(now)
    if not needs_scheduled_scan(db_path, target):
        return None
    return run_scan(db_path, target)


if __name__ == "__main__":
    db_path = Path(os.environ.get("DENNY888_DB_PATH") or Path(__file__).with_name("portfolio.db"))
    result = run_if_due(db_path, datetime.now(timezone.utc))
    if result is not None:
        print(f"Market scan {result['as_of']} {result['status']} "
              f"({result['count']}/{result['target_count']} symbols)", flush=True)
