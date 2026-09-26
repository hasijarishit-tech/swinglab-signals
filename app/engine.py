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
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .swinglab.data import DataFetchError, fetch_ohlcv_with_events
from .swinglab.fundamentals import fetch_company_page, parse_fundamentals, ratios_asof, REPORTING_LAG_DAYS
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


# A single company page fires 3 independent requests (verdict, chart,
# fundamentals) within about a second of each other. Without this, each one
# separately re-fetches the same ticker's price history from Yahoo, and the
# verdict + chart endpoints each separately re-run the same Strategy 1
# backtest — 3x the Yahoo round-trips and 2x the backtest for one page view,
# which is most of why the live site felt slow. Short TTL, not correctness-
# critical: it only ever reuses data that's at most ~2 minutes old within a
# single live analysis, well inside the "live" claim made elsewhere.
_CACHE_TTL_SECONDS = 120
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, compute):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
            return hit[1]
    value = compute()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _get_price_events(ticker: str):
    """Cached (ohlcv, dividends, splits) for `ticker` — the one Yahoo fetch
    every live code path needs, deduplicated across the 3 endpoints a
    single company-page view calls."""
    def compute():
        try:
            return fetch_ohlcv_with_events(ticker, START, END)
        except DataFetchError as e:
            return e
    result = _cached(f"ohlcv:{ticker}", compute)
    if isinstance(result, DataFetchError):
        raise TickerNotFound(str(result))
    return result


def _get_fundamentals_html(symbol: str):
    """Cached screener.in page fetch — screener.in fetch_company_page
    already caches to disk, but that only helps *sequential* requests; two
    endpoints hitting the same never-before-seen ticker at the same instant
    would otherwise both fetch over the network before either finishes
    writing the disk cache."""
    return _cached(f"fundhtml:{symbol}", lambda: fetch_company_page(symbol, cache_dir=str(DATA_DIR / ".fundamentals_cache")))


def _get_s1_backtest(ticker: str, signal_close: pd.Series, dividend: pd.Series, sma_period: int):
    """Cached Strategy-1 single-stock backtest — needed by both the verdict
    endpoint (for the trade log) and the chart endpoint (for buy/sell
    markers). Returns None if there isn't enough history for the SMA yet."""
    def compute():
        sma = signal_close.rolling(sma_period, min_periods=sma_period).mean()
        if len(signal_close) <= 1 or not sma.notna().any():
            return None
        close_panel = signal_close.to_frame(name=ticker)
        div_panel = dividend.to_frame(name=ticker)
        w1 = ma_trend.compute_target_weights(close_panel, sma_period=sma_period)
        return PortfolioEngine(100_000.0).run(close_panel, w1, div_panel)
    return _cached(f"s1bt:{ticker}:{sma_period}", compute)


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

    ohlcv, dividends, splits = _get_price_events(ticker)

    signal_close = split_adjusted_close(ohlcv["Close"], splits)
    dividend = align_dividends(dividends, ohlcv.index)
    last_price = float(signal_close.iloc[-1])
    last_date = signal_close.index[-1]
    tz = signal_close.index.tz

    out: dict = {}

    # ---------------- S1: MA Trend — real single-stock backtest ----------------
    sma_period = int(CHOSEN["S1"]["sma_period"])
    sma_val = signal_close.rolling(sma_period, min_periods=sma_period).mean().iloc[-1]
    r1 = _get_s1_backtest(ticker, signal_close, dividend, sma_period)
    if pd.isna(sma_val) or r1 is None:
        out["S1"] = {
            "held_now": False, "rule_qualifies_now": None, "pct_vs_sma": None,
            "note": f"not enough price history yet (needs {sma_period} trading days, has {len(signal_close)})",
            "trades": [], "stats": {"num_trades": 0, "win_rate_pct": None, "avg_pnl_pct": None, "avg_hold_days": None},
        }
    else:
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
    html = _get_fundamentals_html(symbol)
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

    ohlcv, dividends, splits = _get_price_events(ticker)

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
    r1 = _get_s1_backtest(ticker, signal_close, dividend, sma_period)
    if r1 is not None:
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


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, 2)


def _cagr_pct(series: list[float | None]) -> float | None:
    """CAGR between the first and last non-null, positive points in `series`,
    treating each list position as one year apart (these are annual figures)."""
    pts = [(i, v) for i, v in enumerate(series) if v is not None and v > 0]
    if len(pts) < 2:
        return None
    (i0, v0), (i1, v1) = pts[0], pts[-1]
    years = i1 - i0
    if years <= 0:
        return None
    return round(((v1 / v0) ** (1.0 / years) - 1.0) * 100.0, 1)


def fundamentals_detail(ticker: str) -> dict:
    """Full historical annual fundamentals + a real-numbers-only valuation
    history for one ticker. Returns available=False with a plain reason
    when screener has no usable page (true for most banks/NBFCs, and for
    very recent listings) — never a guessed number in that case."""
    ticker = ticker.upper()
    if not ticker.endswith(".NS"):
        ticker = ticker + ".NS"
    symbol = ticker.replace(".NS", "")

    html = _get_fundamentals_html(symbol)
    if not html:
        return {"ticker": ticker, "available": False,
                "reason": "No public screener.in page found for this symbol."}
    hist = parse_fundamentals(html, symbol)
    if hist is None or not hist.dates:
        return {"ticker": ticker, "available": False,
                "reason": "Fundamentals page found but has no parseable annual report history yet — common for banks/NBFCs (different statement format) or a very recent listing."}

    dates = hist.dates
    years = [d.year for d in dates]
    revenue = [_num(hist.revenue.get(d)) for d in dates]
    ebitda = [_num(hist.operating_profit.get(d)) for d in dates]
    pat = [_num(hist.net_profit.get(d)) for d in dates]
    eps = [_num(hist.eps.get(d)) for d in dates]
    fcf = [_num(hist.free_cash_flow.get(d)) for d in dates]
    ocf = [_num(hist.operating_cash_flow.get(d)) for d in dates]
    debt = [_num(hist.borrowings.get(d)) for d in dates]
    interest = [_num(hist.interest.get(d)) for d in dates]
    equity = [_num(hist.book_equity.get(d)) for d in dates]
    roce = [_num(hist.roce_pct.get(d)) for d in dates]

    def ratio(a, b, mult=1.0):
        return None if a is None or b in (None, 0) else round(a / b * mult, 1)

    ebitda_margin = [ratio(e, r, 100) for e, r in zip(ebitda, revenue)]
    fcf_margin = [ratio(f, r, 100) for f, r in zip(fcf, revenue)]
    roe = [ratio(n, eq, 100) for n, eq in zip(pat, equity)]
    debt_to_equity = [ratio(d, eq) for d, eq in zip(debt, equity)]
    interest_coverage = [ratio(e, i) for e, i in zip(ebitda, interest)]

    # ---- valuation history: real trailing P/E at each reported period,
    # using the price ~60 days after period-end (when results were actually public) ----
    pe_history = []
    last_price, last_date, current_ratios = None, None, None
    dividends = pd.Series(dtype=float)
    try:
        ohlcv, dividends, splits = _get_price_events(ticker)
        signal_close = split_adjusted_close(ohlcv["Close"], splits)
        last_price = float(signal_close.iloc[-1])
        last_date = signal_close.index[-1]
        price_naive = signal_close.copy()
        price_naive.index = price_naive.index.tz_localize(None)
        lag = pd.Timedelta(days=REPORTING_LAG_DAYS)
        for d, e in zip(dates, eps):
            if e is None or e <= 0:
                continue
            asof_date = d + lag
            pos = price_naive.index.searchsorted(asof_date, side="right") - 1
            if pos < 0:
                continue
            price_then = float(price_naive.iloc[pos])
            pe_history.append({"period_end": str(d.date()), "pe": round(price_then / e, 1)})
        current_ratios = ratios_asof(hist, last_date, last_price)
    except TickerNotFound:
        pass

    pe_vals_universe = list(_context.get("s3_pe", {}).values())
    ev_vals_universe = list(_context.get("s3_ev_ebitda", {}).values())
    universe_median_pe = round(float(np.median(pe_vals_universe)), 1) if pe_vals_universe else None
    universe_median_ev_ebitda = round(float(np.median(ev_vals_universe)), 1) if ev_vals_universe else None
    own_median_pe = round(float(np.median([p["pe"] for p in pe_history])), 1) if pe_history else None

    div_yield_pct = None
    if last_price and not dividends.empty:
        cutoff = last_date - pd.DateOffset(years=1)
        ttm_div = float(dividends[dividends.index >= cutoff].sum())
        if last_price > 0:
            div_yield_pct = round(ttm_div / last_price * 100, 2)

    # ---- observations: each one only fires if the underlying numbers support it ----
    obs = []
    if len(revenue) >= 4 and all(v is not None for v in revenue[-4:]):
        recent_cagr = _cagr_pct(revenue[-4:])
        older_cagr = _cagr_pct(revenue[:-3]) if len(revenue) > 4 else None
        if recent_cagr is not None and older_cagr is not None:
            if recent_cagr > older_cagr + 3:
                obs.append(f"Revenue growth has accelerated to {recent_cagr}% CAGR over the last 3 years, up from {older_cagr}% before that.")
            elif recent_cagr < older_cagr - 3:
                obs.append(f"Revenue growth has slowed to {recent_cagr}% CAGR over the last 3 years, down from {older_cagr}% before that.")
    if len([v for v in roce if v is not None]) >= 3:
        roce_pts = [v for v in roce if v is not None]
        if roce_pts[-1] - roce_pts[0] >= 3:
            obs.append(f"ROCE has expanded from {roce_pts[0]}% to {roce_pts[-1]}% over the available history.")
        elif roce_pts[0] - roce_pts[-1] >= 3:
            obs.append(f"ROCE has contracted from {roce_pts[0]}% to {roce_pts[-1]}% over the available history.")
    fcf_pts = [v for v in fcf if v is not None]
    if len(fcf_pts) >= 3:
        neg_years = sum(1 for v in fcf_pts if v < 0)
        if neg_years == 0:
            obs.append(f"Free cash flow has been positive in all {len(fcf_pts)} reported years.")
        else:
            obs.append(f"Free cash flow was negative in {neg_years} of the last {len(fcf_pts)} reported years.")
    margin_pts = [v for v in ebitda_margin if v is not None]
    if len(margin_pts) >= 3 and abs(margin_pts[-1] - margin_pts[0]) >= 2:
        direction = "expanded" if margin_pts[-1] > margin_pts[0] else "compressed"
        obs.append(f"EBITDA margin has {direction} from {margin_pts[0]}% to {margin_pts[-1]}% over the available history.")
    de_pts = [v for v in debt_to_equity if v is not None]
    if len(de_pts) >= 3 and abs(de_pts[-1] - de_pts[0]) >= 0.15:
        direction = "risen" if de_pts[-1] > de_pts[0] else "fallen"
        obs.append(f"Debt-to-equity has {direction} from {de_pts[0]}x to {de_pts[-1]}x over the available history.")
    if current_ratios and own_median_pe and not pd.isna(current_ratios.get("pe", float("nan"))):
        cur_pe = round(current_ratios["pe"], 1)
        gap = round((cur_pe / own_median_pe - 1) * 100)
        if abs(gap) >= 10:
            obs.append(f"Currently trading at {cur_pe}x earnings, {'above' if gap>0 else 'below'} its own {own_median_pe}x median by {abs(gap)}%.")

    return {
        "ticker": ticker,
        "available": True,
        "source": "screener.in (annual reports)",
        "years": years,
        "revenue_cr": revenue,
        "revenue_cagr_pct": _cagr_pct(revenue),
        "ebitda_cr": ebitda,
        "ebitda_margin_pct": ebitda_margin,
        "pat_cr": pat,
        "pat_cagr_pct": _cagr_pct(pat),
        "eps": eps,
        "eps_cagr_pct": _cagr_pct(eps),
        "roe_pct": roe,
        "roce_pct": roce,
        "free_cash_flow_cr": fcf,
        "fcf_margin_pct": fcf_margin,
        "operating_cash_flow_cr": ocf,
        "debt_cr": debt,
        "debt_to_equity": debt_to_equity,
        "interest_coverage": interest_coverage,
        "dividend_yield_ttm_pct": div_yield_pct,
        "valuation": {
            "current": current_ratios and {
                "pe": _num(current_ratios.get("pe")),
                "pb": _num(current_ratios.get("pb")),
                "ev_ebitda": _num(current_ratios.get("ev_ebitda")),
                "as_of": str(current_ratios["period_end"].date()) if current_ratios.get("period_end") is not None else None,
            },
            "own_5y_median_pe": own_median_pe,
            "universe_median_pe": universe_median_pe,
            "universe_median_ev_ebitda": universe_median_ev_ebitda,
            "pe_history": pe_history,
        },
        "observations": obs,
    }
