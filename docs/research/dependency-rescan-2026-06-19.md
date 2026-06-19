# Dependency re-scan — penaltyblog + OddsHarvester (2026-06-19)

**Question (user):** re-read the *live* penaltyblog docs + Colab and the
OddsHarvester README; is anything worth adding to our code? Framed by the
doctrine: the only validated edge is **sharp-vs-soft line shopping + CLV**;
prediction/ratings/fantasy/viz are out unless they improve devig / fair-pricing
/ CLV / market **coverage**. Extends [[penaltyblog-deep-read-2026-06-18]] and
[[data-source-feature-audit]].

## penaltyblog — full-module live re-read: 9/9 SKIP

A 9-agent read of the *current* readthedocs (every module) cross-checked against
our code. Nothing helps that we don't already own or that the doctrine doesn't
reject.

| Module | Verdict | One-line reason |
| --- | --- | --- |
| `models` (8 goal models) | SKIP | DC already wired as a *pricer*; alt pricers are forecasters — we anchor on the devigged sharp line, and DC-goals backtested NEGATIVE CLV. |
| `implied` (devig) | SKIP | Exactly 7 methods; we own all 7, **parity-gated 1e-8** (`tests/test_parity_penaltyblog.py`). No 8th exists. |
| `metrics` (RPS/Brier/Ignorance) | SKIP | We already have log-loss + binary Brier **+ ECE/MCE/reliability bins penaltyblog lacks** (`app/backtesting/calibration.py`). |
| `ratings` (Elo/Massey/Colley/Pi) | SKIP | Pure predictors; "ratings-devigged anchor" is a category error (Elo probs sum to 1 — nothing to devig). |
| `matchflow` (+recipes) | SKIP | Query engine for nested StatsBomb/Opta **event JSON** we don't ingest; `app/resolution/matching.py` fits our flat data better. |
| `scrapers` (4) | SKIP | Only odds-bearing one is `FootballData` = football-data.co.uk CSVs — **we already have `app/ingestion/football_data.py`** (parses Pinnacle closing PSCH/D/A). |
| `betting` + `backtest` | SKIP | Their Kelly = ours minus our caps/drawdown guard; `value_bets` ignores devig + the close (would regress); their backtest has no CLV. |
| `viz` + `xt` + `fpl` | SKIP | Cosmetic charts / expected-threat / fantasy — all need event data we don't have. |
| `agent` / `changelog` / `roadmap` **(new)** | SKIP | penaltyblog now ships its own `.claude/skills/penaltyblog/SKILL.md`, but **ours is better** (pins the rho-convention + push-mass traps theirs omits). Nothing past v1.11.0. |

**Pitch Colab** (the link the user supplied, `drive/1xFfIdvmbFcjHlS_2eHEu3NxD-xLNrbpY`):
read all 38 cells — it is the `penaltyblog.viz.Pitch` interactive-charts tutorial
(shot maps / heatmaps / KDE on a pitch), fed by `Flow.statsbomb.events()`. It
produces no probability/odds/edge, and needs shot/pass x/y event data we don't
collect — so even setting the StatsBomb licence aside, it renders blank for us.
Cosmetic, out of doctrine.

**3 things noted but deferred (not "do now"):**
1. **Push-aware totals** — penaltyblog's `totals(line)->(under,push,over)` beats
   our push-blind `total_goals()`, but only for integer/quarter O/U lines; we
   ingest only half-line 2.5 (push mass = 0). `football_dc.py` already guards it.
2. **`multiple_kelly_criterion`** (correlated-slate portfolio sizing) — the one
   *new* capability; a phase-6 candidate, A/B vs our per-pick path with a
   no-CLV-regression criterion.
3. **football-data closing AH/OU/Max/Avg columns** we fetch-and-discard — already
   the open A1/A2 plan in [[data-source-feature-audit]].

## OddsHarvester — README read

- **We are on the latest.** PyPI `oddsharvester` tops out at **0.3.0** (releases:
  0.1.0/0.2.0/0.2.1/0.3.0); installed = 0.3.0. The GitHub `master` README is just
  *ahead* of any published release — there is **no upgrade to take**.
- **We use it correctly** as a library (`run_scraper`, `app/ingestion/oddsportal.py`)
  for football/basketball/tennis/american-football with our devig-sound markets
  (`1x2,over_under_2_5,btts,double_chance`; basketball OU games lines; tennis
  `match_winner`; AF `home_away`), plus our `register_extra_leagues` (incl. CFL/UFL)
  and the 0.3.0 quirk patches.

### The one genuinely doctrine-positive lever: regional mirrors (`base_url`)

OddsHarvester 0.3.0 **already** supports scraping a regional OddsPortal mirror
(`base_scraper.py:207` `base_url`; `url_builder.rebase_url`; flows through our
existing `run_scraper(**kwargs)`). The README notes a regional mirror "may expose
a **different / larger set of bookmakers**." More soft books to beat against the
sharp anchor **is** our edge — so this is the highest-value OddsHarvester lever,
and it needs **no upgrade** (it is in our installed 0.3.0; we simply never pass
`base_url`, defaulting to `www.oddsportal.com`).

**Not a blind add — validate first.** Before wiring a config knob:
- **Probe**: scrape one league from a candidate mirror (e.g. an Italian/Spanish
  domain) and count *distinct bookmakers per fixture* vs `www`. Only worth it if
  the mirror adds soft books we don't already see.
- **Dedup**: a mirror serves the SAME fixtures — merging bookmaker lists across
  domains per fixture (or scraping only the mirror) is the real work; our
  cross-source matcher would otherwise double-count.
- **ToS posture is unchanged**: still a read-only OddsPortal scrape, just a
  regional domain ("page structure identical; only the domain changes") — no new
  anti-bot bypass, same caution.

### Everything else in the README — not needed

| README feature | Verdict |
| --- | --- |
| More sports (Rugby/Ice Hockey/Baseball/Handball/Volleyball) | Visibility-only, no proven CLV — same gate as tennis/AF; not "needed". |
| More football markets (`european_handicap`, live `asian_handicap`) | We bridge AH from 1x2+OU (`ah_bridge.py`); live AH/EH capture is the deferred chained/gated item. |
| `--preview-only` (avg odds only) | **Anti-doctrine** — line-shopping NEEDS per-bookmaker odds; preview hides them. |
| `--odds-history` (movement) | Already covered by our own snapshot archive + ARCADIA Pinnacle close. |
| `--bookies-filter crypto` | *Maybe* more soft books (crypto books run soft) — same probe-first logic as regional mirrors. |
| Proxy / `--concurrency` / `--request-delay` | Robustness only; we tune concurrency/delay in our scheduler; proxy is geo/anti-block (ToS-gray) — only if OddsPortal starts blocking us. |
| Historic mode, S3 storage, CSV | Backfill/ops; historic is the deferred heavy-scrape item; not a live-edge add. |

## Verdict

Nothing is **needed**. penaltyblog is used correctly + completely for its one
doctrine role (pricing football to devig against); OddsHarvester is at its latest
and used correctly. The single real opportunity surfaced by this re-scan is
**regional-mirror / crypto-book harvesting to widen the soft-book set** (more
line-shopping shots) — available today in 0.3.0, but gated on a validation probe
before wiring, not a blind add.
