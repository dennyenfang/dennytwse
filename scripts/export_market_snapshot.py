"""Export a GitHub-safe SQLite snapshot containing only market scan data."""

import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "portfolio.db"
TARGET = ROOT / "data" / "market_snapshot.sqlite"
MARKET_TABLES = (
    "daily_market_scans",
    "market_scan_attempts",
    "market_scan_confirmations",
    "market_bars",
    "market_universe_lists",
)
PERSONAL_TABLES = ("holdings", "user_holdings", "alerts")


def export_snapshot():
    if not SOURCE.is_file():
        raise FileNotFoundError(f"Source database not found: {SOURCE}")
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_name(TARGET.name + ".tmp")
    temporary.unlink(missing_ok=True)

    source_uri = SOURCE.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        existing = {row[0] for row in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        for table in PERSONAL_TABLES:
            if table in existing:
                target.execute(f'DROP TABLE "{table}"')
        for table in MARKET_TABLES:
            if table not in existing:
                raise RuntimeError(f"Source database is missing required table: {table}")
        target.execute("VACUUM")
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Snapshot integrity check failed: {integrity}")
        counts = {table: target.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                  for table in MARKET_TABLES}
        target.commit()
    finally:
        target.close()
        source.close()

    temporary.replace(TARGET)
    return counts


if __name__ == "__main__":
    counts = export_snapshot()
    print(f"Exported {TARGET.relative_to(ROOT)}")
    for table, count in counts.items():
        print(f"{table}: {count}")
