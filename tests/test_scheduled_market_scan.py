import sqlite3
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scheduled_market_scan import latest_due_session, run_if_due


class ScanScheduleTests(unittest.TestCase):
    def test_regular_close_follows_us_daylight_saving(self):
        for instant, due in [
            ("2026-03-06T21:09:59+00:00", date(2026, 3, 5)),
            ("2026-03-06T21:10:00+00:00", date(2026, 3, 6)),
            ("2026-03-09T20:09:59+00:00", date(2026, 3, 6)),
            ("2026-03-09T20:10:00+00:00", date(2026, 3, 9)),
        ]:
            with self.subTest(instant=instant):
                self.assertEqual(latest_due_session(datetime.fromisoformat(instant)), due)

    def test_thanksgiving_half_day_and_weekend(self):
        for instant, due in [
            ("2026-11-26T23:45:00+00:00", date(2026, 11, 25)),
            ("2026-11-27T18:09:59+00:00", date(2026, 11, 25)),
            ("2026-11-27T18:10:00+00:00", date(2026, 11, 27)),
            ("2026-11-29T08:00:00+08:00", date(2026, 11, 27)),
        ]:
            with self.subTest(instant=instant):
                self.assertEqual(latest_due_session(datetime.fromisoformat(instant)), due)

    def test_catch_up_latest_and_retry_without_repeating_completed_run(self):
        # A reboot in Taipei on Thursday morning is after Wednesday's US close.
        now = datetime.fromisoformat("2026-11-19T08:00:00+08:00")
        target = date(2026, 11, 18)
        path = Path("test.db")
        summary = {"as_of": target.isoformat(), "status": "complete"}
        with patch("market_scan_store.needs_scheduled_scan", side_effect=[True, True, False]), \
                patch("market_scan_store.run_scan", side_effect=[RuntimeError("upstream offline"), summary]) as scan:
            with self.assertRaisesRegex(RuntimeError, "upstream offline"):
                run_if_due(path, now)
            self.assertEqual(run_if_due(path, now), summary)
            self.assertIsNone(run_if_due(path, now))
            self.assertEqual(scan.call_count, 2)
            scan.assert_called_with(path, target)

    def test_partial_snapshot_retries_at_most_twice_and_complete_snapshot_is_skipped(self):
        from market_scan_store import init_schema

        now = datetime.fromisoformat("2026-11-19T08:00:00+08:00")
        day = "2026-11-18"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "portfolio.db"
            with closing(sqlite3.connect(path)) as db, patch("market_scan_store.run_scan") as scan:
                init_schema(db)
                db.execute("INSERT INTO daily_market_scans VALUES (?, ?, '{}', '{}', '{}')", (day, day))
                insert = "INSERT INTO market_scan_attempts(session_date, started_at, status) VALUES (?, ?, ?)"
                db.execute(insert, (day, day, "complete"))
                db.commit()
                self.assertIsNone(run_if_due(path, now))
                scan.assert_not_called()

                db.execute("UPDATE daily_market_scans SET missing = ?", ('{"DELAYED":"missing close"}',))
                db.commit()
                for status in ("failed", "complete"):
                    self.assertIs(run_if_due(path, now), scan.return_value)
                    db.execute(insert, (day, day, status))
                    db.commit()
                self.assertIsNone(run_if_due(path, now))
                self.assertEqual(scan.call_count, 2)
