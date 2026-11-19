import unittest
from unittest.mock import patch

import market_universe as universe


class UniverseTests(unittest.TestCase):
    def test_sheet_reads_symbol_column_and_preserves_order(self):
        text = "股數,代號,股價\n2,NYSE:BRK.B,\n4,brk-b,\n,0050.TW,\n,,\n,TSLA,150\n"
        self.assertEqual(universe.watchlist_symbols(text), ["BRK-B", "0050.TW", "TSLA"])

    def test_empty_and_unavailable_sheets_are_distinct(self):
        self.assertEqual(universe.watchlist_symbols("代號,股價,股數\n"), [])
        with self.assertRaisesRegex(ValueError, "缺少"):
            universe.watchlist_symbols("<html>Sign in</html>")

    def test_all_index_share_classes_are_kept(self):
        symbols = ["T" + str(i) for i in range(501)] + ["BRK.B", "BF.B"]
        html = '<table id="constituents">' + "".join(
            f'<tr><td><a href="/stock">{s}</a>\n</td><td>Company</td></tr>' for s in symbols
        ) + "</table>"
        with patch.object(universe, "fetch_text", return_value=html):
            members = universe.sp500_symbols()
        self.assertEqual(len(members), 503)
        self.assertEqual(members[-2:], ["BRK-B", "BF-B"])

    def test_combined_consumer_ranking_is_taken_across_sectors(self):
        quotes = [{"symbol": "AAA"}, {"symbol": "BBB"}]
        with patch.object(universe.yf, "screen", return_value={"quotes": quotes}) as screen:
            members = universe.ranked_stocks("sector", ["Consumer Defensive", "Consumer Cyclical"], 2)
        self.assertEqual(members, ["AAA", "BBB"])
        query, = screen.call_args.args
        self.assertEqual(query.to_dict()["operands"][1]["operator"], "OR")
        self.assertEqual(screen.call_args.kwargs, dict(size=2, sortField="intradaymarketcap", sortAsc=False))
