"""Все настройки бота. Значения берутся из .env, ниже указаны значения по умолчанию."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _i(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class StrategyConfig:
    name: str = "breakout"         # breakout (по умолчанию, прошла бэктест) | pullback
    timeframe: str = "4h"          # рабочий таймфрейм
    htf_factor: int = 6            # старший ТФ = 6 x 4h = 1 день (тренд-фильтр)
    ema_fast: int = 21
    ema_slow: int = 50
    htf_ema_fast: int = 50
    htf_ema_slow: int = 200
    rsi_period: int = 14
    rsi_long_min: float = 50.0
    rsi_long_max: float = 70.0
    rsi_short_min: float = 30.0
    rsi_short_max: float = 50.0
    adx_period: int = 14
    adx_min: float = 20.0
    atr_period: int = 14
    pullback_lookback: int = 3     # откат к EMA21 должен быть не раньше N свечей назад
    swing_lookback: int = 5        # стоп за локальным экстремумом последних N свечей
    sl_atr_buffer: float = 0.2     # запас за экстремумом в ATR
    sl_atr_min: float = 1.0        # стоп не ближе 1 ATR
    sl_atr_max: float = 3.0        # и не дальше 3 ATR (иначе сделку пропускаем)
    rr: float = 3.0                # тейк-профит = 3R
    breakeven_at_r: float = 99.0   # безубыток при +N R (99 = выкл., трейлинг делает это сам)
    max_hold_bars: int = 10**6     # выход по времени (по умолчанию выкл.)
    breakout_lookback: int = 55    # пробой максимума/минимума последних N свечей
    sl_atr: float = 2.0            # стоп для пробоя: 2 ATR от входа
    trail_atr: float = 3.0         # трейлинг-стоп: экстремум с момента входа -/+ N ATR (0 = выкл.)


@dataclass
class RiskConfig:
    risk_per_trade: float = field(default_factory=lambda: _f("RISK_PER_TRADE", 0.01))
    max_positions: int = field(default_factory=lambda: _i("MAX_POSITIONS", 5))
    leverage: int = field(default_factory=lambda: _i("LEVERAGE", 3))
    daily_loss_limit: float = field(default_factory=lambda: _f("DAILY_LOSS_LIMIT", 0.03))
    max_drawdown_stop: float = field(default_factory=lambda: _f("MAX_DRAWDOWN_STOP", 0.15))
    fee_rate: float = 0.0005       # taker-комиссия Binance Futures
    slippage: float = 0.0002


@dataclass
class BotConfig:
    mode: str = field(default_factory=lambda: os.getenv("MODE", "paper").lower())  # paper | testnet | live
    api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""))
    top_n_symbols: int = field(default_factory=lambda: _i("TOP_N_SYMBOLS", 25))
    min_quote_volume: float = field(default_factory=lambda: _f("MIN_QUOTE_VOLUME", 50_000_000))
    paper_balance: float = field(default_factory=lambda: _f("PAPER_BALANCE", 1000))
    poll_seconds: int = field(default_factory=lambda: _i("POLL_SECONDS", 30))
    telegram_token: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat_id: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    state_file: str = field(default_factory=lambda: os.getenv("STATE_FILE", "data/state.json"))
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)


EXCLUDED_BASES = {"USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "EUR", "BTCDOM"}
