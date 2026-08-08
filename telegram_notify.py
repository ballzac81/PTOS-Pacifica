"""Minimal Telegram notifier used by PTOS."""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.enabled = bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            logger.info(f"[telegram disabled] {text}")
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text[:4000]},
                timeout=10,
            )
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
