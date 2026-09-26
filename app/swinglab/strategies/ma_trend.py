"""
Strategy 1: Moving Average Trend Following.

Long-only, per-stock: hold a stock whenever its (split-adjusted) close is
above its own N-day SMA, flat otherwise. Checked every trading day (as
specified), but a stock only actually gets bought or sold on the day its
own above/below-SMA *state* changes — that's what "avoid unnecessary
transactions" means here.

Portfolio construction (not specified in the request, documented as an
assumption): by default, dynamic equal-weight across every currently-
qualifying stock in the universe, uncapped. This is fine at a small
universe (Nifty 50), but at Nifty 500 scale it's a real, disclosed failure
mode: on days when 200-400+ stocks simultaneously qualify, capital
fragments into positions worth a few hundred rupees each, and a single
flat per-scrip exchange fee then exceeds a position's own value — every
SMA variant tested on the full Nifty 500 destroys ~98% of capital this
way (see the project's sensitivity output). That collapse is reported as
a real finding, not silently patched around.

`max_positions` (optional) is the fix for anyone who wants a usable
version of this strategy at broad-universe scale: cap concurrent holdings
and keep them sticky — an existing holder isn't ejected just because more
names started qualifying elsewhere, only exits when its own state flips
back below the SMA — so newly-qualifying names only fill *open* slots.
Since "moving average trend following" has no built-in ranking signal to
break ties with, slots fill in a fixed, deterministic order (column
order) among today's new qualifiers, not by any measure of trend strength.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

SMA_VARIANTS = [100, 150, 200, 250]


def compute_target_weights(close_panel: pd.DataFrame, sma_period: int, max_positions: int | None = None) -> pd.DataFrame:
    sma = close_panel.rolling(sma_period, min_periods=sma_period).mean()
    qualifies = close_panel > sma

    if max_positions is None:
        count = qualifies.sum(axis=1).replace(0, np.nan)
        return qualifies.div(count, axis=0).fillna(0.0)

    return _capped_weights(close_panel, qualifies, max_positions)


def _capped_weights(close_panel: pd.DataFrame, qualifies: pd.DataFrame, max_positions: int) -> pd.DataFrame:
    index = close_panel.index
    weights = pd.DataFrame(0.0, index=index, columns=close_panel.columns)
    held: set[str] = set()

    for date in index:
        row = qualifies.loc[date]
        held &= set(row[row].index)  # drop anyone who no longer qualifies
        if len(held) < max_positions:
            for tkr in row[row].index:  # fixed, deterministic order
                if len(held) >= max_positions:
                    break
                if tkr not in held:
                    held.add(tkr)
        if held:
            weights.loc[date, list(held)] = 1.0 / len(held)

    return weights
