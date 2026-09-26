"""
Central configuration for SwingLab.

Every "magic number" in the pipeline lives here so a run can be reproduced
and tuned from one place instead of hunting through modules.
"""

from __future__ import annotations

# --- Universe ---
# Three liquid, long-listed NSE large-caps across different sectors, so a
# single sector's regime doesn't dominate the read on the strategy.
TICKERS = ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS"]
BENCHMARK = "%5ENSEI"   # Nifty 50 (URL-encoded ^NSEI)
VIX_SYMBOL = "%5EINDIAVIX"  # India VIX (URL-encoded ^INDIAVIX)

START_DATE = "2019-01-01"
END_DATE = "2026-09-01"

# --- Labeling (triple barrier) ---
SWING_HORIZON = 5          # trading days the label is allowed to play out over
TP_ATR_MULT = 2.0          # take-profit barrier, in multiples of ATR
SL_ATR_MULT = 1.5          # stop-loss barrier, in multiples of ATR
ATR_PERIOD = 14

# --- Risk management on open positions (mirrors the labeling barriers) ---
TRAIL_ATR_MULT = 1.0       # once TP activation is reached, trail by this many ATRs
MAX_HOLD_DAYS = 5          # force a time exit if neither barrier is hit

# --- Primary signal ---
# Composite score quantile threshold, computed on an EXPANDING window (only
# data up to and including "today") so no future information leaks into
# where today's threshold sits. This is stricter than just taking a
# percentile over the whole train/test block.
SCORE_QUANTILE_HIGH = 0.75
SCORE_QUANTILE_LOW = 0.25
MIN_EXPANDING_PERIODS = 60  # need this many days of history before scoring

# --- Meta-model (Random Forest confidence filter) ---
N_ESTIMATORS = 300
RANDOM_STATE = 42
# 0.50 is barely a filter (coin-flip). A confidence sweep (see README /
# tests/test_sensitivity.py) showed higher thresholds trade less often but
# with a meaningfully higher win rate, so this is set above the naive
# default on purpose, not tuned to a single best-looking backtest number.
META_CONFIDENCE = 0.60

FEATURE_COLS = [
    "Return_5d",
    "MA_5_ratio",
    "MA_10_ratio",
    "MA_20_ratio",
    "RSI_14",
    "Volume_change",
    "Volatility_10d",
    "VIX",
    "Nifty_Return_5d",
    "Nifty_MA_20_ratio",
    "BB_pct",
    "MACD_hist",
    "MA_trend",
    "OBV_ratio",
    "Composite_score",
    "Regime_ok",
]

# --- Purged walk-forward CV ---
CV_SPLITS = 5
# Embargo must be at least SWING_HORIZON, since a label dated t can depend on
# prices up to t + SWING_HORIZON. We add a small buffer on top.
PURGE_EMBARGO_DAYS = SWING_HORIZON + 3

# --- Portfolio / position sizing ---
INITIAL_CAPITAL = 100_000.0
RISK_PER_TRADE = 0.01          # fraction of *current* capital risked per trade
MAX_POSITION_FRACTION = 0.35   # cap any single position at this fraction of capital
MAX_CONCURRENT_POSITIONS = 3   # across the whole universe

# --- Indian equity delivery cost model (illustrative, not tax advice) ---
# These are approximate, order-of-magnitude figures meant to keep a backtest
# honest, not a source of truth on current broker/tax schedules. Adjust to
# your actual broker before drawing real conclusions.
BROKERAGE_RATE = 0.0003     # 0.03%, capped below (many discount brokers charge 0 on delivery)
BROKERAGE_CAP = 20.0
STT_RATE = 0.001            # 0.1% of trade value, charged on both legs for delivery
EXCHANGE_CHARGES_RATE = 0.0000297
STAMP_DUTY_RATE = 0.00015   # buy-side only, per SEBI schedule
DP_CHARGE = 15.0            # flat, sell-side only, per scrip per day
SLIPPAGE_RATE = 0.0007      # assumed market-impact/slippage per side
