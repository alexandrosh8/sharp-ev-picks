"""The scrape loop-exception handler must HANDLE (not hide) Playwright wait
futures orphaned on tab close, while still delegating real bugs to the default
loud path.
"""

import logging


def _pw_timeout() -> Exception:
    # A class that looks like playwright._impl._errors.TimeoutError without
    # importing playwright: name "TimeoutError", module under "playwright".
    cls = type("TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"})
    return cls("Timeout 5000ms exceeded")


class _FakeLoop:
    def __init__(self) -> None:
        self.delegated: list[dict] = []

    def default_exception_handler(self, context: dict) -> None:
        self.delegated.append(context)


def test_handler_retrieves_and_logs_orphaned_playwright_timeout(caplog) -> None:  # type: ignore[no-untyped-def]
    from app.ingestion.oddsportal import scrape_loop_exception_handler

    loop = _FakeLoop()
    ctx = {
        "exception": _pw_timeout(),
        "message": "Future exception was never retrieved\nfuture: <Future ...>",
    }
    with caplog.at_level(logging.WARNING):
        scrape_loop_exception_handler(loop, ctx)  # type: ignore[arg-type]

    assert loop.delegated == []  # handled here, NOT dumped via the default ERROR path
    assert any("orphaned" in r.getMessage() for r in caplog.records)  # logged, not hidden
    assert all(r.levelno == logging.WARNING for r in caplog.records)  # benign -> WARNING, not ERROR


def test_handler_retrieves_target_closed_error(caplog) -> None:  # type: ignore[no-untyped-def]
    # A SECOND orphaned-future variant seen in prod: the page/browser was closed
    # mid-op (TargetClosedError, not TimeoutError). Same class — must be handled.
    from app.ingestion.oddsportal import scrape_loop_exception_handler

    cls = type("TargetClosedError", (Exception,), {"__module__": "playwright._impl._errors"})
    loop = _FakeLoop()
    ctx = {
        "exception": cls("Target page, context or browser has been closed"),
        "message": "Future exception was never retrieved\nfuture: <Future ...>",
    }
    with caplog.at_level(logging.WARNING):
        scrape_loop_exception_handler(loop, ctx)  # type: ignore[arg-type]

    assert loop.delegated == []  # handled, not dumped at ERROR
    assert any("orphaned" in r.getMessage() for r in caplog.records)
    assert all(r.levelno == logging.WARNING for r in caplog.records)


def test_handler_delegates_non_playwright_exceptions() -> None:
    from app.ingestion.oddsportal import scrape_loop_exception_handler

    loop = _FakeLoop()
    ctx = {"exception": ValueError("boom"), "message": "Future exception was never retrieved"}
    scrape_loop_exception_handler(loop, ctx)  # type: ignore[arg-type]

    assert len(loop.delegated) == 1  # a real unhandled-future bug still surfaces loudly


def test_handler_delegates_orphan_message_without_exception() -> None:
    from app.ingestion.oddsportal import scrape_loop_exception_handler

    loop = _FakeLoop()
    scrape_loop_exception_handler(loop, {"message": "Task was destroyed but it is pending!"})  # type: ignore[arg-type]
    assert len(loop.delegated) == 1  # not our benign case -> default path
