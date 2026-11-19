"""Compute one closed-session scan and publish it transactionally for web readers."""

import json
import math
import sqlite3
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


GROUP_LABELS = {
    "sp500": "S&P 500", "healthcare": "醫療保健", "financial": "金融",
    "telecom": "電信營運商", "consumer": "消費品", "energy": "能源", "sheet": "USTICK",
}
PRICE_WINDOW = 200
EVENT_WINDOW = 3
VOLUME_WINDOW = 5
VOLUME_BASELINE_WINDOW = 30


class ScanUnavailable(RuntimeError):
    """A scheduled run could not publish a usable market snapshot."""


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS daily_market_scans (
        session_date TEXT PRIMARY KEY, published_at TEXT NOT NULL, members TEXT NOT NULL,
        results TEXT NOT NULL, missing TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS market_scan_attempts (
        id INTEGER PRIMARY KEY, session_date TEXT NOT NULL, started_at TEXT NOT NULL,
        finished_at TEXT, status TEXT NOT NULL, target_count INTEGER NOT NULL DEFAULT 0,
        result_count INTEGER NOT NULL DEFAULT 0, error TEXT, missing TEXT NOT NULL DEFAULT '{}')""")
    # NULL pending means a confirmation has started but none has been published yet.
    db.execute("""CREATE TABLE IF NOT EXISTS market_scan_confirmations (
        session_date TEXT PRIMARY KEY, first_attempt_id INTEGER NOT NULL, pending TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS market_bars (
        symbol TEXT NOT NULL, day TEXT NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
        PRIMARY KEY(symbol,day))""")


@contextmanager

def _db(path, *, readonly=False):
    address = Path(path).resolve().as_uri() + "?mode=ro" if readonly else str(path)
    with closing(sqlite3.connect(address, uri=readonly, timeout=30)) as db, db:
        db.row_factory = sqlite3.Row
        yield db


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latest_completed_session(db_path):
    if not Path(db_path).is_file():
        return None
    with _db(db_path, readonly=True) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_market_scans'").fetchone():
            return None
        latest = db.execute("SELECT MAX(session_date) FROM daily_market_scans").fetchone()[0]
    return date.fromisoformat(latest) if latest else None


def _confirmation(db, day):
    # Web requests and the scheduler also read snapshots written before this table existed.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_scan_confirmations'").fetchone():
        return None
    return db.execute("SELECT * FROM market_scan_confirmations WHERE session_date=?", (day,)).fetchone()

def needs_scheduled_scan(db_path, session_date, *, phase="preliminary", max_partial_retries=2):
    if phase not in ("preliminary", "confirmation"):
        raise ValueError("未知的掃描階段。")
    latest = latest_completed_session(db_path)
    if latest != session_date:
        return latest is None or latest < session_date
    day = session_date.isoformat()
    with _db(db_path, readonly=True) as db:
        db.execute("BEGIN")
        confirmation = _confirmation(db, day)
        if phase == "confirmation":
            if confirmation is None:
                return True
            if confirmation["pending"] == "[]":
                return False
            retries = db.execute("SELECT COUNT(*) FROM market_scan_attempts WHERE session_date=? AND id>?",
                                 (day, confirmation["first_attempt_id"])).fetchone()[0]
            return retries < max_partial_retries
        if confirmation is not None:
            return False
        snapshot = db.execute("SELECT missing FROM daily_market_scans WHERE session_date=?", (day,)).fetchone()
        if not json.loads(snapshot["missing"]):
            return False
        attempts = db.execute("""SELECT COUNT(*) FROM market_scan_attempts WHERE session_date=? AND id >
                (SELECT MIN(id) FROM market_scan_attempts WHERE session_date=? AND status='complete')""",
                              (day, day)).fetchone()[0]
        return attempts < max_partial_retries

def _load_groups(loader):
    if loader is None:
        from market_universe import load_universe
        loader = load_universe
    try:
        raw = loader()
        groups = {}
        for name in GROUP_LABELS:
            symbols = raw[name]
            if not isinstance(symbols, (list, tuple)) or not all(isinstance(s, str) and s for s in symbols):
                raise ValueError(name)
            groups[name] = list(dict.fromkeys(symbols))
            if name != "sheet" and not groups[name]:
                raise ValueError(name)
        return groups
    except Exception as exc:
        raise ScanUnavailable("無法取得完整的掃描名單。") from exc


def _symbol_frame(frame, symbol, batch_size):
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None
    if not isinstance(frame.columns, pd.MultiIndex):
        return frame if batch_size == 1 else None
    for level in range(frame.columns.nlevels):
        if symbol in frame.columns.get_level_values(level):
            return frame.xs(symbol, axis=1, level=level)
    return None


def _extend_confirmed_history(db_path, symbol, fresh, published_count):
    """Reuse the older prefix only when the fresh bars match the published series' suffix."""
    if len(fresh) < 2 or not 35 <= published_count <= PRICE_WINDOW:
        return fresh
    with _db(db_path, readonly=True) as db:
        previous = db.execute("""SELECT day,close,volume FROM market_bars WHERE symbol=? AND day<=?
                                 ORDER BY day DESC LIMIT ?""",
                              (symbol, fresh.index[-1].date().isoformat(), published_count)).fetchall()
    if len(previous) != published_count:
        return fresh
    stored = pd.DataFrame.from_records(previous[::-1], columns=["day", "Close", "Volume"])
    stored.index = pd.to_datetime(stored.pop("day"))
    overlap = stored.tail(len(fresh))
    if not overlap.index.equals(fresh.index) or not np.allclose(
            overlap["Close"], fresh["Close"], rtol=0, atol=0.00001):
        return fresh
    # Use the complete fresh suffix, even when a value is missing. Validation
    # below must reject an unusable new bar instead of substituting an old value.
    return pd.concat([stored.iloc[:-len(fresh)], fresh])

def _calculate(symbol, frame, session, *, db_path=None, published_count=0):
    if frame is None or not {"Close", "Volume"}.issubset(frame.columns):
        raise ValueError("無可用日線")
    data = frame[["Close", "Volume"]].apply(pd.to_numeric, errors="coerce").copy()
    # Daily labels are exchange-local trading dates. Do not convert midnight to a different timezone.
    dates = pd.DatetimeIndex(data.index).tz_localize(None).normalize()
    data.index = dates
    data = data[~data.index.duplicated(keep="last")].sort_index().loc[:session.isoformat()]
    data = data.replace([np.inf, -np.inf], np.nan).dropna(subset=["Close"]).tail(PRICE_WINDOW)
    if data.empty:
        raise ValueError("無可用日線")
    if data.index[-1].date() != session:
        raise ValueError(f"缺少當日收盤，最近資料為 {data.index[-1].date().isoformat()}")
    fresh_count = len(data)
    required_volume_bars = VOLUME_WINDOW + VOLUME_BASELINE_WINDOW
    if fresh_count < required_volume_bars and published_count:
        data = _extend_confirmed_history(db_path, symbol, data, published_count)
    if len(data) < required_volume_bars:
        raise ValueError(f"日線不足 {required_volume_bars} 根（目前 {len(data)} 根）")
    close, volume = data["Close"], data["Volume"]
    if (close <= 0).any() or volume.tail(required_volume_bars).isna().any() or (volume.tail(required_volume_bars) < 0).any():
        raise ValueError("價格或成交量不完整")
    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    previous_macd, previous_signal = macd.shift(), signal.shift()
    crosses = {
        "golden": (previous_macd <= previous_signal) & (macd > signal),
        "death": (previous_macd >= previous_signal) & (macd < signal),
        "positive": (previous_macd <= 0) & (macd > 0),
        "negative": (previous_macd >= 0) & (macd < 0),
    }
    events = {}
    for kind, condition in crosses.items():
        recent = condition.tail(EVENT_WINDOW)
        days = recent.index[recent]
        events[kind] = days[-1].date().isoformat() if len(days) else ""
    # Compare the highest-volume day in the latest five sessions with the prior 30-session average.
    baseline_volume = float(volume.iloc[-required_volume_bars:-VOLUME_WINDOW].mean())
    last_volume = float(volume.iloc[-1])
    recent_volumes = volume.iloc[-VOLUME_WINDOW:]
    highest_volume_date = recent_volumes.idxmax()
    highest_volume = float(recent_volumes.loc[highest_volume_date])
    volume_pct = (highest_volume / baseline_volume - 1) * 100 if baseline_volume else 0.0
    spike_dates = [highest_volume_date.date().isoformat()] if baseline_volume and highest_volume > 1.5 * baseline_volume else []
    row = dict(symbol=symbol, date=session.isoformat(), close=float(close.iloc[-1]),
               change_pct=float((close.iloc[-1] / close.iloc[-2] - 1) * 100),
               volume=last_volume, vol_pct=volume_pct, spike=bool(spike_dates), spike_dates=spike_dates,
               avg_dollar_volume=float((close * volume).tail(20).mean()), macd=float(macd.iloc[-1]),
               signal=float(signal.iloc[-1]), bar_count=len(close), **events)
    if len(data) > fresh_count:
        row["stored_history_bars"] = len(data) - fresh_count
    if any(not math.isfinite(value) for value in row.values() if isinstance(value, float)):
        raise ValueError("無法計算有限的行情指標")
    bars = [(symbol, index.date().isoformat(), float(price), float(turnover))
            for index, price, turnover in data.itertuples() if pd.notna(turnover) and turnover >= 0]
    return row, bars

def _download_results(symbols, session, download, *, db_path, published_rows):
    rows, bars, missing = {}, [], {}
    pending = list(symbols)
    for size in (40, 12, 1):
        # A provider-wide outage should not trigger hundreds of single-name requests.
        if not pending or (size == 1 and len(pending) > 32):
            break
        for start in range(0, len(pending), size):
            batch = pending[start:start + size]
            try:
                frame = download(batch, start=(session - timedelta(days=390)).isoformat(),
                                 end=(session + timedelta(days=1)).isoformat(), interval="1d", auto_adjust=True,
                                 group_by="ticker", threads=4, progress=False, timeout=25, multi_level_index=True)
            except Exception:
                frame = None
            for symbol in batch:
                try:
                    # Retry the provider for full history before considering the stored prefix.
                    published = published_rows.get(symbol, {}) if size < 40 else {}
                    row, history = _calculate(symbol, _symbol_frame(frame, symbol, len(batch)), session,
                                              db_path=db_path, published_count=published.get("bar_count", 0))
                except ValueError as exc:
                    reason = str(exc) if str(exc).startswith(("無", "缺少", "日線", "價格")) else "無法讀取日線資料"
                    if reason != "無可用日線" or symbol not in missing:
                        missing[symbol] = reason
                else:
                    rows[symbol] = row
                    bars.extend(history)
                    missing.pop(symbol, None)
        pending = list(missing)
    return rows, bars, missing

def _coverage_failure(groups, rows):
    benchmark = groups["sp500"]
    available = sum(symbol in rows for symbol in benchmark)
    if available < math.ceil(0.90 * len(benchmark)):
        return f"當日行情覆蓋不足：S&P 500 {available}/{len(benchmark)}"
    return None


def run_scan(db_path, session_date, *, phase="preliminary", load_universe=None, download=None):
    """Publish one session; the first confirmation re-fetches the entire initial roster."""
    if type(session_date) is not date:
        raise TypeError("session_date must be a date")
    if phase not in ("preliminary", "confirmation"):
        raise ValueError("未知的掃描階段。")
    # This lock database has no portfolio data. SQLite releases its lock after a process crash.
    lock_path = str(Path(db_path).resolve()) + ".scan-lock"
    with closing(sqlite3.connect(lock_path, timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            raise ScanUnavailable("已有掃描正在執行。") from exc
        return _run_locked(db_path, session_date, phase, load_universe, download or yf.download)

def _run_locked(db_path, session, phase, loader, download):
    day = session.isoformat()
    with _db(db_path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        init_schema(db)
        db.execute("""UPDATE market_scan_attempts SET status='interrupted', finished_at=?,
                      error='上次掃描未完成。' WHERE status='running'""", (_now(),))
        saved = db.execute("SELECT * FROM daily_market_scans WHERE session_date=?", (day,)).fetchone()
        confirmation = _confirmation(db, day)
        groups = json.loads(saved["members"]) if saved else None
        rows = json.loads(saved["results"]) if saved else {}
        missing = json.loads(saved["missing"]) if saved else {}
        pending = json.loads(confirmation["pending"]) if confirmation and confirmation["pending"] is not None else None
        if saved and ((phase == "preliminary" and (confirmation or not missing)) or pending == []):
            return dict(status="skipped", phase=phase, as_of=day, count=len(rows), target_count=len(rows) + len(missing),
                        missing_count=len(missing))
        attempt = db.execute("INSERT INTO market_scan_attempts(session_date,started_at,status) VALUES(?,?,'running')",
                             (day, _now())).lastrowid
        if phase == "confirmation" and confirmation is None:
            db.execute("INSERT INTO market_scan_confirmations(session_date,first_attempt_id) VALUES(?,?)", (day, attempt))
    try:
        if groups is None:
            groups = _load_groups(loader)
        targets = list(dict.fromkeys(s for symbols in groups.values() for s in symbols))
        with _db(db_path) as db:
            db.execute("UPDATE market_scan_attempts SET target_count=?, result_count=? WHERE id=?",
                       (len(targets), len(rows), attempt))
        if phase == "confirmation":
            requested = targets if pending is None else pending
        else:
            requested = [s for s in targets if s not in rows]
        fresh, bars, missing = _download_results(requested, session, download, db_path=db_path,
                                                published_rows=rows if phase == "confirmation" else {})
        if phase == "confirmation" and not fresh:
            raise ScanUnavailable("確認掃描未取得新的行情。")
        rows.update(fresh)
        coverage = set(targets) - missing.keys() if phase == "confirmation" else rows
        failure = _coverage_failure(groups, coverage)
        if failure:
            raise ScanUnavailable(failure)
        finished = _now()
        unavailable = {s: reason for s, reason in missing.items() if s not in rows}
        with _db(db_path) as db:
            db.executemany("""INSERT INTO market_bars(symbol,day,close,volume) VALUES(?,?,?,?)
                            ON CONFLICT(symbol,day) DO UPDATE SET close=excluded.close,volume=excluded.volume
                            WHERE market_bars.close != excluded.close OR market_bars.volume != excluded.volume""", bars)
            db.execute("""INSERT INTO daily_market_scans(session_date,published_at,members,results,missing)
                        VALUES(?,?,?,?,?) ON CONFLICT(session_date) DO UPDATE SET
                        published_at=excluded.published_at,results=excluded.results,missing=excluded.missing""",
                       (day, finished, json.dumps(groups), json.dumps(rows, allow_nan=False), json.dumps(unavailable)))
            if phase == "confirmation":
                db.execute("UPDATE market_scan_confirmations SET pending=? WHERE session_date=?",
                           (json.dumps(list(missing)), day))
            db.execute("""UPDATE market_scan_attempts SET status='complete',finished_at=?,result_count=?,missing=?
                        WHERE id=?""", (finished, len(rows), json.dumps(missing), attempt))
        return dict(status="complete", phase=phase, as_of=day, count=len(rows), target_count=len(targets),
                    missing_count=len(unavailable), confirmation_pending_count=len(missing) if phase == "confirmation" else None)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ScanUnavailable) else "掃描未完成，請查看排程紀錄。"
        with _db(db_path) as db:
            db.execute("""UPDATE market_scan_attempts SET status='failed',finished_at=?,result_count=?,error=?,missing=?
                        WHERE id=?""", (_now(), len(rows), message, json.dumps(missing), attempt))
        if isinstance(exc, ScanUnavailable):
            raise
        raise ScanUnavailable(message) from exc

def read_scan(db_path, group="all", as_of=None):
    """Return one coherent persisted publication; no source or quote calls on the request path."""
    if group != "all" and group not in GROUP_LABELS:
        raise ValueError("未知的股票分類。")
    if as_of:
        try:
            as_of = date.fromisoformat(as_of).isoformat()
        except ValueError:
            raise ValueError("資料日期須為 YYYY-MM-DD。") from None
    with _db(db_path, readonly=True) as db:
        db.execute("BEGIN")
        if as_of:
            saved = db.execute("SELECT * FROM daily_market_scans WHERE session_date=?", (as_of,)).fetchone()
        else:
            saved = db.execute("SELECT * FROM daily_market_scans ORDER BY session_date DESC LIMIT 1").fetchone()
        attempt = db.execute("SELECT * FROM market_scan_attempts ORDER BY id DESC LIMIT 1").fetchone()
        confirmation = _confirmation(db, saved["session_date"]) if saved else None
        attempt_confirmation = _confirmation(db, attempt["session_date"]) if attempt else None
    latest_attempt = None
    if attempt:
        confirmation_attempt = attempt_confirmation and attempt["id"] >= attempt_confirmation["first_attempt_id"]
        latest_attempt = dict(as_of=attempt["session_date"], status=attempt["status"],
                              phase="confirmation" if confirmation_attempt else "preliminary",
                              started_at=attempt["started_at"], finished_at=attempt["finished_at"],
                              target_count=attempt["target_count"], count=attempt["result_count"],
                              error=attempt["error"], missing=json.loads(attempt["missing"]))
    if not saved:
        return dict(status="pending", error="此交易日尚無已發布的掃描結果。", rows=[], latest_attempt=latest_attempt)
    groups, results, missing_reasons = (json.loads(saved[k]) for k in ("members", "results", "missing"))
    membership, summaries = {}, {}
    for key, symbols in groups.items():
        for rank, symbol in enumerate(symbols, start=1):
            membership.setdefault(symbol, {})[key] = rank
        count = sum(symbol in results for symbol in symbols)
        summaries[key] = dict(label=GROUP_LABELS[key], count=count, target_count=len(symbols), missing_count=len(symbols)-count)
    symbols = list(membership) if group == "all" else groups[group]
    rows, missing = [], []
    for symbol in symbols:
        ranks = membership[symbol]
        if symbol in results:
            rows.append(dict(results[symbol], groups=list(ranks), group_ranks=ranks))
        else:
            missing.append(dict(symbol=symbol, reason=missing_reasons[symbol], groups=list(ranks), group_ranks=ranks))
    if group == "all":
        rows.sort(key=lambda row: (-row["avg_dollar_volume"], row["symbol"]))
    checked = confirmation is not None and confirmation["pending"] is not None
    pending = set(json.loads(confirmation["pending"])) if checked else membership.keys()
    awaiting = [s for s in symbols if s in pending]
    return dict(status="complete", group=group, group_label=GROUP_LABELS.get(group, "全部"), groups=summaries,
                as_of=saved["session_date"], published_at=saved["published_at"], latest_attempt=latest_attempt,
                phase="confirmation" if checked else "preliminary", confirmation_pending=awaiting,
                confirmation_pending_count=len(awaiting),
                target_count=len(symbols), count=len(rows), missing_count=len(missing), missing=missing, rows=rows,
                volume_threshold=1.5, volume_window=VOLUME_WINDOW,
                volume_baseline_window=VOLUME_BASELINE_WINDOW,
                price_window=PRICE_WINDOW, event_window=EVENT_WINDOW)
