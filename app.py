from __future__ import annotations

import sqlite3
import time
import csv
import io
import urllib.request
import math
import os
import re
from contextlib import closing, contextmanager
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path

import jwt
import market_scan_store
from scheduled_market_scan import latest_due_session
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from flask import Flask, g, has_request_context, jsonify, request, send_from_directory

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB_PATH = Path(os.environ.get("DENNY888_DB_PATH") or ROOT / "portfolio.db")
revision_file = ROOT / ".deploy-revision"
RELEASE_COMMIT = revision_file.read_text().strip() if revision_file.is_file() else None
app = Flask(__name__, static_folder="static")
PRICE_CACHE = {}
DETAIL_CACHE = {}
SHEET_ID = os.environ.get("SHEET_ID", "")
SHEET_GID = os.environ.get("SHEET_GID", "0")
SHEET_CACHE = {"at": 0.0, "count": 0, "error": None}
PUBLIC_SHARED = os.environ.get("DENNY888_PUBLIC_SHARED", "0").strip().lower() not in {"", "0", "false", "off"}
PUBLIC_EDITING = os.environ.get("DENNY888_PUBLIC_EDITING", "").strip() == "1"
ACCESS_TEAM = os.environ.get("DENNY888_ACCESS_TEAM_URL", "").rstrip("/")
ACCESS_AUD = os.environ.get("DENNY888_ACCESS_AUD", "")
EDITOR_EMAILS = {email.strip().casefold() for email in os.environ.get("DENNY888_EDITOR_EMAILS", "").split(",") if email.strip()}
PUBLIC_ORIGIN = os.environ.get("DENNY888_PUBLIC_ORIGIN", "").rstrip("/")


@lru_cache(maxsize=1)
def access_keys(team):
    return jwt.PyJWKClient(f"{team}/cdn-cgi/access/certs", timeout=5)


def verified_editor():
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token or not ACCESS_AUD or not 1 <= len(EDITOR_EMAILS) <= 2:
        return False
    if not re.fullmatch(r"https://[a-z0-9-]+\.cloudflareaccess\.com", ACCESS_TEAM):
        return False
    try:
        key = access_keys(ACCESS_TEAM).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256"], issuer=ACCESS_TEAM, audience=ACCESS_AUD,
                            options={"require": ["exp", "iat", "nbf", "sub", "email", "type"]})
    except (jwt.PyJWTError, ValueError, TypeError, OSError):
        return False
    email = claims["email"]
    return claims["type"] == "app" and bool(claims["sub"]) and isinstance(email, str) and email.casefold() in EDITOR_EMAILS


@app.before_request
def protect_shared_portfolio():
    if not PUBLIC_SHARED:
        return
    managing = request.path == "/manage" or request.path.startswith("/manage/")
    if managing:
        if not PUBLIC_EDITING and not verified_editor():
            return jsonify(error="此頁面僅供授權管理員使用。"), 403
        g.editor = True
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        if managing:
            if not PUBLIC_ORIGIN.startswith("https://") or request.headers.get("Origin") != PUBLIC_ORIGIN:
                return jsonify(error="請從本站管理頁面送出變更。"), 403
            g.write_allowed = True
        elif request.path != "/api/scan" or request.method != "POST":
            return jsonify(error="此投資組合僅供公開查閱。"), 403


def read_only_request():
    return PUBLIC_SHARED and has_request_context() and not g.get("write_allowed", False)


def market_error(exc):
    if read_only_request() or (PUBLIC_SHARED and PUBLIC_EDITING):
        return "暫時無法取得此查詢的行情資料。"
    return str(exc)


@contextmanager
def connect():
    if read_only_request():
        db = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    with closing(db), db:
        yield db


def init_db():
    with connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS holdings (symbol TEXT PRIMARY KEY, shares REAL NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'app')")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(holdings)").fetchall()}
        if "source" not in columns:
            db.execute("ALTER TABLE holdings ADD COLUMN source TEXT NOT NULL DEFAULT 'app'")
        db.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, kind TEXT NOT NULL,
            signal_date TEXT NOT NULL, macd REAL NOT NULL, signal REAL NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(symbol, kind, signal_date))""")
        market_scan_store.init_schema(db)


def sync_sheet_holdings(force=False):
    """Import rows from the user's link-shared Google Sheet without deleting app data."""
    if not SHEET_ID.strip():
        return {**SHEET_CACHE, "configured": False, "new": 0}
    now = time.monotonic()
    if not force and now - SHEET_CACHE["at"] < 300:
        return dict(SHEET_CACHE)
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "MACD-Portfolio-PWA/1.0"})
        with urllib.request.urlopen(req, timeout=15) as response:
            text = response.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not reader.fieldnames:
            raise ValueError("Google Sheet 沒有可讀取的標題列。")
        header_map = {str(name or "").strip().casefold(): name for name in reader.fieldnames}
        def find_header(*names):
            return next((header_map[n.casefold()] for n in names if n.casefold() in header_map), None)
        symbol_col = find_header("代號", "股票代號", "ticker", "symbol")
        shares_col = find_header("股數", "數量", "shares")
        cost_col = find_header("平均成本", "每股成本", "持有成本", "cost", "avg cost")
        if not symbol_col:
            raise ValueError("Google Sheet 找不到「代號」欄。")
        imported = 0
        with connect() as db:
            for row in rows:
                raw_symbol = str(row.get(symbol_col, "")).strip()
                if not raw_symbol:
                    continue
                symbol = valid_symbol(raw_symbol)
                raw_shares = str(row.get(shares_col, "")).strip() if shares_col else ""
                raw_cost = str(row.get(cost_col, "")).strip() if cost_col else ""
                shares = float(raw_shares.replace(",", "")) if raw_shares else None
                cost = float(raw_cost.replace(",", "").replace("$", "")) if raw_cost else None
                existing = db.execute("SELECT shares,cost,source FROM holdings WHERE symbol=?", (symbol,)).fetchone()
                if existing is None:
                    db.execute("INSERT INTO holdings(symbol,shares,cost,source) VALUES(?,?,?,?)", (symbol, shares if shares is not None else 5, cost or 0, "sheet"))
                    imported += 1
                elif existing["source"] == "sheet" and (existing["shares"] <= 0 or (shares is not None or cost is not None)):
                    db.execute("UPDATE holdings SET shares=?,cost=? WHERE symbol=?", (shares if shares is not None else 5 if existing["shares"] <= 0 else existing["shares"], cost if cost is not None else existing["cost"], symbol))
        SHEET_CACHE.update({"at": now, "count": len([r for r in rows if str(r.get(symbol_col, "")).strip()]), "error": None})
    except Exception as exc:
        error = "暫時無法同步已設定的試算表。" if PUBLIC_SHARED and PUBLIC_EDITING else str(exc)
        SHEET_CACHE.update({"at": now, "count": 0, "error": error})
    return {**SHEET_CACHE, "new": imported if SHEET_CACHE["error"] is None else 0}


def valid_symbol(value):
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 20 or not all(c.isalnum() or c in ".-^=" for c in symbol):
        raise ValueError("請輸入有效的美股代號，例如 AAPL 或 BRK-B。")
    return symbol


def holding_dict(row):
    return {"symbol": row["symbol"], "shares": row["shares"], "cost": row["cost"]}


@app.after_request
def private_response_headers(response):
    if request.path.startswith(("/api/", "/manage")) or request.path == "/healthz":
        response.headers["Cache-Control"] = "private, no-store"
    if request.path.startswith("/manage"):
        response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/")
@app.get("/manage/", strict_slashes=False)
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/site-mode")
@app.get("/manage/api/site-mode")
def site_mode():
    can_edit = not PUBLIC_SHARED or g.get("editor", False) or (not PUBLIC_EDITING and verified_editor())
    return jsonify(publicShared=PUBLIC_SHARED, canEdit=can_edit)


@app.get("/healthz")
def health():
    try:
        with connect() as db:
            db.execute("SELECT symbol FROM holdings LIMIT 1").fetchone()
    except sqlite3.Error:
        return jsonify(status="unavailable", commit=RELEASE_COMMIT), 503
    return jsonify(status="ok", commit=RELEASE_COMMIT)


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(ROOT / "static", "manifest.webmanifest")


@app.get("/icon.svg")
def icon():
    return send_from_directory(ROOT / "static", "icon.svg")


@app.get("/favicon.ico")
def favicon():
    return send_from_directory(ROOT / "static", "favicon.ico", mimetype="image/x-icon")


@app.get("/sw.js")
def service_worker():
    response = send_from_directory(ROOT / "static", "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/api/holdings")
def list_holdings():
    if not read_only_request():
        sync_sheet_holdings()
    with connect() as db:
        rows = db.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    return jsonify([holding_dict(row) for row in rows])


@app.post("/api/holdings")
@app.post("/manage/api/holdings")
def save_holding():
    if PUBLIC_SHARED and PUBLIC_EDITING and not request.is_json:
        return jsonify(error="請使用 JSON 格式送出持股。"), 415
    try:
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            raise ValueError("請提供股票代號、股數及成本。")
        symbol = valid_symbol(data.get("symbol"))
        shares, cost = float(data.get("shares")), float(data.get("cost"))
        if not math.isfinite(shares) or not math.isfinite(cost) or shares <= 0 or cost < 0:
            raise ValueError("股數與持有成本須為有限數值，股數須大於 0，成本不可小於 0。")
        with connect() as db:
            db.execute("INSERT INTO holdings(symbol, shares, cost) VALUES(?,?,?) ON CONFLICT(symbol) DO UPDATE SET shares=excluded.shares, cost=excluded.cost", (symbol, shares, cost))
        return jsonify({"symbol": symbol, "shares": shares, "cost": cost})
    except (ValueError, TypeError, OverflowError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/holdings/<symbol>")
@app.delete("/manage/api/holdings/<symbol>")
def delete_holding(symbol):
    try:
        symbol = valid_symbol(symbol)
        with connect() as db:
            db.execute("DELETE FROM holdings WHERE symbol=?", (symbol,))
        return jsonify({"ok": True})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


def fetch_macd(symbol):
    session = latest_due_session(datetime.now(timezone.utc))
    cached = PRICE_CACHE.get(symbol)
    if cached and cached[1] == session and time.monotonic() - cached[0] < 240:
        return cached[2]
    frame = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
    if frame.empty or "Close" not in frame:
        raise ValueError("找不到價格資料，請確認代號。")
    close = frame.loc[:session.isoformat(), "Close"].dropna().tail(market_scan_store.PRICE_WINDOW)
    if len(close) < 35:
        raise ValueError("可用的日線資料不足。")
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    values = pd.DataFrame({"close": close, "macd": macd, "signal": signal}).dropna()
    PRICE_CACHE[symbol] = (time.monotonic(), session, values)
    return values


@app.get("/api/market-scan")
@app.get("/api/market-scan/latest")
def market_scan():
    try:
        payload = market_scan_store.read_scan(DB_PATH, request.args.get("group", "all"), request.args.get("as_of"))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except sqlite3.Error:
        return jsonify(status="unavailable", error="暫時無法讀取已儲存的行情掃描。"), 503
    return jsonify(payload), 200 if payload["status"] == "complete" else 503


@app.post("/api/scan")
@app.post("/manage/api/scan")
def scan():
    read_only = read_only_request()
    if not read_only:
        sync_sheet_holdings()
    with connect() as db:
        holdings = db.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    problems, new_alerts = [], []
    for holding in holdings:
        symbol = holding["symbol"]
        try:
            values = fetch_macd(symbol)
            previous, current = values.iloc[-2], values.iloc[-1]
            signal_date = values.index[-1].date().isoformat()
            price = float(current["close"])
            events = []
            if previous.macd <= 0 < current.macd:
                events.append(("macd_positive", "MACD 線轉正"))
            if previous.macd >= 0 > current.macd:
                events.append(("macd_negative", "MACD 線轉負"))
            if previous.macd <= previous.signal and current.macd > current.signal:
                events.append(("golden_cross", "MACD 黃金交叉"))
            if previous.macd >= previous.signal and current.macd < current.signal:
                events.append(("death_cross", "MACD 死亡交叉"))
            for kind, label in events:
                if not read_only:
                    with connect() as db:
                        cursor = db.execute("INSERT OR IGNORE INTO alerts(symbol,kind,signal_date,macd,signal,created_at) VALUES(?,?,?,?,?,?)", (symbol, kind, signal_date, float(current.macd), float(current.signal), datetime.now(timezone.utc).isoformat()))
                    if not cursor.rowcount:
                        continue
                new_alerts.append({"symbol": symbol, "kind": kind, "label": label, "date": signal_date, "macd": round(float(current.macd), 4), "signal": round(float(current.signal), 4)})
        except Exception as exc:
            problems.append({"symbol": symbol, "error": market_error(exc)})
    return jsonify({"alerts": new_alerts, "errors": problems})


@app.get("/api/alerts")
def list_alerts():
    with connect() as db:
        rows = db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100").fetchall()
    labels = {"macd_positive": "MACD 線轉正", "macd_negative": "MACD 線轉負", "golden_cross": "MACD 黃金交叉", "death_cross": "MACD 死亡交叉"}
    return jsonify([{"id": row["id"], "symbol": row["symbol"], "kind": row["kind"], "label": labels[row["kind"]], "date": row["signal_date"], "macd": row["macd"], "signal": row["signal"]} for row in rows])


@app.get("/api/quotes")
def quotes():
    read_only = read_only_request()
    sheet = {} if read_only else sync_sheet_holdings()
    with connect() as db:
        holdings = db.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    results, errors = [], []
    for holding in holdings:
        try:
            values = fetch_macd(holding["symbol"])
            latest = values.iloc[-1]
            shares, cost = float(holding["shares"]), float(holding["cost"])
            price = float(latest.close)
            if not read_only and holding["source"] == "sheet" and cost <= 0:
                cost = price
                with connect() as db:
                    db.execute("UPDATE holdings SET cost=? WHERE symbol=? AND source='sheet' AND cost<=0", (cost, holding["symbol"]))
            results.append({"symbol": holding["symbol"], "shares": shares, "cost": cost, "price": price, "value": price * shares if shares else None, "pnl": (price - cost) * shares if shares and cost else None, "pnl_pct": ((price / cost) - 1) * 100 if cost else None, "macd": float(latest.macd), "signal": float(latest.signal), "histogram": float(latest.macd - latest.signal), "date": values.index[-1].date().isoformat()})
        except Exception as exc:
            errors.append({"symbol": holding["symbol"], "error": market_error(exc)})
    return jsonify({"quotes": results, "errors": errors, "sheet": sheet, "holding_count": len(holdings)})


@app.post("/api/sync-sheet")
@app.post("/manage/api/sync-sheet")
def sync_sheet_route():
    return jsonify(sync_sheet_holdings(force=True))


@app.get("/api/history/<symbol>")
def macd_history(symbol):
    try:
        symbol = valid_symbol(symbol)
        values = fetch_macd(symbol).tail(120)
        return jsonify({"symbol": symbol, "points": [{
            "date": idx.date().isoformat(), "close": float(row.close),
            "macd": float(row.macd), "signal": float(row.signal),
            "histogram": float(row.macd - row.signal)
        } for idx, row in values.iterrows()]})
    except Exception as exc:
        return jsonify({"error": market_error(exc)}), 400


@app.get("/api/stock-detail/<symbol>")
def stock_detail(symbol):
    try:
        symbol = valid_symbol(symbol)
        session = latest_due_session(datetime.now(timezone.utc))
        cached = DETAIL_CACHE.get(symbol)
        if cached and cached[1] == session and time.monotonic() - cached[0] < 240:
            return jsonify(cached[2])
        frame = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=True)
        if frame.empty or not {"Open", "High", "Low", "Close", "Volume"}.issubset(frame.columns):
            raise ValueError("找不到完整的日線 OHLCV 資料。")
        frame = frame.loc[:session.isoformat()].dropna(subset=["Close"]).tail(market_scan_store.PRICE_WINDOW)
        close = frame["Close"]
        macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        signal = macd.ewm(span=9, adjust=False).mean()
        frame = frame.tail(120).copy()
        frame["MACD"] = macd.reindex(frame.index)
        frame["Signal"] = signal.reindex(frame.index)
        frame["Histogram"] = frame["MACD"] - frame["Signal"]
        points = [{"date": idx.date().isoformat(), "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]), "volume": float(row["Volume"]),
            "macd": float(row["MACD"]), "signal": float(row["Signal"]), "histogram": float(row["Histogram"])}
            for idx, row in frame.iterrows()]
        events = []
        for i in range(1, len(points)):
            a, b = points[i - 1], points[i]
            if a["macd"] <= a["signal"] and b["macd"] > b["signal"]:
                events.append({"kind": "golden", "label": "黃金交叉", "date": b["date"]})
            if a["macd"] >= a["signal"] and b["macd"] < b["signal"]:
                events.append({"kind": "death", "label": "死亡交叉", "date": b["date"]})
            if a["macd"] <= 0 < b["macd"]:
                events.append({"kind": "positive", "label": "MACD 負轉正", "date": b["date"]})
            if a["macd"] >= 0 > b["macd"]:
                events.append({"kind": "negative", "label": "MACD 正轉負", "date": b["date"]})
        result = {"symbol": symbol, "date": points[-1]["date"] if points else "", "points": points, "events": list(reversed(events))}
        DETAIL_CACHE[symbol] = (time.monotonic(), session, result)
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": market_error(exc)}), 400


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5001, debug=False)
