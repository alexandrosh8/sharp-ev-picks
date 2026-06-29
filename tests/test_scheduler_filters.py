"""Poll-skip log-noise filter (app/scheduler.py).

The 60s poll interval + max_instances=1 + coalesce is the documented
continuous-polling design: a 20-40 min scrape cycle makes apscheduler skip
every overlapping slot, which is expected — not WARNING-worthy. The skip is
the ONLY scheduler-side evidence of a hung poll cycle, so it is DOWNGRADED
to INFO — and must then honour the logger's effective level: the WARNING
gate already passed before the filter ran, so without a re-check the
"downgraded" line still emits under LOG_LEVEL=WARNING. Skips of any OTHER
job stay warnings.
"""

import logging
from collections.abc import Iterator

import pytest

from app.scheduler import _HUNG_CYCLE_SKIP_THRESHOLD, _PollSkipNoiseFilter

_SKIP_MSG = (
    'Execution of job "build_scheduler.<locals>.poll_odds '
    '(trigger: interval[0:01:00], next run at: 2026-06-11 15:32:29 EEST)" '
    "skipped: maximum number of running instances reached (1)"
)


def _skip_msg_for(job: str) -> str:
    return (
        f'Execution of job "build_scheduler.<locals>.{job} '
        '(trigger: interval[0:01:00], next run at: 2026-06-11 15:32:29 EEST)" '
        "skipped: maximum number of running instances reached (1)"
    )


def _rec(msg: str) -> logging.LogRecord:
    return logging.LogRecord("apscheduler.scheduler", logging.WARNING, __file__, 0, msg, None, None)


@pytest.fixture
def scheduler_logger() -> Iterator[logging.Logger]:
    logger = logging.getLogger("apscheduler.scheduler")
    original = logger.level
    try:
        yield logger
    finally:
        logger.setLevel(original)


def test_downgrades_poll_odds_skip_to_info_and_emits_at_info_level(
    scheduler_logger: logging.Logger,
) -> None:
    scheduler_logger.setLevel(logging.INFO)  # LOG_LEVEL=INFO
    record = _rec(_SKIP_MSG)
    assert _PollSkipNoiseFilter().filter(record)  # kept, not dropped
    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"


def test_downgraded_skip_suppressed_under_warning_level(
    scheduler_logger: logging.Logger,
) -> None:
    # Level-leak regression: the WARNING gate passed BEFORE the filter ran;
    # the record downgraded to INFO must NOT slip out under LOG_LEVEL=WARNING.
    scheduler_logger.setLevel(logging.WARNING)
    record = _rec(_SKIP_MSG)
    assert not _PollSkipNoiseFilter().filter(record)
    assert record.levelno == logging.INFO  # downgrade applied, emission denied


@pytest.mark.parametrize("job", ["capture_betfair_exchange", "capture_pinnacle_arcadia"])
def test_downgrades_interval_capture_skips_to_info(
    scheduler_logger: logging.Logger, job: str
) -> None:
    # The interval CAPTURE jobs (betfair/arcadia) use the SAME continuous-poll
    # coalesce design as poll_odds — their max-instances skip is by-design while a
    # long cycle runs, not WARNING-worthy. (Real-time monitoring SNR: keep the
    # benign skip at INFO so genuine warnings stand out.)
    scheduler_logger.setLevel(logging.INFO)
    record = _rec(
        f'Execution of job "build_scheduler.<locals>.{job} (trigger: '
        'interval[0:05:00], next run at: 2026-06-21 19:25:21 EEST)" '
        "skipped: maximum number of running instances reached (1)"
    )
    assert _PollSkipNoiseFilter().filter(record)
    assert record.levelno == logging.INFO


def test_keeps_other_job_skips_and_other_warnings_at_warning(
    scheduler_logger: logging.Logger,
) -> None:
    scheduler_logger.setLevel(logging.WARNING)
    f = _PollSkipNoiseFilter()
    # A CRON job (settle_results) is NOT continuous-poll — its skip is a real
    # signal and must stay WARNING.
    other_skip = _rec(
        'Execution of job "settle_results" skipped: maximum number of running instances reached (1)'
    )
    assert f.filter(other_skip)
    assert other_skip.levelno == logging.WARNING

    missed = _rec("Run time of job poll_odds was missed by 0:00:05")
    assert f.filter(missed)
    assert missed.levelno == logging.WARNING


def test_single_coalesce_skip_stays_info(scheduler_logger: logging.Logger) -> None:
    # A one-off coalesce (count == 1) is by-design and must NOT escalate.
    scheduler_logger.setLevel(logging.INFO)
    f = _PollSkipNoiseFilter()
    record = _rec(_SKIP_MSG)
    assert f.filter(record)
    assert record.levelno == logging.INFO
    assert "HUNG" not in record.getMessage()


def test_sustained_skips_escalate_to_warning(scheduler_logger: logging.Logger) -> None:
    # K consecutive skips of the SAME job (no completion between them) is a
    # genuine hang — the K-th skip ESCALATES to WARNING and names the run length.
    scheduler_logger.setLevel(logging.WARNING)
    f = _PollSkipNoiseFilter()
    # The first K-1 skips are benign coalesces (INFO, suppressed at WARNING level).
    for _ in range(_HUNG_CYCLE_SKIP_THRESHOLD - 1):
        rec = _rec(_SKIP_MSG)
        assert not f.filter(rec)  # downgraded to INFO -> denied under WARNING
        assert rec.levelno == logging.INFO

    hung = _rec(_SKIP_MSG)
    assert f.filter(hung)  # emitted (it IS a real WARNING now)
    assert hung.levelno == logging.WARNING
    assert hung.levelname == "WARNING"
    message = hung.getMessage()
    assert "HUNG" in message
    assert str(_HUNG_CYCLE_SKIP_THRESHOLD) in message  # the consecutive count


def test_reset_returns_job_to_info(scheduler_logger: logging.Logger) -> None:
    # A successful submission (cycle restarted) clears the counter, so the next
    # one-off coalesce is INFO again — one-offs never accumulate across cycles.
    scheduler_logger.setLevel(logging.INFO)
    f = _PollSkipNoiseFilter()
    for _ in range(_HUNG_CYCLE_SKIP_THRESHOLD):
        f.filter(_rec(_SKIP_MSG))
    f.reset("poll_odds")

    after = _rec(_SKIP_MSG)
    assert f.filter(after)
    assert after.levelno == logging.INFO
    assert "HUNG" not in after.getMessage()


def test_per_job_skip_counters_are_independent(scheduler_logger: logging.Logger) -> None:
    # One job hanging must not push an unrelated job's single coalesce to WARNING.
    scheduler_logger.setLevel(logging.INFO)
    f = _PollSkipNoiseFilter()
    for _ in range(_HUNG_CYCLE_SKIP_THRESHOLD):
        f.filter(_rec(_skip_msg_for("poll_odds")))  # poll_odds now HUNG

    other = _rec(_skip_msg_for("capture_betfair_exchange"))
    assert f.filter(other)
    assert other.levelno == logging.INFO  # its own counter is still 1
    assert "HUNG" not in other.getMessage()
