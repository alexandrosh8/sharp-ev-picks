"""Shared database URL selected by the pytest bootstrap before collection."""

from __future__ import annotations

import os

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://betting_ai:betting_ai@localhost:5433/betting_ai_test"
)

# ``tests/conftest.py::pytest_configure`` installs a process/xdist-worker-
# specific URL before test modules are collected. An explicitly supplied URL
# remains supported for developers targeting a pre-provisioned test database.
TEST_DATABASE_URL = os.environ.get("TEST_DB_URL", DEFAULT_TEST_DATABASE_URL)
