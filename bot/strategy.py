"""Стратегии бота.

breakout (по умолчанию): пробой канала Дончиана по тренду.
  Лонг (шорт зеркально): дневной тренд вверх (EMA50 > EMA200, цена выше EMA50),
  ADX >= 20, свеча 4h закрылась выше максимума последних 55 свечей.
  Стоп 2 ATR, тейк 3R, трейлинг-стоп 3 ATR от лучшей цены с момента входа.

pullback: торговля по тренду с входом на откате (на бэктесте убыточна, оставлена для сравнения).
Логика (лонг; шорт зеркально):
1. Старший ТФ (4h): EMA50 > EMA200 и цена выше EMA50, то есть восходящий тренд.
2. Рабочий ТФ (1h): EMA21 > EMA50, ADX >= 20 (тренд есть, а не флэт).
3. Откат: за последние 3 свечи low касался EMA21.
4. Подтверждение: свеча закрылась выше EMA21, она бычья, RSI в диапазоне 50–70
   (импульс вверх, но без перекупленности).
5. Стоп: за минимумом последних 5 свечей с запасом 0.2 ATR, в пределах 1–3 ATR.
6. Тейк: 2R. При +1R стоп переносится в безубыток.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import indicators as ind
from .config import StrategyConfig

TF_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


@dataclass
class Signal:
    side: str          # "long" | "short"
    price: float       # цена закрытия сигнальной свечи
    stop: float
    atr: float
    time: pd.Timestamp


def prepare(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """Добавляет индикаторы и колонки long_signal / short_signal / stop.

    df: свечи с индексом по времени ОТКРЫТИЯ (UTC) и колонками open/high/low/close/volume.
    Сигнал на строке i считается по ЗАКРЫТОЙ свече i; вход происходит на открытии i+1.
    """
    d = df.copy()
    d["ema_f"] = ind.ema(d["close"], cfg.ema_fast)
    d["ema_s"] = ind.ema(d["close"], cfg.ema_slow)
    d["rsi"] = ind.rsi(d["close"], cfg.rsi_period)
    d["adx"] = ind.adx(d, cfg.adx_period)
    d["atr"] = ind.atr(d, cfg.atr_period)

    # --- старший таймфрейм без заглядывания в будущее ---
    tf_min = TF_MINUTES[cfg.timeframe]
    htf_rule = f"{tf_min * cfg.htf_factor}min"
    htf = d[["open", "high", "low", "close"]].resample(htf_rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    htf["h_ema_f"] = ind.ema(htf["close"], cfg.htf_ema_fast)
    htf["h_ema_s"] = ind.ema(htf["close"], cfg.htf_ema_slow)
    htf["h_close"] = htf["close"]
    # значения 4h-свечи доступны только после её закрытия
    htf.index = htf.index + pd.Timedelta(minutes=tf_min * cfg.htf_factor)
    close_time = d.index + pd.Timedelta(minutes=tf_min)
    h = htf[["h_ema_f", "h_ema_s", "h_close"]].reindex(close_time, method="ffill")
    h.index = d.index
    d = d.join(h)

    htf_up = (d["h_ema_f"] > d["h_ema_s"]) & (d["h_close"] > d["h_ema_f"])
    htf_down = (d["h_ema_f"] < d["h_ema_s"]) & (d["h_close"] < d["h_ema_f"])

    if cfg.name == "breakout":
        return _breakout(d, cfg, htf_up, htf_down)

    lb = cfg.pullback_lookback
    touched_from_above = (d["low"] <= d["ema_f"]).rolling(lb).max().astype(bool)
    touched_from_below = (d["high"] >= d["ema_f"]).rolling(lb).max().astype(bool)
    trending = d["adx"] >= cfg.adx_min

    long_sig = (
        htf_up & trending & (d["ema_f"] > d["ema_s"]) & touched_from_above
        & (d["close"] > d["ema_f"]) & (d["close"] > d["open"])
        & d["rsi"].between(cfg.rsi_long_min, cfg.rsi_long_max)
    )
    short_sig = (
        htf_down & trending & (d["ema_f"] < d["ema_s"]) & touched_from_below
        & (d["close"] < d["ema_f"]) & (d["close"] < d["open"])
        & d["rsi"].between(cfg.rsi_short_min, cfg.rsi_short_max)
    )

    swing_low = d["low"].rolling(cfg.swing_lookback).min() - cfg.sl_atr_buffer * d["atr"]
    swing_high = d["high"].rolling(cfg.swing_lookback).max() + cfg.sl_atr_buffer * d["atr"]
    long_dist = (d["close"] - swing_low).clip(lower=cfg.sl_atr_min * d["atr"])
    short_dist = (swing_high - d["close"]).clip(lower=cfg.sl_atr_min * d["atr"])

    d["long_signal"] = long_sig & (long_dist <= cfg.sl_atr_max * d["atr"])
    d["short_signal"] = short_sig & (short_dist <= cfg.sl_atr_max * d["atr"])
    d["long_stop"] = d["close"] - long_dist
    d["short_stop"] = d["close"] + short_dist
    d[["long_signal", "short_signal"]] = d[["long_signal", "short_signal"]].fillna(False)
    return d


def _breakout(d: pd.DataFrame, cfg: StrategyConfig, htf_up: pd.Series, htf_down: pd.Series) -> pd.DataFrame:
    """Пробой канала Дончиана по направлению старшего тренда."""
    n = cfg.breakout_lookback
    upper = d["high"].shift(1).rolling(n).max()
    lower = d["low"].shift(1).rolling(n).min()
    trending = d["adx"] >= cfg.adx_min
    d["long_signal"] = (htf_up & trending & (d["close"] > upper)
                        & (d["close"].shift(1) <= upper.shift(1))).fillna(False)
    d["short_signal"] = (htf_down & trending & (d["close"] < lower)
                         & (d["close"].shift(1) >= lower.shift(1))).fillna(False)
    d["long_stop"] = d["close"] - cfg.sl_atr * d["atr"]
    d["short_stop"] = d["close"] + cfg.sl_atr * d["atr"]
    return d


def trail_stop(side: str, stop: float, extreme: float, atr_value: float, cfg: StrategyConfig) -> float:
    """Подтягивает стоп за ценой; никогда не отодвигает его назад."""
    if cfg.trail_atr <= 0:
        return stop
    if side == "long":
        return max(stop, extreme - cfg.trail_atr * atr_value)
    return min(stop, extreme + cfg.trail_atr * atr_value)


def last_signal(df: pd.DataFrame, cfg: StrategyConfig) -> Signal | None:
    """Сигнал по последней закрытой свече (df должен содержать только закрытые свечи)."""
    d = prepare(df, cfg)
    row = d.iloc[-1]
    if bool(row["long_signal"]):
        return Signal("long", float(row["close"]), float(row["long_stop"]), float(row["atr"]), d.index[-1])
    if bool(row["short_signal"]):
        return Signal("short", float(row["close"]), float(row["short_stop"]), float(row["atr"]), d.index[-1])
    return None


def take_profit(side: str, entry: float, stop: float, rr: float) -> float:
    risk = abs(entry - stop)
    return entry + rr * risk if side == "long" else entry - rr * risk


def min_history(cfg: StrategyConfig) -> int:
    """Сколько свечей рабочего ТФ нужно для прогрева индикаторов."""
    return cfg.htf_ema_slow * cfg.htf_factor + 300
