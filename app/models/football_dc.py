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
import math
import re
from collections.abc import Sequence
from datetime import date
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


class DixonColesFootballModel:
    """ProbabilityModel backed by penaltyblog.models.DixonColesGoalModel."""

    name = "football-dixon-coles-penaltyblog"
    version = "pb-1.11"

    def __init__(
        self,
        directory: EventDirectory,
        xi: float = 0.0018,
        confidence: float = 0.65,
        totals_line: float = 2.5,
        aliases: dict[str, str] | None = None,
        predict_neutral: bool = False,
    ) -> None:
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
        usable = [(r, n) for r, n in pairs if r.match_date <= as_of]
        if len(usable) < 50:
            raise ValueError(f"need >= 50 historical matches to fit, got {len(usable)}")

        from penaltyblog.models import DixonColesGoalModel

        weights = [math.exp(-self._xi * (as_of - r.match_date).days) for r, _ in usable]
        neutral_flag = [1 if n else 0 for _, n in usable]
        model = DixonColesGoalModel(
            goals_home=[r.home_goals for r, _ in usable],
            goals_away=[r.away_goals for r, _ in usable],
            teams_home=[r.home_team for r, _ in usable],
            teams_away=[r.away_team for r, _ in usable],
            weights=weights,
            neutral_venue=neutral_flag if any(neutral_flag) else None,
        )
        model.fit(minimizer_options={"maxiter": 1000})
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
        """Map a loader team name (e.g. OddsPortal) to a trained team name."""
        norm = _normalize(name)
        if norm in self._trained:
            return self._trained[norm]
        alias = self._aliases.get(norm)
        if alias and alias in self._trained:
            return self._trained[alias]
        # containment heuristic: trained name tokens within the longer name
        tokens = set(norm.split())
        for trained_norm, raw in self._trained.items():
            if set(trained_norm.split()) <= tokens:
                return raw
        return None

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
        try:
            grid = self._model.predict(home, away, neutral_venue=neutral_venue)
        except ValueError as exc:
            # Dixon-Coles' low-score rho correction can yield an invalid grid
            # for extreme matchups; skip the fixture rather than crash the poll.
            logger.warning("dixon-coles grid invalid for %s vs %s: %s", home, away, exc)
            return ()
        line = self._totals_line
        conf = self._confidence
        # Selection strings MUST match app/ingestion/oddsportal.py.
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
