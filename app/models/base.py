"""Model serving contracts. Real engines land in roadmap phases 3 (football
Dixon-Coles) and 5 (NBA gradient boosting); the pipeline is wired against the
protocol so models drop in without pipeline changes."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from app.schemas.base import Market


@dataclass(frozen=True)
class PredictedProbability:
    market: Market
    selection: str
    probability: float
    confidence: float


class ProbabilityModel(Protocol):
    """A registered model that emits probabilities for an event's markets."""

    name: str
    version: str

    async def predict(self, event_id: str) -> Sequence[PredictedProbability]: ...


class NullModel:
    """Placeholder until trained engines ship: predicts nothing, so the
    pipeline produces no picks (fail-safe default)."""

    name = "null-model"
    version = "0"

    async def predict(self, event_id: str) -> Sequence[PredictedProbability]:
        return ()
