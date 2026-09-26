"""
SwingLab Signals API — serves the 500 pre-computed stocks instantly, and
fetches + analyzes any other valid NSE ticker live on request.

Run locally:   uvicorn app.main:app --reload
Deployed with: uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import db
from .engine import TickerNotFound, analyze_ticker, price_chart, fundamentals_detail
from .market import get_market_snapshot

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="SwingLab Signals API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

_names: dict[str, str] = {}
_meta: dict = {}
_strategy_lab = json.loads((DATA_DIR / "strategy_lab.json").read_text()) if (DATA_DIR / "strategy_lab.json").exists() else None
_market_cache: dict = {"data": None, "ts": 0.0}
_market_lock = threading.Lock()
MARKET_CACHE_TTL_SECONDS = 300


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    inserted = db.seed_from_bundle(DATA_DIR / "lookup_bundle.json")
    print(f"DB ready: {db.count()} tickers cached ({inserted} newly seeded this run).")
    bundle = json.loads((DATA_DIR / "lookup_bundle.json").read_text())
    _names.update(bundle["names"])
    _meta["strategy_meta"] = bundle["strategy_meta"]
    _meta["chosen_params"] = bundle["chosen_params"]
    _meta["as_of"] = bundle["as_of"]


@app.get("/api/health")
def health():
    return {"status": "ok", "cached_tickers": db.count()}


@app.get("/api/meta")
def meta():
    return _meta


@app.get("/api/search")
def search(q: str = ""):
    q = q.strip().lower()
    if not q:
        return []
    results = []
    for ticker, name in _names.items():
        symbol = ticker.replace(".NS", "")
        if q in symbol.lower() or q in name.lower():
            results.append({"ticker": ticker, "symbol": symbol, "name": name})
    results.sort(key=lambda r: (not r["symbol"].lower().startswith(q), r["symbol"]))
    return results[:10]


@app.get("/api/stock/{ticker}")
def get_stock(ticker: str):
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"

    cached = db.get_cached(ticker)
    if cached is not None:
        return cached

    try:
        result = analyze_ticker(ticker)
    except TickerNotFound:
        raise HTTPException(status_code=404, detail=f"No data found for {ticker} — check the symbol is a valid NSE ticker.")
    except Exception as e:  # noqa: BLE001 - surface a clean error instead of a 500 stack trace
        raise HTTPException(status_code=502, detail=f"Couldn't analyze {ticker} right now: {e}")

    result["name"] = _names.get(ticker, ticker.replace(".NS", ""))
    db.set_cached(ticker, result, is_live=True)
    return result


@app.get("/api/market")
def market():
    now = time.time()
    if _market_cache["data"] is None or now - _market_cache["ts"] > MARKET_CACHE_TTL_SECONDS:
        with _market_lock:
            # re-check: another thread may have refreshed it while we waited for the lock
            if _market_cache["data"] is None or time.time() - _market_cache["ts"] > MARKET_CACHE_TTL_SECONDS:
                _market_cache["data"] = get_market_snapshot()
                _market_cache["ts"] = time.time()
    return _market_cache["data"]


@app.get("/api/strategy-lab")
def strategy_lab():
    if _strategy_lab is None:
        raise HTTPException(status_code=404, detail="Strategy lab data not available.")
    return _strategy_lab


@app.get("/api/stock/{ticker}/fundamentals")
def get_fundamentals(ticker: str):
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"
    try:
        return fundamentals_detail(ticker)
    except TickerNotFound:
        raise HTTPException(status_code=404, detail=f"No price data found for {ticker}.")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't load fundamentals for {ticker} right now: {e}")


@app.get("/api/stock/{ticker}/chart")
def get_stock_chart(ticker: str):
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"
    try:
        return price_chart(ticker)
    except TickerNotFound:
        raise HTTPException(status_code=404, detail=f"No price data found for {ticker}.")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Couldn't load a chart for {ticker} right now: {e}")


# ---- static frontend ----
app.mount("/assets", StaticFiles(directory=str(STATIC_DIR)), name="assets")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))
