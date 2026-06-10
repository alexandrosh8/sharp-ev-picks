"""Parity: our devig pipeline math vs the directly-installed penaltyblog
(ADR-0011 — proven libraries used directly; our pure-math core must agree).

Skips when the `football` extra is not installed (CI default profile).
"""

import pytest

penaltyblog_implied = pytest.importorskip("penaltyblog.implied")

from app.probabilities.devig import DevigMethod, devig  # noqa: E402

BOOKS = [
    [2.5, 3.2, 2.9],  # 1X2 with ~5.7% overround
    [1.5, 4.0, 6.0],  # longshot-heavy 3-way
    [1.9, 1.9],  # symmetric 2-way
    [2.1, 1.75],  # asymmetric 2-way
]

METHOD_MAP = {
    DevigMethod.MULTIPLICATIVE: "multiplicative",
    DevigMethod.ADDITIVE: "additive",
    DevigMethod.POWER: "power",
    DevigMethod.SHIN: "shin",
}


@pytest.mark.parametrize("odds", BOOKS)
@pytest.mark.parametrize("method", list(METHOD_MAP))
def test_devig_matches_penaltyblog(method: DevigMethod, odds: list[float]) -> None:
    ours = devig(odds, method=method)
    theirs = penaltyblog_implied.calculate_implied(odds, METHOD_MAP[method]).probabilities
    assert ours == pytest.approx(list(theirs), abs=1e-8)
