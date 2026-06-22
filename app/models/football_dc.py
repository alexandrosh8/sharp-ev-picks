"""Football probabilities via penaltyblog's Dixon-Coles, used directly
(ADR-0011/0012, ADR-0004 math).

penaltyblog (MIT, martineastwood/penaltyblog) provides the fitted model and
the score-grid market derivation; this module adapts it to our
`ProbabilityModel` protocol: fit on football-data.co.uk history (MatchRow)
with exponential time decay, resolve OddsPortal team names to trained names,
and emit 1X2 / totals / BTTS probabilities whose selection strings match the
OddsPortal loader's.

The penaltyblog import is lazy (football extra): `uv sync --extra football`.
"""

import logging
import re
from collections.abc import Sequence
from datetime import date
from importlib import metadata
from typing import Any

from app.ingestion.base import EventDirectory
from app.ingestion.football_data import MatchRow
from app.models.base import PredictedProbability
from app.schemas.base import Market

logger = logging.getLogger(__name__)

# normalized oddsportal name -> normalized football-data name (EPL focus;
# extend per league as resolution misses are logged)
DEFAULT_ALIASES: dict[str, str] = {
    "manchester united": "man united",
    "manchester city": "man city",
    "newcastle united": "newcastle",
    "tottenham hotspur": "tottenham",
    "wolverhampton wanderers": "wolves",
    "nottingham forest": "nottm forest",
    "west ham united": "west ham",
    "brighton hove albion": "brighton",
    "afc bournemouth": "bournemouth",
    "leeds united": "leeds",
    "leicester city": "leicester",
    "ipswich town": "ipswich",
    "sheffield united": "sheffield utd",
    "west bromwich albion": "west brom",
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _penaltyblog_version() -> str:
    """Registry-honest model version derived from the installed package, so
    rows written under `version` stay truthful across penaltyblog bumps."""
    try:
        return f"pb-{metadata.version('penaltyblog')}"
    except metadata.PackageNotFoundError:  # football extra not installed
        return "pb-unavailable"


# Optimizer tolerances from penaltyblog's own docs example
# (docs/models/example.ipynb upstream): a 3x iteration budget plus explicit
# tolerances keep thin/degenerate samples convergent where the library
# default (maxiter=1000) hard-fails with "Optimization failed".
_FIT_MINIMIZER_OPTIONS: dict[str, float] = {"maxiter": 3000, "gtol": 1e-8, "ftol": 1e-9}


class DixonColesFootballModel:
    """ProbabilityModel backed by penaltyblog.models.DixonColesGoalModel."""

    name = "football-dixon-coles-penaltyblog"
    version = _penaltyblog_version()

    def __init__(
        self,
        directory: EventDirectory,
        xi: float = 0.0018,
        confidence: float = 0.65,
        totals_line: float = 2.5,
        aliases: dict[str, str] | None = None,
        predict_neutral: bool = False,
    ) -> None:
        if totals_line % 1 != 0.5:
            # grid.total_goals("over"/"under") silently EXCLUDES the push
            # probability, so on integer/quarter lines over+under sums < 1
            # and every EV computed from it is wrong. Half lines have no
            # push mass and are safe. Supporting other lines requires
            # grid.totals(line) -> (under, push, over) plus a push-aware
            # EV path downstream.
            raise ValueError(
                f"totals_line={totals_line} is not a half line (x.5): integer/"
                "quarter lines carry push probability that total_goals() "
                "silently drops; use grid.totals(line) -> (under, push, over) "
                "with push-aware EV before enabling such lines"
            )
        self._directory = directory
        self._xi = xi
        self._confidence = confidence
        self._totals_line = totals_line
        self._aliases = dict(DEFAULT_ALIASES)
        if aliases:
            self._aliases.update(aliases)
        self._predict_neutral = predict_neutral  # WC/intl matches at neutral venues
        self._model: Any = None
        self._trained: dict[str, str] = {}  # normalized -> raw trained name

    @property
    def fitted(self) -> bool:
        return self._model is not None

    def fit(
        self,
        rows: Sequence[MatchRow],
        as_of: date,
        neutral_venues: Sequence[bool] | None = None,
    ) -> None:
        """Fit Dixon-Coles with exponential time-decay weights (ADR-0004).

        `neutral_venues` (parallel to `rows`) lets the model learn home
        advantage from non-neutral matches — required for international /
        tournament football. Synchronous and CPU-bound — call via
        asyncio.to_thread from async code.
        """
        if neutral_venues is not None and len(neutral_venues) != len(rows):
            raise ValueError("neutral_venues must be parallel to rows")
        pairs = (
            list(zip(rows, neutral_venues, strict=True))
            if neutral_venues
            else [(r, False) for r in rows]
        )
        # Strict `<`: a same-day (== as_of) result must NEVER train the model that
        # prices later-same-day fixtures (no same-day leak, audit #8). MatchRow is
        # date-only, so `<` excludes the whole as_of date.
        usable = [(r, n) for r, n in pairs if r.match_date < as_of]
        if len(usable) < 50:
            raise ValueError(f"need >= 50 historical matches to fit, got {len(usable)}")

        from penaltyblog.models import DixonColesGoalModel, dixon_coles_weights

        weights = dixon_coles_weights([r.match_date for r, _ in usable], self._xi, base_date=as_of)
        neutral_flag = [1 if n else 0 for _, n in usable]
        model = DixonColesGoalModel(
            goals_home=[r.home_goals for r, _ in usable],
            goals_away=[r.away_goals for r, _ in usable],
            teams_home=[r.home_team for r, _ in usable],
            teams_away=[r.away_team for r, _ in usable],
            weights=weights,
            neutral_venue=neutral_flag if any(neutral_flag) else None,
        )
        try:
            model.fit(minimizer_options=dict(_FIT_MINIMIZER_OPTIONS))
        except ValueError:
            # The analytical gradient can stall SLSQP on degenerate/thin
            # samples; penaltyblog's fit() docstring calls numerical
            # gradients "sometimes more stable". Retry once — a second
            # failure propagates (existing no-model-for-the-cycle behavior).
            logger.info(
                "dixon-coles fit failed with analytical gradient; "
                "retrying once with use_gradient=False"
            )
            model.fit(minimizer_options=dict(_FIT_MINIMIZER_OPTIONS), use_gradient=False)
        self._model = model
        self._trained = {
            _normalize(team): team for r, _ in usable for team in (r.home_team, r.away_team)
        }
        logger.info(
            "dixon-coles fitted on %d matches, %d teams (as of %s)",
            len(usable),
            len(self._trained),
            as_of,
        )

    def resolve_team(self, name: str) -> str | None:
        """Map a loader team name (e.g. OddsPortal) to a trained team name.

        Resolution order: exact normalized match > alias (alias values are
        normalized at lookup, so raw-name aliases work) > UNIQUE longest
        token-containment. Ambiguous containment resolves to None — silently
        pricing the wrong team is worse than no prediction (review finding).
        """
        norm = _normalize(name)
        if norm in self._trained:
            return self._trained[norm]
        alias = self._aliases.get(norm)
        if alias:
            alias_norm = _normalize(alias)
            if alias_norm in self._trained:
                return self._trained[alias_norm]
        # containment heuristic: trained name tokens within the longer name,
        # accepted only when there is a single longest unambiguous candidate.
        tokens = set(norm.split())
        candidates = [
            (trained_norm, raw)
            for trained_norm, raw in self._trained.items()
            if set(trained_norm.split()) <= tokens
        ]
        if not candidates:
            return None
        best_len = max(len(t.split()) for t, _ in candidates)
        best = [(t, r) for t, r in candidates if len(t.split()) == best_len]
        if len(best) != 1:
            logger.debug("ambiguous team resolution for %r: %s", name, sorted(t for t, _ in best))
            return None
        return best[0][1]

    async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
        if self._model is None:
            return ()
        teams = self._directory.lookup(event_id)
        if teams is None:
            return ()
        return self.predict_match(teams.home, teams.away, neutral=self._predict_neutral)

    def predict_match(
        self, home_raw: str, away_raw: str, neutral: bool | None = None
    ) -> tuple[PredictedProbability, ...]:
        """Price a single fixture by team names, with an explicit neutral flag
        (per-fixture — host nations at the World Cup are NOT neutral)."""
        if self._model is None:
            return ()
        home = self.resolve_team(home_raw)
        away = self.resolve_team(away_raw)
        if home is None or away is None:
            logger.debug("team resolution miss: %r->%r, %r->%r", home_raw, home, away_raw, away)
            return ()
        neutral_venue = self._predict_neutral if neutral is None else neutral
        return self._predict_resolved(home_raw, away_raw, home, away, neutral_venue)

    def predict_matches(
        self,
        fixtures: Sequence[tuple[str, str]],
        neutral: Sequence[bool] | None = None,
    ) -> tuple[tuple[PredictedProbability, ...], ...]:
        """Price a slate of (home_raw, away_raw) fixtures in ONE penaltyblog
        predict_many call (batch fast-path with shared validation, pb>=1.10).

        Per-fixture skip semantics are preserved exactly: a fixture whose
        teams do not resolve, or whose score grid is invalid, yields an
        empty tuple without affecting the rest of the slate. predict_many
        itself is all-or-nothing (one invalid grid raises for the whole
        batch), so on ValueError the slate is repriced per fixture.
        """
        if neutral is not None and len(neutral) != len(fixtures):
            raise ValueError("neutral must be parallel to fixtures")
        results: list[tuple[PredictedProbability, ...]] = [() for _ in fixtures]
        if self._model is None or not fixtures:
            return tuple(results)
        flags = [self._predict_neutral] * len(fixtures) if neutral is None else list(neutral)
        # (slate index, home_raw, away_raw, home trained, away trained, neutral)
        resolved: list[tuple[int, str, str, str, str, bool]] = []
        for i, (home_raw, away_raw) in enumerate(fixtures):
            home = self.resolve_team(home_raw)
            away = self.resolve_team(away_raw)
            if home is None or away is None:
                logger.debug("team resolution miss: %r->%r, %r->%r", home_raw, home, away_raw, away)
                continue
            resolved.append((i, home_raw, away_raw, home, away, flags[i]))
        if not resolved:
            return tuple(results)
        try:
            grids = self._model.predict_many(
                [home for _, _, _, home, _, _ in resolved],
                [away for _, _, _, _, away, _ in resolved],
                neutral_venue=[nv for *_, nv in resolved],
            )
        except ValueError:
            # One invalid grid (low-score rho correction on an extreme
            # matchup) fails the entire predict_many batch — recover the
            # priceable fixtures via the per-fixture path instead.
            logger.info(
                "dixon-coles batch prediction failed; repricing %d fixtures individually",
                len(resolved),
            )
            for i, home_raw, away_raw, home, away, nv in resolved:
                results[i] = self._predict_resolved(home_raw, away_raw, home, away, nv)
            return tuple(results)
        for (i, home_raw, away_raw, _, _, _), grid in zip(resolved, grids, strict=True):
            results[i] = self._grid_predictions(grid, home_raw, away_raw)
        return tuple(results)

    def _predict_resolved(
        self, home_raw: str, away_raw: str, home: str, away: str, neutral_venue: bool
    ) -> tuple[PredictedProbability, ...]:
        """Single-fixture pricing for already-resolved trained team names.
        Callers guarantee self._model is fitted."""
        try:
            grid = self._model.predict(home, away, neutral_venue=neutral_venue)
        except ValueError as exc:
            # Dixon-Coles' low-score rho correction can yield an invalid grid
            # for extreme matchups; skip the fixture rather than crash the poll.
            logger.warning("dixon-coles grid invalid for %s vs %s: %s", home, away, exc)
            return ()
        return self._grid_predictions(grid, home_raw, away_raw)

    def _grid_predictions(
        self, grid: Any, home_raw: str, away_raw: str
    ) -> tuple[PredictedProbability, ...]:
        line = self._totals_line
        conf = self._confidence
        # Selection strings MUST match app/ingestion/oddsportal.py.
        # total_goals() is push-blind, safe ONLY because __init__ enforces
        # half lines (push mass is zero at x.5).
        return (
            PredictedProbability(Market.H2H, home_raw, float(grid.home_win), conf),
            PredictedProbability(Market.H2H, "Draw", float(grid.draw), conf),
            PredictedProbability(Market.H2H, away_raw, float(grid.away_win), conf),
            PredictedProbability(
                Market.TOTALS, f"Over {line}", float(grid.total_goals("over", line)), conf
            ),
            PredictedProbability(
                Market.TOTALS, f"Under {line}", float(grid.total_goals("under", line)), conf
            ),
            PredictedProbability(Market.BTTS, "BTTS Yes", float(grid.btts_yes), conf),
            PredictedProbability(Market.BTTS, "BTTS No", float(grid.btts_no), conf),
        )
