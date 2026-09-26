"""
Nifty 500 universe construction and a disk-cached, resumable price fetch
across the whole universe.

Known, disclosed limitation: NSE does not publish a clean point-in-time
constituent-history feed for free. `fetch_index_list` gets *today's*
constituents only. Backtesting a long history against today's list means
stocks that were removed from the index (delisted, or simply dropped for
underperforming) are invisible to the backtest — classic survivorship bias.
This is not silently ignored: every report generated from this module
carries that caveat forward (see `run_strategy_backtests.py`).

Fetching and caching ~500 tickers takes real wall-clock time and Yahoo's
endpoint is not built for that request volume, so every ticker's raw fetch
is cached to disk individually and the fetch is resumable — a failed run
does not have to re-fetch tickers it already got.
"""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

from .data import DataFetchError, fetch_ohlcv_with_events
from .totalreturn import align_dividends, split_adjusted_close

_INDEX_LIST_URLS = {
    "nifty500": "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "nifty200": "https://nsearchives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty50": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
}

_HEADERS = {"User-Agent": "Mozilla/5.0 (SwingLab research tool)"}

DEFAULT_CACHE_DIR = Path(".cache")


def fetch_index_list(index: str = "nifty500", cache_dir: Path = DEFAULT_CACHE_DIR) -> list[str]:
    """
    Today's constituents of `index` ("nifty500" / "nifty200" / "nifty50"),
    as Yahoo-format tickers ("RELIANCE.NS"). Cached to disk — this list
    changes rarely enough that re-fetching every run is unnecessary, and
    NSE's archive endpoint is more prone to transient blocking than the
    chart API.
    """
    cache_path = Path(cache_dir) / f"{index}_constituents.csv"
    if cache_path.exists():
        return pd.read_csv(cache_path)["ticker"].tolist()

    url = _INDEX_LIST_URLS[index]
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    from io import StringIO

    df = pd.read_csv(StringIO(resp.text))
    tickers = [f"{sym.strip()}.NS" for sym in df["Symbol"]]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"ticker": tickers}).to_csv(cache_path, index=False)
    return tickers


@dataclass
class TickerData:
    ohlcv: pd.DataFrame          # raw Open/High/Low/Close/Volume
    signal_close: pd.Series      # split-adjusted Close, for SMA/momentum/52w-high math
    dividend: pd.Series          # per-share cash dividend, aligned to ohlcv's calendar, 0 elsewhere


def _fetch_one(ticker: str, start: str, end: str) -> TickerData:
    ohlcv, dividends, splits = fetch_ohlcv_with_events(ticker, start, end)
    signal_close = split_adjusted_close(ohlcv["Close"], splits)
    dividend = align_dividends(dividends, ohlcv.index)
    return TickerData(ohlcv=ohlcv, signal_close=signal_close, dividend=dividend)


def fetch_universe_prices(
    tickers: list[str],
    start: str,
    end: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    delay_seconds: float = 0.6,
    force_refresh: bool = False,
    progress: bool = True,
) -> tuple[dict[str, TickerData], list[str]]:
    """
    Fetch + split-adjust + cache price data for every ticker in `tickers`.

    Returns (data, failed): `data` maps ticker -> TickerData for every
    ticker that fetched successfully; `failed` lists tickers that didn't
    (delisted, renamed, or a Yahoo symbol mismatch — reported, not hidden).

    Resumable: each ticker is cached to its own file under
    `cache_dir/prices/`, so re-running after a partial failure only
    fetches what's missing. `delay_seconds` rate-limits requests since this
    can be hundreds of tickers against an endpoint not meant for that
    volume.
    """
    price_dir = Path(cache_dir) / "prices"
    price_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, TickerData] = {}
    failed: list[str] = []

    for i, ticker in enumerate(tickers):
        cache_path = price_dir / f"{ticker.replace('/', '_')}.pkl"
        if cache_path.exists() and not force_refresh:
            with open(cache_path, "rb") as f:
                data[ticker] = pickle.load(f)
            continue

        try:
            td = _fetch_one(ticker, start, end)
        except DataFetchError as e:
            failed.append(ticker)
            if progress:
                print(f"  [{i+1}/{len(tickers)}] {ticker}: skip ({e})")
            time.sleep(delay_seconds)
            continue

        with open(cache_path, "wb") as f:
            pickle.dump(td, f)
        data[ticker] = td
        if progress:
            print(f"  [{i+1}/{len(tickers)}] {ticker}: {len(td.ohlcv)} bars")
        time.sleep(delay_seconds)

    return data, failed
