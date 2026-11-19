"""Stock lists used by the closing scan, independent of the editable portfolio."""

import csv
import html
import io
import logging
import re
import urllib.request

import yfinance as yf


WATCHLIST_CSV = "https://docs.google.com/spreadsheets/d/16yDbTE_8iPUApTqiL3eZ_qRZnTwYricccNkztk2nqQM/export?format=csv&gid=0"
SP500_PAGE = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
SP500_MIRROR = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def fetch_text(url):
    request = urllib.request.Request(url, headers={"User-Agent": "Denny888/1.0 (+https://denny888.usdollar.win)"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8-sig")


def yahoo_symbol(value):
    symbol = value.strip().upper()
    symbol = re.sub(r"^(NASDAQ|NYSE|NYSEARCA|NYSEAMERICAN|AMEX):", "", symbol)
    if not re.fullmatch(r"[A-Z0-9^][A-Z0-9.^=\-]{0,19}", symbol):
        raise ValueError("無效的股票代號")
    # US share classes use a hyphen on Yahoo. Preserve other exchange suffixes.
    return re.sub(r"^([A-Z]+)\.([AB])$", r"\1-\2", symbol)


def watchlist_symbols(source):
    reader = csv.DictReader(io.StringIO(source))
    fields = {name.strip().casefold(): name for name in reader.fieldnames or []}
    column = next((fields[k] for k in ("代號", "股票代號", "ticker", "symbol") if k in fields), None)
    if column is None:
        raise ValueError("USTICK 缺少股票代號欄")
    symbols = []
    for number, row in enumerate(reader, start=2):
        value = (row.get(column) or "").strip()
        if value:
            try:
                symbols.append(yahoo_symbol(value))
            except ValueError as exc:
                raise ValueError(f"USTICK 第 {number} 列的股票代號無法辨識") from exc
    return list(dict.fromkeys(symbols))


def validate_sp500(values):
    members = list(dict.fromkeys(yahoo_symbol(value) for value in values))
    if len(members) < 490:
        raise ValueError(f"S&P 500 成分清單僅取得 {len(members)} 檔")
    return members


def sp500_symbols():
    try:
        page = fetch_text(SP500_PAGE)
        table = re.search(r'<table\b[^>]*\bid="constituents"[^>]*>(.*?)</table>', page, re.S)
        if not table:
            raise ValueError("找不到 S&P 500 成分表")
        cells = re.findall(r"<tr\b[^>]*>\s*<td\b[^>]*>(.*?)</td>", table[1], re.S)
        return validate_sp500(html.unescape(re.sub(r"<[^>]+>", "", cell)) for cell in cells)
    except (OSError, ValueError) as exc:
        logging.warning("S&P 500 constituent table unavailable (%s); trying CSV mirror", exc)
        rows = csv.DictReader(io.StringIO(fetch_text(SP500_MIRROR)))
        return validate_sp500(row["Symbol"] for row in rows)


def ranked_stocks(field, values, limit):
    matches = [yf.EquityQuery("eq", [field, value]) for value in values]
    classification = matches[0] if len(matches) == 1 else yf.EquityQuery("or", matches)
    query = yf.EquityQuery("and", [yf.EquityQuery("eq", ["region", "us"]), classification])
    result = yf.screen(query, size=limit, sortField="intradaymarketcap", sortAsc=False)
    members = list(dict.fromkeys(yahoo_symbol(item["symbol"]) for item in result["quotes"]))
    if len(members) != limit:
        raise ValueError(f"{', '.join(values)} 僅取得 {len(members)} / {limit} 檔")
    return members


def load_universe():
    return {
        "sp500": sp500_symbols(),
        "healthcare": ranked_stocks("sector", ["Healthcare"], 50),
        "financial": ranked_stocks("sector", ["Financial Services"], 10),
        "telecom": ranked_stocks("industry", ["Telecom Services"], 10),
        "consumer": ranked_stocks("sector", ["Consumer Defensive", "Consumer Cyclical"], 50),
        "energy": ranked_stocks("sector", ["Energy"], 20),
        "sheet": watchlist_symbols(fetch_text(WATCHLIST_CSV)),
    }
