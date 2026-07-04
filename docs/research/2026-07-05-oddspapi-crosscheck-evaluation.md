# OddsPapi as the redundant football sharp-close cross-check — bounded evaluation (Task A7)

**Status:** research brief, read-only evaluation. NO integration code written.
**Date:** 2026-07-04 (filed under the task's assigned 2026-07-05 name).
**Author:** quant-sports-researcher agent.

## Question

football-data.co.uk stopped publishing Pinnacle columns (`PSH/PSCH/…`) after
**2026-01-15** (0% populated thereafter, proven in
`docs/research/2026-07-03-bsp-h2-slate-readiness.md` §b). That kills the only
independent reference we had for **validating our own Pinnacle ARCADIA
capture** (`app/ingestion/pinnacle_arcadia.py`, warehouse `bookmaker='Pinnacle'`)
— a single point of failure for the 2026-H2 slate's anchor source. Should
OddsPapi (free tier, already scoped in
`docs/research/data-source-feature-audit.md`) become the redundant sharp-close
cross-check for football?

## Method

- Repo reads: `docs/research/data-source-feature-audit.md` (OddsPapi REJECT
  note, line 116), `docs/research/2026-07-03-bsp-h2-slate-readiness.md`
  (anchor landscape), `app/ingestion/oddspapi.py` (existing loader),
  `scripts/value_backtest.py` (`--source oddspapi-nba` path, lines 52,
  1313–1384), `tests/test_oddspapi.py` (exists),
  `docs/research/2026-06-28-backtest-data-and-build-plan.md` (row 5),
  `.claude/memory/decisions.md:881` (conflicting "PAID" note).
- 3 polite HTTP requests to public OddsPapi pages (no signup, no key):
  `oddspapi.io/us/pricing`, `oddspapi.io/us/docs`, `oddspapi.io/`
  (2026-07-04).

## Findings

### F1 — A working, safety-reviewed OddsPapi client already exists in-repo

`app/ingestion/oddspapi.py` is a GET-only client for
`GET /v4/historical-odds?fixtureId=…&bookmakers=…&apiKey=…` with:

- chronological per-outcome price history → `price_history_open_close()`
  reduces to (open, close); Decimal at the boundary; UTC-aware times;
- key hygiene (httpx/httpcore pinned to WARNING so the query-string key never
  logs); tenacity retry; `ODDSPAPI_KEY` optional — absent key skips cleanly;
- an operator-placed per-fixture "bundle" JSON format (the opaque
  `marketId`/`outcomeId` differ per fixture, so ids are resolved once and
  stored in the bundle), consumed by `load_oddspapi_dir()`;
- an NBA backtest adapter (`scripts/value_backtest.py:1313-1384`,
  `--source oddspapi-nba`): Pinnacle open = anchor, Pinnacle close = CLV ref.

The parsing/secret/format work is **done**. What does not exist: football
fixture resolution, a kickoff cutoff, and a comparison script (F5).

### F2 — Endpoint shape and close semantics (docs overview, fetched 2026-07-04)

Documented endpoints: `GET /tournaments` (leagues per `sportId`),
`GET /odds-by-tournaments` (upcoming fixtures + odds per league — returns
`fixtureId`s directly), `GET /fixtures` / `GET /fixture`, `GET /odds`,
`GET /historical-odds`, `GET /settlements`, `GET /scores`. Auth = `apiKey`
query param. 60+ sports; football/soccer demonstrated (Premier League, LaLiga
examples); 300+ bookmakers with **Pinnacle prominently featured** in the doc
examples; max 3 bookmaker slugs per request (per the loader docstring's
2026-06-29 doc fetch).

**Close semantics:** `historical-odds` returns the full timestamped
(`createdAt`) price history per outcome. "Close" is therefore *derived*: last
`active` entry with `createdAt < kickoff`. There is no `is_closing` flag —
same situation as our ARCADIA capture. Caveat: Pinnacle prices soccer
**in-play**, so the raw last entry may be post-kickoff; the existing
`price_history_open_close()` takes the last entry unconditionally (fine for
operator-curated NBA bundles, **not** safe for a football close — a pre-KO
cutoff using the fixture `startTime` is a required adaptation).

### F3 — Pricing/ToS: free tier advertised but quota NOT independently re-verified

- Homepage (fetched 2026-07-04): historical odds described as **"Data that's
  usually expensive — made free for our users"** + "flexible,
  developer-friendly pricing". A free historical tier is advertised.
- Pricing page is a **dynamic calculator** (bookmakers × sports ×
  requests/month); no static tier numbers were retrievable without JS.
- The **250 req/month** figure comes from this repo only: the audit doc
  (line 116, sourced from web search 2026-06-18) and the loader docstring
  (docs fetched 2026-06-29). Treat it as plausible but
  **operator-verify-at-signup**.
- Conflict note: `.claude/memory/decisions.md:881` calls OddsPapi "PAID" (in
  the context of rejecting the OddsTracker repo, entry predating the
  2026-06-29 doc fetch). The later, direct-doc-sourced loader docstring
  ("free tier … shallow") supersedes it; both agree the free tier is
  **shallow** (history depth / book breadth / fixture coverage limited, thin
  rows expected).
- ToS live at `oddspapi.io/us/legal/terms` — **not read** (would have cost the
  remaining request budget on a JS-rendered page). Internal analytical use is
  the normal aggregator-API case, but redistribution/derived-data terms must
  be confirmed by the operator at signup. No scraping is involved — this is a
  documented keyed API, so the ToS risk class is far below OddsPortal's.

### F4 — Independence: right kind for capture validation, wrong kind for anchoring

- OddsPapi is a third-party aggregator with **its own collection
  infrastructure**; our ARCADIA capture hits
  `guest.api.arcadia.pinnacle.com` directly through our proxy pool. Different
  operator, network path, cadence, and failure modes → a stale/broken/
  wrong-game ARCADIA capture **would** show up as disagreement.
- Both observe the **same underlying book (Pinnacle)**. For *capture
  validation* that is exactly what's required (same ground truth, independent
  measurement). For *strategy anchoring* it adds nothing: OddsPapi cannot
  serve as an independent anchor, cannot validate BSP, and cannot answer
  whether Pinnacle's close is itself efficient.
- Bonus property vs The Odds API (#2 in the readiness doc): OddsPapi's
  history endpoint is **retroactive per fixture** — the sample can be pulled
  days after the matches, no capture process must be running at close time.
  The Odds API free tier is forward-capture only for this purpose.

### F5 — Budget math: a monthly 50–100-fixture cross-check fits inside 250 req/month

Per monthly sample (Pinnacle-only slug, so the 3-bookmaker cap is irrelevant):

| Step | Requests |
|---|---|
| `GET /tournaments` (per sport, cacheable across months) | 1 (amortized ~0) |
| Fixture-id resolution (`/odds-by-tournaments` or `/fixtures`), ~5–10 leagues × ~2–4 matchdays sampled | ~10–30 |
| `GET /historical-odds`, 1 req/fixture | 50–100 |
| Re-pull allowance (errors, spot re-checks) | ~10 |
| **Total** | **~70 (N=50) to ~140 (N=100)** |

Against the repo-cited 250/month: N=50 leaves ~3.5× headroom, N=100 leaves
~1.8×. **Recommended sample: N=50–60** (≥2× headroom). Statistical adequacy:
the check is a paired price comparison per outcome (≈150 1X2 pairs at N=50),
ample to detect systematic capture staleness or a wrong-game attachment at
the rates that would matter. Even if the true free quota is half the assumed
figure (~125/mo), an N=40 check still fits.

ARCADIA-side volume to check against: ~5.5k soccer events/month, last capture
median 17.5 min pre-KO (readiness doc §c1) — sampling 50 is trivial.

### F6 — What the cross-check WOULD and would NOT validate

**Would validate (monthly, offline, read-only):**

1. **ARCADIA close fidelity** — our last-pre-KO snapshot vs OddsPapi's last
   pre-KO Pinnacle point, with `createdAt` deltas to control for timing;
   flags systematic staleness or parsing drift.
2. **Quasi-close gap quantification** — how much our median-17.5-min-pre-KO
   capture differs from a nearer-to-KO Pinnacle point (directly informs the
   H2 pre-registration's anchor definition).
3. **Wrong-game detection** — gross price disagreement on a matched fixture
   is a canonical-matcher red flag (complements, never replaces,
   `canonical-matcher-verifier` invariants).
4. **Capture-outage confirmation** — distinguishes "Pinnacle didn't price
   it" from "our capture missed it".

**Would NOT validate / provide:**

1. **No Feb–May 2026 backfill** for the spent slate — free-tier history depth
   is unverified/shallow, and per ADR-0019 that window is spent for selection
   AND evaluation regardless.
2. **Not an anchor source** and not a strategy input — same book as ARCADIA;
   using it as anchor would be redundant, and wiring it into the live
   pipeline is out of scope by design.
3. **No BSP validation** (different ground truth) and no soft-book fill
   validation.
4. **No market-breadth guarantee** — totals/AH presence on the free tier per
   fixture is unknown until sampled; the check should start 1X2-first with
   totals/AH opportunistic.
5. **No calibration/CLV performance claims** — this is a data-fidelity
   instrument only.

## Implications for this project

The H2 slate's blocking prerequisite (readiness doc §a1) is an ARCADIA-based
anchor loader. An anchor source with **no independent verification path** is
how the Jan–Jun slate got burned (anchor died 2026-01-15, unnoticed until the
bet-rate collapsed 24×). A ~70-requests/month third-party check on the same
book is cheap insurance on the exact failure mode that already cost one slate.
Effort is small because the client, secret hygiene, bundle format, and tests
already exist; the delta is football fixture resolution + a pre-KO cutoff +
one comparison script against the warehouse (canonical matcher only for the
event join).

## Recommended decision

**GO — conditional, and narrowly scoped.** Adopt OddsPapi as the **monthly
offline cross-check of ARCADIA football close fidelity** (N=50–60 fixtures,
`pinnacle` slug only, ~70 req/month), NOT as an anchor, NOT as backfill, NOT
in the live pipeline.

Conditions precedent (operator, at signup — no code before these):

1. Confirm the actual free-tier quota (repo-cited 250 req/mo; unverified —
   the pricing page is a JS calculator). Bar: ≥ ~100 req/mo makes an N=40+
   check viable; below that, NO-GO on the free tier.
2. Confirm ToS permit internal analytical use of the data (terms at
   `oddspapi.io/us/legal/terms`); key into `.env` as `ODDSPAPI_KEY`
   (gitignored, 0600) — the loader already reads it optionally.
3. First-month probe (within budget): verify free-tier responses actually
   carry Pinnacle football history near kickoff for the sampled leagues; if
   the free tier is too shallow to return pre-KO points, downgrade to NO-GO
   and fall back to The Odds API forward capture (readiness doc §c2) as the
   redundancy layer.

Build guardrails when implemented (future task, not this one): read-only
GET client already compliant; add pre-KO cutoff before calling anything a
close; comparison script is offline/scripts-side, never imported by the live
scheduler; disagreements produce a review-queue report, never an automatic
data mutation.

## Open questions

1. Actual free-tier quota and whether `historical-odds` is quota-weighted
   differently from listing endpoints (docs have a "Requests & Quota" section
   not readable without JS/signup).
2. Free-tier history **granularity near kickoff** — enough points in the last
   30–60 min pre-KO to define a close? (First-month probe answers this.)
3. Free-tier league coverage vs our premium-pick league distribution
   (obscure-league premiums are exactly where capture validation matters
   most, and exactly where a free aggregator tier is most likely thin).
4. OddsPapi's own capture lag vs Pinnacle real-time — `createdAt` timestamps
   let the comparison control for it, but a large systematic lag would blunt
   staleness detection.
5. Whether football-data.co.uk `PS*` columns return in the 2026-27 season
   files (readiness doc §c4) — if they do, they resume the cross-check role
   for covered leagues at zero request cost, and OddsPapi drops to
   obscure-league coverage only.

## Sources

- `app/ingestion/oddspapi.py` (loader + endpoint semantics; upstream docs
  fetched 2026-06-29 per its docstring).
- `scripts/value_backtest.py:52,1244,1313-1384` (NBA path shape).
- `docs/research/data-source-feature-audit.md:116-119` (prior scoping).
- `docs/research/2026-07-03-bsp-h2-slate-readiness.md` (anchor landscape,
  coverage collapse proof, ARCADIA close-quality stats).
- `docs/research/2026-06-28-backtest-data-and-build-plan.md` (Part A row 5).
- `.claude/memory/decisions.md:881` (conflicting PAID note, superseded).
- Live fetches 2026-07-04: `oddspapi.io/us/pricing` (dynamic calculator),
  `oddspapi.io/us/docs` (endpoint map, fixture resolution, Pinnacle in
  examples), `oddspapi.io/` ("free historical data" marketing claim).
