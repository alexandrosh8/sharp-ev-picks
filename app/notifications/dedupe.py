"""Idempotency stores for duplicate-alert prevention."""

from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import WatchError

# How long an UNCHANGED market state stays suppressed. Open picks routinely
# live for days (some are taken weeks before kickoff): a 24h TTL re-alerted
# every still-open same-odds pick daily. Seven days covers the pick->kickoff
# horizon of the dated scrape windows; a price move mints a NEW key (the
# dedupe key includes decimal_odds — notifications/base.py) and alerts
# immediately regardless of this TTL. Settings knob: ALERT_DEDUPE_TTL_SECONDS.
DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class IdempotencyStore(Protocol):
    async def claim(self, key: str, *, legacy_key: str | None = None) -> bool:
        """True if `key` was claimed now (first occurrence), False if seen."""
        ...

    async def release(self, key: str) -> None:
        """Hand back a claim whose dispatch failed, so the next cycle's
        re-dispatch of the same market state retries instead of being
        suppressed by a claim that never reached any channel."""
        ...


class InMemoryIdempotencyStore:
    """Test/dev store; process-local."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def claim(self, key: str, *, legacy_key: str | None = None) -> bool:
        if legacy_key is not None and legacy_key in self._seen:
            # Seed the new per-sink key while the pre-upgrade global claim is
            # still alive, so removing/expiring the bridge cannot replay it.
            self._seen.add(key)
            return False
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    async def release(self, key: str) -> None:
        self._seen.discard(key)


class RedisIdempotencyStore:
    """SET NX EX — atomic first-claim with TTL."""

    def __init__(self, redis: Redis, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis
        self._ttl = ttl_seconds

    async def claim(self, key: str, *, legacy_key: str | None = None) -> bool:
        target = f"alert:dedupe:{key}"
        if legacy_key is None:
            result = await self._redis.set(target, "1", nx=True, ex=self._ttl)
            return bool(result)

        # One-TTL rollout bridge. WATCH makes "legacy global key exists?" and
        # "claim/seed the new per-sink key" one optimistic transaction: an old
        # process cannot create the legacy key between our check and write.
        legacy = f"alert:dedupe:{legacy_key}"
        for _ in range(3):
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(legacy, target)
                    legacy_seen = bool(await pipe.exists(legacy))
                    pipe.multi()
                    pipe.set(target, "1", nx=True, ex=self._ttl)
                    transaction_results = await pipe.execute()
                    return False if legacy_seen else bool(transaction_results[0])
                except WatchError:
                    continue
        # Contended rollout: fail closed rather than replay an unchanged pick.
        return False

    async def release(self, key: str) -> None:
        await self._redis.delete(f"alert:dedupe:{key}")
