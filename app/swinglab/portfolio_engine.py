"""
One shared, long-only, multi-asset portfolio backtest engine used by every
strategy in `swinglab/strategies/`.

Every strategy module's actual job is just to produce a `target_weights`
DataFrame (dates x tickers, values in [0, 1], forward-filled between
formation/signal dates so the target only changes on a real decision day).
This engine is the one place that turns a change in target weights into
trades, applies `swinglab.costs`, tracks cash/shares, and reports both a
price-return and a total-return equity curve from the same trade sequence
— which is what makes "distinguish price return from total return" a real
distinction here rather than a derived approximation. Position sizing is
computed once, here, the same way this project's existing swing backtester
(`backtest.py`) keeps to one sizing implementation for its own strategy.

Trades only happen on a date where the *input* `target_weights` row
actually changed from the previous row — natural price drift between
rebalances is not chased daily, which is what "avoid unnecessary
transactions" means in practice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import entry_cost, exit_cost
from .universe import TickerData

EPS = 1e-9


def build_price_panels(
    universe_data: dict[str, TickerData], start: str, end: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Align every ticker's split-adjusted close and per-share dividend onto a
    shared calendar (the union of all trading days in range), NaN/0 where a
    ticker has no data that day (not yet listed, delisted, or a holiday
    mismatch).
    """
    start_ts, end_ts = pd.Timestamp(start, tz="Asia/Kolkata"), pd.Timestamp(end, tz="Asia/Kolkata")
    calendar = sorted(
        set().union(
            *(td.signal_close.index[(td.signal_close.index >= start_ts) & (td.signal_close.index <= end_ts)]
              for td in universe_data.values())
        )
    )
    calendar = pd.DatetimeIndex(calendar)

    close_panel = pd.DataFrame(index=calendar, columns=list(universe_data.keys()), dtype=float)
    div_panel = pd.DataFrame(0.0, index=calendar, columns=list(universe_data.keys()), dtype=float)
    for tkr, td in universe_data.items():
        close_panel[tkr] = td.signal_close.reindex(calendar)
        div_panel[tkr] = td.dividend.reindex(calendar).fillna(0.0)

    return close_panel, div_panel


@dataclass
class _Ledger:
    """One independent cash+shares book. TR and PR each get their own —
    see the note in `PortfolioEngine.run` on why they can't share one."""
    cash: float
    shares: dict[str, float] = field(default_factory=dict)
    entry_info: dict[str, tuple[pd.Timestamp, float]] = field(default_factory=dict)
    just_closed: list[tuple[str, pd.Timestamp, float, float]] = field(default_factory=list)


def _max_affordable_buy(requested_value: float, available_cash: float) -> float:
    """The largest buy_value <= requested_value such that
    buy_value + entry_cost(buy_value) <= available_cash.

    entry_cost is (nearly) linear in trade value — the only non-linearity is
    the brokerage cap, which only ever *reduces* cost — so a few fixed-point
    iterations converge to within a fraction of a rupee. Solving this
    properly (rather than capping buy_value and then subtracting cost
    computed on the pre-cap value) is what keeps cash from going negative
    when a rebalance wants to buy more than is actually affordable.
    """
    if requested_value <= 0:
        return 0.0
    if requested_value + entry_cost(requested_value) <= available_cash:
        return requested_value
    value = max(0.0, available_cash - entry_cost(requested_value))
    for _ in range(4):
        value = max(0.0, min(requested_value, available_cash - entry_cost(value)))
    return value


def _rebalance(ledger: _Ledger, target_row: pd.Series, prices_today: pd.Series) -> float:
    """Move `ledger` from its current holdings toward `target_row`
    (fractions of ledger equity), sells first then buys. Returns this
    rebalance's turnover (as a fraction of equity). Appends any fully-
    closed positions to `ledger.just_closed`."""
    equity_now = ledger.cash + sum(ledger.shares.get(t, 0.0) * prices_today.get(t, 0.0) for t in ledger.shares)
    if equity_now <= 0:
        return 0.0

    deltas: dict[str, float] = {}
    for tkr in set(ledger.shares) | set(target_row[target_row > EPS].index):
        price = prices_today.get(tkr, np.nan)
        if pd.isna(price) or price <= 0:
            continue
        target_value = float(target_row.get(tkr, 0.0)) * equity_now
        current_value = ledger.shares.get(tkr, 0.0) * price
        deltas[tkr] = target_value - current_value

    turnover = sum(abs(d) for d in deltas.values()) / (2.0 * equity_now)

    for tkr, delta in sorted(deltas.items(), key=lambda kv: kv[1]):
        price = prices_today[tkr]
        if delta < -EPS:
            sell_value = -delta
            # A flat per-scrip fee (costs.DP_CHARGE) can exceed a tiny
            # position's entire value once a portfolio has fragmented into
            # many small holdings — a real cost, not a bug, but a cash
            # account can't go into debt over it the way this simulation
            # has no margin/borrowing to model, so the floor is 0.
            ledger.cash = max(0.0, ledger.cash + sell_value - exit_cost(sell_value))
            ledger.shares[tkr] = ledger.shares.get(tkr, 0.0) - sell_value / price
            if ledger.shares[tkr] <= EPS * price:
                if tkr in ledger.entry_info:
                    e_date, e_price = ledger.entry_info.pop(tkr)
                    ledger.just_closed.append((tkr, e_date, e_price, price))
                ledger.shares.pop(tkr, None)
        elif delta > EPS:
            buy_value = _max_affordable_buy(delta, ledger.cash)
            ledger.cash -= buy_value + entry_cost(buy_value)
            if buy_value > 0:
                ledger.shares[tkr] = ledger.shares.get(tkr, 0.0) + buy_value / price
                if tkr not in ledger.entry_info:
                    ledger.entry_info[tkr] = (prices_today.name, price)

    return turnover


@dataclass
class ClosedPosition:
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    pnl_pct: float
    hold_days: int


@dataclass
class OpenPosition:
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float


@dataclass
class PortfolioResult:
    equity_curve: pd.Series           # total-return curve (dividends credited)
    price_return_curve: pd.Series     # same trades, dividends NOT credited
    weights_history: pd.DataFrame     # realized weight per ticker per day
    holdings_log: list[dict] = field(default_factory=list)   # one entry per formation date
    closed_positions: list[ClosedPosition] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)  # still held when the backtest ended
    turnover_history: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


class PortfolioEngine:
    def __init__(self, initial_capital: float = 100_000.0) -> None:
        self.initial_capital = initial_capital

    def run(
        self,
        close_panel: pd.DataFrame,
        target_weights: pd.DataFrame,
        dividend_panel: pd.DataFrame | None = None,
    ) -> PortfolioResult:
        """
        `close_panel`, `target_weights` (and `dividend_panel`, if given)
        must share the same DatetimeIndex and columns (use
        `build_price_panels` + a strategy's own weight-generation to
        produce these consistently).
        """
        dates = close_panel.index
        tickers = close_panel.columns
        target_weights = target_weights.reindex(index=dates, columns=tickers).fillna(0.0)
        if dividend_panel is None:
            dividend_panel = pd.DataFrame(0.0, index=dates, columns=tickers)
        else:
            dividend_panel = dividend_panel.reindex(index=dates, columns=tickers).fillna(0.0)

        tr = _Ledger(cash=self.initial_capital)
        pr = _Ledger(cash=self.initial_capital)

        equity_tr, equity_pr = [], []
        weights_rows = []
        holdings_log: list[dict] = []
        closed: list[ClosedPosition] = []
        turnover_dates, turnover_vals = [], []

        prev_target_row = pd.Series(0.0, index=tickers)  # implicit start: fully in cash

        for date in dates:
            prices_today = close_panel.loc[date]

            # 1) dividends on currently-held positions credit the total-return
            #    ledger's cash only — the price-return ledger never sees them.
            for tkr, qty in tr.shares.items():
                div = dividend_panel.at[date, tkr] if tkr in dividend_panel.columns else 0.0
                if div and qty > 0:
                    tr.cash += qty * div

            # 2) rebalance only if today's target actually differs from yesterday's.
            #    TR and PR are rebalanced independently — each sized off its own
            #    equity — since a dividend-swollen TR portfolio legitimately
            #    buys/sells different share quantities than the PR one would for
            #    the exact same target weight.
            target_row = target_weights.loc[date]
            changed = not target_row.equals(prev_target_row)
            if changed:
                turnover = _rebalance(tr, target_row, prices_today)
                _rebalance(pr, target_row, prices_today)

                for tkr, e_date, e_price, exit_price in tr.just_closed:
                    closed.append(ClosedPosition(
                        ticker=tkr, entry_date=e_date, exit_date=date,
                        entry_price=e_price, exit_price=exit_price,
                        pnl_pct=(exit_price / e_price - 1.0) * 100.0,
                        hold_days=(date - e_date).days,
                    ))
                tr.just_closed.clear()
                pr.just_closed.clear()

                turnover_dates.append(date)
                turnover_vals.append(turnover)

                held = {t: q for t, q in tr.shares.items() if q > EPS}
                equity_snapshot = tr.cash + sum(q * prices_today.get(t, 0.0) for t, q in held.items())
                holdings_log.append({
                    "date": str(date.date()),
                    "tickers": list(held.keys()),
                    "weights": {t: round(q * prices_today.get(t, 0.0) / equity_snapshot, 4) for t, q in held.items()}
                    if equity_snapshot > 0 else {},
                })

            prev_target_row = target_row

            # 3) mark to market
            tr_position_value = sum(tr.shares.get(t, 0.0) * prices_today.get(t, 0.0) for t in tr.shares)
            pr_position_value = sum(pr.shares.get(t, 0.0) * prices_today.get(t, 0.0) for t in pr.shares)
            equity_tr.append(tr.cash + tr_position_value)
            equity_pr.append(pr.cash + pr_position_value)
            tr_equity_today = tr.cash + tr_position_value
            weights_rows.append({
                t: (tr.shares.get(t, 0.0) * prices_today.get(t, 0.0)) / tr_equity_today
                if tr_equity_today > 0 else 0.0
                for t in tickers
            })

        equity_curve = pd.Series(equity_tr, index=dates)
        price_return_curve = pd.Series(equity_pr, index=dates)
        weights_history = pd.DataFrame(weights_rows, index=dates)
        turnover_history = pd.Series(turnover_vals, index=pd.DatetimeIndex(turnover_dates))
        open_positions = [
            OpenPosition(ticker=tkr, entry_date=e_date, entry_price=e_price)
            for tkr, (e_date, e_price) in tr.entry_info.items()
        ]

        return PortfolioResult(
            equity_curve=equity_curve,
            price_return_curve=price_return_curve,
            weights_history=weights_history,
            holdings_log=holdings_log,
            closed_positions=closed,
            open_positions=open_positions,
            turnover_history=turnover_history,
        )
