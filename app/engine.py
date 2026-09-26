"""
On-demand single-stock analysis: fetches one ticker's price + fundamentals
live and scores it against the 5 strategies' chosen parameters, using the
saved universe distributions (data/universe_context.json) to rank it
without re-fetching or re-ranking all 500 names.

Output schema matches the pre-computed lookup_bundle.json entries exactly
(same S1-S5 shape) so the frontend renders both with the same code.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .swinglab.data import DataFetchError, fetch_ohlcv_with_events
from .swinglab.fundamentals import fetch_company_page, parse_fundamentals, ratios_asof
from .swinglab.portfolio_engine import PortfolioEngine
from .swinglab.strategies import ma_trend
from .swinglab.totalreturn import align_dividends, split_adjusted_close

START, END = "2010-01-01", "2030-01-01"
DATA_DIR = Path(__file__).parent.parent / "data"

_verdicts = json.loads((DATA_DIR / "verdicts.json").read_text())
CHOSEN = _verdicts["chosen_params"]
_context = json.loads((DATA_DIR / "universe_context.json").read_text())


class TickerNotFound(Exception):
    pass


def _trades_from_result(result) -> tuple[list[dict], dict]:
    trades = [
        {"entry_date": str(p.entry_date.date()), "exit_date": str(p.exit_date.date()),
         "entry_price": round(p.entry_price, 2), "exit_price": round(p.exit_price, 2),
         "pnl_pct": round(p.pnl_pct, 2), "hold_days": p.hold_days, "status": "closed"}
        for p in result.closed_positions
    ]
    for p in result.open_positions:
        trades.append({"entry_date": str(p.entry_date.date()), "exit_date": None,
                        "entry_price": round(p.entry_price, 2), "exit_price": None,
                        "pnl_pct": None, "hold_days": None, "status": "open"})
    trades.sort(key=lambda t: t["entry_date"])
    closed = [t for t in trades if t["status"] == "closed"]
    stats = {
        "num_trades": len(closed),
        "win_rate_pct": round(100.0 * sum(1 for t in closed if t["pnl_pct"] > 0) / len(closed), 1) if closed else None,
        "avg_pnl_pct": round(sum(t["pnl_pct"] for t in closed) / len(closed), 2) if closed else None,
        "avg_hold_days": round(sum(t["hold_days"] for t in closed) / len(closed), 1) if closed else None,
    }
    return trades, stats


def analyze_ticker(ticker: str) -> dict:
    """Fetch + score one ticker live. Raises TickerNotFound if Yahoo has no
    data for it (bad symbol, delisted, etc). Returns a dict with the same
    S1-S5 shape as the pre-computed database."""
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"

    try:
        ohlcv, dividends, splits = fetch_ohlcv_with_events(ticker, START, END)
    except DataFetchError as e:
        raise TickerNotFound(str(e))

    signal_close = split_adjusted_close(ohlcv["Close"], splits)
    dividend = align_dividends(dividends, ohlcv.index)
    last_price = float(signal_close.iloc[-1])
    last_date = signal_close.index[-1]
    tz = signal_close.index.tz

    out: dict = {}

    # ---------------- S1: MA Trend — real single-stock backtest ----------------
    sma_period = int(CHOSEN["S1"]["sma_period"])
    sma_val = signal_close.rolling(sma_period, min_periods=sma_period).mean().iloc[-1]
    close_panel = signal_close.to_frame(name=ticker)
    div_panel = dividend.to_frame(name=ticker)
    if pd.isna(sma_val):
        out["S1"] = {
            "held_now": False, "rule_qualifies_now": None, "pct_vs_sma": None,
            "note": f"not enough price history yet (needs {sma_period} trading days, has {len(signal_close)})",
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }
    else:
        w1 = ma_trend.compute_target_weights(close_panel, sma_period=sma_period)
        r1 = PortfolioEngine(100_000.0).run(close_panel, w1, div_panel)
        trades1, stats1 = _trades_from_result(r1)
        qualifies = last_price > sma_val
        out["S1"] = {
            "held_now": bool(qualifies), "rule_qualifies_now": bool(qualifies),
            "pct_vs_sma": round((last_price / float(sma_val) - 1) * 100, 2),
            "note": "price is above its 250-day SMA" if qualifies else "price is below its 250-day SMA",
            "trades": trades1, "stats": stats1,
        }

    # ---------------- S2: Momentum — ranked against saved distribution ----------------
    lb2 = int(CHOSEN["S2"]["lookback_months"])
    skip_date = pd.Timestamp(_context["last_rebalance_monthly"], tz=tz) - pd.DateOffset(months=1)
    look_date = pd.Timestamp(_context["last_rebalance_monthly"], tz=tz) - pd.DateOffset(months=1 + lb2)
    idx = signal_close.index
    if look_date < idx[0]:
        out["S2"] = {"held_now": False, "momentum_pct": None, "rank": None,
                      "universe_ranked": len(_context["s2_momentum_pct"]["values"]) + 1,
                      "note": "not enough price history yet to compute momentum",
                      "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None}}
    else:
        skip_pos = idx.get_indexer([skip_date], method="pad")[0]
        look_pos = idx.get_indexer([look_date], method="pad")[0]
        mom2 = float(signal_close.iloc[skip_pos] / signal_close.iloc[look_pos] - 1.0)
        universe_vals = list(_context["s2_momentum_pct"]["values"].values())
        rank = sum(1 for v in universe_vals if v > mom2 * 100) + 1
        held = rank <= int(CHOSEN["S2"]["top_n"])
        out["S2"] = {
            "held_now": bool(held), "momentum_pct": round(mom2 * 100, 2), "rank": rank,
            "universe_ranked": len(universe_vals) + 1,
            "note": f"ranked #{rank} of {len(universe_vals)+1} by momentum",
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }

    # ---------------- S4: 52-week high — ranked against saved distribution ----------------
    high_252 = signal_close.rolling(252, min_periods=252).max().iloc[-1]
    if pd.isna(high_252):
        out["S4"] = {"held_now": False, "pct_from_52w_high": None, "within_entry_threshold": None,
                      "note": f"not enough history yet (needs 252 trading days, has {len(signal_close)})",
                      "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None}}
    else:
        pct_high = float(last_price / float(high_252) - 1.0)
        within = pct_high >= -float(CHOSEN["S4"]["threshold_pct"]) / 100.0
        out["S4"] = {
            "held_now": bool(within), "pct_from_52w_high": round(pct_high * 100, 2),
            "within_entry_threshold": bool(within),
            "note": "within range of its 52-week high" if within else "too far below its 52-week high to qualify",
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }

    # ---------------- S3 & S5: fundamentals-based ----------------
    symbol = ticker.replace(".NS", "")
    html = fetch_company_page(symbol, cache_dir=str(DATA_DIR / ".fundamentals_cache"))
    hist = parse_fundamentals(html, symbol) if html else None
    fr = ratios_asof(hist, last_date, last_price) if hist else None

    if fr is None:
        no_data_stats = {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None}
        out["S3"] = {"held_now": False, "in_cheap_half": None, "value_rank": None,
                      "momentum_rank_within_value_group": None,
                      "note": "no fundamentals data (financial-sector company, or not enough reported history)",
                      "trades": [], "stats": no_data_stats}
        out["S5"] = {"held_now": False, "passes_all_filters": None, "checks": None,
                      "roe_pct": None, "roce_pct": None,
                      "note": "no fundamentals data (financial-sector company, or not enough reported history)",
                      "trades": [], "stats": no_data_stats}
    else:
        pe_vals = list(_context["s3_pe"].values())
        ev_vals = list(_context["s3_ev_ebitda"].values())
        pe_rank = sum(1 for v in pe_vals if v < fr["pe"]) + 1 if fr["pe"] and not pd.isna(fr["pe"]) else None
        ev_rank = sum(1 for v in ev_vals if v < fr["ev_ebitda"]) + 1 if fr["ev_ebitda"] and not pd.isna(fr["ev_ebitda"]) else None
        combined_ranks = [r for r in (pe_rank, ev_rank) if r is not None]
        value_rank = int(np.mean(combined_ranks)) if combined_ranks else None
        n_universe = max(len(pe_vals), len(ev_vals)) + 1
        in_cheap_half = value_rank is not None and value_rank <= n_universe / 2
        out["S3"] = {
            "held_now": bool(in_cheap_half) if in_cheap_half else False,  # momentum-within-group not computed live; conservative
            "in_cheap_half": in_cheap_half, "value_rank": value_rank,
            "momentum_rank_within_value_group": None,
            "note": ("in the cheap half by valuation — momentum-within-group rank needs the full universe re-ranked, not computed live"
                     if in_cheap_half else "not in the cheaper half by valuation"),
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }

        roe, roce = fr["roe_pct"], fr["roce_pct"]
        checks = {
            "roe_above_15": bool(not pd.isna(roe) and roe > 15.0),
            "roce_above_15": bool(not pd.isna(roce) and roce > 15.0),
            "positive_fcf": bool(not pd.isna(fr["free_cash_flow_cr"]) and fr["free_cash_flow_cr"] > 0),
            "debt_ok": bool(not pd.isna(fr["debt_to_equity"]) and fr["debt_to_equity"] <= 1.5),
        }
        passes_checked = all(checks.values())
        out["S5"] = {
            "held_now": False,  # top-N rank among quality-passers not computed live; report qualification only
            "passes_all_filters": passes_checked, "checks": checks,
            "roe_pct": round(float(roe), 1) if not pd.isna(roe) else None,
            "roce_pct": round(float(roce), 1) if not pd.isna(roce) else None,
            "note": ("passes the 4 checkable quality filters (earnings-growth & 3yr-consistency need deeper history)"
                     if passes_checked else "fails at least one quality filter"),
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }

    return {
        "ticker": ticker,
        "as_of": str(last_date.date()),
        "live": True,
        "strategies": out,
    }


def price_chart(ticker: str, max_points: int = 400) -> dict:
    """Fetch one ticker's price history live and return a downsampled series
    for plotting, plus Strategy 1's real buy/sell markers on it. Always
    live — unlike the main verdict, there's no cache to go stale here, so
    this chart is the most current the data gets."""
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"

    try:
        ohlcv, dividends, splits = fetch_ohlcv_with_events(ticker, START, END)
    except DataFetchError as e:
        raise TickerNotFound(str(e))

    signal_close = split_adjusted_close(ohlcv["Close"], splits)
    dividend = align_dividends(dividends, ohlcv.index)
    sma_period = int(CHOSEN["S1"]["sma_period"])
    sma = signal_close.rolling(sma_period, min_periods=sma_period).mean()

    n = len(signal_close)
    date_index = signal_close.index
    stride = max(1, n // max_points)
    sample_pos = list(range(0, n, stride))
    if sample_pos[-1] != n - 1:
        sample_pos.append(n - 1)

    trades: list[dict] = []
    if n > 1 and sma.notna().any():
        close_panel = signal_close.to_frame(name=ticker)
        div_panel = dividend.to_frame(name=ticker)
        w1 = ma_trend.compute_target_weights(close_panel, sma_period=sma_period)
        r1 = PortfolioEngine(100_000.0).run(close_panel, w1, div_panel)
        for p in r1.closed_positions:
            entry_pos = date_index.get_indexer([p.entry_date], method="nearest")[0]
            exit_pos = date_index.get_indexer([p.exit_date], method="nearest")[0]
            trades.append({
                "entry_frac": entry_pos / (n - 1), "entry_price": round(p.entry_price, 2), "entry_date": str(p.entry_date.date()),
                "exit_frac": exit_pos / (n - 1), "exit_price": round(p.exit_price, 2), "exit_date": str(p.exit_date.date()),
            })
        for p in r1.open_positions:
            entry_pos = date_index.get_indexer([p.entry_date], method="nearest")[0]
            trades.append({
                "entry_frac": entry_pos / (n - 1), "entry_price": round(p.entry_price, 2), "entry_date": str(p.entry_date.date()),
            })

    return {
        "ticker": ticker,
        "sma_period": sma_period,
        "dates": [str(date_index[i].date()) for i in sample_pos],
        "close": [round(float(signal_close.iloc[i]), 2) for i in sample_pos],
        "sma": [None if pd.isna(sma.iloc[i]) else round(float(sma.iloc[i]), 2) for i in sample_pos],
        "trades": trades,
    }
