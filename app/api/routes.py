"""API routes: latest picks, manual result tracking, health.

POST /picks/{id}/result is the MANUAL result-tracking entrypoint — the user
records what THEY did (bet placed or not, stake, outcome). Nothing here can
place a bet.
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    SESSION_COOKIE,
    authenticate,
    is_authenticated,
    require_dashboard_auth,
    sign_session,
)
from app.api.deps import get_session
from app.backtesting.live_evidence import live_evidence_report
from app.edge.confidence import confidence_rating
from app.schemas.events import EventResultIn, ResultIn
from app.settlement.engine import settle_event_picks
from app.storage.models import Event, ManualBetLog, Pick, ResultTracking
from app.storage.repositories import (
    latest_available_games_with_events,
    latest_picks_with_events,
    live_evidence_rows,
    performance_report,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Self-contained dashboard page (no build step, no CDN — works offline and
# identically on the Ubuntu VPS). Data is fetched from /picks client-side.
_DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

# Self-contained dark login page (no CDN/JS libs). Posts JSON to /login; on
# success redirects to /. No credential is ever embedded here, and the error
# message is set via textContent (never innerHTML) so a server string can't
# inject markup.
_LOGIN_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>betting-ai — sign in</title>
    <style>
      :root {
        --bg: #060906;
        --panel: #0d130d;
        --line: #1a241a;
        --text: #cfe3cf;
        --dim: #5e7a5e;
        --pos: #00ff9d;
        --neg: #ff5c57;
        --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      }
      * { box-sizing: border-box; margin: 0; }
      html { background: var(--bg); }
      body {
        color: var(--text);
        font: 13px/1.5 var(--mono);
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 24px;
        background:
          radial-gradient(1000px 420px at 50% -10%,
            rgba(0, 255, 157, 0.05), transparent 60%), var(--bg);
      }
      .card {
        width: 100%;
        max-width: 340px;
        border: 1px solid var(--line);
        border-radius: 8px;
        background: var(--panel);
        padding: 22px 20px 20px;
      }
      .brand {
        font-size: 16px;
        font-weight: 800;
        letter-spacing: 0.22em;
        color: var(--pos);
        text-shadow: 0 0 14px rgba(0, 255, 157, 0.3);
      }
      .sub {
        color: var(--dim);
        font-size: 11px;
        letter-spacing: 0.05em;
        margin: 6px 0 18px;
      }
      label {
        display: block;
        color: var(--dim);
        font-size: 10px;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        margin: 12px 0 5px;
      }
      input {
        width: 100%;
        background: var(--bg);
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 4px;
        padding: 9px 11px;
        font: 13px var(--mono);
      }
      input:focus { outline: 1px solid var(--pos); outline-offset: 0; }
      button {
        width: 100%;
        margin-top: 18px;
        cursor: pointer;
        background: rgba(0, 255, 157, 0.08);
        color: var(--pos);
        border: 1px solid var(--line);
        border-radius: 4px;
        padding: 10px 12px;
        font: 700 11px var(--mono);
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }
      button:hover { border-color: var(--pos); }
      .err {
        color: var(--neg);
        font-size: 11px;
        min-height: 16px;
        margin-top: 12px;
        letter-spacing: 0.03em;
      }
    </style>
  </head>
  <body>
    <form class="card" id="login-form" autocomplete="off">
      <div class="brand">PICKS&nbsp;TERMINAL</div>
      <div class="sub">decision-support · sign in to continue</div>
      <label for="u">Username</label>
      <input id="u" name="username" type="text" autocomplete="username" autofocus />
      <label for="p">Password</label>
      <input id="p" name="password" type="password" autocomplete="current-password" />
      <button type="submit">Sign in</button>
      <div class="err" id="err" role="alert"></div>
    </form>
    <script>
      "use strict";
      const form = document.getElementById("login-form");
      const errEl = document.getElementById("err");
      form.addEventListener("submit", async (ev) => {
        ev.preventDefault();
        errEl.textContent = "";
        const username = document.getElementById("u").value;
        const password = document.getElementById("p").value;
        try {
          const res = await fetch("/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ username, password }),
          });
          if (res.ok) {
            window.location = "/";
            return;
          }
          errEl.textContent =
            res.status === 401
              ? "Invalid username or password"
              : "Sign-in failed (HTTP " + res.status + ")";
        } catch (e) {
          errEl.textContent = "Sign-in failed — could not reach the server";
        }
      });
    </script>
  </body>
</html>
"""


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,
    dependencies=[Depends(require_dashboard_auth)],
)
async def dashboard() -> str:
    return _DASHBOARD_HTML


class _LoginIn(BaseModel):
    username: str
    password: str


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request) -> Response:
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_LOGIN_HTML)


@router.post("/login", include_in_schema=False)
async def login_submit(payload: _LoginIn) -> Response:
    from app.config import get_settings

    settings = get_settings()
    if not authenticate(payload.username, payload.password):
        return JSONResponse({"detail": "invalid credentials"}, status_code=401)
    token = sign_session(
        settings.dashboard_auth_username,
        settings.dashboard_session_secret,
        settings.dashboard_session_ttl_seconds,
    )
    resp = JSONResponse({"status": "ok"})
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=(settings.app_env != "local"),
        path="/",
    )
    return resp


@router.post("/logout", include_in_schema=False)
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/health")
async def health() -> dict[str, Any]:
    from app.config import get_settings
    from app.maintenance.upstream_watch import LAST_CHECK
    from app.pipeline import LAST_POLL

    return {
        "status": "ok",
        "mode": "picks-only",
        "upstream": LAST_CHECK,
        "polls": LAST_POLL,
        # The dashboard derives its "verified within" window from the actual
        # poll cadence (max(45min, 3 * interval)) instead of hardcoding it.
        "poll_interval_seconds": get_settings().poll_interval_seconds,
    }


def _coerce_float(value: Any) -> float | None:
    """Best-effort str/Decimal -> float for repo rows (every numeric is a
    serialized string). None/blank/unparseable -> None so the caller can fall
    back to a stated neutral input."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attach_confidence(rows: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Add a `confidence_rating` block to each /picks row from existing fields.

    The star rating is the dashboard headline that replaces the recommended
    stake (the stake moves to a hover tooltip). It rates confidence in the
    EDGE (sharp-vs-soft line value), NOT a win probability — see
    app/edge/confidence. Computed here at the route (composition root) so the
    repository layer and the pure rating module both stay clean: the repo only
    serializes DB rows, the rating module only does arithmetic.

    `threshold` is Settings.value_min_edge (the premium edge floor); the live
    edge is preferred over alert-time edge when present, mirroring the
    dashboard's own `current_edge ?? edge` choice. There is no per-pick
    book-count field today, so book_count is None.
    """
    for row in rows:
        edge = _coerce_float(row.get("current_edge"))
        if edge is None:
            edge = _coerce_float(row.get("edge")) or 0.0
        rating = confidence_rating(
            edge=edge,
            threshold=threshold,
            value_filter_score=_coerce_float(row.get("value_filter_score")),
            anchor_type=row.get("anchor_type"),
            book_count=None,
        )
        row["confidence_rating"] = {
            "level": rating.level,
            "label": rating.label,
            "reasons": list(rating.reasons),
        }
    return rows


@router.get("/picks", dependencies=[Depends(require_dashboard_auth)])
async def latest_picks(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    tier: Annotated[str | None, Query(pattern="^(premium|volume)$")] = None,
) -> list[dict[str, Any]]:
    """Latest picks, newest first. `tier` scopes the window server-side —
    the volume shadow tier runs ~6x premium volume, so an unscoped
    latest-200 window would fill with volume rows and hide open premium
    picks entirely (the dashboard fetches each tier separately).
    None = both tiers (legacy feed).

    Each row carries a `confidence_rating` (1..5 star edge-quality headline);
    the recommended stake stays on the row but is surfaced only in a hover
    tooltip on the dashboard (informational, never advice).

    `min_acceptable_odds` per row is the execution helper: the minimum
    displayed odds at which the pick still retains the premium edge floor
    ("still +EV down to X.XX" on the dashboard)."""
    from app.config import get_settings

    threshold = get_settings().value_min_edge
    rows = await latest_picks_with_events(session, limit, tier=tier, min_edge=threshold)
    return _attach_confidence(rows, threshold)


async def _warehouse_available_games(
    request: Request,
    limit: int,
    sport: str | None,
) -> list[dict[str, Any]]:
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        return []
    try:
        async with session_factory() as session:
            return await latest_available_games_with_events(session, limit=limit, sport=sport)
    except Exception as exc:
        logger.warning("available games warehouse fallback failed: %s", type(exc).__name__)
        return []


@router.get("/games", dependencies=[Depends(require_dashboard_auth)])
async def available_games(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=2000)] = 1000,
    sport: Annotated[str | None, Query(pattern="^(soccer|basketball|tennis)$")] = None,
) -> list[dict[str, Any]]:
    """Latest unrestricted football/NBA fixture list from odds ingestion.

    This is a read-only visibility feed. It does not apply edge, odds-age,
    exposure, tier, or pick-status gates; those remain exclusive to /picks.
    """
    from app.pipeline import AVAILABLE_GAMES

    rows: list[dict[str, Any]] = []
    for sport_key in sorted(AVAILABLE_GAMES):
        if sport is not None and sport_key != sport and not sport_key.startswith(f"{sport}_"):
            continue
        rows.extend(AVAILABLE_GAMES[sport_key])
    if not rows:
        rows = await _warehouse_available_games(request, limit=limit, sport=sport)
    rows.sort(key=lambda row: (row["starts_at"] is None, row["starts_at"] or "", row["event"]))
    return rows[:limit]


@lru_cache(maxsize=1)
def _ml_operating_point() -> float | None:
    """The configured value-filter manifest's frozen q* (None = no artifact).

    Cached for the process lifetime: artifacts only change at deploy, and a
    per-request disk read would be blocking IO in the event loop. Reports
    accept ANY manifest verdict — stratifying shadow scores is annotation,
    never enforcement (demotion keeps ValueFilterModel.load's ADOPT gate).
    """
    from app.config import get_settings
    from app.models.value_filter import manifest_operating_point

    settings = get_settings()
    return manifest_operating_point(
        Path(settings.value_ml_model_dir), settings.value_ml_manifest_filename
    )


@router.get("/performance", dependencies=[Depends(require_dashboard_auth)])
async def performance(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, Any]:
    """ROI + stake-weighted log-CLV over settled picks (phase 4 report).

    Headline fields are PREMIUM-tier scoped ("tier_scope": "premium"); the
    volume shadow tier's aggregates ride under "volume" so its many small
    edges can never distort the alerted strategy's numbers.

    "live_evidence" stratifies the settled picks by ML score bucket
    (q* from the configured manifest), tier, and — once the column lands —
    anchor type: the accumulating live instrument for the VALUE_ML_FILTER
    flip decision. Every stratum carries its n; strata under min_n are
    flagged insufficient and the dashboard shows the state, not estimates.
    """
    report = await performance_report(session)
    rows = await live_evidence_rows(session)
    report["live_evidence"] = live_evidence_report(rows, ml_threshold=_ml_operating_point())
    return report


@router.post("/events/{event_id}/result", dependencies=[Depends(require_dashboard_auth)])
async def settle_event(
    event_id: int,
    payload: EventResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    """Settle ALL open picks of an event from a user-entered final score.

    Manual settlement path (dashboard settle button) — records outcomes
    only; nothing here can place a bet.
    """
    event = await session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="event not found")
    settled, skipped = await settle_event_picks(
        session, event_id, payload.home_score, payload.away_score, datetime.now(tz=UTC)
    )
    await session.commit()
    return {"settled": settled, "skipped": skipped}


@router.post(
    "/picks/{pick_id}/result",
    status_code=201,
    dependencies=[Depends(require_dashboard_auth)],
)
async def record_result(
    pick_id: int,
    payload: ResultIn,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    pick = await session.get(Pick, pick_id)
    if pick is None:
        raise HTTPException(status_code=404, detail="pick not found")

    pnl: Decimal | None = None
    roi: Decimal | None = None
    if payload.bet_placed and payload.actual_stake is not None:
        odds = payload.actual_odds or float(pick.decimal_odds)
        if payload.outcome == "won":
            pnl = payload.actual_stake * Decimal(str(odds - 1.0))
        elif payload.outcome == "lost":
            pnl = -payload.actual_stake
        else:  # void / push: stake returned
            pnl = Decimal("0.00")
        if payload.actual_stake > 0:
            roi = pnl / payload.actual_stake

    await session.execute(
        insert(ManualBetLog).values(
            pick_id=pick_id,
            bet_placed=payload.bet_placed,
            actual_stake=payload.actual_stake,
            actual_odds=payload.actual_odds,
            bookmaker_used=payload.bookmaker_used,
            notes=payload.notes,
        )
    )
    await session.execute(
        insert(ResultTracking).values(
            pick_id=pick_id,
            outcome=str(payload.outcome),
            pnl=pnl,
            roi=roi,
            settled_at=payload.settled_at,
        )
    )
    await session.execute(update(Pick).where(Pick.id == pick_id).values(status="settled"))
    await session.commit()
    return {"status": "recorded", "outcome": str(payload.outcome)}
