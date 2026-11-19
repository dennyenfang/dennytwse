"""Taiwan-market symbols and the C-column watchlist import."""

import csv
import io
import os
import re
import urllib.parse
import json

from http_data import get_bytes


SHEET_ID = os.environ.get("WATCHLIST_SHEET_ID", "1miH6Rv7AS0xI8bxM-9X-kHTErM3SuPmx-55xh4-nj28")
SHEET_GID = os.environ.get("WATCHLIST_SHEET_GID", "0")
TWSE_DAILY = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TPEX_DAILY = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
_NAME_CACHE = {"at": 0.0, "values": {}}


def fetch_json(url):
    return json.loads(get_bytes(url, timeout=30))


def fetch_sheet_column_c():
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?"
           f"{urllib.parse.urlencode({'format': 'csv', 'gid': SHEET_GID})}")
    source = get_bytes(url, timeout=25).decode("utf-8-sig")
    symbols = []
    for row in csv.reader(io.StringIO(source)):
        if len(row) < 3:
            continue
        symbol = row[2].strip().upper().replace(".0", "")
        if re.fullmatch(r"(?:\d{4}|00\d{2,4}[A-Z]?)", symbol):
            symbols.append(symbol)
    return list(dict.fromkeys(symbols))


def _field(row, *names):
    by_casefold = {str(key).strip().casefold(): value for key, value in row.items()}
    for name in names:
        value = by_casefold.get(name.casefold())
        if value is not None:
            return str(value).strip()
    return ""


def security_names(symbols):
    """Return official issuer names for requested TWSE/TPEx symbols, cached for one hour."""
    import time
    if time.monotonic() - _NAME_CACHE["at"] >= 3600 or not _NAME_CACHE["values"]:
        values = {}
        for rows in (fetch_json(TWSE_DAILY), fetch_json(TPEX_DAILY)):
            for row in rows:
                code = _field(row, "Code", "SecuritiesCompanyCode", "證券代號", "股票代號")
                name = _field(row, "Name", "CompanyName", "SecuritiesCompanyName", "公司名稱", "證券名稱", "名稱")
                if code and name:
                    values[code] = name
        _NAME_CACHE.update(at=time.monotonic(), values=values)
    return {symbol: _NAME_CACHE["values"].get(symbol, "") for symbol in symbols}


def _codes(rows):
    codes = []
    for row in rows:
        code = _field(row, "Code", "SecuritiesCompanyCode", "證券代號", "股票代號")
        if not re.fullmatch(r"(?:\d{4}|00\d{2,4}[A-Z]?)", code):
            continue
        # Taiwan ETF codes generally begin with 00; keep them in a separate block.
        codes.append(code)
    return list(dict.fromkeys(codes))


def load_universe():
    listed_all = _codes(fetch_json(TWSE_DAILY))
    otc_all = _codes(fetch_json(TPEX_DAILY))
    etf = [code for code in dict.fromkeys(listed_all + otc_all) if code.startswith("00")]
    return {
        "listed": [code for code in listed_all if not code.startswith("00")],
        "otc": [code for code in otc_all if not code.startswith("00")],
        "etf": etf,
        "sheet": fetch_sheet_column_c(),
    }
