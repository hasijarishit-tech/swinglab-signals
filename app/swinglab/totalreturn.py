"""
Corporate-action handling: split-adjusting the price series used for signal
generation, and aligning dividends onto the trading calendar for the
portfolio engine to credit as cash on the ex-date.

Two separate things get adjusted for two separate reasons:

  - SPLITS distort price levels, so anything computed off raw Close (a
    200-day SMA, a 52-week high, a momentum return) would see a fake crash
    on the split date if not corrected. `split_adjusted_close` fixes this
    for every strategy's signal math.
  - DIVIDENDS are not adjusted into the price series at all here. Instead
    the portfolio engine credits held positions with dividend cash on the
    ex-date directly (see `portfolio_engine.py`), which is closer to how a
    real account actually receives dividends than inventing a synthetic
    "total return price" nothing was ever traded at. This is also what
    makes it possible to report price return and total return as two
    genuinely different, both-real numbers from the same underlying trades,
    rather than one being a derived approximation of the other.
"""

from __future__ import annotations

import pandas as pd


def split_adjusted_close(close: pd.Series, splits: pd.DataFrame) -> pd.Series:
    """
    Backward-adjust `close` for every split in `splits` (a DataFrame with a
    "ratio" column indexed by ex-date, ratio = new shares per old share,
    e.g. 2.0 for a 2-for-1 split — see `fetch_ohlcv_with_events`).

    Every price strictly before a split's ex-date is divided by that split's
    ratio, so a 2-for-1 split halves all prior prices to match the new
    share count instead of showing as a 50% one-day drop.
    """
    if splits.empty:
        return close.copy()

    factor = pd.Series(1.0, index=close.index)
    for ex_date, row in splits.iterrows():
        ratio = float(row["ratio"])
        if ratio <= 0:
            continue
        factor.loc[close.index < ex_date] /= ratio
    return close * factor


def align_dividends(dividends: pd.Series, calendar: pd.DatetimeIndex) -> pd.Series:
    """
    Reindex `dividends` (sparse, indexed by ex-date) onto `calendar` (the
    ticker's actual trading days), filling non-ex-dates with 0.0. Ex-dates
    that fall on a non-trading day (holiday) roll forward to the next
    trading day, since that's the first day the cash would actually be
    receivable/reflected.
    """
    out = pd.Series(0.0, index=calendar, name="Dividend")
    if dividends.empty:
        return out
    for ex_date, amount in dividends.items():
        pos = calendar.searchsorted(ex_date)
        if pos < len(calendar):
            out.iloc[pos] += float(amount)
    return out
