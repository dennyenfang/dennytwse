import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd

from market_scan_store import GROUP_LABELS, ScanUnavailable, latest_completed_session, needs_scheduled_scan, read_scan, run_scan


class MarketScanTest(unittest.TestCase):
    day = date(2026, 11, 18)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "portfolio.sqlite3"
        self.groups = {key: ["AAA"] for key in GROUP_LABELS}
        self.groups.update(sp500=["AAA", "BBB", "AAA"], financial=["BBB", "AAA"], sheet=["CCC", "AAA"])

    def download(self, tickers, *, end, **options):
        target = date.fromisoformat(end) - timedelta(days=1)
        dates = pd.bdate_range(end=target, periods=209)
        values = {}
        for ticker in tickers:
            close = np.linspace(100, 74, len(dates))
            close[-1] = 250.0
            volume = np.full(len(dates), 1_000.0)
            volume[-1] = 1501.0
            values[ticker] = pd.DataFrame({"Close": close, "Volume": volume}, index=dates)
        return pd.concat(values, axis=1) if values else pd.DataFrame()

    def run_day(self, day=None, downloader=None):
        return run_scan(self.path, day or self.day, load_universe=lambda: self.groups,
                        download=downloader or self.download)

    def test_stored_union_groups_indicators_and_dated_read(self):
        calls = Mock(wraps=self.download)
        result = self.run_day(downloader=calls)
        self.assertEqual(result["target_count"], 3)
        self.assertEqual(calls.call_args.args[0], ["AAA", "BBB", "CCC"])
        self.assertEqual(latest_completed_session(self.path), self.day)
        all_rows = read_scan(self.path)
        self.assertEqual(all_rows["count"], 3)
        self.assertEqual(all_rows["groups"]["financial"]["target_count"], 2)
        self.assertEqual(all_rows["latest_attempt"]["status"], "complete")
        first = all_rows["rows"][0]
        self.assertEqual(first["group_ranks"]["sheet"], 2)
        self.assertEqual(first["group_ranks"]["financial"], 2)
        self.assertEqual(first["bar_count"], 200)
        self.assertTrue(first["spike"])
        self.assertEqual(first["vol_pct"], 50.1)
        self.assertEqual(first["golden"], self.day.isoformat())
        self.assertEqual(first["positive"], self.day.isoformat())
        subset = read_scan(self.path, "financial")
        self.assertEqual([r["symbol"] for r in subset["rows"]], ["BBB", "AAA"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0], 600)
        self.assertEqual(self.run_day(downloader=Mock(side_effect=AssertionError("already done")))["status"], "skipped")
        self.run_day(self.day + timedelta(days=1))
        self.assertEqual(read_scan(self.path, as_of=self.day.isoformat())["published_at"], all_rows["published_at"])
        self.assertNotEqual(read_scan(self.path)["as_of"], self.day.isoformat())

    def test_partial_sheet_coverage_retries_only_missing_then_repairs(self):
        batches = []
        def incomplete(tickers, **opts):
            batches.append(tickers)
            return self.download([symbol for symbol in tickers if symbol != "CCC"], **opts)
        self.run_day(downloader=incomplete)
        self.assertEqual(batches, [["AAA", "BBB", "CCC"], ["CCC"], ["CCC"]])
        report = read_scan(self.path, "sheet")
        self.assertEqual(report["target_count"], 2)
        self.assertEqual([r["symbol"] for r in report["missing"]], ["CCC"])
        retry = Mock(wraps=self.download)
        result = run_scan(self.path, self.day, load_universe=Mock(side_effect=AssertionError("keep captured roster")),
                          download=retry)
        self.assertEqual(result["missing_count"], 0)
        self.assertEqual(retry.call_args.args[0], ["CCC"])
        self.assertEqual(read_scan(self.path, "sheet")["count"], 2)

    def test_sector_with_four_stale_tickers_still_publishes_and_reports_them(self):
        telecom = [f"TEL{i}" for i in range(10)]
        self.groups["telecom"] = telecom
        stale = telecom[6:]
        def partly_stale(tickers, **opts):
            frame = self.download(tickers, **opts)
            for ticker in set(tickers).intersection(stale):
                frame.loc[frame.index[-1], (ticker, "Close")] = np.nan
            return frame
        result = self.run_day(downloader=partly_stale)
        self.assertEqual(result["status"], "complete")
        sector = read_scan(self.path, "telecom")
        self.assertEqual((sector["count"], sector["target_count"]), (6, 10))
        self.assertEqual([item["symbol"] for item in sector["missing"]], stale)
        self.assertTrue(all("2026-11-17" in item["reason"] for item in sector["missing"]))
        self.assertEqual(read_scan(self.path, "sp500")["missing_count"], 0)

    def test_signal_older_than_three_sessions_is_not_reported(self):
        dates = pd.bdate_range(end=self.day, periods=200)
        frame = pd.DataFrame({"Close": np.r_[np.full(80, 100.0), 100.0 + np.arange(1, 121)],
                              "Volume": 1000}, index=dates)
        self.run_day(downloader=lambda symbols, **opts: pd.concat({s: frame for s in symbols}, axis=1))
        row = read_scan(self.path)["rows"][0]
        self.assertEqual(row["golden"], "")
        self.assertEqual(row["positive"], "")

    def test_scheduled_retry_budget_starts_after_first_publication(self):
        self.assertTrue(needs_scheduled_scan(self.path, self.day))
        with self.assertRaises(ScanUnavailable):
            self.run_day(downloader=lambda *args, **opts: pd.DataFrame())
        self.assertTrue(needs_scheduled_scan(self.path, self.day))
        def partial(tickers, **opts):
            return self.download([s for s in tickers if s != "CCC"], **opts)
        self.run_day(downloader=partial)
        for attempt in range(2):
            self.assertTrue(needs_scheduled_scan(self.path, self.day))
            self.run_day(downloader=partial)
        self.assertFalse(needs_scheduled_scan(self.path, self.day))
        self.assertFalse(needs_scheduled_scan(self.path, self.day - timedelta(days=1)))
        self.assertTrue(needs_scheduled_scan(self.path, self.day + timedelta(days=1)))

    def test_outage_and_stale_bars_preserve_last_good_publication(self):
        self.run_day()
        tomorrow = self.day + timedelta(days=1)
        def stale(symbols, **opts):
            return self.download(symbols, end=tomorrow.isoformat())
        with self.assertRaises(ScanUnavailable):
            self.run_day(tomorrow, stale)
        report = read_scan(self.path)
        self.assertEqual(report["as_of"], self.day.isoformat())
        latest = report["latest_attempt"]
        self.assertEqual(latest["status"], "failed")
        self.assertEqual(latest["as_of"], tomorrow.isoformat())
        self.assertEqual(latest["count"], 0)
        self.assertIn(self.day.isoformat(), latest["missing"]["AAA"])
        self.assertEqual(latest_completed_session(self.path), self.day)

    def test_unavailable_roster_is_distinct_from_a_valid_empty_sheet(self):
        self.groups["sheet"] = []
        self.run_day()
        self.assertEqual(read_scan(self.path, "sheet")["target_count"], 0)
        del self.groups["sheet"]
        with self.assertRaises(ScanUnavailable):
            self.run_day(self.day + timedelta(days=1))
        self.assertEqual(read_scan(self.path)["latest_attempt"]["status"], "failed")

    def test_concurrent_run_cannot_start_second_producer(self):
        with closing(sqlite3.connect(str(self.path) + ".scan-lock")) as existing:
            existing.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(ScanUnavailable):
                self.run_day()
        self.assertIsNone(latest_completed_session(self.path))


if __name__ == "__main__":
    unittest.main()
