"""Риск-менеджмент: размер позиции, лимиты на число позиций и убыток за день."""
from __future__ import annotations

from .config import RiskConfig


def position_size(equity: float, entry: float, stop: float, cfg: RiskConfig) -> float:
    """Количество монет, при котором срабатывание стопа = risk_per_trade от депозита.

    Ограничение по марже: номинал позиции не больше equity * leverage / max_positions,
    чтобы все позиции одновременно помещались в депозит без риска ликвидации.
    """
    risk_per_unit = abs(entry - stop) + entry * 2 * (cfg.fee_rate + cfg.slippage)
    if risk_per_unit <= 0 or equity <= 0:
        return 0.0
    qty = equity * cfg.risk_per_trade / risk_per_unit
    max_notional = equity * cfg.leverage / cfg.max_positions
    return min(qty, max_notional / entry)


def daily_limit_hit(day_start_equity: float, equity: float, cfg: RiskConfig) -> bool:
    if day_start_equity <= 0:
        return False
    return (day_start_equity - equity) / day_start_equity >= cfg.daily_loss_limit


def drawdown_limit_hit(peak_equity: float, equity: float, cfg: RiskConfig) -> bool:
    if peak_equity <= 0:
        return False
    return (peak_equity - equity) / peak_equity >= cfg.max_drawdown_stop
