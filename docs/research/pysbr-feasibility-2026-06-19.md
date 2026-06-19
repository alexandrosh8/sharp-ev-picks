# PySBR Feasibility — Free Tennis/NFL Closing-Line Source?

**Date:** 2026-06-19
**Repo:** [jemorriso/PySBR](https://github.com/jemorriso/PySBR) (`python-sbr` on PyPI, v0.3.2)
**Question:** Is PySBR a viable FREE, doctrine-compliant source of TENNIS (ATP) and
NFL closing / line-history odds for our picks-only platform, to unblock CLV evidence
for tennis (n=20 graded) and American football (n=0)?
**Method:** File-by-file inspection via `gh api` (plugin GitHub MCP was rate-limited
unauthenticated; `gh api` is the doctrine fallback). Read-only. No clone, no install,
no install-script execution. One passive `curl` liveness probe of the public endpoint
(GET, no auth, no query payload).

---

## VERDICT: REJECT (as a live data feed) / reference-only (as code patterns)

The library is clean, MIT-licensed, unauthenticated, read-only, and carries **zero
automatic-betting risk** — it would have been doctrine-compliant. **But its single data
source is dead.** The entire library depends on one hardcoded GraphQL endpoint,
`https://www.sportsbookreview.com/ms-odds-v2/odds-v2-service`, which:

- The repo's own README warns is "undocumented and subject to change, so use at your own risk."
- Issue **#12 (2022-01-17), "SBR Service is no longer working"** confirms broke ~Jan 2022.
- My passive probe today returns **HTTP 404** on that path (host + TLS are live — SBR's
  marketing site resolves fine — but the `ms-odds-v2` microservice path is gone).
- The repo is **archived** (read-only since 2021-09), last commit **2021-02-03**.
- **No fork repointed it.** Recent forks (p4moss12 2025-10, alexshen339 2024-04) are
  0-star clones with no endpoint fix; downstream vendored copies
  (`paxcullin/crowdsourcedscores-web`, `jeremyjpj0916/sportsbet-python-tooling`) still
  carry the same dead URL.

There is no working endpoint to point an `app/ingestion/sbr_*.py` probe at. A probe is
**not warranted** — the answer to "is the closing-line claim real?" is already known
from the recorded cassettes (it WAS real and excellent), and the answer to "can we fetch
it today?" is a definitive NO from the 404 + Issue #12. Building a probe would only
re-confirm the 404.

---

## Scoring row (standard 11-column)

| Column                     | Assessment                                                                                                                                                                                                                                                                                           |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Repository**             | jemorriso/PySBR (`python-sbr` 0.3.2)                                                                                                                                                                                                                                                                 |
| **Category**               | Read-only odds/line-history client (SportsbookReview GraphQL wrapper)                                                                                                                                                                                                                                |
| **Stars / activity**       | 80★, 23 forks, 7 open issues — **ARCHIVED**, last push 2021-09-08, last commit 2021-02-03                                                                                                                                                                                                            |
| **Core function**          | Fetch current/best/opening lines + full time-ordered **line history** per (event, market, sportsbook) across ATP/NFL/NBA/EPL/etc. from SBR                                                                                                                                                           |
| **Code quality**           | High — typed, decorated arg validation (`@Query.typecheck`), clean Query/Lines class hierarchy, YAML-driven config, VCR cassette tests for NFL+ATP+line history                                                                                                                                      |
| **Maintenance**            | **Dead** — archived, endpoint defunct since ~2022, no maintained fork                                                                                                                                                                                                                                |
| **Directly reusable**      | **No** (live feed dead). Partial only as code patterns (GraphQL transport scaffolding, devig-free line-history reconstruction logic)                                                                                                                                                                 |
| **Best file to adapt**     | `pysbr/queries/lines.py` (`_clean_lines`, line-history flattening, id→name translation) as a _reference pattern_ only — not the live transport                                                                                                                                                       |
| **Security concern**       | Low. No secrets, no credentials, no install scripts. `fake-useragent` randomizes the User-Agent (soft UA spoofing) and headers mimic a browser (`Referer`, `Host`) — mild ToS-grey scraping posture, but **not** an anti-bot _bypass_ (no CAPTCHA solving, no proxy rotation, no Cloudflare evasion) |
| **Automatic-betting risk** | **NONE** — see dedicated section below                                                                                                                                                                                                                                                               |
| **Final decision**         | **reject** (live source) / **reference-only** (patterns)                                                                                                                                                                                                                                             |

---

## Automatic-betting risk: NONE (verified)

Every code path is GET-shaped, read-only market data. Confirmed by inspecting all line
query classes and the base transport:

- **No order placement / modify / cancel.** No exchange order submission. No Betfair/
  bookmaker POST-to-place anywhere.
- **No bookmaker login / auth.** The transport sends NO API key, token, cookie, or
  session. Headers are UA + content-type + browser-mimic Referer/Host only.
- **No browser automation to a betting slip.** No Selenium/Playwright; it is a pure
  `gql` over `requests` HTTP client.
- **No credential storage.** `setup.py` `install_requires=["gql","pandas","pytz","pyyaml","fake-useragent"]` — no secrets-handling deps, no postinstall/custom build hooks.
- The only "betting math" present is **settlement/grading** of _past_ lines
  (`lines.py::_resolve_bet`/`_evaluate_bet` → returns 'W'/'L' and the $100 profit a
  completed bet _would have_ returned). This is informational result-tracking, identical
  in spirit to our own settler — it places nothing.

This library could not place a bet even if instructed to. It has no write surface at all.

---

## What the inspection PROVED (one function quoted per core file)

### 1. LICENSE — MIT confirmed

`LICENSE`: `MIT License / Copyright (c) 2020 Jeremy Morrison`. `setup.py` declares
`license="MIT"` + `"License :: OSI Approved :: MIT License"`. **Adaptation of patterns
is license-clean** (attribution + notice retention required).

### 2. GraphQL transport — `pysbr/queries/query.py` (read-only intent, unauthenticated)

The `Query.__init__` hardcodes the single endpoint and builds an **unauthenticated**
transport — no key, no token, no login:

```python
transport = RequestsHTTPTransport(
    url="https://www.sportsbookreview.com/ms-odds-v2/odds-v2-service",
    headers=headers,  # UA (fake_useragent random) + content-type + Referer/Host only
)
self.client = Client(transport=transport, fetch_schema_from_transport=False)
```

Execution path (`_execute_query`):

```python
def _execute_query(self, q: str) -> Dict:
    """Execute the GraphQL query specified by the string q."""
    return self.client.execute(gql(q))
```

Note: GraphQL queries via `RequestsHTTPTransport` go out as HTTP **POST** (the GraphQL
standard for this transport), not literally GET — but they are strictly **read queries**
(no mutations anywhere) and **carry no credentials**. Functionally this is a read-only
data pull. The `fake_useragent.UserAgent().random` UA + browser-mimic `Referer`/`Host`
headers are a soft scraping posture worth noting (ToS-grey), but not an anti-bot bypass.

### 3. Line history — `pysbr/queries/linehistory.py` (time-ordered per event/market/book — YES)

`LineHistory(Lines)` queries the exact tuple we need and flattens the nested history:

```python
def _find_data(self):
    lines = self._raw[self.name]
    cleaned_lines = []
    for el in lines:
        cleaned_lines.extend(el["lines"])
    return cleaned_lines
```

Args (from `arguments.yaml`): `eid, mtid, paid, partid` = (event, market, **sportsbook**,
participant). The recorded cassette `tests/graphql_responses/test_line_history_nfl1.yaml`
shows a **real Pinnacle (`paid: 20`) NFL line history** with multiple timestamped entries:

```yaml
lineHistory:
- lines:
  - ap: -114        # American odds
    pri: 1.8772     # decimal price
    adj: -3         # spread/handicap
    eid: 4143532    mtid: 401   paid: 20   partid: 1520
    tim: 1604984377000   # Unix epoch ms  (2020-11-10 UTC)
  - ap: 104   pri: 2.04   adj: 3   partid: 1530   tim: 1604982556000
```

**Closing-line reconstruction is mechanically sound:** for a given (event, market,
Pinnacle), select the entry with the greatest `tim` <= kickoff_utc. `best/current/opening`
also exist (`bestlines.py` → `bestLines(eids, mtids)`; `currentlines.py` →
`currentLines(eids, mtids, paids)`; `openinglines.py` → `openingLines(eids, mtids, paid)`),
so opening→closing movement is fully available **when the endpoint is alive** — which it
is not.

### 4. Config — ATP + NFL coverage + Pinnacle present

- `config/atp.yaml`: `lid: 23 / name: ATP Tour / abbreviation: ATP` — **ATP tennis covered.**
- `config/nfl.yaml`: `lid: 16 / name: National Football League` with full 32-team id map — **NFL covered.**
- `config/sportsbooks.yaml`: **`sbid: 238 / paid: 20 / nam: Pinnacle / alias: Pinnacle`**
  — **our sharp anchor is present** (line-query `sportsbook_id` = `paid: 20`). Also sharp:
  5Dimes, BetOnline, Bookmaker, Matchbook, Heritage. ~27 active books; many (Bet365 on,
  William Hill / Unibet / BetFred commented out). `sportsbook.py::Sportsbook.id("pinnacle")`
  resolves the name → id, case-insensitive.

### 5. Endpoint liveness — DEAD

- README self-warns the endpoint is "undocumented and subject to change."
- Issue **#12 (2022-01-17)**: _"As of today, SBR odds service appears to no longer be working."_
- Issue #5 ("HTTPError: 463 Client Error") = bot-block status seen even in 2021.
- Passive GET probe (2026-06-19): `HTTP 404` on
  `https://www.sportsbookreview.com/ms-odds-v2/odds-v2-service/` (host live, path gone).
- Last commit 2021-02-03; archived 2021-09. SportsbookReview's site was rebuilt
  (SBR/Pickswise era) and the `ms-odds-v2` microservice was retired.

---

## Files opened (inspected, with one-line descriptions)

- `LICENSE` — MIT, © 2020 Jeremy Morrison (adaptation-clean).
- `setup.py` — pkg `python-sbr` 0.3.2; deps gql/pandas/pytz/pyyaml/fake-useragent; **no install scripts**.
- `README.md` — confirms scope (NFL/ATP/EPL/... lines + history) and the "endpoint subject to change, use at own risk" warning.
- `pysbr/queries/query.py` — base `Query`: hardcoded SBR GraphQL endpoint, unauthenticated transport, `_execute_query`, arg typechecking.
- `pysbr/queries/lines.py` — `Lines` base: league/sport/sportsbook id maps (Pinnacle→20), `_clean_lines`, id→name translation, **read-only settlement math** (`_resolve_bet`).
- `pysbr/queries/linehistory.py` — `LineHistory`: time-ordered history per (event, market, sportsbook, participant); flattens nested `lines`.
- `pysbr/queries/bestlines.py` — `BestLines(eids, mtids)`: best price across books per event/market.
- `pysbr/queries/currentlines.py` — `CurrentLines(eids, mtids, paids)`: latest line per book.
- `pysbr/queries/openinglines.py` — `OpeningLines(eids, mtids, paid)`: opening line + timestamp.
- `pysbr/config/sportsbooks.yaml` — book id map; **Pinnacle present (paid 20)** + other sharps.
- `pysbr/config/sportsbook.py` — `Sportsbook` config class; name↔id resolution.
- `pysbr/config/atp.yaml` — ATP Tour (lid 23). `pysbr/config/nfl.yaml` — NFL (lid 16) + 32 teams.
- `pysbr/config/fields.yaml` + `arguments.yaml` — `line_history` GraphQL field/arg templates (eid/mtid/paid/partid).
- `tests/graphql_responses/test_line_history_nfl1.yaml` — **recorded real Pinnacle NFL line history** (ap/pri/adj/tim) proving the closing-line claim was genuine.
- Repo metadata + issues #5/#11/#12 + commit log + fork list — established archived/dead status and absence of a working fork.

---

## Recommendation

1. **Do not integrate PySBR or its endpoint.** Add SBR `ms-odds-v2` to the
   known-dead-source list (alongside API-Football SUSPENDED) so it isn't re-evaluated.
2. **Do not build the `app/ingestion/sbr_*.py` probe.** The conditional "IF viable"
   from the task is not met — a probe against a 404 endpoint yields no information beyond
   what's already proven here. If a future maintained fork ever repoints to a _live_
   SBR/Pinnacle endpoint, the minimal read-only probe to validate would be: ATP +
   `moneyline` market for one in-progress event, and NFL + `pointspread` (mtid 401) for
   one event, calling `LineHistory(event, market, paid=20, partids)` and asserting
   `max(tim)<=kickoff` reconstructs a sane Pinnacle close. Until then: N/A.
3. **Tennis/NFL closing lines remain blocked by this avenue.** To unblock CLV evidence,
   redirect to live free sharp-line sources already in scope: OddsPortal via OddsHarvester
   (`app/ingestion/oddsportal.py`) covers ATP tennis + NFL with Pinnacle history when the
   scrape lands; and The Odds API free tier carries `tennis_atp_*` and `americanfootball_nfl`
   with Pinnacle. Those, not a defunct 2021 GraphQL endpoint, are the path.
4. **Patterns worth borrowing (reference-only, MIT-attributed):** the line-history→closing
   reconstruction (pick latest `tim`<=kickoff) and the id→name/sportsbook translation
   layer in `lines.py` are clean references for our own normalizer — but copy the _idea_,
   not the dead transport.
