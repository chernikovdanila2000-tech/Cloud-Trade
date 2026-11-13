"""Запуск торгового бота.

    python run_bot.py            # режим из .env (MODE=paper | testnet | live)
    python run_bot.py --once     # один проход цикла (для проверки) и выход
"""
from __future__ import annotations

import argparse
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from bot.config import BotConfig
from bot.exchange import make_broker
from bot.notifier import Notifier
from bot.trader import Trader


def setup_logging():
    Path("data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), RotatingFileHandler("data/bot.log", maxBytes=5_000_000, backupCount=3)):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def check_orders(broker, symbol: str = "XRP/USDT:USDT", notional: float = 7.0) -> bool:
    """Проверка связки с биржей на минимальной позиции: вход -> SL+TP -> проверка -> закрытие."""
    log = logging.getLogger("check")
    if symbol in broker.positions():
        raise SystemExit(f"По {symbol} уже есть открытая позиция, проверка отменена")
    price = broker.last_price(symbol)
    qty = broker.normalize_qty(symbol, notional / price * 1.05, price)
    if qty <= 0:
        raise SystemExit("Не удалось подобрать минимальный объём для проверки")
    log.info("Баланс: %.2f USDT. Открываю тестовый LONG %s %g (~%.1f USDT)",
             broker.equity(), symbol, qty, qty * price)
    broker.prepare_symbol(symbol, 1)
    ok = False
    try:
        entry = broker.market_order(symbol, "buy", qty)
        sl_id = broker.place_stop(symbol, "long", qty, entry * 0.95)
        tp_id = broker.place_take(symbol, "long", qty, entry * 1.05)
        live = broker.open_trigger_ids(symbol)
        ok = sl_id in live and tp_id in live
        log.info("Вход @ %s; стоп-лосс %s, тейк-профит %s на бирже: %s", entry, sl_id, tp_id,
                 "ДА" if ok else f"НЕТ (открытые условные ордера: {live})")
    finally:
        broker.cancel_all(symbol)
        if symbol in broker.positions():
            broker.market_order(symbol, "sell", qty, reduce_only=True)
        left = broker.positions().get(symbol)
        log.info("Позиция закрыта: %s, ордеров осталось: %d", "ДА" if not left else f"НЕТ {left}",
                 len(broker.open_trigger_ids(symbol)))
    log.info("ПРОВЕРКА ПРОЙДЕНА, можно запускать бота" if ok else "ПРОВЕРКА НЕ ПРОЙДЕНА, бота не запускайте")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один проход цикла и выход")
    ap.add_argument("--check-orders", action="store_true",
                    help="открыть и закрыть минимальную позицию с SL/TP, чтобы проверить связку с биржей")
    a = ap.parse_args()
    setup_logging()
    cfg = BotConfig()
    if cfg.mode == "live" and os.getenv("I_UNDERSTAND_THE_RISK") != "yes":
        raise SystemExit("Режим live торгует реальными деньгами. Добавьте I_UNDERSTAND_THE_RISK=yes в .env")
    broker = make_broker(cfg, os.getenv("PAPER_DATA", "mainnet"))
    trader = Trader(cfg, broker, Notifier(cfg.telegram_token, cfg.telegram_chat_id, f"[{cfg.mode}] "))
    if a.check_orders:
        raise SystemExit(0 if check_orders(broker) else 1)
    if a.once:
        trader.step()
        logging.info("Депозит: %.2f USDT, открытые сделки: %s", broker.equity(), list(trader.trades) or "нет")
    else:
        trader.run_forever()


if __name__ == "__main__":
    main()
