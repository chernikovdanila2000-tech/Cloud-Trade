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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="один проход цикла и выход")
    a = ap.parse_args()
    setup_logging()
    cfg = BotConfig()
    if cfg.mode == "live" and os.getenv("I_UNDERSTAND_THE_RISK") != "yes":
        raise SystemExit("Режим live торгует реальными деньгами. Добавьте I_UNDERSTAND_THE_RISK=yes в .env")
    broker = make_broker(cfg, os.getenv("PAPER_DATA", "mainnet"))
    trader = Trader(cfg, broker, Notifier(cfg.telegram_token, cfg.telegram_chat_id, f"[{cfg.mode}] "))
    if a.once:
        trader.step()
        logging.info("Депозит: %.2f USDT, открытые сделки: %s", broker.equity(), list(trader.trades) or "нет")
    else:
        trader.run_forever()


if __name__ == "__main__":
    main()
