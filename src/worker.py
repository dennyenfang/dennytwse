from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote

from workers import WorkerEntrypoint, Response, fetch


GROUPS = {
    "sp500": ("S&P 500", None, 500),
    "healthcare": ("Healthcare", "Healthcare", 50),
    "financial": ("Financials", "Financial Services", 10),
    "telecom": ("Telecom operators", "Telecom Services", 10),
    "consumer": ("Consumer products", "Consumer Defensive", 50),
    "energy": ("Energy", "Energy", 20),
}
LABELS = {
    "macd_positive": "MACD 線轉正",
    "macd_negative": "MACD 線轉負",
    "golden_cross": "MACD 黃金交叉",
    "death_cross": "MACD 死亡交叉",
}
SCHEMA_READY = False


def plain(value):
    return value.to_py() if hasattr(value, "to_py") else value


def valid_symbol(value):
    symbol = str(value or "").strip().upper()
    if not symbol or len(symbol) > 20 or not re.fullmatch(r"[A-Z0-9.^=-]+", symbol):
        raise ValueError("請輸入有效的美股代號，例如 AAPL 或 BRK-B。")
    return symbol


def ema(values, period):
    factor = 2 / (period + 1)
    result = []
    current = values[0]
    for value in values:
        current = value * factor + current * (1 - factor)
        result.append(current)
    return result


def latest_event(points, kind):
    for i in range(len(points) - 1, 0, -1):
        prev, cur = points[i - 1], points[i]
        if kind == "golden" and prev["macd"] <= prev["signal"] and cur["macd"] > cur["signal"]:
            return cur["date"]
        if kind == "death" and prev["macd"] >= prev["signal"] and cur["macd"] < cur["signal"]:
            return cur["date"]
        if kind == "positive" and prev["macd"] <= 0 < cur["macd"]:
            return cur["date"]
        if kind == "negative" and prev["macd"] >= 0 > cur["macd"]:
            return cur["date"]
    return ""


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url
        path = str(url.split("?", 1)[0].split("/", 3)[-1]) if "/" in url else ""
        # URL parsing is delegated to the JS URL implementation via the request.
        from js import URL
        parsed = URL.new(url)
        path = str(parsed.pathname)
        method = str(request.method).upper()
        try:
            await self.ensure_schema()
            if path == "/health":
                return Response.json({"ok": True, "worker": "denny888"})
            if path == "/api/holdings" and method == "GET":
                await self.sync_sheet()
                return Response.json(await self.all("SELECT symbol, shares, cost FROM holdings ORDER BY symbol"))
            if path == "/api/holdings" and method == "POST":
                data = plain(await request.json())
                symbol = valid_symbol(data.get("symbol"))
                shares, cost = float(data.get("shares", 0)), float(data.get("cost", 0))
                if shares <= 0 or cost < 0:
                    raise ValueError("股數須大於 0，持有成本不可小於 0。")
                await self.run("INSERT INTO holdings(symbol,shares,cost,source) VALUES(?,?,?,'app') ON CONFLICT(symbol) DO UPDATE SET shares=excluded.shares,cost=excluded.cost,source='app'", symbol, shares, cost)
                return Response.json({"symbol": symbol, "shares": shares, "cost": cost})
            if path.startswith("/api/holdings/") and method == "DELETE":
                symbol = valid_symbol(unquote(path.rsplit("/", 1)[-1]))
                await self.run("DELETE FROM holdings WHERE symbol=?", symbol)
                return Response.json({"ok": True})
            if path == "/api/sync-sheet" and method == "POST":
                result = await self.sync_sheet(force=True)
                return Response.json(result)
            if path == "/api/alerts" and method == "GET":
                rows = await self.all("SELECT id,symbol,kind,signal_date,macd,signal FROM alerts ORDER BY id DESC LIMIT 100")
                return Response.json([{"id": r["id"], "symbol": r["symbol"], "kind": r["kind"], "label": LABELS.get(r["kind"], r["kind"]), "date": r["signal_date"], "macd": r["macd"], "signal": r["signal"]} for r in rows])
            if path == "/api/quotes" and method == "GET":
                sheet = await self.sync_sheet()
                holdings = await self.all("SELECT symbol,shares,cost,source FROM holdings ORDER BY symbol")
                quotes, errors = [], []
                for h in holdings:
                    try:
                        data = await self.stock_data(h["symbol"])
                        p = data["points"][-1]
                        cost = float(h["cost"] or 0)
                        if h["source"] == "sheet" and cost <= 0:
                            cost = p["close"]
                            await self.run("UPDATE holdings SET cost=? WHERE symbol=? AND source='sheet' AND cost<=0", cost, h["symbol"])
                        shares = float(h["shares"] or 0)
                        pnl = (p["close"] - cost) * shares if shares and cost else None
                        quotes.append({"symbol": h["symbol"], "shares": shares, "cost": cost, "price": p["close"], "value": p["close"] * shares if shares else None, "pnl": pnl, "pnl_pct": (p["close"] / cost - 1) * 100 if cost else None, "macd": p["macd"], "signal": p["signal"], "histogram": p["histogram"], "date": p["date"]})
                    except Exception as error:
                        errors.append({"symbol": h["symbol"], "error": str(error)})
                return Response.json({"quotes": quotes, "errors": errors, "sheet": sheet, "holding_count": len(holdings)})
            if path == "/api/scan" and method == "POST":
                return Response.json(await self.scan_holdings())
            if path.startswith("/api/history/"):
                symbol = valid_symbol(unquote(path.rsplit("/", 1)[-1]))
                data = await self.stock_data(symbol)
                return Response.json({"symbol": symbol, "points": [{"date": p["date"], "close": p["close"], "macd": p["macd"], "signal": p["signal"], "histogram": p["histogram"]} for p in data["points"][-120:]]})
            if path.startswith("/api/stock-detail/"):
                symbol = valid_symbol(unquote(path.rsplit("/", 1)[-1]))
                data = await self.stock_data(symbol)
                return Response.json({"symbol": symbol, "date": data["points"][-1]["date"], "points": data["points"][-120:], "events": data["events"]})
            if path == "/api/market-scan" and method == "GET":
                return Response.json(await self.market_scan(parsed))
            asset = await self.env.ASSETS.fetch(request)
            return asset
        except ValueError as error:
            return Response.json({"error": str(error)}, status=400)
        except Exception as error:
            return Response.json({"error": str(error) or "服務暫時無法完成請求"}, status=503)

    async def all(self, sql, *values):
        result = await self.env.DB.prepare(sql).bind(*values).all()
        return plain(result.results) or []

    async def run(self, sql, *values):
        return await self.env.DB.prepare(sql).bind(*values).run()

    async def ensure_schema(self):
        global SCHEMA_READY
        if SCHEMA_READY:
            return
        await self.run("CREATE TABLE IF NOT EXISTS holdings(symbol TEXT PRIMARY KEY,shares REAL NOT NULL DEFAULT 0,cost REAL NOT NULL DEFAULT 0,source TEXT NOT NULL DEFAULT 'app')")
        await self.run("CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,symbol TEXT NOT NULL,kind TEXT NOT NULL,signal_date TEXT NOT NULL,macd REAL NOT NULL,signal REAL NOT NULL,created_at TEXT NOT NULL,UNIQUE(symbol,kind,signal_date))")
        await self.run("CREATE TABLE IF NOT EXISTS universe_cache(group_name TEXT PRIMARY KEY,symbols TEXT NOT NULL,updated_at TEXT NOT NULL)")
        await self.run("CREATE TABLE IF NOT EXISTS market_scan_results(group_name TEXT NOT NULL,as_of TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(group_name,as_of))")
        SCHEMA_READY = True

    async def get_json(self, url, init=None):
        response = await fetch(url, **(init or {}))
        if not response.ok:
            raise ValueError(f"資料來源回應 {response.status}")
        return plain(await response.json())

    async def stock_data(self, symbol):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=1y&interval=1d&events=history&includeAdjustedClose=true"
        payload = await self.get_json(url, {"headers": {"Accept": "application/json", "User-Agent": "Mozilla/5.0 Denny888/1.0"}})
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"{symbol} 找不到價格資料")
        item = result[0]
        quote_data = (item.get("indicators") or {}).get("quote", [{}])[0] or {}
        closes, opens, highs, lows, volumes = [quote_data.get(k) or [] for k in ("close", "open", "high", "low", "volume")]
        adjusted = ((item.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or closes
        timestamps = item.get("timestamp") or []
        rows = []
        for i, stamp in enumerate(timestamps):
            vals = (closes[i] if i < len(closes) else None, opens[i] if i < len(opens) else None, highs[i] if i < len(highs) else None, lows[i] if i < len(lows) else None)
            if any(v is None for v in vals):
                continue
            close = float(vals[0]); adj = adjusted[i] if i < len(adjusted) else close
            factor = float(adj) / close if close else 1
            rows.append({"date": datetime.fromtimestamp(stamp, timezone.utc).date().isoformat(), "open": float(vals[1]) * factor, "high": float(vals[2]) * factor, "low": float(vals[3]) * factor, "close": float(adj), "volume": float(volumes[i] or 0) if i < len(volumes) else 0})
        if len(rows) < 35:
            raise ValueError(f"{symbol} 日線資料不足")
        c = [r["close"] for r in rows]
        macd12, macd26 = ema(c, 12), ema(c, 26)
        macd = [a - b for a, b in zip(macd12, macd26)]
        signal = ema(macd, 9)
        points = []
        for i, row in enumerate(rows):
            points.append({**row, "macd": macd[i], "signal": signal[i], "histogram": macd[i] - signal[i]})
        events = []
        for i in range(1, len(points)):
            prev, cur = points[i - 1], points[i]
            for condition, kind, label in ((prev["macd"] <= prev["signal"] and cur["macd"] > cur["signal"], "golden", "黃金交叉"), (prev["macd"] >= prev["signal"] and cur["macd"] < cur["signal"], "death", "死亡交叉"), (prev["macd"] <= 0 < cur["macd"], "positive", "MACD 負轉正"), (prev["macd"] >= 0 > cur["macd"], "negative", "MACD 正轉負")):
                if condition:
                    events.append({"kind": kind, "label": label, "date": cur["date"]})
        return {"points": points, "events": list(reversed(events))}

    async def scan_holdings(self):
        await self.sync_sheet()
        holdings = await self.all("SELECT symbol FROM holdings ORDER BY symbol")
        alerts = []; errors = []
        for item in holdings:
            symbol = item["symbol"]
            try:
                points = (await self.stock_data(symbol))["points"]
                if len(points) < 2: continue
                a, b = points[-2], points[-1]; found = []
                if a["macd"] <= 0 < b["macd"]: found.append(("macd_positive", "MACD 線轉正"))
                if a["macd"] >= 0 > b["macd"]: found.append(("macd_negative", "MACD 線轉負"))
                if a["macd"] <= a["signal"] and b["macd"] > b["signal"]: found.append(("golden_cross", "MACD 黃金交叉"))
                if a["macd"] >= a["signal"] and b["macd"] < b["signal"]: found.append(("death_cross", "MACD 死亡交叉"))
                for kind, label in found:
                    inserted = await self.run("INSERT OR IGNORE INTO alerts(symbol,kind,signal_date,macd,signal,created_at) VALUES(?,?,?,?,?,?)", symbol, kind, b["date"], b["macd"], b["signal"], datetime.now(timezone.utc).isoformat())
                    meta = plain(getattr(inserted, "meta", None)) or {}
                    if meta.get("changes", 0): alerts.append({"symbol": symbol, "kind": kind, "label": label, "date": b["date"], "macd": round(b["macd"], 4), "signal": round(b["signal"], 4)})
            except Exception as error: errors.append({"symbol": symbol, "error": str(error)})
        return {"alerts": alerts, "errors": errors}

    async def sync_sheet(self, force=False):
        sheet_id = str(getattr(self.env, "SHEET_ID", "") or "")
        if not sheet_id: return {"count": 0, "new": 0, "error": None, "message": "尚未設定 Google Sheet"}
        gid = str(getattr(self.env, "SHEET_GID", "0") or "0")
        url = f"https://docs.google.com/spreadsheets/d/{quote(sheet_id)}/export?format=csv&gid={quote(gid)}"
        response = await fetch(url, headers={"User-Agent": "Denny888-PWA/1.0"})
        if not response.ok: return {"count": 0, "new": 0, "error": f"Google Sheet 回應 {response.status}"}
        text = await response.text(); reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not reader.fieldnames: return {"count": 0, "new": 0, "error": "Google Sheet 沒有標題列"}
        header = {str(x or "").strip().casefold(): x for x in reader.fieldnames}
        symcol = next((header[k] for k in ("代號", "股票代號", "ticker", "symbol") if k in header), None)
        qtycol = next((header[k] for k in ("股數", "數量", "shares") if k in header), None)
        costcol = next((header[k] for k in ("平均成本", "每股成本", "持有成本", "cost", "avg cost") if k in header), None)
        if not symcol: return {"count": 0, "new": 0, "error": "Google Sheet 找不到代號欄"}
        imported = 0; count = 0
        for row in rows:
            raw = str(row.get(symcol) or "").strip()
            if not raw: continue
            symbol = valid_symbol(raw); count += 1
            parse_num = lambda val: float(str(val or "").replace(",", "").replace("$", "").strip()) if str(val or "").strip() else None
            shares = parse_num(row.get(qtycol)) if qtycol else None
            cost = parse_num(row.get(costcol)) if costcol else None
            existing = await self.all("SELECT symbol FROM holdings WHERE symbol=?", symbol)
            if not existing:
                await self.run("INSERT INTO holdings(symbol,shares,cost,source) VALUES(?,?,?,'sheet')", symbol, shares if shares is not None else 5, cost or 0)
                imported += 1
        return {"count": count, "new": imported, "error": None}

    async def market_symbols(self, group):
        if group not in GROUPS: raise ValueError("未知的股票分類")
        cached = await self.all("SELECT symbols,updated_at FROM universe_cache WHERE group_name=?", group)
        if cached:
            updated = datetime.fromisoformat(cached[0]["updated_at"])
            if datetime.now(timezone.utc) - updated < timedelta(days=7): return json.loads(cached[0]["symbols"])
        label, sector, limit = GROUPS[group]
        if group == "sp500":
            response = await fetch("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers={"User-Agent": "Mozilla/5.0 Denny888/1.0"})
            html = await response.text(); symbols = []
            match = re.search(r'<table[^>]*id="constituents"[^>]*>(.*?)</table>', html, re.S)
            if not match: raise ValueError("暫時無法讀取 S&P 500 名單")
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", match.group(1), re.S):
                cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                if cells:
                    ticker = re.sub(r"<[^>]+>", "", cells[0]).strip()
                    if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", ticker): symbols.append(ticker.replace(".", "-"))
        else:
            sectors = ("Consumer Defensive", "Consumer Cyclical") if group == "consumer" else (sector,)
            collected = {}
            for sec in sectors:
                query = {"operator": "AND", "operands": [{"operator": "eq", "operands": ["region", "us"]}, {"operator": "eq", "operands": ["sector" if group != "telecom" else "industry", sec]}]}
                body = {"query": query, "quoteType": "EQUITY", "offset": 0, "size": 250, "sortField": "intradaymarketcap", "sortType": "DESC", "userId": "", "userIdType": "guid"}
                data = await self.get_json("https://query1.finance.yahoo.com/v1/finance/screener?corsDomain=finance.yahoo.com&formatted=false&lang=en-US&region=US", {"method": "POST", "headers": {"Content-Type": "application/json", "Accept": "application/json"}, "body": json.dumps(body)})
                quotes = (((data.get("finance") or {}).get("result") or [{}])[0].get("quotes") or [])
                for item in quotes:
                    if item.get("symbol"): collected[item["symbol"]] = item
            symbols = [item["symbol"] for item in sorted(collected.values(), key=lambda x: x.get("marketCap") or 0, reverse=True)[:limit]]
        symbols = list(dict.fromkeys(symbols))[:limit]
        if not symbols: raise ValueError(f"{label} 名單目前無法取得")
        await self.run("INSERT INTO universe_cache(group_name,symbols,updated_at) VALUES(?,?,?) ON CONFLICT(group_name) DO UPDATE SET symbols=excluded.symbols,updated_at=excluded.updated_at", group, json.dumps(symbols), datetime.now(timezone.utc).isoformat())
        return symbols

    async def market_scan(self, parsed):
        group = str(parsed.searchParams.get("group") or "healthcare")
        offset = int(parsed.searchParams.get("offset") or "0")
        limit = min(15, max(1, int(parsed.searchParams.get("limit") or "15")))
        symbols = await self.market_symbols(group)
        rows = []
        for symbol in symbols[offset:offset + limit]:
            try:
                points = (await self.stock_data(symbol))["points"]
                if len(points) < 35: continue
                current, previous = points[-1], points[-2]
                recent_volumes = points[-5:]
                baseline_volumes = points[-35:-5]
                baseline_volume = sum(p["volume"] for p in baseline_volumes) / 30
                max_volume_day = max(recent_volumes, key=lambda p: p["volume"])
                vol_pct = (max_volume_day["volume"] / baseline_volume - 1) * 100 if baseline_volume else 0
                spike = baseline_volume > 0 and max_volume_day["volume"] > 1.5 * baseline_volume
                avg_dollar = sum(p["close"] * p["volume"] for p in points[-20:]) / 20
                rows.append({"symbol": symbol, "close": current["close"], "change_pct": (current["close"] / previous["close"] - 1) * 100 if previous["close"] else 0,
                    "golden": latest_event(points, "golden"), "positive": latest_event(points, "positive"), "negative": latest_event(points, "negative"), "death": latest_event(points, "death"),
                    "volume": current["volume"], "vol_pct": vol_pct, "avg_dollar_volume": avg_dollar, "spike": spike,
                    "spike_dates": [max_volume_day["date"]] if spike else [], "macd": current["macd"], "signal": current["signal"], "date": current["date"]})
            except Exception:
                continue
        label = GROUPS[group][0]
        return {"group": group, "group_label": label, "as_of": max((r["date"] for r in rows), default=""), "rows": rows, "count": len(rows), "total": len(symbols), "offset": offset, "next_offset": offset + limit if offset + limit < len(symbols) else None, "volume_threshold": 1.5, "volume_window": 5, "volume_baseline_window": 30}
