"""Notifications: idempotency, fan-out, never-raise sinks, reminder presence."""

from datetime import UTC, datetime
from decimal import Decimal

import fakeredis.aioredis

from app.notifications.base import Alert, build_pick_alert
from app.notifications.dedupe import (
    DEFAULT_TTL_SECONDS,
    InMemoryIdempotencyStore,
    RedisIdempotencyStore,
)
from app.notifications.dispatcher import AlertDispatcher
from app.schemas.base import Market
from app.schemas.picks import ALERT_FOOTER, PickOut, StakeBreakdownOut


class RecordingSink:
    name = "recording"

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.sent.append(alert)
        return True


class ExplodingSink:
    name = "exploding"

    async def send(self, alert: Alert) -> bool:
        raise RuntimeError("boom")


def make_alert(key: str = "k1") -> Alert:
    return Alert(pick_id="p1", title="t", body="b", dedupe_key=key)


def make_pick() -> PickOut:
    return PickOut(
        pick_id="pick-1",
        sport="soccer",
        league="EPL",
        event="Alpha FC vs Beta United",
        event_id="evt-abc",
        market=Market.H2H,
        selection="Alpha FC",
        bookmaker="bookie_one",
        decimal_odds=2.1,
        model_probability=0.55,
        fair_probability=0.50,
        edge=0.05,
        ev=0.155,
        confidence=0.75,
        recommended_stake_fraction=0.02,
        recommended_stake_amount=Decimal("20.00"),
        stake_breakdown=StakeBreakdownOut(
            raw_kelly=0.10, fractional=0.025, capped=True, final=0.02
        ),
        odds_age_seconds=60.0,
        liquidity=None,
        reason_summary="model edge over devigged market",
        created_at=datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
    )


def test_pick_alert_tags_premium_tier() -> None:
    alert = build_pick_alert(make_pick())  # default tier="premium"
    assert "⭐ PREMIUM" in alert.title
    assert "⭐ PREMIUM" in alert.body


def test_pick_alert_tags_volume_tier() -> None:
    alert = build_pick_alert(make_pick().model_copy(update={"tier": "volume"}))
    assert "🔵 VOLUME" in alert.title
    assert "🔵 VOLUME" in alert.body


def test_pick_alert_dedupe_key_differs_by_tier() -> None:
    # A volume alert must NOT suppress a later premium UPGRADE alert at the same
    # market+odds: the dedupe key includes the tier so the two are distinct.
    premium = make_pick()  # tier="premium"
    volume = make_pick().model_copy(update={"tier": "volume"})
    assert build_pick_alert(premium).dedupe_key != build_pick_alert(volume).dedupe_key


async def test_duplicate_alert_suppressed() -> None:
    sink = RecordingSink()
    dispatcher = AlertDispatcher([sink], InMemoryIdempotencyStore())
    first = await dispatcher.dispatch(make_alert())
    second = await dispatcher.dispatch(make_alert())
    assert first.skipped_duplicate is False
    assert second.skipped_duplicate is True
    assert len(sink.sent) == 1


async def test_different_keys_both_deliver() -> None:
    sink = RecordingSink()
    dispatcher = AlertDispatcher([sink], InMemoryIdempotencyStore())
    await dispatcher.dispatch(make_alert("k1"))
    await dispatcher.dispatch(make_alert("k2"))
    assert len(sink.sent) == 2


async def test_sink_failure_does_not_raise_or_block_others() -> None:
    recording = RecordingSink()
    dispatcher = AlertDispatcher([ExplodingSink(), recording], InMemoryIdempotencyStore())
    result = await dispatcher.dispatch(make_alert())
    assert result.sink_results[0] == ("exploding", False)
    assert result.sink_results[1] == ("recording", True)
    assert len(recording.sent) == 1


class FlakySink:
    """Raises on the first send, delivers afterwards — a transient outage."""

    name = "flaky"

    def __init__(self) -> None:
        self.calls = 0
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> bool:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient outage")
        self.sent.append(alert)
        return True


class UnconfiguredSink:
    """Mirrors Telegram/Webhook sinks with no token/url: skips by design."""

    name = "unconfigured"
    configured = False

    async def send(self, alert: Alert) -> bool:
        return False


async def test_failed_dispatch_releases_claim_so_next_cycle_retries() -> None:
    # Claim-leak regression: claim succeeds, the send raises -> the key must
    # be RELEASED, so the pipeline's next-cycle re-dispatch of the same
    # market state retries instead of being suppressed forever.
    sink = FlakySink()
    dispatcher = AlertDispatcher([sink], InMemoryIdempotencyStore())
    first = await dispatcher.dispatch(make_alert())
    assert first.skipped_duplicate is False
    assert first.sink_results == (("flaky", False),)

    second = await dispatcher.dispatch(make_alert())  # next cycle, same key
    assert second.skipped_duplicate is False  # claim was released
    assert second.sink_results == (("flaky", True),)
    assert len(sink.sent) == 1

    third = await dispatcher.dispatch(make_alert())  # delivered -> claim sticks
    assert third.skipped_duplicate is True
    assert len(sink.sent) == 1


async def test_partial_delivery_keeps_claim() -> None:
    # One channel delivered: releasing would duplicate the alert to the
    # healthy channel on the next cycle.
    recording = RecordingSink()
    dispatcher = AlertDispatcher([ExplodingSink(), recording], InMemoryIdempotencyStore())
    await dispatcher.dispatch(make_alert())
    second = await dispatcher.dispatch(make_alert())
    assert second.skipped_duplicate is True
    assert len(recording.sent) == 1


async def test_unconfigured_sinks_do_not_release_claim() -> None:
    # No-channel deployments: an unconfigured sink's False is a skip by
    # design, not a delivery failure — without this, every alert would
    # re-dispatch every cycle forever.
    dispatcher = AlertDispatcher([UnconfiguredSink()], InMemoryIdempotencyStore())
    first = await dispatcher.dispatch(make_alert())
    assert first.skipped_duplicate is False
    second = await dispatcher.dispatch(make_alert())
    assert second.skipped_duplicate is True


async def test_redis_idempotency_store_claims_once() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = RedisIdempotencyStore(redis)
    assert await store.claim("key-1") is True
    assert await store.claim("key-1") is False
    assert await store.claim("key-2") is True


async def test_redis_idempotency_store_release_reopens_key() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = RedisIdempotencyStore(redis)
    assert await store.claim("key-1") is True
    await store.release("key-1")
    assert await store.claim("key-1") is True  # claimable again after release


async def test_redis_idempotency_ttl_keeps_unchanged_odds_quiet_for_seven_days() -> None:
    # Unchanged odds on a still-open pick must NOT re-alert daily: the claim
    # TTL is 7 days by default (a price move mints a new key regardless).
    assert DEFAULT_TTL_SECONDS == 7 * 24 * 60 * 60
    redis = fakeredis.aioredis.FakeRedis()
    store = RedisIdempotencyStore(redis)
    assert await store.claim("key-ttl") is True
    assert await redis.ttl("alert:dedupe:key-ttl") == DEFAULT_TTL_SECONDS
    assert await store.claim("key-ttl") is False  # quiet within the TTL

    custom = RedisIdempotencyStore(redis, ttl_seconds=3600)
    assert await custom.claim("key-custom") is True
    assert await redis.ttl("alert:dedupe:key-custom") == 3600


def test_pick_alert_contains_required_fields_without_footer() -> None:
    alert = build_pick_alert(make_pick())
    assert ALERT_FOOTER not in alert.body  # footer removed per operator request
    for fragment in (
        "Alpha FC vs Beta United",
        "Alpha FC @ 2.10 · bookie_one",
        "Edge +5.0%",
        "EV +15.5%",
        "Conf 75%",
        "Fair 2.00",
        "Stake 2.0% of bankroll",
        "odds 60s old",
    ):
        assert fragment in alert.body, fragment
    assert len(alert.dedupe_key) == 32


def test_same_pick_same_dedupe_key() -> None:
    assert build_pick_alert(make_pick()).dedupe_key == build_pick_alert(make_pick()).dedupe_key


def test_pick_alert_still_ev_line_from_value_min_edge() -> None:
    # Value-pipeline alerts carry the execution helper: model_probability is
    # the sharp fair prob there, so the floor is 1/(0.55-0.03)=1.923 -> 1.93
    # after the round-UP display rule (rounding down would lose the edge).
    alert = build_pick_alert(make_pick(), value_min_edge=0.03)
    assert "Value holds to 1.93" in alert.body
    assert "skip below" in alert.body
    # default (model strategy / legacy callers): no line, identical key
    plain = build_pick_alert(make_pick())
    assert "Value holds to" not in plain.body
    assert plain.dedupe_key == alert.dedupe_key


def test_strategy_version_bump_changes_dedupe_key() -> None:
    # A strategy-version bump re-emits the same market state as a genuinely
    # new signal: its alert must NOT collide with a stale Redis key from the
    # previous version. Same pick, different model_version -> different key.
    v3 = build_pick_alert(make_pick(), model_name="value-sharp-vs-soft", model_version="v3")
    v4 = build_pick_alert(make_pick(), model_name="value-sharp-vs-soft", model_version="v4")
    assert v3.dedupe_key != v4.dedupe_key


def test_strategy_name_changes_dedupe_key() -> None:
    # Two distinct strategies pricing the same market are independent signals.
    value = build_pick_alert(make_pick(), model_name="value-sharp-vs-soft", model_version="v3")
    model = build_pick_alert(make_pick(), model_name="football-dixon-coles", model_version="v3")
    assert value.dedupe_key != model.dedupe_key


def test_same_strategy_identity_same_dedupe_key() -> None:
    # Stability within a version: identical pick + identical strategy id ->
    # identical key, so a re-detection at unchanged odds is still suppressed.
    a = build_pick_alert(make_pick(), model_name="value-sharp-vs-soft", model_version="v3")
    b = build_pick_alert(make_pick(), model_name="value-sharp-vs-soft", model_version="v3")
    assert a.dedupe_key == b.dedupe_key
    # value_min_edge is a display-only arg: it must not perturb the key.
    c = build_pick_alert(
        make_pick(), value_min_edge=0.03, model_name="value-sharp-vs-soft", model_version="v3"
    )
    assert a.dedupe_key == c.dedupe_key


def test_pick_alert_omits_still_ev_line_when_no_price_retains_edge() -> None:
    # fair prob at/below the threshold: the helper has no honest floor to
    # print, and the alert must not invent one.
    pick = make_pick().model_copy(update={"model_probability": 0.02})
    alert = build_pick_alert(pick, value_min_edge=0.03)
    assert "Value holds to" not in alert.body


def make_value_pick() -> PickOut:
    """A VALUE-strategy pick (real example: Grazer AK or Draw @ 1.83 · 10bet).

    Value-path field semantics differ from the model path: model_probability
    carries the devigged SHARP fair prob (the TRUE fair, 0.748 -> 1.34) and
    fair_probability carries the OFFERED odds' implied prob (1/1.83 = 0.546).
    The displayed "🎯 Fair" line must show the sharp fair ODDS (1.34), never
    the offered odds (1.83) nor the bare probability (0.748).
    """
    return make_pick().model_copy(
        update={
            "selection": "Grazer AK or Draw",
            "bookmaker": "10bet",
            "decimal_odds": 1.83,
            "model_probability": 0.748,  # sharp fair prob -> fair odds 1.34
            "fair_probability": 0.546,  # offered odds' implied prob (1/1.83)
            "edge": 0.201,
            "ev": 0.368,
            "anchor_type": "sharp",
            "reason_summary": "value: Betfair Exchange fair 1.34 vs 10bet 1.83",
        }
    )


def test_value_pick_alert_headline_shows_sharp_fair_odds_not_prob_or_offered() -> None:
    # Value-path bug: the headline "🎯 Fair" line must render the SHARP fair
    # ODDS (1/model_probability = 1/0.748 = 1.34), apples-to-apples with the
    # offered odds — NOT the fair probability (0.748) and NOT the offered odds
    # (1.83, which 1/fair_probability would wrongly produce).
    alert = build_pick_alert(make_value_pick(), value_min_edge=0.03)
    assert "🎯 Fair 1.34 (Sharp) → 1.83 beats it" in alert.body
    # never the probability, never "Fair == offered" nonsense
    assert "Fair 0.74" not in alert.body
    assert "Fair 0.75" not in alert.body
    assert "Fair 1.83" not in alert.body


def test_value_pick_reason_line_shows_fair_odds_apples_to_apples() -> None:
    # The reason line must compare like with like: fair ODDS vs offered ODDS,
    # not the fair PROBABILITY (0.748) next to the offered ODDS (1.83).
    alert = build_pick_alert(make_value_pick(), value_min_edge=0.03)
    assert "value: Betfair Exchange fair 1.34 vs 10bet 1.83" in alert.body
    assert "fair 0.748" not in alert.body


def test_model_pick_fair_line_unchanged() -> None:
    # The MODEL path (value_min_edge=None) keeps fair_probability as the TRUE
    # devigged market fair, so its "Fair" line stays 1/0.50 = 2.00. The value
    # fix must not perturb it.
    alert = build_pick_alert(make_pick())
    assert "🎯 Fair 2.00 → 2.10 beats it" in alert.body
