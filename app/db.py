"""Tiny SQLite cache: the 500 pre-computed stocks seed it on first startup,
and any ticker analyzed live gets cached here too, so the second person
(or the same person a minute later) who asks for it gets an instant hit
instead of a re-fetch. A file-based cache is fine for a personal tool —
see the README for the one real caveat (some free hosts wipe local disk
on redeploy, not on every request)."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "cache.db"
MAX_AGE_DAYS = 4  # covers weekends/holidays; a stale entry gets re-analyzed live on next request


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS stocks (
                ticker TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                is_live INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)


def get_cached(ticker: str) -> dict | None:
    """Returns the cached entry, or None if there isn't one *or* it's gone
    stale — judged by the market data's own `as_of` date, not by when it
    was written to the cache (a pre-seeded row is written fresh at every
    startup even though its underlying numbers might be weeks old)."""
    with _conn() as c:
        row = c.execute("SELECT data FROM stocks WHERE ticker = ?", (ticker,)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        as_of = data.get("as_of")
        if as_of:
            try:
                age_days = (datetime.utcnow().date() - datetime.strptime(as_of, "%Y-%m-%d").date()).days
                if age_days > MAX_AGE_DAYS:
                    return None
            except ValueError:
                pass
        return data


def set_cached(ticker: str, data: dict, is_live: bool) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO stocks (ticker, data, is_live, updated_at) VALUES (?, ?, ?, datetime('now'))",
            (ticker, json.dumps(data), int(is_live)),
        )


def count() -> int:
    with _conn() as c:
        return c.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]


def seed_from_bundle(bundle_path: Path) -> int:
    """Load the pre-computed 500-stock bundle into the cache, skipping any
    ticker already present (so re-running a deploy doesn't clobber
    on-demand-analyzed stocks with the older seed data)."""
    bundle = json.loads(bundle_path.read_text())
    inserted = 0
    with _conn() as c:
        existing = {row[0] for row in c.execute("SELECT ticker FROM stocks")}
        for ticker, strategies in bundle["tickers"].items():
            if ticker in existing:
                continue
            name = bundle["names"].get(ticker, ticker.replace(".NS", ""))
            payload = {"ticker": ticker, "name": name, "as_of": bundle["as_of"], "live": False, "strategies": strategies}
            c.execute(
                "INSERT OR REPLACE INTO stocks (ticker, data, is_live, updated_at) VALUES (?, ?, 0, datetime('now'))",
                (ticker, json.dumps(payload)),
            )
            inserted += 1
    return inserted
