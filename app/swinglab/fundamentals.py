"""
Point-in-time fundamentals for Strategies 3 & 5, sourced from screener.in's
public per-company pages — the free path this project's data-feasibility
check found after Yahoo's fundamentals endpoint came back blocked (see
README / the multi-strategy plan for the full writeup).

What this is, plainly stated:
  - Real reported annual figures (Sales, Net Profit, EPS, Operating Profit
    ~= EBITDA, Equity, Borrowings, and screener's own ROCE % / Free Cash
    Flow), scraped from a public, no-login, server-rendered page.
  - P/E, ROE, EV/EBITDA, D/E and P/B are computed HERE, via formula, from
    those reported figures — not pulled from a pre-packaged ratios API.
  - Point-in-time discipline: a period's figures are only considered
    "known" `REPORTING_LAG_DAYS` (default 60, matching SEBI's audited
    annual-result filing deadline) after the fiscal year-end. Asking for
    ratios "as of" some date only ever returns the most recent period that
    would actually have been public by then.
  - Real, disclosed limitations: ~12 years of annual history (FY2015+ as
    of when this was built), not the multi-decade window the price-only
    strategies get. Financial-sector companies (banks, NBFCs) parse to
    None here and are excluded from Strategies 3 & 5 entirely — confirmed
    against real data that banks report Revenue/Interest/"Financing
    Profit" instead of Sales/Expenses/"Operating Profit", so there's no
    EBITDA-equivalent line to compute EV/EBITDA from. This isn't a scraper
    bug to patch around: EV/EBITDA is not a meaningful metric for a bank's
    business model in the first place, which is why real factor-investing
    practice screens financials on P/B and ROE instead — that sector-
    specific screen isn't implemented here, so financials are excluded
    rather than silently scored on a ratio that doesn't apply to them.
    EV approximates enterprise value as
    (price x shares + borrowings), NOT netting cash — screener's free
    balance-sheet is too aggregated to isolate a clean cash figure. Share
    count is *derived* (net_profit / eps), not sourced directly, so it
    inherits whatever rounding screener's displayed EPS carries. This is
    scraping a public page outside any official API — rate-limited and
    cached to disk here, but not the same tier of sanctioned access as the
    NSE CSV or Yahoo's chart endpoint (see `universe.py`).
"""

from __future__ import annotations

import calendar
import re
import time
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (SwingLab research tool)"}
_URL_CONSOLIDATED = "https://www.screener.in/company/{symbol}/consolidated/"
_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"

REPORTING_LAG_DAYS = 60  # SEBI LODR Reg. 33: audited annual results due within 60 days of FY end

_MONTHS = {m: i for i, m in enumerate(calendar.month_abbr) if m}


def _period_end(label: str) -> pd.Timestamp | None:
    m = re.match(r"([A-Za-z]{3})\s+(\d{4})", str(label).strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    month, year = _MONTHS[m.group(1)], int(m.group(2))
    last_day = calendar.monthrange(year, month)[1]
    return pd.Timestamp(year=year, month=month, day=last_day)


def _clean_numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        series.astype(str).str.replace(",", "").str.replace("%", "").str.replace("\xa0+", "", regex=True).str.strip(),
        errors="coerce",
    )


def _is_annual(table: pd.DataFrame) -> bool:
    months = [_period_end(c) for c in table.columns[1:]]
    months = [m.month for m in months if m is not None]
    if len(months) < 5:
        return False
    most_common_count = max(months.count(m) for m in set(months))
    return most_common_count / len(months) > 0.8  # same fiscal-year-end month, repeated


def _find_table(tables: list[pd.DataFrame], first_row_contains: str, prefer_annual: bool | None = None) -> pd.DataFrame | None:
    matches = [t for t in tables if len(t) and first_row_contains in str(t.iloc[0, 0])]
    if not matches:
        return None
    if prefer_annual is None:
        return matches[0]
    for t in matches:
        if _is_annual(t) == prefer_annual:
            return t
    return matches[0]


def _row_series(table: pd.DataFrame, label_contains: str) -> pd.Series:
    for i in range(len(table)):
        if label_contains in str(table.iloc[i, 0]):
            row = table.iloc[i, 1:]
            row.index = [_period_end(c) for c in row.index]
            row = row[row.index.notna()]
            return _clean_numeric(row).rename(label_contains)
    return pd.Series(dtype=float)


@dataclass
class FundamentalsHistory:
    symbol: str
    eps: pd.Series               # Rs per share, annual EPS
    revenue: pd.Series           # Rs Cr, "Sales"
    net_profit: pd.Series        # Rs Cr
    operating_profit: pd.Series  # Rs Cr, ~= EBITDA
    interest: pd.Series          # Rs Cr, P&L "Interest" expense
    equity_capital: pd.Series    # Rs Cr
    reserves: pd.Series          # Rs Cr
    borrowings: pd.Series        # Rs Cr
    roce_pct: pd.Series          # screener's own figure, not recomputed
    free_cash_flow: pd.Series    # Rs Cr, screener's own figure
    operating_cash_flow: pd.Series  # Rs Cr, screener's own figure

    @property
    def dates(self) -> list[pd.Timestamp]:
        return sorted(self.eps.index)

    @property
    def book_equity(self) -> pd.Series:
        return (self.equity_capital.add(self.reserves, fill_value=0.0)).dropna()

    @property
    def shares_cr(self) -> pd.Series:
        """Implied shares outstanding, in crores — derived from net_profit / eps
        since screener's free tables don't expose share count directly."""
        eps_nonzero = self.eps.replace(0.0, pd.NA)
        return (self.net_profit / eps_nonzero).dropna()


def fetch_company_page(
    symbol: str, cache_dir: Path = Path(".cache/fundamentals"), force_refresh: bool = False, delay_seconds: float = 1.0
) -> str | None:
    """`symbol` is the bare NSE symbol (no ".NS"). Tries the consolidated
    view first, falls back to standalone for companies without one. Cached
    to disk — this is a one-time cost per ticker, not a per-run one."""
    cache_dir = Path(cache_dir)
    cache_path = cache_dir / f"{symbol}.html"
    if cache_path.exists() and not force_refresh:
        return cache_path.read_text(encoding="utf-8")

    for url in (_URL_CONSOLIDATED.format(symbol=symbol), _URL_STANDALONE.format(symbol=symbol)):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=20)
        except requests.RequestException:
            continue
        if resp.status_code == 200 and "Profit &amp; Loss" in resp.text:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(resp.text, encoding="utf-8")
            time.sleep(delay_seconds)
            return resp.text
        time.sleep(delay_seconds)
    return None


def parse_fundamentals(html: str, symbol: str) -> FundamentalsHistory | None:
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:
        return None

    pl = _find_table(tables, "Sales", prefer_annual=True)
    bs = _find_table(tables, "Equity Capital")
    cf = _find_table(tables, "Cash from Operating Activity")
    ratios = _find_table(tables, "Debtor Days")

    if pl is None or bs is None:
        return None

    return FundamentalsHistory(
        symbol=symbol,
        eps=_row_series(pl, "EPS in Rs"),
        revenue=_row_series(pl, "Sales"),
        net_profit=_row_series(pl, "Net Profit"),
        operating_profit=_row_series(pl, "Operating Profit"),
        interest=_row_series(pl, "Interest"),
        equity_capital=_row_series(bs, "Equity Capital"),
        reserves=_row_series(bs, "Reserves"),
        borrowings=_row_series(bs, "Borrowings"),
        roce_pct=_row_series(ratios, "ROCE") if ratios is not None else pd.Series(dtype=float),
        free_cash_flow=_row_series(cf, "Free Cash Flow") if cf is not None else pd.Series(dtype=float),
        operating_cash_flow=_row_series(cf, "Cash from Operating Activity") if cf is not None else pd.Series(dtype=float),
    )


def asof(hist: FundamentalsHistory, date: pd.Timestamp, reporting_lag_days: int = REPORTING_LAG_DAYS) -> pd.Timestamp | None:
    """The latest reported period whose figures would actually have been
    public by `date` — the point-in-time mechanism. Returns None if no
    period qualifies yet (e.g. `date` is before this company's earliest
    covered annual report + lag).

    `hist.dates` are tz-naive (screener.in has no timezone concept); `date`
    may be tz-aware (the price panels are, in Asia/Kolkata). Both sides are
    compared naive since the fundamentals side is date-only anyway.
    """
    if date.tzinfo is not None:
        date = date.tz_localize(None)
    lag = pd.Timedelta(days=reporting_lag_days)
    eligible = [d for d in hist.dates if d + lag <= date]
    return max(eligible) if eligible else None


def ratios_asof(hist: FundamentalsHistory, date: pd.Timestamp, price: float, reporting_lag_days: int = REPORTING_LAG_DAYS) -> dict | None:
    """P/E, ROE %, EV/EBITDA, D/E, P/B as of `date`, computed via formula
    from whatever period was actually public by then. `price` should be
    the split-adjusted close on `date` (see `totalreturn.py`). Returns
    None if no fundamentals period is available yet."""
    period = asof(hist, date, reporting_lag_days)
    if period is None:
        return None

    eps = hist.eps.get(period)
    net_profit = hist.net_profit.get(period)
    op_profit = hist.operating_profit.get(period)
    equity = hist.book_equity.get(period)
    borrowings = hist.borrowings.get(period, 0.0) or 0.0
    shares = hist.shares_cr.get(period)
    roce = hist.roce_pct.get(period)
    fcf = hist.free_cash_flow.get(period)

    pe = price / eps if eps and eps > 0 else float("nan")
    roe_pct = (net_profit / equity * 100.0) if equity and equity > 0 else float("nan")
    de = (borrowings / equity) if equity and equity > 0 else float("nan")

    market_cap = price * shares if shares and shares > 0 else float("nan")
    ev = market_cap + borrowings if not pd.isna(market_cap) else float("nan")
    ev_ebitda = ev / op_profit if op_profit and op_profit > 0 and not pd.isna(ev) else float("nan")
    pb = market_cap / equity if equity and equity > 0 and not pd.isna(market_cap) else float("nan")

    return {
        "period_end": period,
        "pe": pe,
        "roe_pct": roe_pct,
        "roce_pct": float(roce) if roce is not None and not pd.isna(roce) else float("nan"),
        "ev_ebitda": ev_ebitda,
        "debt_to_equity": de,
        "pb": pb,
        "free_cash_flow_cr": float(fcf) if fcf is not None and not pd.isna(fcf) else float("nan"),
        "net_profit_cr": float(net_profit) if net_profit is not None and not pd.isna(net_profit) else float("nan"),
    }


def fetch_universe_fundamentals(
    symbols: list[str],
    cache_dir: Path = Path(".cache/fundamentals"),
    delay_seconds: float = 1.0,
    force_refresh: bool = False,
    progress: bool = True,
) -> tuple[dict[str, FundamentalsHistory], list[str]]:
    """`symbols` are bare NSE symbols (no ".NS"). Returns (data, failed) —
    same shape as `universe.fetch_universe_prices`."""
    data: dict[str, FundamentalsHistory] = {}
    failed: list[str] = []
    for i, symbol in enumerate(symbols):
        html = fetch_company_page(symbol, cache_dir=cache_dir, force_refresh=force_refresh, delay_seconds=delay_seconds)
        if html is None:
            failed.append(symbol)
            if progress:
                print(f"  [{i+1}/{len(symbols)}] {symbol}: page fetch failed")
            continue
        hist = parse_fundamentals(html, symbol)
        if hist is None or not len(hist.dates):
            failed.append(symbol)
            if progress:
                print(f"  [{i+1}/{len(symbols)}] {symbol}: parse failed / no annual data")
            continue
        data[symbol] = hist
        if progress:
            print(f"  [{i+1}/{len(symbols)}] {symbol}: {len(hist.dates)} annual periods")
    return data, failed
