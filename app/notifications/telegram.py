"""Telegram alert sink. Never raises; failures are logged and reported False.

Secret hygiene: the bot token appears only in the request URL; errors are
logged as exception type + status code, never the URL.
"""

import logging

import httpx

from app.notifications.base import Alert
from app.schemas.picks import ALERT_FOOTER

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096


def _bounded_message(body: str) -> str:
    """Fit Telegram's limit while preserving safety/correlation tail text."""
    if len(body) <= TELEGRAM_MESSAGE_LIMIT:
        return body
    marker = "\n…\n"
    available = TELEGRAM_MESSAGE_LIMIT - len(marker)
    # Keep most of the actionable header and a substantial tail. The tail
    # contains the optional correlation warning and mandatory ALERT_FOOTER.
    head_len = (available * 2) // 3
    tail_len = available - head_len
    bounded = f"{body[:head_len].rstrip()}{marker}{body[-tail_len:].lstrip()}"
    if not bounded.endswith(ALERT_FOOTER):
        suffix = f"{marker}{ALERT_FOOTER}"
        bounded = f"{bounded[: TELEGRAM_MESSAGE_LIMIT - len(suffix)].rstrip()}{suffix}"
    return bounded


class TelegramSink:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._token and self._chat_id)

    async def send(self, alert: Alert) -> bool:
        if not self.configured:
            logger.info("telegram sink not configured; skipping alert %s", alert.pick_id)
            return False
        text = _bounded_message(alert.body)
        try:
            response = await self._client.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat_id, "text": text},
                timeout=15.0,
            )
            if response.status_code != 200:
                logger.error(
                    "telegram send failed for pick %s: status %d",
                    alert.pick_id,
                    response.status_code,
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.error("telegram send error for pick %s: %s", alert.pick_id, type(exc).__name__)
            return False
