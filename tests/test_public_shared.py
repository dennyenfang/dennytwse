import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import jwt
import pandas as pd
from cryptography.hazmat.primitives.asymmetric import rsa

import app as portfolio


class SharedPortfolioTest(unittest.TestCase):
    team = "https://test-team.cloudflareaccess.com"
    audience = "portfolio-management-test"
    origin = "https://portfolio.example"
    editors = ("editor-one@example.org", "editor-two@example.org")

    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key()))
        cls.public_key.update(kid="test-key", use="sig", alg="RS256")

    def setUp(self):
        resources = ExitStack()
        self.addCleanup(resources.close)
        directory = resources.enter_context(tempfile.TemporaryDirectory())
        resources.enter_context(patch.multiple(portfolio, PUBLIC_SHARED=True, PUBLIC_EDITING=False, ACCESS_TEAM=self.team,
                                        ACCESS_AUD=self.audience, EDITOR_EMAILS=set(self.editors),
                                        PUBLIC_ORIGIN=self.origin, DB_PATH=Path(directory) / "db.sqlite3"))
        resources.enter_context(patch.object(jwt.PyJWKClient, "fetch_data", return_value={"keys": [self.public_key]}))
        portfolio.init_db()
        self.client = portfolio.app.test_client()

    def headers(self, email=None, **overrides):
        now = int(time.time())
        claims = dict(iss=self.team, aud=[self.audience], type="app", sub="test-user", email=email or self.editors[0],
                      iat=now - 1, nbf=now - 1, exp=now + 600)
        claims.update(overrides)
        token = jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "test-key"})
        return {"Origin": self.origin, "Cf-Access-Jwt-Assertion": token}

    def test_management_requires_valid_access_identity(self):
        self.assertEqual(self.client.get("/api/site-mode").json, {"publicShared": True, "canEdit": False})
        for headers in ({}, {"Cf-Access-Authenticated-User-Email": self.editors[0]}, self.headers("other@example.org"),
                        self.headers(aud=["unrelated"]), self.headers(iss="https://other.cloudflareaccess.com"),
                        self.headers(exp=int(time.time()) - 1), self.headers(nbf=int(time.time()) + 600),
                        self.headers(type="org"), self.headers(sub="")):
            with self.subTest(headers=list(headers)):
                response = self.client.get("/manage/", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertIn("no-store", response.headers["Cache-Control"])
                self.assertEqual(self.client.get("/manage/api/site-mode", headers=headers).status_code, 403)
        for email in self.editors:
            for path in ("/manage", "/manage/"):
                with self.client.get(path, headers=self.headers(email)) as page:
                    self.assertEqual(page.status_code, 200)
            mode = self.client.get("/manage/api/site-mode", headers=self.headers(email)).json
            self.assertEqual(mode, {"publicShared": True, "canEdit": True})
        with patch.object(portfolio, "ACCESS_AUD", ""):
            self.assertEqual(self.client.get("/manage/", headers=self.headers()).status_code, 403)
        with patch.object(self, "key", rsa.generate_private_key(public_exponent=65537, key_size=2048)):
            self.assertEqual(self.client.get("/manage/", headers=self.headers()).status_code, 403)

    def test_only_same_origin_authenticated_management_writes(self):
        data = {"symbol": "TEST", "shares": 3, "cost": 5}
        valid = self.headers()
        public = "/api/holdings"
        managed = "/manage/api/holdings"
        self.assertEqual(self.client.post(public, json=data, headers=valid).status_code, 403)
        for headers in ({"Origin": self.origin}, {**valid, "Origin": "https://elsewhere.example"},
                        {"Cf-Access-Jwt-Assertion": valid["Cf-Access-Jwt-Assertion"]}):
            self.assertEqual(self.client.post(managed, json=data, headers=headers).status_code, 403)
        self.assertEqual(self.client.post(managed, json=data, headers=valid).status_code, 200)
        self.assertEqual(self.client.get(public).json, [data])
        self.assertEqual(self.client.delete(public + "/TEST", headers=valid).status_code, 403)
        self.assertEqual(self.client.post("/api/sync-sheet", headers=valid).status_code, 403)
        self.assertEqual(self.client.delete(managed + "/TEST", headers=self.headers(self.editors[1])).status_code, 200)
        self.assertEqual(self.client.get(public).json, [])

    def test_public_editing_is_opt_in_and_scoped_to_management(self):
        same_origin = {"Origin": self.origin}
        holding = {"symbol": "TEST", "shares": 2, "cost": 10}
        with patch.multiple(portfolio, PUBLIC_EDITING=True, SHEET_ID=""), \
             patch.object(portfolio, "verified_editor", side_effect=AssertionError("guest requested JWT verification")):
            self.assertEqual(self.client.get("/api/site-mode").json, {"publicShared": True, "canEdit": False})
            self.assertEqual(self.client.get("/manage/api/site-mode").json, {"publicShared": True, "canEdit": True})
            with self.client.get("/manage/") as page:
                self.assertEqual(page.status_code, 200)
            for headers in ({}, {"Origin": "https://another.example"}):
                self.assertEqual(self.client.post("/manage/api/holdings", json=holding, headers=headers).status_code, 403)
                self.assertEqual(self.client.delete("/manage/api/holdings/TEST", headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/manage/api/scan", headers=headers).status_code, 403)
                self.assertEqual(self.client.post("/manage/api/sync-sheet", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/api/holdings", json=holding, headers=same_origin).status_code, 403)
            self.assertEqual(self.client.post("/api/sync-sheet", headers=same_origin).status_code, 403)
            self.assertEqual(self.client.post("/manage/api/holdings", data=holding, headers=same_origin).status_code, 415)
            self.assertEqual(self.client.post("/manage/api/holdings", json={"symbol": "TEST", "shares": "inf", "cost": 5}, headers=same_origin).status_code, 400)
            self.assertEqual(self.client.post("/manage/api/holdings", json=holding, headers=same_origin).status_code, 200)
            holding["shares"] = 4
            self.assertEqual(self.client.post("/manage/api/holdings", json=holding, headers=same_origin).status_code, 200)
            self.assertEqual(self.client.get("/api/holdings").json, [holding])
            self.assertEqual(self.client.delete("/api/holdings/TEST", headers=same_origin).status_code, 403)
            self.assertEqual(self.client.delete("/manage/api/holdings/TEST", headers=same_origin).status_code, 200)
            self.assertEqual(self.client.get("/api/holdings").json, [])
            self.assertEqual(self.client.post("/manage/api/scan", headers=same_origin).status_code, 200)
            sync = self.client.post("/manage/api/sync-sheet", headers=same_origin)
            self.assertEqual(sync.status_code, 200)
            self.assertFalse(sync.json["configured"])

    def test_public_quotes_and_scan_leave_database_unchanged(self):
        with portfolio.connect() as db:
            db.execute("INSERT INTO holdings VALUES ('TEST', 2, 0, 'sheet')")
            before = list(db.iterdump())
        prices = pd.DataFrame({"close": [10, 12], "macd": [-1, 1], "signal": [-0.5, 0.5]},
                              index=pd.date_range("2026-01-05", periods=2))
        with patch.object(portfolio, "fetch_macd", return_value=prices), patch.object(portfolio, "sync_sheet_holdings") as sync:
            self.assertEqual(self.client.get("/api/holdings").json[0]["cost"], 0)
            self.assertEqual(self.client.get("/api/quotes").json["quotes"][0]["cost"], 0)
            self.assertEqual(len(self.client.post("/api/scan").json["alerts"]), 2)
            self.assertEqual(self.client.get("/api/alerts").json, [])
            sync.assert_not_called()
            with portfolio.connect() as db:
                self.assertEqual(list(db.iterdump()), before)
            self.assertEqual(len(self.client.post("/manage/api/scan", headers=self.headers()).json["alerts"]), 2)
            self.assertEqual(len(self.client.get("/api/alerts").json), 2)
        for path in ("/portfolio.db", "/.env", "/static/portfolio.db"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_public_market_scan_does_not_contact_yahoo_or_launch_a_job(self):
        with patch.object(portfolio.yf, "download") as download, patch.object(portfolio.yf, "screen") as screen:
            for path in ("/api/market-scan/latest", "/api/market-scan?group=telecom"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json["status"], "pending")
            self.assertEqual(self.client.get("/api/market-scan?group=unknown").status_code, 400)
            download.assert_not_called()
            screen.assert_not_called()
        with portfolio.connect() as db:
            for table in ("daily_market_scans", "market_bars", "market_scan_attempts"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_quotes_and_stock_detail_cross_half_day_close_together(self):
        # The Friday after Thanksgiving closes at 13:00 Eastern. A live cache filled at
        # 13:09 should immediately admit Friday's candle at 13:10, even across the holiday.
        days = pd.bdate_range(end="2026-11-27", periods=60).difference(pd.DatetimeIndex(["2026-11-26"]))
        frame = pd.DataFrame({key: range(1, len(days) + 1) for key in ("Open", "High", "Low", "Close", "Volume")}, index=days)
        with patch.object(portfolio, "PRICE_CACHE", {}), patch.object(portfolio, "DETAIL_CACHE", {}), \
             patch.object(portfolio, "datetime", wraps=datetime) as clock, patch.object(portfolio.yf, "Ticker") as ticker:
            ticker.return_value.history.return_value = frame
            for minute, expected in (("09", "2026-11-25"), ("10", "2026-11-27")):
                clock.now.return_value = datetime.fromisoformat(f"2026-11-27T18:{minute}:00+00:00")
                self.assertEqual(portfolio.fetch_macd("TEST").index[-1].date().isoformat(), expected)
                self.assertEqual(self.client.get("/api/stock-detail/TEST").json["date"], expected)

    def test_unspecified_development_mode_keeps_original_endpoints(self):
        with patch.object(portfolio, "PUBLIC_SHARED", False):
            self.assertEqual(self.client.get("/api/site-mode").json, {"publicShared": False, "canEdit": True})
            data = {"symbol": "TEST", "shares": 2, "cost": 3}
            self.assertEqual(self.client.post("/api/holdings", json=data).status_code, 200)
            self.assertEqual(self.client.delete("/api/holdings/TEST").status_code, 200)


if __name__ == "__main__":
    unittest.main()
