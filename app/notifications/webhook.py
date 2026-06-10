"""Generic webhook alert sink. Never raises; failures logged and reported."""

import logging

import httpx

from app.notifications.base import Alert

logger = logging.getLogger(__name__)


class WebhookSink:
    name = "webhook"

    def __init__(self, url: str, client: httpx.AsyncClient) -> None:
        self._url = url
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._url)

    async def send(self, alert: Alert) -> bool:
        if not self.configured:
            logger.info("webhook sink not configured; skipping alert %s", alert.pick_id)
            return False
        payload = {
            "pick_id": alert.pick_id,
            "title": alert.title,
            "body": alert.body,
            "dedupe_key": alert.dedupe_key,
        }
        try:
            response = await self._client.post(self._url, json=payload, timeout=15.0)
            if response.status_code >= 300:
                logger.error(
                    "webhook send failed for pick %s: status %d",
                    alert.pick_id,
                    response.status_code,
                )
                return False
            return True
        except httpx.HTTPError as exc:
            logger.error("webhook send error for pick %s: %s", alert.pick_id, type(exc).__name__)
            return False
