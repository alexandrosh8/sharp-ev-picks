"""Alert dispatcher with cancellation-safe per-sink idempotency.

Each configured delivery channel owns an independent claim. A partial fan-out
therefore retries only failed channels: a healthy channel is never duplicated,
and a transiently failed channel is never suppressed for seven days.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.notifications.base import Alert, AlertSink
from app.notifications.dedupe import IdempotencyStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    alert: Alert
    skipped_duplicate: bool
    sink_results: tuple[tuple[str, bool], ...]  # (sink name, delivered)


class AlertDispatcher:
    def __init__(self, sinks: Sequence[AlertSink], store: IdempotencyStore) -> None:
        self._sinks = tuple(sinks)
        self._store = store

    def _sink_key(self, alert: Alert, sink: AlertSink, index: int) -> str:
        # Stable across reordering distinct channels, while an occurrence
        # ordinal keeps duplicate configured instances of one sink name from
        # suppressing each other.
        ordinal = sum(1 for prior in self._sinks[:index] if prior.name == sink.name)
        return f"{alert.dedupe_key}:sink:{sink.name}:{ordinal}"

    async def _release(self, key: str) -> None:
        """Complete a release even when the caller is being cancelled."""
        task = asyncio.create_task(self._store.release(key))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if task.done() and not task.cancelled():
                task.exception()
            raise

    async def _claim(self, key: str, *, legacy_key: str | None = None) -> bool:
        """Make claim cancellation atomic from the dispatcher's perspective.

        Redis SET NX may complete while the surrounding job is cancelled. Wait
        for its result; if it won the key, release it before propagating the
        cancellation so a notification is never consumed without a send.
        """
        task = asyncio.create_task(self._store.claim(key, legacy_key=legacy_key))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            claimed = False
            while not task.done():
                try:
                    claimed = await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
                else:
                    break
            if task.done() and not task.cancelled():
                try:
                    claimed = task.result()
                except Exception:
                    claimed = False
            if claimed:
                await self._release(key)
            raise

    async def dispatch(self, alert: Alert) -> DispatchResult:
        results: list[tuple[str, bool]] = []
        any_new_claim = False
        any_sink = False
        for index, sink in enumerate(self._sinks):
            any_sink = True
            key = self._sink_key(alert, sink, index)
            if not await self._claim(key, legacy_key=alert.dedupe_key):
                # The sink-specific claim exists because this channel already
                # accepted the alert. Report success, not a fresh failure, while
                # skipping the network call.
                results.append((sink.name, True))
                continue
            any_new_claim = True
            configured = bool(getattr(sink, "configured", True))
            try:
                delivered = await sink.send(alert)
            except asyncio.CancelledError:
                await self._release(key)
                raise
            except Exception as exc:  # sinks should not raise; belt and braces
                logger.error("sink %s raised %s", sink.name, type(exc).__name__)
                delivered = False
            results.append((sink.name, delivered))
            if configured and not delivered:
                await self._release(key)
                logger.warning(
                    "alert for pick %s failed at sink %s; sink claim released for retry",
                    alert.pick_id,
                    sink.name,
                )
            # An unconfigured channel is a deliberate no-op. Its claim sticks so
            # a no-channel deployment does not rebuild/retry the same alert each
            # poll forever.

        skipped = any_sink and not any_new_claim
        if skipped:
            logger.info("duplicate alert suppressed for pick %s", alert.pick_id)
        return DispatchResult(
            alert=alert,
            skipped_duplicate=skipped,
            sink_results=tuple(results),
        )
