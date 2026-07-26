"""Startup warning when value-filter artifacts are missing (app/scheduler.py).

The value pipeline loads the value-filter meta-model best-effort; before this
guard, missing artifacts (e.g. the data/ml bind mount absent in the container)
left every pick's value_filter_score NULL in SILENCE — the loud guard only
fired under VALUE_ML_FILTER=true. Composition must warn ONCE, clearly, and
continue unfiltered.
"""

import logging
from pathlib import Path

import pytest

from app.scheduler import _load_value_filter


def test_missing_artifacts_emit_one_clear_warning_and_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        result = _load_value_filter(
            tmp_path,  # empty dir — no manifest, no model
            manifest_filename="value_filter_manifest.json",
            model_filename="value_filter_model.txt",
            allow_shadow=False,
            enforced=False,
        )
    assert result is None  # pipeline continues unfiltered
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "value-filter artifacts missing" in r.getMessage()
        and "unscored" in r.getMessage()
    ]
    assert len(warnings) == 1  # exactly ONE clear startup warning


def test_enforced_mode_missing_artifacts_do_not_double_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Under VALUE_ML_FILTER=true the composition root already fails LOUDLY
    # with RuntimeError right after this returns None — no duplicate warning.
    with caplog.at_level(logging.INFO):
        result = _load_value_filter(
            tmp_path,
            manifest_filename="value_filter_manifest.json",
            model_filename="value_filter_model.txt",
            allow_shadow=False,
            enforced=True,
        )
    assert result is None
    assert not [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "value-filter artifacts missing" in r.getMessage()
    ]
