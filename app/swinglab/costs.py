"""
Approximate Indian equity-delivery trading costs.

These numbers are illustrative, meant to keep a backtest from reporting
fantasy zero-cost returns, not a source of truth on current broker/exchange/
tax schedules (those change, and vary by broker). Swap the constants in
config.py for your own broker's actual rates before drawing real
conclusions from this.
"""

from __future__ import annotations

from .config import (
    BROKERAGE_CAP,
    BROKERAGE_RATE,
    DP_CHARGE,
    EXCHANGE_CHARGES_RATE,
    SLIPPAGE_RATE,
    STAMP_DUTY_RATE,
    STT_RATE,
)


def entry_cost(trade_value: float) -> float:
    if trade_value <= 0:
        return 0.0
    brokerage = min(trade_value * BROKERAGE_RATE, BROKERAGE_CAP)
    exchange = trade_value * EXCHANGE_CHARGES_RATE
    stamp_duty = trade_value * STAMP_DUTY_RATE
    slippage = trade_value * SLIPPAGE_RATE
    return brokerage + exchange + stamp_duty + slippage


def exit_cost(trade_value: float) -> float:
    if trade_value <= 0:
        return 0.0
    brokerage = min(trade_value * BROKERAGE_RATE, BROKERAGE_CAP)
    exchange = trade_value * EXCHANGE_CHARGES_RATE
    stt = trade_value * STT_RATE
    slippage = trade_value * SLIPPAGE_RATE
    return brokerage + exchange + stt + slippage + DP_CHARGE
