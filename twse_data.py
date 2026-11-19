"""Official TWSE / TPEx daily price history adapters."""

import json
import re
import time
import urllib.parse
from datetime import date

import pandas as pd
import exchange_calendars as xcals
from http_data import get_bytes


TWSE_MONTH = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
TPEX_MONTH = "https://www.tpex.org.tw/www/zh-tw/afterTrading/tradingStock"
TWSE_BULK_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX_BULK_DAY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
_SCAN_CACHE = {}


def _get_json(url):
    return json.loads(get_bytes(url, timeout=25))


def _month_starts(month_count=14):
    today = date.today()
    year, month = today.year, today.month
    for _ in range(month_count):
        yield date(year, month, 1)
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)


def _roc_date(value):
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{2,3})[/年]?(\d{1,2})[/月]?(\d{1,2})日?", raw)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    if year < 1911:
        year += 1911
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _number(value):
    raw = str(value or "").strip().replace(",", "")
    if raw in {"", "--", "X", "-"}:
        return None
    return float(raw)


def _rows(payload):
    if isinstance(payload.get("data"), list):
        return payload.get("fields", []), payload["data"]
    for table in payload.get("tables", []):
        fields = [str(value).strip() for value in table.get("fields", [])]
        has_code = any("證券代號" in field or "代號" == field or field.casefold() == "code" for field in fields)
        has_close = any("收盤" in field or field.casefold() == "close" for field in fields)
        if has_code and has_close and isinstance(table.get("data"), list):
            return fields, table["data"]
    return [], []


def _bulk_rows(payload, *, tpex=False):
    fields, rows = _rows(payload)
    normalized = [str(field).strip().casefold() for field in fields]
    def column(*needles):
        return next((i for i, field in enumerate(normalized) if any(n in field for n in needles)), None)
    code_i = column("證券代號", "代號", "code")
    volume_i = column("成交股數", "成交仟股", "trading shares", "volume")
    open_i, high_i, low_i, close_i = (column(name) for name in ("開盤", "最高", "最低", "收盤"))
    if None in (code_i, volume_i, open_i, high_i, low_i, close_i):
        return []
    output = []
    for row in rows:
        try:
            code = str(row[code_i]).strip()
            if not re.fullmatch(r"(?:\d{4}|00\d{2,4}[A-Z]?)", code):
                continue
            vals = [_number(row[i]) for i in (open_i, high_i, low_i, close_i, volume_i)]
            if any(value is None for value in vals):
                continue
            opening, high, low, close, volume = vals
            if "仟股" in normalized[volume_i]:
                volume *= 1000
            output.append((code, opening, high, low, close, volume))
        except (IndexError, TypeError, ValueError):
            continue
    return output


def _daily_market(day, *, tpex=False):
    if tpex:
        params = {"date": day.strftime("%Y/%m/%d"), "type": "EW", "response": "json"}
        payload = _get_json(f"{TPEX_BULK_DAY}?{urllib.parse.urlencode(params)}")
        return _bulk_rows(payload, tpex=True)
    params = {"date": day.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"}
    payload = _get_json(f"{TWSE_BULK_DAY}?{urllib.parse.urlencode(params)}")
    return _bulk_rows(payload)


def _frame(rows, *, volume_in_thousands=False):
    output = []
    for row in rows:
        if len(row) < 7:
            continue
        day = _roc_date(row[0])
        if not day:
            continue
        try:
            volume = _number(row[1])
            opening, high, low, close = (_number(value) for value in row[3:7])
        except (TypeError, ValueError):
            continue
        if None in (volume, opening, high, low, close):
            continue
        if volume_in_thousands:
            volume *= 1000
        output.append((pd.Timestamp(day), opening, high, low, close, volume))
    if not output:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"], dtype=float)
    frame = pd.DataFrame(output, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    return frame.drop_duplicates("Date", keep="last").set_index("Date").sort_index()


def history(symbol, *, period="1y", start=None, end=None, lookback_months=14, **_ignored):
    """Return standard OHLCV bars, preferring TWSE and falling back to TPEx."""
    symbol = str(symbol).strip().upper()
    if not re.fullmatch(r"(?:\d{4}|00\d{2,4}[A-Z]?)", symbol):
        raise ValueError("台股代號需為 4 至 6 位數字，例如 2330。")
    twse_rows, tpex_rows, tpex_fields = [], [], []
    # Each official historical interface returns one calendar month per request.
    for month in _month_starts(lookback_months):
        stamp = month.strftime("%Y%m%d")
        qs = urllib.parse.urlencode({"date": stamp, "stockNo": symbol, "response": "json"})
        try:
            response = _get_json(f"{TWSE_MONTH}?{qs}")
            if str(response.get("stat", "")).upper() == "OK":
                twse_rows.extend(response.get("data", []))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if not twse_rows:
            tpqs = urllib.parse.urlencode({"code": symbol, "date": month.strftime("%Y/%m/%d"), "id": "", "response": "json"})
            try:
                response = _get_json(f"{TPEX_MONTH}?{tpqs}")
                if str(response.get("stat", "")).casefold() == "ok":
                    _fields, data = _rows(response)
                    tpex_fields = _fields or tpex_fields
                    tpex_rows.extend(data)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    volume_in_thousands = any("仟股" in str(field) for field in tpex_fields)
    frame = _frame(twse_rows) if twse_rows else _frame(tpex_rows, volume_in_thousands=volume_in_thousands)
    if start:
        frame = frame.loc[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame.loc[frame.index < pd.Timestamp(end)]
    if period.endswith("y"):
        frame = frame.tail(int(period[:-1]) * 250)
    return frame


def download(symbols, *, start=None, end=None, **kwargs):
    """Fetch bulk daily OHLCV from both exchanges for the scan calculation pipeline."""
    symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
    if not symbols:
        return pd.DataFrame()
    end_day = pd.Timestamp(end).date() - pd.Timedelta(days=1) if end else date.today()
    start_day = max(pd.Timestamp(start).date() if start else end_day - pd.Timedelta(days=140), end_day - pd.Timedelta(days=140))
    cache_key = (start_day.isoformat(), end_day.isoformat())
    cached = _SCAN_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 600:
        all_frames = cached[1]
    else:
        sessions = xcals.get_calendar("XTAI").sessions_in_range(
            pd.Timestamp(start_day), pd.Timestamp(end_day))
        collected = {}
        for session in sessions:
            day = session.date()
            for tpex in (False, True):
                try:
                    for code, opening, high, low, close, volume in _daily_market(day, tpex=tpex):
                        collected.setdefault(code, []).append((pd.Timestamp(day), opening, high, low, close, volume))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        all_frames = {}
        for code, rows in collected.items():
            all_frames[code] = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"]) \
                .drop_duplicates("Date", keep="last").set_index("Date").sort_index()
        _SCAN_CACHE.clear()
        _SCAN_CACHE[cache_key] = (time.monotonic(), all_frames)
    frames = {symbol: all_frames[symbol] for symbol in symbols if symbol in all_frames}
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)
