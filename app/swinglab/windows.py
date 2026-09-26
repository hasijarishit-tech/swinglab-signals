"""
In-sample/out-of-sample and rolling walk-forward window utilities for the
rule-based strategies in this package.

These strategies don't fit a model (no ML here — SMA crossovers and
momentum ranks aren't parameters learned from data), so there's no leakage
risk from a label depending on future prices the way `swinglab/cv.py`'s
purged embargo exists to prevent. What in-sample/out-of-sample *does* still
guard against is a different, just as real problem: picking whichever
parameter variant (SMA length, momentum lookback, portfolio size...) looks
best on the full history and reporting that as "the result". The discipline
here is: pick on in-sample, report on out-of-sample, and re-check the
picked parameter's stability with a rolling walk-forward pass across the
whole history.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


def _group_last(index: pd.DatetimeIndex, freq: str) -> pd.DatetimeIndex:
    # to_period() drops tz info (and warns) on a tz-aware index; group on
    # naive labels instead and keep the original tz-aware values.
    naive = index.tz_localize(None) if index.tz is not None else index
    labels = naive.to_period(freq)
    s = pd.Series(index, index=index)
    return pd.DatetimeIndex(s.groupby(labels).last().to_numpy())


def month_end_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last trading day actually present in `index` for each calendar
    month — used as monthly rebalance dates by the cross-sectional
    strategies."""
    return _group_last(index, "M")


def quarter_end_dates(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return _group_last(index, "Q")


def asof(index: pd.DatetimeIndex, target: pd.Timestamp) -> pd.Timestamp | None:
    """The latest date in `index` that is <= `target` — "as of" lookup, so
    a signal never resolves to a date after the one it's being computed
    for. Returns None if `target` is before every date in `index`."""
    pos = index.get_indexer([target], method="pad")[0]
    return None if pos < 0 else index[pos]


@dataclass(frozen=True)
class Window:
    start: pd.Timestamp
    end: pd.Timestamp

    def slice(self, series_or_df):
        return series_or_df.loc[self.start : self.end]


def in_sample_out_of_sample(dates: pd.DatetimeIndex, is_fraction: float = 0.6) -> tuple[Window, Window]:
    """Split `dates` chronologically: first `is_fraction` = in-sample
    (parameter selection), the rest = out-of-sample (the number that
    counts)."""
    if len(dates) < 2:
        raise ValueError("Need at least 2 dates to split")
    split_idx = int(len(dates) * is_fraction)
    split_idx = min(max(split_idx, 1), len(dates) - 1)
    is_window = Window(dates[0], dates[split_idx - 1])
    oos_window = Window(dates[split_idx], dates[-1])
    return is_window, oos_window


def rolling_walk_forward(
    dates: pd.DatetimeIndex,
    train_years: float = 5.0,
    test_years: float = 1.0,
    step_years: float = 1.0,
) -> list[tuple[Window, Window]]:
    """Rolling train/test windows across the full history: train on
    `train_years`, test on the following `test_years`, slide forward by
    `step_years`, repeat until the data runs out. Used as a robustness
    check on top of the single in-sample/out-of-sample split — a strategy
    that only looks good on one particular OOS slice isn't robust."""
    if len(dates) < 2:
        return []

    start = dates[0]
    end = dates[-1]
    train_delta = pd.Timedelta(days=int(train_years * 365.25))
    test_delta = pd.Timedelta(days=int(test_years * 365.25))
    step_delta = pd.Timedelta(days=int(step_years * 365.25))

    windows: list[tuple[Window, Window]] = []
    train_start = start
    while True:
        train_end = train_start + train_delta
        test_start = train_end
        test_end = test_start + test_delta
        if test_start > end:
            break
        actual_test_end = min(test_end, end)
        if actual_test_end <= test_start:
            break
        windows.append((Window(train_start, train_end), Window(test_start, actual_test_end)))
        train_start = train_start + step_delta

    return windows
