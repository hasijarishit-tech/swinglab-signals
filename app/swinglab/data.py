"""
Market data access.

Deliberately does NOT depend on the `yfinance` package. That library's newer
versions need a cookie/"crumb" handshake against fc.yahoo.com / guce.yahoo.com
before it will serve historical data, and that handshake is blocked from a
lot of cloud / datacenter networks (this is a known, common failure, not
specific to any one environment). The underlying public chart API
(query1.finance.yahoo.com/v8/finance/chart/...) that yfinance itself calls
under the hood works fine without that handshake, so we talk to it directly
with `requests`. This is more robust for a research tool that might run in
CI or a sandbox, not just on a laptop.
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass

import pandas as pd
import requests

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (SwingLab research tool)"}


def _to_unix(date_str: str) -> int:
    y, m, d = (int(p) for p in date_str.split("-"))
    return calendar.timegm(dt.date(y, m, d).timetuple())


class DataFetchError(RuntimeError):
    pass


def fetch_ohlcv(symbol: str, start: str, end: str, retries: int = 3) -> pd.DataFrame:
    """
    Fetch daily OHLCV for `symbol` between `start` and `end` (YYYY-MM-DD).

    `symbol` should already be in Yahoo's format, e.g. "RELIANCE.NS",
    "%5ENSEI" (Nifty 50), "%5EINDIAVIX" (India VIX).
    """
    params = {
        "period1": _to_unix(start),
        "period2": _to_unix(end),
        "interval": "1d",
        "events": "history",
    }
    url = _CHART_URL.format(symbol=symbol)

    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as e:  # noqa: BLE001 - retry on anything transient
            last_err = e
    else:
        raise DataFetchError(f"Failed to fetch {symbol}: {last_err}")

    result = payload.get("chart", {}).get("result")
    if not result:
        err = payload.get("chart", {}).get("error")
        raise DataFetchError(f"No data for {symbol}: {err}")

    r = result[0]
    timestamps = r.get("timestamp")
    if not timestamps:
        raise DataFetchError(f"No timestamps returned for {symbol}")

    quote = r["indicators"]["quote"][0]
    df = pd.DataFrame(
        {
            "Open": quote["open"],
            "High": quote["high"],
            "Low": quote["low"],
            "Close": quote["close"],
            "Volume": quote["volume"],
        },
        index=pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Kolkata").normalize(),
    )
    df.index.name = "Date"
    df = df.dropna(how="all")
    return df


def fetch_ohlcv_with_events(
    symbol: str, start: str, end: str, retries: int = 3
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """
    Like `fetch_ohlcv`, but also returns dividend and split events for
    corporate-action adjustment (see `swinglab.totalreturn`).

    Returns (ohlcv, dividends, splits):
      - ohlcv: same as `fetch_ohlcv` (raw, as-traded Close/High/Low/Open/Volume)
      - dividends: Series of per-share cash amount, indexed by ex-date
      - splits: DataFrame with a "ratio" column (new shares per old share,
        e.g. 2.0 for a 2-for-1 split), indexed by ex-date

    A separate fetch from `fetch_ohlcv` (not a shared helper) because this
    hits `events=div,split` instead of `events=history`, and every existing
    caller of `fetch_ohlcv` should keep getting exactly the response shape
    it already depends on.
    """
    params = {
        "period1": _to_unix(start),
        "period2": _to_unix(end),
        "interval": "1d",
        "events": "div,split",
    }
    url = _CHART_URL.format(symbol=symbol)

    last_err = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=20)
            resp.raise_for_status()
            payload = resp.json()
            break
        except Exception as e:  # noqa: BLE001 - retry on anything transient
            last_err = e
    else:
        raise DataFetchError(f"Failed to fetch {symbol}: {last_err}")

    result = payload.get("chart", {}).get("result")
    if not result:
        err = payload.get("chart", {}).get("error")
        raise DataFetchError(f"No data for {symbol}: {err}")

    r = result[0]
    timestamps = r.get("timestamp")
    if not timestamps:
        raise DataFetchError(f"No timestamps returned for {symbol}")

    quote = r["indicators"]["quote"][0]
    index = pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Kolkata").normalize()
    ohlcv = pd.DataFrame(
        {
            "Open": quote["open"],
            "High": quote["high"],
            "Low": quote["low"],
            "Close": quote["close"],
            "Volume": quote["volume"],
        },
        index=index,
    )
    ohlcv.index.name = "Date"
    ohlcv = ohlcv.dropna(how="all")

    events = r.get("events", {})

    div_events = events.get("dividends", {})
    if div_events:
        div_dates = pd.to_datetime(
            [v["date"] for v in div_events.values()], unit="s", utc=True
        ).tz_convert("Asia/Kolkata").normalize()
        dividends = pd.Series(
            [float(v["amount"]) for v in div_events.values()], index=div_dates, name="Dividend"
        ).sort_index()
    else:
        dividends = pd.Series(dtype=float, name="Dividend")

    split_events = events.get("splits", {})
    if split_events:
        split_dates = pd.to_datetime(
            [v["date"] for v in split_events.values()], unit="s", utc=True
        ).tz_convert("Asia/Kolkata").normalize()
        ratios = [float(v["numerator"]) / float(v["denominator"]) for v in split_events.values()]
        splits = pd.DataFrame({"ratio": ratios}, index=split_dates).sort_index()
    else:
        splits = pd.DataFrame({"ratio": []}, index=pd.DatetimeIndex([]))

    return ohlcv, dividends, splits


@dataclass
class MarketContext:
    """Nifty and India VIX series, reused across every ticker."""

    nifty_close: pd.Series
    vix_close: pd.Series

    @classmethod
    def fetch(cls, start: str, end: str) -> "MarketContext":
        from .config import BENCHMARK, VIX_SYMBOL

        nifty = fetch_ohlcv(BENCHMARK, start, end)["Close"]
        vix = fetch_ohlcv(VIX_SYMBOL, start, end)["Close"]
        return cls(nifty_close=nifty, vix_close=vix)
