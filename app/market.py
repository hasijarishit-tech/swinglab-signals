"""Live market snapshot: Nifty 50, Nifty Midcap 50, India VIX — fetched
fresh on every request (same Yahoo chart endpoint the rest of the app
uses). Breadth/sector-leaders come from the pre-computed 500-stock
universe bundle instead, since that needs 500 fetches to compute
properly — real numbers, but only as fresh as the last batch run, and
labeled with that date rather than passed off as live."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .swinglab.data import DataFetchError, fetch_ohlcv

DATA_DIR = Path(__file__).parent.parent / "data"

INDICES = {
    "nifty50": {"symbol": "%5ENSEI", "label": "NIFTY 50"},
    "midcap": {"symbol": "%5ENSEMDCP50", "label": "NIFTY MIDCAP 50"},
    "vix": {"symbol": "%5EINDIAVIX", "label": "INDIA VIX"},
}


def _index_snapshot(symbol: str) -> dict | None:
    try:
        df = fetch_ohlcv(symbol, "2025-01-01", "2030-01-01")
    except DataFetchError:
        return None
    close = df["Close"].dropna()
    if len(close) < 2:
        return None
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    sma50 = close.rolling(50, min_periods=50).mean().iloc[-1]
    sma200 = close.rolling(200, min_periods=200).mean().iloc[-1]
    return {
        "level": round(last, 2),
        "chg_pct": round((last / prev - 1) * 100, 2),
        "as_of": str(close.index[-1].date()),
        "above_50dma": None if pd.isna(sma50) else bool(last > sma50),
        "above_200dma": None if pd.isna(sma200) else bool(last > sma200),
    }


def _vix_regime(level: float) -> str:
    if level < 13:
        return "Calm"
    if level < 18:
        return "Normal"
    if level < 25:
        return "Elevated"
    return "Stressed"


def _trend_regime(snap: dict | None) -> str | None:
    if snap is None or snap["above_50dma"] is None or snap["above_200dma"] is None:
        return None
    if snap["above_50dma"] and snap["above_200dma"]:
        return "Uptrend"
    if not snap["above_50dma"] and not snap["above_200dma"]:
        return "Downtrend"
    return "Mixed"


def get_market_snapshot() -> dict:
    nifty = _index_snapshot(INDICES["nifty50"]["symbol"])
    midcap = _index_snapshot(INDICES["midcap"]["symbol"])
    vix = _index_snapshot(INDICES["vix"]["symbol"])

    out = {
        "nifty50": {**(nifty or {}), "regime": _trend_regime(nifty)} if nifty else None,
        "midcap": {**(midcap or {}), "regime": _trend_regime(midcap)} if midcap else None,
        "vix": {**(vix or {}), "regime": _vix_regime(vix["level"])} if vix else None,
    }

    # breadth + sector-style leaders from the pre-computed universe bundle —
    # real counts, but only as fresh as that batch run.
    try:
        bundle = json.loads((DATA_DIR / "lookup_bundle.json").read_text())
        tickers = bundle["tickers"]
        above_sma = sum(1 for t in tickers.values() if t.get("S1", {}).get("rule_qualifies_now") is True)
        below_sma = sum(1 for t in tickers.values() if t.get("S1", {}).get("rule_qualifies_now") is False)
        pos_momentum = sum(1 for t in tickers.values() if (t.get("S2", {}).get("momentum_pct") or 0) > 0)
        neg_momentum = sum(1 for t in tickers.values() if t.get("S2", {}).get("momentum_pct") is not None and t["S2"]["momentum_pct"] <= 0)
        movers = sorted(
            ((tk, t["S2"]["momentum_pct"]) for tk, t in tickers.items() if t.get("S2", {}).get("momentum_pct") is not None),
            key=lambda x: x[1], reverse=True,
        )
        names = bundle.get("names", {})
        out["breadth"] = {
            "as_of": bundle["as_of"],
            "above_250dma": above_sma, "below_250dma": below_sma,
            "positive_6m_momentum": pos_momentum, "negative_6m_momentum": neg_momentum,
            "universe_size": len(tickers),
            "top_movers": [{"ticker": tk.replace(".NS", ""), "name": names.get(tk, tk), "momentum_pct": round(m, 1)} for tk, m in movers[:6]],
            "bottom_movers": [{"ticker": tk.replace(".NS", ""), "name": names.get(tk, tk), "momentum_pct": round(m, 1)} for tk, m in movers[-6:][::-1]],
        }
    except (FileNotFoundError, KeyError):
        out["breadth"] = None

    return out
