"""Betfair price-increment ("tick") ladder — PURE math (stdlib only).

Relocated verbatim from ``app/ingestion/betfair_api.py`` (staleness-guard
package, 2026-07-02) so the mint-path guard can consume tick math inside the
pure-math boundary (CLAUDE.md: ``app/edge/`` takes no env/DB/HTTP). The
ingestion module imports these names back — one implementation, no duplication.

Semantics (kept verbatim — conservative by construction):

* The tick for a comparison is taken at the COARSER (higher) of the two
  prices, so a near-agreement is never overstated.
* ``None`` propagates: an absent price is undefined — it never silently
  "agrees" and never yields a distance.

Source: Betfair "Betting → Price increments" table (the official minimum
quotable gaps: <2 → 0.01, 2–3 → 0.02, 3–4 → 0.05, 4–6 → 0.1, 6–10 → 0.2,
10–20 → 0.5, 20–30 → 1, 30–50 → 2, 50–100 → 5, 100–1000 → 10).
"""

from __future__ import annotations

# The Betfair price-increment ("tick") ladder. The minimum quotable gap widens
# as the price climbs; "within one tick" uses the COARSER (higher) of the two
# prices so a near-agreement is never overstated.
_TICK_LADDER: tuple[tuple[float, float], ...] = (
    (2.0, 0.01),
    (3.0, 0.02),
    (4.0, 0.05),
    (6.0, 0.10),
    (10.0, 0.20),
    (20.0, 0.50),
    (30.0, 1.00),
    (50.0, 2.00),
    (100.0, 5.00),
    (1000.0, 10.0),
)

# Float-noise tolerance shared by every tick comparison in this module (the
# exact value ``within_one_tick`` has always used).
_EPS = 1e-9


def betfair_tick_size(price: float) -> float:
    """The Betfair minimum price increment at ``price`` (the exchange tick)."""
    for upper, tick in _TICK_LADDER:
        if price < upper:
            return tick
    return 10.0


def within_one_tick(a: float | None, b: float | None) -> bool | None:
    """True when two BACK prices are within one exchange tick of each other.

    None when EITHER price is missing — an absent price is undefined, never a
    silent "agree". The tick is taken at the coarser (higher) price so the test
    is conservative (a wider band never inflates the agreement rate)."""
    if a is None or b is None:
        return None
    tick = betfair_tick_size(max(a, b))
    return abs(a - b) <= tick + _EPS


def tick_distance(a: float | None, b: float | None) -> float | None:
    """|a − b| expressed in Betfair ticks, or None when either price is absent.

    The tick is taken at the COARSER (higher) price — the same conservative
    convention as :func:`within_one_tick`, so ``tick_distance(a, b) <= 1``
    exactly when ``within_one_tick(a, b)`` is True (modulo float dust).
    ``None`` propagates: an absent price never yields a distance (and so can
    never demote an anchor)."""
    if a is None or b is None:
        return None
    tick = betfair_tick_size(max(a, b))
    return abs(a - b) / tick
