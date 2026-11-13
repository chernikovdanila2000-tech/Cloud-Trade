"""Портфельный бэктест стратегии на нескольких парах сразу.

Моделирует то же, что делает живой бот: общий депозит, риск 1% на сделку, не больше
MAX_POSITIONS одновременно, вход по рынку на открытии следующей свечи, SL/TP,
безубыток при +1R, выход по времени, дневной лимит убытка, комиссии и проскальзывание.
Если в одной свече задеты и стоп, и тейк, считается, что сработал стоп (пессимистично).

Пример:
    python backtest.py --start 2024-01 --end 2026-10
    python backtest.py --symbols BTCUSDT ETHUSDT --start 2025-01 --end 2026-10
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from bot import history
from bot.config import RiskConfig, StrategyConfig
from bot.risk import position_size
from bot.strategy import prepare, take_profit, trail_stop

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    "LINKUSDT", "LTCUSDT", "DOTUSDT", "TRXUSDT", "BCHUSDT", "NEARUSDT", "SUIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "ATOMUSDT", "FILUSDT", "INJUSDT", "UNIUSDT", "AAVEUSDT", "ETCUSDT",
]


@dataclass
class Pos:
    symbol: str
    side: str
    entry: float
    qty: float
    stop: float
    tp: float
    risk: float          # первоначальное расстояние до стопа
    opened: pd.Timestamp
    bars: int = 0
    be_moved: bool = False
    extreme: float = 0.0     # лучший экстремум цены с момента входа (для трейлинга)


def _stop_reason(p: Pos) -> str:
    direction = 1 if p.side == "long" else -1
    return "trail/BE stop" if direction * (p.stop - p.entry) >= 0 else "stop"


def run(data: dict[str, pd.DataFrame], s: StrategyConfig, r: RiskConfig, start_balance: float = 1000.0):
    prepared = {sym: prepare(df, s) for sym, df in data.items()}
    timeline = sorted(set().union(*[d.index for d in prepared.values()]))
    balance = start_balance
    peak = balance
    positions: dict[str, Pos] = {}
    pending: list[tuple[str, str, float, float]] = []   # (symbol, side, stop, adx)
    trades, equity_curve = [], []
    day, day_start_balance = None, balance
    cost = r.fee_rate + r.slippage

    def close(p: Pos, price: float, t, reason: str):
        nonlocal balance
        direction = 1 if p.side == "long" else -1
        exit_px = price * (1 - direction * r.slippage)
        pnl = direction * (exit_px - p.entry) * p.qty - exit_px * p.qty * r.fee_rate
        balance += pnl
        trades.append({"symbol": p.symbol, "side": p.side, "opened": p.opened, "closed": t,
                       "entry": p.entry, "exit": exit_px, "pnl": pnl,
                       "r": direction * (exit_px - p.entry) / p.risk, "reason": reason})

    for t in timeline:
        if t.date() != day:
            day, day_start_balance = t.date(), balance
        halted = (day_start_balance - balance) / day_start_balance >= r.daily_loss_limit

        # 1) входы по сигналам прошлой свечи на открытии текущей
        for sym, side, stop, _ in sorted(pending, key=lambda x: -x[3]):
            d = prepared[sym]
            if halted or len(positions) >= r.max_positions or sym in positions or t not in d.index:
                continue
            direction = 1 if side == "long" else -1
            entry = d.at[t, "open"] * (1 + direction * r.slippage)
            if direction * (entry - stop) <= 0:      # гэп через стоп
                continue
            qty = position_size(balance, entry, stop, r)
            if qty <= 0:
                continue
            balance -= entry * qty * r.fee_rate
            positions[sym] = Pos(sym, side, entry, qty, stop, take_profit(side, entry, stop, s.rr),
                                 abs(entry - stop), t, extreme=entry)
        pending = []

        # 2) сопровождение открытых позиций на текущей свече
        for sym in list(positions):
            d = prepared[sym]
            if t not in d.index:
                continue
            p = positions[sym]
            o, hi, lo, c = d.loc[t, ["open", "high", "low", "close"]]
            p.bars += 1
            if p.side == "long":
                if lo <= p.stop:
                    close(p, min(o, p.stop), t, _stop_reason(p))
                elif hi >= p.tp:
                    close(p, max(o, p.tp), t, "take")
                else:
                    if not p.be_moved and hi >= p.entry + s.breakeven_at_r * p.risk:
                        p.stop, p.be_moved = max(p.stop, p.entry * (1 + 2 * cost)), True
                    p.extreme = max(p.extreme, hi)
                    p.stop = trail_stop("long", p.stop, p.extreme, d.at[t, "atr"], s)
                    if p.bars >= s.max_hold_bars:
                        close(p, c, t, "time")
                    else:
                        continue
            else:
                if hi >= p.stop:
                    close(p, max(o, p.stop), t, _stop_reason(p))
                elif lo <= p.tp:
                    close(p, min(o, p.tp), t, "take")
                else:
                    if not p.be_moved and lo <= p.entry - s.breakeven_at_r * p.risk:
                        p.stop, p.be_moved = min(p.stop, p.entry * (1 - 2 * cost)), True
                    p.extreme = min(p.extreme, lo)
                    p.stop = trail_stop("short", p.stop, p.extreme, d.at[t, "atr"], s)
                    if p.bars >= s.max_hold_bars:
                        close(p, c, t, "time")
                    else:
                        continue
            del positions[sym]

        # 3) новые сигналы на закрытой свече t
        for sym, d in prepared.items():
            if t in d.index and sym not in positions:
                row = d.loc[t]
                if row["long_signal"]:
                    pending.append((sym, "long", row["long_stop"], row["adx"]))
                elif row["short_signal"]:
                    pending.append((sym, "short", row["short_stop"], row["adx"]))

        unreal = sum(((1 if p.side == "long" else -1) * (prepared[p.symbol].at[t, "close"] - p.entry) * p.qty)
                     for p in positions.values() if t in prepared[p.symbol].index)
        equity_curve.append((t, balance + unreal))
        peak = max(peak, balance + unreal)

    return pd.DataFrame(trades), pd.Series(dict(equity_curve)).sort_index()


def stats(trades: pd.DataFrame, equity: pd.Series, label: str = "") -> dict:
    if trades.empty:
        return {"period": label, "trades": 0}
    wins = trades[trades.pnl > 0]
    losses = trades[trades.pnl <= 0]
    dd = (equity / equity.cummax() - 1).min()
    days = max((equity.index[-1] - equity.index[0]).days, 1)
    ret = equity.iloc[-1] / equity.iloc[0] - 1
    daily = equity.resample("1D").last().pct_change().dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(365) if daily.std() > 0 else 0
    return {
        "period": label,
        "trades": len(trades),
        "win_rate": f"{len(wins) / len(trades):.1%}",
        "profit_factor": round(wins.pnl.sum() / abs(losses.pnl.sum()), 2) if len(losses) else float("inf"),
        "avg_R": round(trades.r.mean(), 3),
        "return": f"{ret:.1%}",
        "annualized": f"{(1 + ret) ** (365 / days) - 1:.1%}",
        "max_drawdown": f"{dd:.1%}",
        "sharpe": round(sharpe, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_SYMBOLS)
    ap.add_argument("--start", default="2024-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--balance", type=float, default=1000)
    ap.add_argument("--save", default="data/backtest_trades.csv")
    a = ap.parse_args()

    s, r = StrategyConfig(), RiskConfig()
    y0, m0 = map(int, a.start.split("-"))
    y1, m1 = map(int, a.end.split("-"))
    data = {}
    for sym in a.symbols:
        df = history.load(sym, s.timeframe, date(y0, m0, 1), date(y1, m1, 1))
        if len(df) > 500:
            data[sym] = df
        print(f"  {sym}: {len(df)} свечей")

    trades, equity = run(data, s, r, a.balance)
    mid = equity.index[len(equity) // 2]
    rows = [stats(trades, equity, "весь период")]
    for label, mask_t, mask_e in [
        ("1-я половина", trades.opened < mid if len(trades) else None, equity.index < mid),
        ("2-я половина", trades.opened >= mid if len(trades) else None, equity.index >= mid),
    ]:
        rows.append(stats(trades[mask_t] if len(trades) else trades, equity[mask_e], label))
    print("\n" + pd.DataFrame(rows).set_index("period").T.to_string())

    if len(trades):
        print("\nПричины выхода:\n" + trades.reason.value_counts().to_string())
        by_sym = trades.groupby("symbol").agg(trades=("pnl", "size"), pnl=("pnl", "sum"), avg_R=("r", "mean"))
        print("\nПо парам:\n" + by_sym.sort_values("pnl", ascending=False).round(2).to_string())
        monthly = equity.resample("ME").last().pct_change().dropna()
        print("\nПо месяцам:\n" + monthly.map(lambda x: f"{x:+.1%}").to_string())
        trades.to_csv(a.save, index=False)
        print(f"\nСделки сохранены в {a.save}")


if __name__ == "__main__":
    main()
