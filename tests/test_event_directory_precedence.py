"""EventDirectory kickoff precedence: a date-only midnight (00:00:00 UTC, the
sentinel for a source that only knew the DATE) must NEVER overwrite an already-
registered REAL kickoff time, and a real time always wins over a stored midnight.

Root cause (2026-06-24 live investigation): OddsPortal serves a date-only
midnight `eventBody.startDate` for a residual tail of basketball fixtures (e.g.
Puerto Rico BSN, some WNBA). When such a fixture is also seen on a cycle where a
real time was known, the blind last-write-wins `register` let the midnight clobber
the real time. The directory is the first overwrite layer; the DB upsert is the
second (see test_persistence)."""

from datetime import UTC, datetime

from app.ingestion.base import EventDirectory, EventTeams

EVENT_ID = "https://www.oddsportal.com/basketball/h2h/team-a/team-b/"
REAL_TIME = datetime(2026, 6, 25, 2, 0, tzinfo=UTC)  # 02:00 UTC — a real tip time
MIDNIGHT = datetime(2026, 6, 25, 0, 0, tzinfo=UTC)  # date-only sentinel


def _teams(
    starts_at: datetime | None,
    home_score: int | None = None,
    away_score: int | None = None,
    finished: bool | None = None,
) -> EventTeams:
    return EventTeams(
        home="Team A",
        away="Team B",
        league="bsn",
        starts_at=starts_at,
        home_score=home_score,
        away_score=away_score,
        finished=finished,
    )


def test_real_time_is_not_clobbered_by_later_date_only_midnight() -> None:
    directory = EventDirectory()
    directory.register(EVENT_ID, _teams(REAL_TIME))
    # A later cycle re-registers the same fixture with only the DATE (midnight).
    directory.register(EVENT_ID, _teams(MIDNIGHT))
    stored = directory.lookup(EVENT_ID)
    assert stored is not None
    assert stored.starts_at == REAL_TIME  # real time survives the midnight re-register


def test_real_time_wins_over_already_stored_midnight() -> None:
    directory = EventDirectory()
    directory.register(EVENT_ID, _teams(MIDNIGHT))  # first seen date-only
    directory.register(EVENT_ID, _teams(REAL_TIME))  # later a real time arrives
    stored = directory.lookup(EVENT_ID)
    assert stored is not None
    assert stored.starts_at == REAL_TIME  # upgraded to the real time


def test_midnight_register_still_preserves_other_fields() -> None:
    # A date-only midnight re-register must keep its OWN fresher non-kickoff data
    # (e.g. a finished score / finished flag) while keeping the real kickoff. The
    # kickoff is the only field guarded; everything else is the latest write.
    directory = EventDirectory()
    directory.register(EVENT_ID, _teams(REAL_TIME))
    directory.register(EVENT_ID, _teams(MIDNIGHT, home_score=95, away_score=96, finished=True))
    stored = directory.lookup(EVENT_ID)
    assert stored is not None
    assert stored.starts_at == REAL_TIME  # kickoff preserved
    assert stored.home_score == 95  # later score still applied
    assert stored.away_score == 96
    assert stored.finished is True


def test_midnight_persists_when_no_real_time_ever_seen() -> None:
    # A genuinely date-only fixture keeps its midnight (better than NULL/TBD) —
    # the guard only protects an EXISTING real time, it never discards midnight
    # when midnight is all we have.
    directory = EventDirectory()
    directory.register(EVENT_ID, _teams(MIDNIGHT))
    directory.register(EVENT_ID, _teams(MIDNIGHT))
    stored = directory.lookup(EVENT_ID)
    assert stored is not None
    assert stored.starts_at == MIDNIGHT


def test_none_kickoff_never_clobbers_a_known_time() -> None:
    # A later TBD (None) register must not wipe a known kickoff (real OR midnight).
    directory = EventDirectory()
    directory.register(EVENT_ID, _teams(REAL_TIME))
    directory.register(EVENT_ID, _teams(None))
    stored = directory.lookup(EVENT_ID)
    assert stored is not None
    assert stored.starts_at == REAL_TIME
