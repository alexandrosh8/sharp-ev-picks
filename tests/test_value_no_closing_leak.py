"""Leakage regression LOCK for the value-strategy backtest.

The bet DECISION — which selection, its edge, the price taken — must depend ONLY
on PRE-MATCH prices (devigged Pinnacle pre-match vs the best available pre-match
price). The CLOSING columns (PSC*/MaxC*) may feed the CLV *label* and nothing
else: a closing-into-decision leak would let the backtest "peek" at the result
the market converges to, inflating held-out CLV/ROI — the cardinal backtest sin.

These guard scripts/value_backtest.py:bets_for: corrupting or removing the
closing columns must leave (won, odds, edge) byte-identical while ONLY the CLV
fields move. The code is already correct (leakage was audited); this locks it
against future regression.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

from app.probabilities.devig import DevigMethod

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vb: Any = _load(_SCRIPTS / "value_backtest.py", "value_backtest")


def _row(
    psc: tuple[str, str, str] = ("2.00", "3.60", "4.40"),
    maxc: tuple[str, str, str] = ("2.00", "3.70", "4.50"),
) -> dict[str, str]:
    """A 1x2 football-data row that yields a home value bet (best price 2.25
    beats the sharp pre-match fair). ``psc``/``maxc`` are the Pinnacle/Max
    CLOSING columns — the only thing the tests vary; everything the DECISION
    uses (PS*, Max*, FTR) is pre-match and fixed."""
    return {
        "FTR": "H",
        "FTHG": "2",
        "FTAG": "1",
        # pre-match: Pinnacle (sharp anchor) + Max (best available price)
        "PSH": "1.95",
        "PSD": "3.60",
        "PSA": "4.30",
        "MaxH": "2.25",
        "MaxD": "3.80",
        "MaxA": "4.60",
        # CLOSING — CLV label only, never the decision
        "PSCH": psc[0],
        "PSCD": psc[1],
        "PSCA": psc[2],
        "MaxCH": maxc[0],
        "MaxCD": maxc[1],
        "MaxCA": maxc[2],
    }


def test_bets_for_decision_is_invariant_to_closing_columns() -> None:
    base = vb.bets_for([_row()], 0.0, DevigMethod.POWER, ("1x2",), 1.0)
    # absurd-but-valid closing prices: if they leaked into the decision, the
    # selection / edge / price would move. They must not.
    leaked = vb.bets_for(
        [_row(psc=("99.0", "99.0", "99.0"), maxc=("99.0", "99.0", "99.0"))],
        0.0,
        DevigMethod.POWER,
        ("1x2",),
        1.0,
    )
    assert len(base) == len(leaked) == 1
    b, ll = base[0], leaked[0]
    # DECISION invariants — identical under any closing prices
    assert b.won == ll.won
    assert b.odds == ll.odds
    assert b.edge == ll.edge
    # ...but the CLV LABEL is computed FROM the close, so it MUST move — proving
    # the closing columns ARE consumed, only here, not silently ignored.
    assert b.clv_pinn != ll.clv_pinn
    assert b.clv_max != ll.clv_max


def test_bets_for_missing_closing_yields_null_clv_not_a_decision_change() -> None:
    base = vb.bets_for([_row()], 0.0, DevigMethod.POWER, ("1x2",), 1.0)
    row = _row()
    for col in ("PSCH", "PSCD", "PSCA", "MaxCH", "MaxCD", "MaxCA"):
        row[col] = ""  # closing absent
    nocls = vb.bets_for([row], 0.0, DevigMethod.POWER, ("1x2",), 1.0)
    assert len(base) == len(nocls) == 1
    # same bet placed — the decision never needed the close
    assert base[0].won == nocls[0].won
    assert base[0].odds == nocls[0].odds
    assert base[0].edge == nocls[0].edge
    # ...only the CLV label degrades to None (no close to value against)
    assert nocls[0].clv_pinn is None
    assert nocls[0].clv_max is None
