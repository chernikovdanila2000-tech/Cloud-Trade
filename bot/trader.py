"""Главная логика бота: сканирование пар, вход, сопровождение позиций, риск-лимиты.

Цикл (каждые POLL_SECONDS секунд):
  1. Синхронизация позиций с биржей. Закрылась позиция (сработал SL или TP), значит
     бот снимает оставшиеся ордера и шлёт уведомление.
  2. Защита: у каждой открытой позиции должны стоять стоп-лосс и тейк-профит на бирже.
     Если ордер пропал, он ставится заново, а если цена уже за стопом, позиция закрывается.
  3. На закрытии новой свечи 4h: подтягивается трейлинг-стоп, обновляется список пар,
     ищутся новые сигналы, открываются сделки в пределах лимитов риска.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import indicators as ind
from .config import BotConfig
from .notifier import Notifier
from .risk import daily_limit_hit, drawdown_limit_hit, position_size
from .strategy import last_signal, min_history, take_profit, trail_stop

log = logging.getLogger("trader")


@dataclass
class Trade:
    symbol: str
    side: str
    qty: float
    entry: float
    stop: float
    tp: float
    risk: float
    extreme: float
    opened_at: str
    sl_id: str = ""
    tp_id: str = ""
    bars: int = 0
    be_moved: bool = False


class Trader:
    def __init__(self, cfg: BotConfig, broker, notifier: Notifier):
        self.cfg, self.b, self.n = cfg, broker, notifier
        self.s, self.r = cfg.strategy, cfg.risk
        self.state_path = Path(cfg.state_file)
        self.trades: dict[str, Trade] = {}
        self.day = ""
        self.day_start_equity = 0.0
        self.peak_equity = 0.0
        self.last_candle = ""
        self.halted_reason = ""
        self.universe: list[str] = []
        self._load()

    # ---------- состояние ----------
    def _load(self):
        if self.state_path.exists():
            s = json.loads(self.state_path.read_text())
            self.trades = {k: Trade(**v) for k, v in s.get("trades", {}).items()}
            self.day = s.get("day", "")
            self.day_start_equity = s.get("day_start_equity", 0.0)
            self.peak_equity = s.get("peak_equity", 0.0)
            self.last_candle = s.get("last_candle", "")
            self.halted_reason = s.get("halted_reason", "")

    def _save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "trades": {k: asdict(v) for k, v in self.trades.items()},
            "day": self.day, "day_start_equity": self.day_start_equity,
            "peak_equity": self.peak_equity, "last_candle": self.last_candle,
            "halted_reason": self.halted_reason}, indent=1))

    # ---------- риск ----------
    def _risk_check(self, equity: float) -> bool:
        """True, если новые сделки разрешены."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self.day:
            if self.day:
                self.n.send(f"📊 Итог дня {self.day}: депозит {equity:.2f} USDT "
                            f"({(equity / self.day_start_equity - 1) * 100:+.2f}%)")
            self.day, self.day_start_equity = today, equity
            if self.halted_reason.startswith("daily"):
                self.halted_reason = ""
        self.peak_equity = max(self.peak_equity, equity)
        if not self.halted_reason and daily_limit_hit(self.day_start_equity, equity, self.r):
            self.halted_reason = "daily"
            self.n.send(f"⛔ Дневной лимит убытка {self.r.daily_loss_limit:.0%} достигнут. "
                        "Новых сделок до завтра (UTC) не будет, открытые позиции сопровождаются.")
        if self.halted_reason != "drawdown" and drawdown_limit_hit(self.peak_equity, equity, self.r):
            self.halted_reason = "drawdown"
            self.n.send(f"🛑 Просадка от максимума ≥ {self.r.max_drawdown_stop:.0%}. Торговля остановлена "
                        "до ручной проверки: удалите halted_reason из data/state.json, чтобы продолжить.")
        if Path(self.state_path.parent, "STOP").exists():
            return False
        return not self.halted_reason

    # ---------- синхронизация и защита ----------
    def _sync(self):
        positions = self.b.positions()
        for sym in list(self.trades):
            if sym not in positions:
                t = self.trades.pop(sym)
                self.b.cancel_all(sym)
                price = self.b.last_price(sym)
                d = 1 if t.side == "long" else -1
                r_mult = d * (price - t.entry) / t.risk
                self.n.send(f"✅ Позиция закрыта {sym} {t.side.upper()} (SL/TP на бирже). "
                            f"Вход {t.entry:.6g}, текущая цена {price:.6g} (~{r_mult:+.2f}R)")
        for sym, p in positions.items():
            if sym not in self.trades:
                log.warning("Позиция %s открыта не ботом: бот её не трогает", sym)
        return positions

    def _ensure_protection(self, positions):
        for sym, t in list(self.trades.items()):
            p = positions[sym]
            live = self.b.open_trigger_ids(sym)
            price = self.b.last_price(sym)
            d = 1 if t.side == "long" else -1
            if d * (price - t.stop) <= 0:
                self.n.send(f"⚠️ {sym}: цена за стопом без сработавшего ордера, закрываю по рынку")
                self._close(sym, "аварийное закрытие")
                continue
            if t.sl_id not in live:
                t.sl_id = self.b.place_stop(sym, t.side, p.qty, t.stop)
                log.info("%s: стоп-лосс восстановлен @ %s", sym, t.stop)
            if t.tp_id not in live:
                t.tp_id = self.b.place_take(sym, t.side, p.qty, t.tp)
                log.info("%s: тейк-профит восстановлен @ %s", sym, t.tp)
        self._save()

    def _close(self, sym: str, reason: str):
        t = self.trades.pop(sym)
        self.b.cancel_all(sym)
        price = self.b.market_order(sym, "sell" if t.side == "long" else "buy", t.qty, reduce_only=True)
        d = 1 if t.side == "long" else -1
        self.n.send(f"🔚 {sym} закрыта ({reason}) @ {price:.6g}, {d * (price - t.entry) / t.risk:+.2f}R")
        self._save()

    # ---------- на закрытии свечи ----------
    def _manage_on_candle(self):
        for sym in list(self.trades):
            t = self.trades[sym]
            df = self.b.closed_candles(sym, self.s.timeframe, 100)
            bar = df.iloc[-1]
            atr_val = float(ind.atr(df, self.s.atr_period).iloc[-1])
            t.bars += 1
            t.extreme = max(t.extreme, bar.high) if t.side == "long" else min(t.extreme, bar.low)
            new_stop = t.stop
            d = 1 if t.side == "long" else -1
            if not t.be_moved and d * (t.extreme - t.entry) >= self.s.breakeven_at_r * t.risk:
                be = t.entry * (1 + d * 2 * (self.r.fee_rate + self.r.slippage))
                new_stop = max(new_stop, be) if d > 0 else min(new_stop, be)
                t.be_moved = True
            new_stop = trail_stop(t.side, new_stop, t.extreme, atr_val, self.s)
            if t.bars >= self.s.max_hold_bars:
                self._close(sym, "выход по времени")
                continue
            if d * (new_stop - t.stop) > 0 and d * (self.b.last_price(sym) - new_stop) <= 0:
                self._close(sym, "трейлинг-стоп (цена уже за новым стопом)")
                continue
            if d * (new_stop - t.stop) > 0:
                old_id = t.sl_id
                t.sl_id = self.b.place_stop(sym, t.side, t.qty, new_stop)  # сначала новый стоп,
                self.b.cancel_trigger(sym, old_id)                         # потом снимаем старый
                log.info("%s: стоп подтянут %.6g -> %.6g", sym, t.stop, new_stop)
                t.stop = new_stop
        self._save()

    def _scan_and_enter(self, equity: float):
        free = self.r.max_positions - len(self.trades)
        if free <= 0:
            return
        self.universe = self.b.top_symbols(self.cfg.top_n_symbols, self.cfg.min_quote_volume)
        need = min_history(self.s)
        signals = []
        for sym in self.universe:
            if sym in self.trades:
                continue
            try:
                df = self.b.closed_candles(sym, self.s.timeframe, need)
                if len(df) < need * 0.9:
                    continue
                sig = last_signal(df, self.s)
                if sig:
                    adx = float(ind.adx(df, self.s.adx_period).iloc[-1])
                    signals.append((adx, sym, sig))
            except Exception as e:
                log.warning("Скан %s: %s", sym, e)
        log.info("Скан %d пар: сигналов %d", len(self.universe), len(signals))
        for _, sym, sig in sorted(signals, key=lambda x: x[0], reverse=True)[:free]:
            self._open(sym, sig, equity)

    def _open(self, sym, sig, equity):
        price = self.b.last_price(sym)
        d = 1 if sig.side == "long" else -1
        if d * (price - sig.stop) <= 0:
            return
        qty = self.b.normalize_qty(sym, position_size(equity, price, sig.stop, self.r), price)
        if qty <= 0:
            log.info("%s: объём меньше минимального для биржи, пропуск", sym)
            return
        self.b.prepare_symbol(sym, self.r.leverage)
        entry = self.b.market_order(sym, "buy" if d > 0 else "sell", qty)
        tp = take_profit(sig.side, entry, sig.stop, self.s.rr)
        t = Trade(sym, sig.side, qty, entry, sig.stop, tp, abs(entry - sig.stop), entry,
                  datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.trades[sym] = t
        self._save()
        try:
            t.sl_id = self.b.place_stop(sym, sig.side, qty, sig.stop)
            t.tp_id = self.b.place_take(sym, sig.side, qty, tp)
        except Exception as e:
            self.n.send(f"⚠️ {sym}: не удалось поставить SL/TP ({e}), позиция закрывается")
            self._close(sym, "нет защиты")
            return
        self._save()
        risk_usdt = abs(entry - sig.stop) * qty
        self.n.send(f"🚀 {sym} {sig.side.upper()} {qty:g} @ {entry:.6g}\n"
                    f"SL {sig.stop:.6g} | TP {tp:.6g} | риск {risk_usdt:.2f} USDT")

    # ---------- главный цикл ----------
    def step(self):
        positions = self._sync()
        self._ensure_protection(positions)
        equity = self.b.equity()
        allowed = self._risk_check(equity)
        candle = self.b.closed_candles("BTC/USDT:USDT", self.s.timeframe, 2).index[-1].isoformat()
        if candle != self.last_candle:
            log.info("Новая свеча %s %s, депозит %.2f USDT", self.s.timeframe, candle, equity)
            self.last_candle = candle
            self._manage_on_candle()
            if allowed:
                self._scan_and_enter(equity)
        self._save()

    def run_forever(self):
        self.n.send(f"🤖 Бот запущен: режим {self.cfg.mode.upper()}, стратегия {self.s.name} {self.s.timeframe}, "
                    f"риск {self.r.risk_per_trade:.1%} на сделку, до {self.r.max_positions} позиций, "
                    f"плечо {self.r.leverage}x")
        errors = 0
        while True:
            try:
                self.step()
                errors = 0
            except KeyboardInterrupt:
                raise
            except Exception as e:
                errors += 1
                log.exception("Ошибка цикла")
                if errors in (3, 20):
                    self.n.send(f"⚠️ Ошибка повторяется ({errors} раз подряд): {e}")
            time.sleep(self.cfg.poll_seconds)
