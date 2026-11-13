"""Проверка, что сигналы не заглядывают в будущее.

Если обрезать историю на свече i, сигнал и стоп на этой свече должны совпасть
с расчётом по полной истории. Иначе бэктест был бы нечестным.
"""
import numpy as np
import pandas as pd

from bot.config import StrategyConfig
from bot.strategy import prepare


def _random_walk(n=2500, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.02, n)))
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) * (1 + rng.uniform(0, 0.01, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0, 0.01, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)


def test_no_lookahead():
    cfg = StrategyConfig()
    df = _random_walk()
    full = prepare(df, cfg)
    for i in range(1500, len(df), 37):
        cut = prepare(df.iloc[: i + 1], cfg).iloc[-1]
        row = full.iloc[i]
        for col in ("long_signal", "short_signal"):
            assert bool(cut[col]) == bool(row[col]), (i, col)
        for col in ("h_ema_f", "long_stop", "short_stop"):
            assert np.isclose(cut[col], row[col], equal_nan=True), (i, col)


def test_produces_signals():
    full = prepare(_random_walk(), StrategyConfig())
    assert full["long_signal"].sum() + full["short_signal"].sum() > 0
