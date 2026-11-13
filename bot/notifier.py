"""Уведомления в Telegram (необязательно: без токена сообщения только пишутся в лог)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger("notify")


class Notifier:
    def __init__(self, token: str, chat_id: str, prefix: str = ""):
        self.token, self.chat_id, self.prefix = token, chat_id, prefix

    def send(self, text: str):
        msg = f"{self.prefix}{text}"
        log.info(msg.replace("\n", " | "))
        if not (self.token and self.chat_id):
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                          json={"chat_id": self.chat_id, "text": msg}, timeout=10)
        except Exception as e:
            log.warning("Telegram недоступен: %s", e)
