      "use strict";
      (function () {
        // ===== fetch layer — every request goes through this timeout guard ==
        const FETCH_TIMEOUT_MS = 15000;
        const MATCH_RATE_TIMEOUT_MS = 45000;
        const MAX_JSON_BYTES = 4 * 1024 * 1024;
        const responseGuards = new WeakMap();
        function fetchGuarded(url, opts, timeoutMs) {
          // The deadline covers headers AND body consumption. The old helper
          // cleared its timer as soon as response headers arrived, leaving a
          // stalled/chunked body and JSON decode unbounded.
          const ctrl = new AbortController();
          const timeout = timeoutMs || FETCH_TIMEOUT_MS;
          const timer = setTimeout(() => ctrl.abort(), timeout);
          return fetch(url, Object.assign({}, opts, { signal: ctrl.signal })).then(
            (res) => {
              responseGuards.set(res, { ctrl, timer, timeout });
              return res;
            },
            (err) => {
              clearTimeout(timer);
              throw err;
            }
          );
        }
        const isTimeoutErr = (e) => !!e && (e.name === "AbortError" || e.name === "TimeoutError");

        function releaseResponseGuard(res) {
          const guard = responseGuards.get(res);
          if (!guard) return;
          clearTimeout(guard.timer);
          responseGuards.delete(res);
        }

        function responseError(message, name) {
          const err = new Error(message);
          err.name = name || "ResponseError";
          return err;
        }

        async function readJsonBody(res) {
          const guard = responseGuards.get(res);
          try {
            const contentType = res.headers && res.headers.get("content-type");
            if (contentType && contentType.toLowerCase().indexOf("json") === -1) {
              throw responseError("Expected a JSON response.", "ResponseFormatError");
            }
            const rawLength = res.headers && res.headers.get("content-length");
            const statedLength = rawLength == null ? NaN : Number(rawLength);
            if (Number.isFinite(statedLength) && statedLength > MAX_JSON_BYTES) {
              throw responseError("JSON response exceeds the size limit.", "PayloadTooLargeError");
            }

            let text = "";
            if (res.body && typeof res.body.getReader === "function") {
              const reader = res.body.getReader();
              const decoder = new TextDecoder();
              let received = 0;
              while (true) {
                const part = await reader.read();
                if (part.done) break;
                received += part.value.byteLength;
                if (received > MAX_JSON_BYTES) {
                  await reader.cancel();
                  throw responseError("JSON response exceeds the size limit.", "PayloadTooLargeError");
                }
                text += decoder.decode(part.value, { stream: true });
              }
              text += decoder.decode();
            } else {
              text = await res.text();
              if (new TextEncoder().encode(text).byteLength > MAX_JSON_BYTES) {
                throw responseError("JSON response exceeds the size limit.", "PayloadTooLargeError");
              }
            }
            if (guard && guard.ctrl.signal.aborted) {
              throw responseError("Response deadline exceeded.", "TimeoutError");
            }
            if (!text.trim()) throw responseError("JSON response body is empty.", "ResponseFormatError");
            try {
              return JSON.parse(text);
            } catch (err) {
              throw responseError("JSON response is malformed.", "ResponseFormatError");
            }
          } catch (err) {
            if (guard && guard.ctrl.signal.aborted && !isTimeoutErr(err)) {
              throw responseError("Response deadline exceeded.", "TimeoutError");
            }
            throw err;
          } finally {
            releaseResponseGuard(res);
          }
        }

        function authRequired() {
          setPillText("Authentication required.", "degraded");
          window.location.assign("/login");
        }

        async function readJson(res, validator) {
          if (res.status === 401) {
            releaseResponseGuard(res);
            authRequired();
            throw new Error("Authentication required.");
          }
          if (!res.ok) {
            const e = new Error("HTTP " + res.status);
            e.httpStatus = res.status;
            releaseResponseGuard(res);
            throw e;
          }
          const body = await readJsonBody(res);
          return validator ? validator(body) : body;
        }

        function isRecord(value) {
          return value !== null && typeof value === "object" && !Array.isArray(value);
        }
        function expectArrayPayload(value, label) {
          if (!Array.isArray(value)) throw responseError(label + " payload must be an array.", "SchemaError");
          if (value.some((row) => !isRecord(row))) {
            throw responseError(label + " payload contains a non-object row.", "SchemaError");
          }
          return value;
        }
        function expectObjectPayload(value, label) {
          if (!isRecord(value)) throw responseError(label + " payload must be an object.", "SchemaError");
          return value;
        }
        function validateHealthPayload(value, httpStatus) {
          const health = expectObjectPayload(value, "Health");
          if (health.status !== "ok" && health.status !== "degraded") {
            throw responseError("Health payload has an unknown status.", "SchemaError");
          }
          if ((httpStatus === 200 && health.status !== "ok") ||
              (httpStatus === 503 && health.status !== "degraded")) {
            throw responseError("Health HTTP status and payload disagree.", "SchemaError");
          }
          if (health.mode !== "picks-only" || !isRecord(health.polls)) {
            throw responseError("Health payload is missing authenticated detail.", "SchemaError");
          }
          Object.values(health.polls).forEach((poll) => {
            if (!isRecord(poll) || (poll.per_market != null && !isRecord(poll.per_market))) {
              throw responseError("Health payload contains an invalid poll record.", "SchemaError");
            }
            if (poll.finished_at != null && !Number.isFinite(timestampMs(poll.finished_at))) {
              throw responseError("Health payload contains an invalid poll timestamp.", "SchemaError");
            }
          });
          const age = health.newest_poll_age_seconds;
          if (age !== null && (typeof age !== "number" || !Number.isFinite(age) || age < 0)) {
            throw responseError("Health payload has an invalid poll age.", "SchemaError");
          }
          for (const key of ["poll_interval_seconds", "max_odds_age_seconds", "poll_max_age_seconds"]) {
            if (typeof health[key] !== "number" || !Number.isFinite(health[key]) || health[key] <= 0) {
              throw responseError("Health payload has an invalid " + key + ".", "SchemaError");
            }
          }
          for (const key of ["value_min_edge", "value_volume_min_edge"]) {
            if (typeof health[key] !== "number" || !Number.isFinite(health[key]) || health[key] < 0) {
              throw responseError("Health payload has an invalid " + key + ".", "SchemaError");
            }
          }
          return health;
        }
        // /health legitimately answers 503 WITH a full degraded body. Every
        // other HTTP status, schema mismatch, or cold-start body fails closed.
        async function readHealthJson(res) {
          if (res.status === 401) {
            releaseResponseGuard(res);
            authRequired();
            throw new Error("Authentication required.");
          }
          if (res.status !== 200 && res.status !== 503) {
            const err = new Error("HTTP " + res.status);
            err.httpStatus = res.status;
            releaseResponseGuard(res);
            throw err;
          }
          return validateHealthPayload(await readJsonBody(res), res.status);
        }

        // ===== formatting family — missing/invalid always renders "—" =======
        const $ = (id) => document.getElementById(id);
        function fmt(v) { return v === null || v === undefined || v === "" ? "—" : v; }
        // Market-qualified selection label: bare Yes/No markets (e.g. BTTS) are
        // ambiguous without the market name — prefix it so "Yes @ 3.75" reads
        // "BTTS Yes @ 3.75". Self-describing selections (team names, Over/Under
        // lines, handicaps) are returned unchanged.
        function selLabel(p) {
          const sel = fmt(p && p.selection);
          const raw = String((p && p.selection) || "").trim().toLowerCase();
          if (raw === "yes" || raw === "no") {
            // Fix 2026-07-10 #2: route through the ONE shared market
            // formatter so the same market never renders two different ways.
            const mk = marketLabel((p && p.market) || "");
            return mk !== "—" ? mk + " " + sel : sel;
          }
          return sel;
        }
        // Fix 2026-07-10 #2 — ONE shared human label for raw market/bet-type
        // keys (h2h, double_chance, oc_half_time_full_time, spreads_minus_1_5,
        // …). Explicit map for known keys, generic decoding for the rest.
        const MARKET_LABELS = {
          h2h: "Moneyline / H2H",
          moneyline: "Moneyline / H2H",
          double_chance: "Double Chance",
          half_time_full_time: "Half Time / Full Time",
          correct_score: "Correct Score",
          btts: "Both Teams To Score",
          totals: "Totals (O/U)",
          spreads: "Spread / Handicap",
          dnb: "Draw No Bet",
          team_totals: "Team Totals",
        };
        function marketLabel(key) {
          if (key === null || key === undefined || String(key).trim() === "") return "—";
          let k = String(key).trim().toLowerCase();
          if (k.indexOf("oc_") === 0) k = k.slice(3);
          if (MARKET_LABELS[k]) return MARKET_LABELS[k];
          let m = k.match(/^spreads_(minus|plus)_(\d+)_(\d+)$/);
          if (m) return "Spread " + (m[1] === "minus" ? "−" : "+") + m[2] + "." + m[3];
          m = k.match(/^totals_(\d+)_(\d+)$/);
          if (m) return "Totals " + m[1] + "." + m[2];
          // NminusM score patterns: set_betting_3minus_0 -> "Set Betting 3−0"
          k = k.replace(/(\d+)minus_?(\d+)/g, "$1−$2");
          return k.split("_").filter(Boolean)
            .map((w) => (/\d/.test(w) ? w : w.charAt(0).toUpperCase() + w.slice(1)))
            .join(" ");
        }
        // Fix 2026-07-10 #4 — DISPLAY-ONLY typo corrections for event/team
        // names. This never touches matching/resolution/alias code (wrong-game
        // risk lives there); it only fixes how an upstream misspelling renders.
        const TEAM_TYPO_FIXES = {
          "Abroath": "Arbroath",
          "Ferrovario": "Ferroviario",
        };
        function eventLabel(v) {
          const s = fmt(v);
          if (s === "—") return s;
          let out = String(s);
          Object.keys(TEAM_TYPO_FIXES).forEach((bad) => {
            out = out.split(bad).join(TEAM_TYPO_FIXES[bad]);
          });
          return out;
        }
        function fmtNum(v, digits) {
          const n = Number(v);
          if (v === null || v === undefined || v === "" || !isFinite(n)) return "—";
          return n.toFixed(digits == null ? 2 : digits);
        }
        function fmtOdds(v) { return fmtNum(v, 2); }
        function fmtPct(v, digits) {
          const n = Number(v);
          if (v === null || v === undefined || v === "" || !isFinite(n)) return "—";
          return (n * 100).toFixed(digits == null ? 1 : digits) + "%";
        }
        function fmtSignedPct(v) {
          const n = Number(v);
          if (v === null || v === undefined || v === "" || !isFinite(n)) return "—";
          return (n >= 0 ? "+" : "") + (n * 100).toFixed(1) + "%";
        }
        function fmtLocal(iso) {
          if (!iso) return "—";
          const d = new Date(iso);
          if (isNaN(d.getTime())) return "—";
          return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
        }
        function fmtRelAge(iso) {
          if (!iso) return "—";
          const ms = Date.now() - new Date(iso).getTime();
          if (isNaN(ms)) return "—";
          if (ms < 0) return "just now";
          const m = Math.round(ms / 60000);
          if (m < 1) return "just now";
          if (m < 120) return m + "m ago";
          const h = ms / 3.6e6;
          if (h < 48) return h.toFixed(1) + "h ago";
          return Math.round(h / 24) + "d ago";
        }
        function fmtCountdown(iso) {
          if (!iso) return "TBD";
          const kickoff = timestampMs(iso);
          if (!Number.isFinite(kickoff)) return "TBD";
          const ms = kickoff - Date.now();
          if (ms <= 0) return "started";
          const h = Math.floor(ms / 3.6e6), m = Math.floor((ms % 3.6e6) / 6e4);
          return (h >= 24 ? Math.floor(h / 24) + "d " + (h % 24) + "h" : h + "h " + m + "m");
        }
        const numOf = (v) => (v === null || v === undefined || v === "" ? NaN : Number(v));
        const EDGE_CHUNK = 30;              // edges rendered per group before "Show more"
        let edgeCaps = {};                  // per-group reveal cap; reset on filter/sort change
        const clvPctFromLog = (log) => (Math.exp(Number(log)) - 1) * 100;

        // ===== shared state ===================================================
        const state = {
          picks: [],
          picksErr: null,
          premiumErr: null,
          volumeErr: null,
          premiumLastGoodAt: null,
          volumeLastGoodAt: null,
          games: [],
          gamesErr: null,
          gamesLoading: true,
          gamesLastGoodAt: null,
          perf: null,
          perfErr: null,
          perfLastGoodAt: null,
          health: null,
          healthErr: null,
          healthLastGoodAt: null,
          matchRate: null,
          matchRateErr: null,
          matchRateLoading: false,
          reviewQueue: null,
          reviewQueueErr: null,
          reviewQueueLoading: false,
          reviewQueueAt: null,
          promo: null,
          promoErr: null,
          promoLoading: false,
          promoAt: null,
          ceiling: null,
          ceilingErr: null,
          ceilingLoading: false,
          ceilingAt: null,
          bankroll: null,
          bankrollErr: null,
          bankrollLoading: false,
          bankrollAt: null,
          lastOkAt: null,
          globalDegraded: true,
          coreLoaded: false,
        };
        let selectedId = null;
        const VIEW_KEYS = ["today", "edges", "radar", "lab", "sources"];
        let activeView = "today";

        function captureFocusState() {
          const el = document.activeElement;
          if (!(el instanceof HTMLElement) || el === document.body) return null;
          const key = el.dataset.focusKey || null;
          const id = el.id || null;
          if (!key && !id) return null;
          return {
            id,
            key,
            start: typeof el.selectionStart === "number" ? el.selectionStart : null,
            end: typeof el.selectionEnd === "number" ? el.selectionEnd : null,
          };
        }
        function visibleFocusTarget(snapshot) {
          if (!snapshot) return null;
          if (snapshot.id) {
            const byId = $(snapshot.id);
            if (byId && byId.offsetParent !== null) return byId;
          }
          if (!snapshot.key) return null;
          return Array.from(document.querySelectorAll("[data-focus-key]")).find((el) =>
            el.dataset.focusKey === snapshot.key && el.offsetParent !== null
          ) || null;
        }
        function restoreFocusState(snapshot) {
          const target = visibleFocusTarget(snapshot);
          if (!target) return;
          target.focus({ preventScroll: true });
          if (snapshot.start !== null && typeof target.setSelectionRange === "function") {
            const max = String(target.value || "").length;
            target.setSelectionRange(Math.min(snapshot.start, max), Math.min(snapshot.end, max));
          }
        }
        function rerenderLiveViewsPreservingFocus() {
          const focus = captureFocusState();
          renderToday();
          renderEdgesList();
          restoreFocusState(focus);
        }

        // ===== derivations (fresh, not ported from any prior implementation) =
        const tierOf = (p) => p.tier || "premium";
        const leagueLabel = (p) => (!p ? "" : p.country ? p.country + " — " + p.league : p.league);
        function verifiedWindowMs(health) {
          const floor = 45 * 60 * 1000;
          if (health && health.max_odds_age_seconds != null) return Number(health.max_odds_age_seconds) * 1000;
          if (health && health.poll_interval_seconds != null) return Math.max(floor, 3 * Number(health.poll_interval_seconds) * 1000);
          return floor;
        }
        const MAX_FUTURE_TIMESTAMP_MS = 0;
        function timestampMs(value) {
          if (typeof value !== "string" || !value.trim()) return NaN;
          const raw = value.trim();
          // Naive timestamps depend on the browser timezone and can move a
          // kickoff/revalidation across the trust boundary. Require an offset.
          if (!/(?:Z|[+\-]\d{2}:\d{2})$/i.test(raw)) return NaN;
          const parsed = new Date(raw).getTime();
          return Number.isFinite(parsed) ? parsed : NaN;
        }
        function hasStarted(p) {
          const kickoff = timestampMs(p && p.starts_at);
          return Number.isFinite(kickoff) && kickoff <= Date.now();
        }
        function hasFutureKickoff(p) {
          const kickoff = timestampMs(p && p.starts_at);
          return Number.isFinite(kickoff) && kickoff > Date.now();
        }
        function hasQualifyingEdgeNow(p, health) {
          // "Qualified now" requires an actual live re-price. Falling back to
          // the mint edge when current_edge is null made stale/unrevalidated
          // rows look actionable. The current edge must still clear its tier
          // floor, not merely remain a few basis points above zero.
          const edge = numOf(p && p.current_edge);
          const floor = edgeFloorOf(p, health);
          return Number.isFinite(edge) && Number.isFinite(floor) && floor >= 0 && edge >= floor;
        }
        function isRevalidationFresh(p, health) {
          if (p.status !== "alerted") return true;
          if (!p.revalidated_at) return false;
          const observed = timestampMs(p.revalidated_at);
          if (!Number.isFinite(observed)) return false;
          const age = Date.now() - observed;
          // A far-future provider timestamp previously produced a negative age
          // and therefore passed the freshness comparison indefinitely.
          return age >= -MAX_FUTURE_TIMESTAMP_MS && age < verifiedWindowMs(health);
        }
        function healthHasCompletedPoll(health) {
          if (!health || health.newest_poll_age_seconds === null ||
              typeof health.newest_poll_age_seconds !== "number" ||
              !Number.isFinite(health.newest_poll_age_seconds) ||
              health.newest_poll_age_seconds < 0) return false;
          // The public (redacted, unauthenticated) /health payload carries no
          // per-poll `polls` detail but reports has_completed_poll — a valid
          // completed cycle exists. Trust it so an anonymous / expired-session
          // fetch does not fail-closed to a false "odds data is stale" banner.
          if (health.has_completed_poll === true) return true;
          if (!isRecord(health.polls)) return false;
          return Object.values(health.polls).some((poll) => {
            const finishedAt = isRecord(poll) ? timestampMs(poll.finished_at) : NaN;
            return Number.isFinite(finishedAt) && finishedAt <= Date.now();
          });
        }
        function dataIsStale(health) {
          // Fail closed on transport/schema error, 503/degraded, and cold start.
          // A process with no completed poll has no evidence that its prices are
          // current even if the backend liveness endpoint itself answers 200.
          if (state.healthErr !== null || !health || health.status !== "ok") return true;
          if (!healthHasCompletedPoll(health)) return true;
          return Number(health.newest_poll_age_seconds) * 1000 > verifiedWindowMs(health);
        }
        const COVERAGE_INCOMPLETE_COPY = "Source coverage incomplete — some fixtures may be missing or unverified.";
        function staleIsCoverageOnly(health) {
          // Task F 2026-07-26 #2 — COPY ONLY, gating unchanged: any status
          // !== "ok" (including the upcoming backend "partial") still fails
          // closed via dataIsStale(). But when the payload proves a completed
          // poll INSIDE the freshness window, "stale odds" is the wrong
          // diagnosis — prices are current; source coverage is what's degraded.
          return state.healthErr === null && !!health && health.status !== "ok" &&
            healthHasCompletedPoll(health) &&
            Number(health.newest_poll_age_seconds) * 1000 <= verifiedWindowMs(health);
        }
        function healthIsTrusted(health) {
          return !dataIsStale(health) && state.healthErr === null && health && health.status === "ok";
        }
        function isActionable(p, health) {
          // Audit 2026-07-09: structural_sane === false is the server's
          // stored-impossible flag (offered below its own min-acceptable, or
          // an inverted fair/offered pair) — such a row must never render as
          // a bettable pick, whatever its tier/status/freshness say.
          // Audit 2026-07-10: qualification shares the banner's PRICING
          // trust bar — no sharp anchor or no anchor-match record means
          // the card renders 'untrusted', so it must never qualify either.
          const sharpAnchored = p.anchor_type === "pinnacle" || p.anchor_type === "sharp";
          const confidence = numOf(p.anchor_match_confidence);
          const matchPresent = Number.isFinite(confidence) && confidence >= 0 && confidence <= 1 && !!p.anchor_match_method;
          const offered = numOf(p.decimal_odds);
          return p.structural_sane === true && sharpAnchored && matchPresent &&
            tierOf(p) === "premium" && state.premiumErr === null && state.premiumLastGoodAt !== null &&
            p.status === "alerted" && hasFutureKickoff(p) && Number.isFinite(offered) && offered > 1 &&
            isRevalidationFresh(p, health) && hasQualifyingEdgeNow(p, health) && healthIsTrusted(health);
        }
        // Fix 2026-07-10 #3: ranking exclusion — structurally inconsistent
        // (structural_sane === false) and negative-edge picks stay VISIBLE in
        // the lists (quarantined styling) but never enter "top edges" rankings.
        function isRankable(p) {
          // Audit 2026-07-10: superseded rows are dedup twins of a live pick —
          // ranking them double-counts the same edge in Top tracked edges and
          // the Next-kickoffs per-event counts.
          return p.status !== "superseded" &&
            p.structural_sane === true &&
            numOf(p.current_edge == null ? p.edge : p.current_edge) > 0;
        }
        // Fix 2026-07-10 #17: sport precedence inside each Edges group —
        // football/soccer → basketball → tennis → american football; sports
        // outside the list sort last. Ordering WITHIN a sport is unchanged.
        const SPORT_ORDER = ["soccer", "basketball", "tennis", "american_football"];
        function sportRank(p) {
          const s = String((p && p.sport) || "").toLowerCase();
          for (let i = 0; i < SPORT_ORDER.length; i++) {
            if (s === SPORT_ORDER[i] || s.indexOf(SPORT_ORDER[i] + "_") === 0) return i;
          }
          return SPORT_ORDER.length;
        }
        // Edges grouping: Closed (kicked off / settled / superseded, any tier) >
        // Tracked (shadow / volume tier, never actionable) > Actionable (premium,
        // pre-kickoff — stale odds strip its actionable styling but it stays here).
        function edgeGroupOf(p) {
          if (hasStarted(p) || p.status === "settled" || p.status === "superseded") return "closed";
          if (tierOf(p) === "volume") return "tracked";
          // The Actionable group is exactly the fail-closed qualification
          // predicate. Every other premium pre-kickoff row is non-actionable
          // and remains visible under Stale / awaiting re-price.
          if (isActionable(p, state.health)) return "actionable";
          return "stale";
        }
        function edgeFloorOf(p, health) {
          if (p.edge_floor != null && p.edge_floor !== "") return Number(p.edge_floor);
          if (health) return tierOf(p) === "volume" ? numOf(health.value_volume_min_edge) : numOf(health.value_min_edge);
          return NaN;
        }
        function anchorLabel(p) {
          const at = p.anchor_type;
          if (at === "pinnacle" || at === "sharp") return "Sharp Anchor";
          if (at === "consensus") return "Consensus Anchor";
          return "MISSING ANCHOR";
        }
        function isWeakMatch(p) {
          const c = numOf(p.anchor_match_confidence);
          return isFinite(c) && c < 0.92;
        }
        const TRUSTED_CLOSE_ANCHORS = ["pinnacle", "sharp"];
        // Audit 2026-07-09 (updated 2026-07-26 AH-1): mirror the backend
        // trusted-subset gate (_settled_close_is_trusted,
        // app/storage/repositories.py) as far as the /picks row exposes it: a
        // MEASURED clv_log, a GENUINE snapshot close (has_snapshot_close ===
        // true — the fallback close writer in app/clv_trueup.py stamps
        // close_independent_of_fill WITHOUT it, so independence alone
        // overcounts), a sharp close anchor, independence exactly true, and no
        // honesty exclusion (tautological/circular/fabricated). The
        // devig-fallback flags are still NOT serialized on /picks; the backend
        // treats missing devig provenance as symmetric, so omitting them here
        // matches its conservative reading.
        function isTrustedClv(p) {
          return p.clv_log != null && p.close_independent_of_fill === true &&
            p.has_snapshot_close === true &&
            TRUSTED_CLOSE_ANCHORS.indexOf(p.closing_anchor_type) !== -1 &&
            clvExclusionOf(p) === null;
        }
        // Fresh honesty guards over the per-pick close (mirrors the intent of the
        // backend's settled-ledger guards; independently derived here for the
        // per-row evidence read, not ported from any prior render function).
        const CLOSE_IMPLIED_EDGE_CEILING = 0.2;
        const CLV_LOG_CEILING = 0.5;
        const TAUTOLOGY_EPSILON = 1e-3;
        // Audit 2026-07-09: mirror the backend's fixed _clv_row_is_fabricated
        // (app/storage/repositories.py) ordering EXACTLY — when BOTH real
        // inputs (decimal_odds + closing_fair_probability) are present the
        // close-implied edge is the ONLY fabrication test; a legitimate
        // plausible-close longshot (modest edge but |clv_log| > 0.5) is NOT
        // fabricated. The |clv_log| ceiling is a FALLBACK evaluated only when
        // an input is missing/unusable, where the edge cannot be computed.
        // AH-2 (audit 2026-07-26): the backend judges the COMMISSION-NETTED
        // effective fill (app/edge/value.py effective_odds) — raw
        // 1/decimal_odds understates the implied probability on exchange
        // fills. Mirror of app/edge/value.py EXCHANGE_COMMISSION (parity
        // pinned by tests/test_dashboard_contract.py); a null/unknown book
        // degrades to the raw price, same as the backend.
        const EXCHANGE_COMMISSION = {
          "betfair exchange": 0.05,
          "betfair": 0.05,
          "smarkets": 0.02,
          "matchbook": 0.02,
        };
        function effectiveOddsOf(book, odds) {
          const key = String(book == null ? "" : book).trim().toLowerCase();
          const c = Object.prototype.hasOwnProperty.call(EXCHANGE_COMMISSION, key)
            ? EXCHANGE_COMMISSION[key] : 0;
          return 1 + (odds - 1) * (1 - c);
        }
        function clvIsFabricated(p) {
          if (p.clv_log == null) return false;
          const d = numOf(p.decimal_odds), cf = numOf(p.closing_fair_probability);
          if (isFinite(d) && d > 0 && isFinite(cf)) {
            // Both real inputs present: judge by the close-implied edge ONLY,
            // against the commission-netted effective fill.
            return cf - 1 / effectiveOddsOf(p.bookmaker, d) > CLOSE_IMPLIED_EDGE_CEILING;
          }
          // Fallback (odds or fair prob absent/unusable): |clv_log| tripwire.
          const logv = Number(p.clv_log);
          return isFinite(logv) && Math.abs(logv) > CLV_LOG_CEILING;
        }
        function clvIsTautological(p) {
          const cf = numOf(p.closing_fair_probability), mf = numOf(p.model_probability);
          if (p.clv_log == null || !isFinite(cf) || !isFinite(mf)) return false;
          return Math.abs(cf - mf) < TAUTOLOGY_EPSILON;
        }
        function clvExclusionOf(p) {
          if (clvIsTautological(p)) return "tautological";
          if (p.close_independent_of_fill === false) return "circular";
          if (clvIsFabricated(p)) return "fabricated";
          return null;
        }
        function clvStateLabel(p) {
          const excl = clvExclusionOf(p);
          if (excl === "tautological") return "Tautological Close Excluded";
          if (excl === "circular") return "Circular Close Excluded";
          if (excl === "fabricated") return "Close excluded — implausible price";
          if (p.clv_log == null) return "—";
          return isTrustedClv(p) ? "Trusted CLV" : "Untrusted Close — indicative";
        }
        function outcomeLabel(oc) {
          const m = { won: "Won", lost: "Lost", push: "Push", void: "Void", half_won: "Half Won", half_lost: "Half Lost" };
          return m[oc] || null;
        }
        // ===== load layer ======================================================
        function validatePicksPayload(value, expectedTier) {
          const rows = expectArrayPayload(value, expectedTier + " picks");
          if (rows.some((row) => tierOf(row) !== expectedTier)) {
            throw responseError("Picks payload contains the wrong tier.", "SchemaError");
          }
          return rows;
        }
        let loadSeq = 0, loadInFlight = false;
        async function load() { if (loadInFlight) return; loadInFlight = true; try { await loadOnce(); } finally { loadInFlight = false; } }

        function lastGoodSuffix(at) {
          return at instanceof Date
            ? " (last good " + at.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) + ")"
            : "";
        }
        function coreRefreshHasErrors() {
          return state.premiumErr !== null || state.volumeErr !== null ||
            state.gamesErr !== null || state.perfErr !== null;
        }
        function gamesPendingWithoutCache() {
          return state.gamesLoading && state.gamesLastGoodAt === null;
        }
        function gamesUnavailableWithoutCache() {
          return gamesPendingWithoutCache() ||
            (state.gamesErr !== null && state.gamesLastGoodAt === null);
        }
        function systemCondition(health) {
          // Keep transport/schema uncertainty distinct from a backend-declared
          // degraded source. Partial core refreshes are also their own state so
          // a games/performance failure never falsely blames the odds source.
          if (state.healthErr !== null || !health) {
            return { label: "Health unknown", dot: "bad", pillMode: "degraded" };
          }
          if (health.status === "degraded") {
            return { label: "Source Degraded", dot: "bad", pillMode: "degraded" };
          }
          if (coreRefreshHasErrors()) {
            return { label: "Data refresh degraded", dot: "bad", pillMode: "degraded" };
          }
          if (!healthHasCompletedPoll(health)) {
            return { label: "Health unverified", dot: "bad", pillMode: "degraded" };
          }
          if (dataIsStale(health)) {
            return { label: "Stale", dot: "warn", pillMode: "stale" };
          }
          return { label: "Verified", dot: "ok", pillMode: null };
        }
        function renderGlobalDegradedBanner() {
          const banner = $("offline-banner");
          const messages = [];
          const premiumCached = state.picks.some((p) => tierOf(p) === "premium");
          const volumeCached = state.picks.some((p) => tierOf(p) === "volume");
          if (state.premiumErr) messages.push(premiumCached
            ? "Could not refresh premium picks — showing the last loaded rows" + lastGoodSuffix(state.premiumLastGoodAt) + "."
            : "Could not load premium picks.");
          if (state.volumeErr) messages.push(volumeCached
            ? "Could not refresh volume picks — showing the last loaded rows" + lastGoodSuffix(state.volumeLastGoodAt) + "."
            : "Could not load volume picks.");
          if (state.gamesErr) messages.push(state.gamesLastGoodAt
            ? "Could not refresh games — showing cached fixtures" + lastGoodSuffix(state.gamesLastGoodAt) + "."
            : "Could not load games.");
          if (state.perfErr) messages.push(state.perfLastGoodAt
            ? "Could not refresh performance — showing cached evidence" + lastGoodSuffix(state.perfLastGoodAt) + "."
            : "Could not load performance data.");
          if (state.healthErr) messages.push(state.healthLastGoodAt
            ? "Could not verify health — cached health is not trusted" + lastGoodSuffix(state.healthLastGoodAt) + "."
            : "Could not verify system health.");
          if (!state.healthErr && state.health && state.health.status === "degraded") {
            messages.push("System health reports Source Degraded.");
          } else if (!state.healthErr && state.health && !healthHasCompletedPoll(state.health)) {
            messages.push("System health is unverified — no completed poll cycle yet.");
          } else if (!state.healthErr && state.health && dataIsStale(state.health)) {
            messages.push(staleIsCoverageOnly(state.health)
              ? COVERAGE_INCOMPLETE_COPY
              : "Odds data is stale — cached prices are not actionable.");
          }
          if (state.premiumErr && state.volumeErr && !premiumCached && !volumeCached) {
            messages.unshift("Could not load picks.");
          }
          state.globalDegraded = messages.length > 0;
          banner.classList.toggle("show", state.globalDegraded);
          banner.textContent = messages.join(" ");
        }

        async function loadOnce() {
          const seq = ++loadSeq;
          state.gamesLoading = true;
          // Start every request together and consume each body as soon as its
          // headers arrive. Fixtures are operational context, not a dependency
          // of picks/health/performance: a slow /games query must not hold the
          // entire first dashboard paint hostage.
          const premiumBodyP = fetchGuarded("/picks?limit=200&tier=premium")
            .then((res) => readJson(res, (body) => validatePicksPayload(body, "premium")));
          const volumeBodyP = fetchGuarded("/picks?limit=200&tier=volume")
            .then((res) => readJson(res, (body) => validatePicksPayload(body, "volume")));
          const gamesBodyP = fetchGuarded("/games?limit=1000")
            .then((res) => readJson(res, (body) => expectArrayPayload(body, "Games")));
          // Attach both handlers immediately so a fast fixture failure cannot
          // become an unhandled rejection while the critical responses settle.
          const gamesResultP = gamesBodyP.then(
            (value) => ({ status: "fulfilled", value }),
            (reason) => ({ status: "rejected", reason })
          );
          const perfBodyP = fetchGuarded("/performance")
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Performance")));
          const healthBodyP = fetchGuarded("/health").then((res) => readHealthJson(res));
          const [premiumBodyR, volumeBodyR, perfBodyR, healthBodyR] = await Promise.allSettled([
            premiumBodyP,
            volumeBodyP,
            perfBodyP,
            healthBodyP,
          ]);
          const valueOrNull = (result) => result.status === "fulfilled" ? result.value : null;
          const errorOrNull = (result) => result.status === "rejected" ? result.reason : null;
          const premiumRows = valueOrNull(premiumBodyR), premiumErr = errorOrNull(premiumBodyR);
          const volumeRows = valueOrNull(volumeBodyR), volumeErr = errorOrNull(volumeBodyR);
          const perf = valueOrNull(perfBodyR), perfErr = errorOrNull(perfBodyR);
          const health = valueOrNull(healthBodyR), healthErr = errorOrNull(healthBodyR);
          if (seq !== loadSeq) return;

          const focusBeforeRender = captureFocusState();
          const drawerWasOpen = $("edge-detail").classList.contains("open");
          const loadedAt = new Date();
          const oldPremium = state.picks.filter((p) => tierOf(p) === "premium");
          const oldVolume = state.picks.filter((p) => tierOf(p) === "volume");
          if (premiumErr === null) state.premiumLastGoodAt = loadedAt;
          if (volumeErr === null) state.volumeLastGoodAt = loadedAt;
          state.premiumErr = premiumErr;
          state.volumeErr = volumeErr;
          state.picks = (premiumRows || oldPremium).concat(volumeRows || oldVolume);
          state.picksErr = premiumErr !== null && volumeErr !== null ? premiumErr : null;

          state.perfErr = perfErr;
          if (perfErr === null) { state.perf = perf; state.perfLastGoodAt = loadedAt; }
          state.healthErr = healthErr;
          if (healthErr === null) {
            state.health = health;
            if (healthHasCompletedPoll(health) && health.status === "ok") state.healthLastGoodAt = loadedAt;
          }
          state.coreLoaded = true;
          renderGlobalDegradedBanner();

          renderPill();
          renderStaleBanner();
          renderToday();
          renderViewHeaders();
          renderEdgesList();
          renderEdgeDetail();
          if (activeView === "radar") renderRadar();
          if (activeView === "sources") renderSources();
          renderLab();
          syncDrawerFromRoute(false);
          if (drawerWasOpen || !$("edge-detail").classList.contains("open")) {
            restoreFocusState(focusBeforeRender);
          }
          if ($("edge-detail").classList.contains("open") &&
              !$("edge-detail").contains(document.activeElement)) {
            $("edge-back").focus({ preventScroll: true });
          }

          // Merge fixtures when they finish without re-rendering unrelated
          // edge/detail state (which could overwrite an operator's active
          // drawer form). Existing last-good fixtures remain intact on error.
          const gamesBodyR = await gamesResultP;
          if (seq !== loadSeq) return;
          const gamesLoadedAt = new Date();
          const games = valueOrNull(gamesBodyR), gamesErr = errorOrNull(gamesBodyR);
          state.gamesLoading = false;
          state.gamesErr = gamesErr;
          if (gamesErr === null) {
            state.games = games;
            state.gamesLastGoodAt = gamesLoadedAt;
          }
          const allCoreSucceeded = premiumErr === null && volumeErr === null && gamesErr === null &&
            perfErr === null && healthErr === null && healthIsTrusted(state.health);
          if (allCoreSucceeded) state.lastOkAt = gamesLoadedAt;

          const gamesFocusBeforeRender = captureFocusState();
          renderGlobalDegradedBanner();
          renderPill();
          renderToday();
          renderViewHeaders();
          if (activeView === "radar") renderRadar();
          if (activeView === "sources") renderSources();
          restoreFocusState(gamesFocusBeforeRender);
        }

        // ===== lazy /resolution/match-rate loader (Radar + Sources only) =====
        const MATCH_RATE_TTL_MS = 5 * 60 * 1000;
        function loadMatchRate() {
          // Bug fix (review 2026-07-04 #4): success used to cache forever —
          // Radar/Sources numbers went permanently stale. Refetch after a TTL.
          const fresh = state.matchRateAt != null && Date.now() - state.matchRateAt < MATCH_RATE_TTL_MS;
          if ((state.matchRate && fresh) || state.matchRateLoading) return;
          state.matchRateLoading = true;
          fetchGuarded("/resolution/match-rate?days=180", undefined, MATCH_RATE_TIMEOUT_MS)
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Match rate")))
            .then((body) => { state.matchRate = body; state.matchRateErr = null; state.matchRateAt = Date.now(); })
            .catch((e) => { state.matchRateErr = e; })
            .finally(() => {
              state.matchRateLoading = false;
              if (activeView === "radar") renderRadar();
              if (activeView === "sources") renderSources();
            });
        }

        function staleNoticeEl(message) {
          const notice = document.createElement("p");
          notice.className = "muted stale-cache";
          notice.setAttribute("role", "status");
          notice.textContent = message;
          return notice;
        }
        function setCacheNotice(id, message) {
          const notice = $(id);
          if (!notice) return;
          notice.hidden = !message;
          notice.textContent = message || "";
        }

        // ===== lazy /resolution/review-queue browse (Sources disclosure) =====
        // Collapsed by default; fetched ONLY on first expand (timeout-guarded
        // like match-rate). STRICTLY read-only — no review action exists here.
        function renderReviewQueueBrowse() {
          const status = $("reviewq-browse-status");
          const tb = $("reviewq-browse-rows"); tb.replaceChildren();
          if (state.reviewQueueLoading && !state.reviewQueue) { status.textContent = "Loading review queue…"; return; }
          if (state.reviewQueueErr && !state.reviewQueue) { status.textContent = "Could not load review queue."; return; }
          if (!state.reviewQueue) { status.textContent = "Not loaded yet."; return; }
          const rows = Array.isArray(state.reviewQueue.rows) ? state.reviewQueue.rows : [];
          const cachePrefix = state.reviewQueueErr
            ? "Could not refresh review queue — showing last loaded data. "
            : state.reviewQueueLoading ? "Refreshing review queue — showing last loaded data. " : "";
          if (rows.length === 0) { status.textContent = cachePrefix + "Review queue is empty."; return; }
          status.textContent = cachePrefix + "Newest " + rows.length + " rows (read-only; triage via the operator CLI).";
          rows.forEach((r) => {
            const tr = document.createElement("tr");
            const conf = r.confidence != null ? fmtNum(r.confidence, 4) : null;
            const st = r.review_status === "pending" && r.reviewed_at == null ? r.review_status
              : fmt(r.review_status) + (r.reviewed_at != null ? " @ " + r.reviewed_at : "");
            [eventLabel(r.event), eventLabel(r.candidate), r.kickoff_utc, r.source, conf, r.reason, st, r.created_at].forEach((v) => {
              const td = document.createElement("td");
              td.textContent = String(fmt(v));
              tr.appendChild(td);
            });
            tb.appendChild(tr);
          });
        }
        function loadReviewQueue() {
          const fresh = state.reviewQueueAt != null && Date.now() - state.reviewQueueAt < MATCH_RATE_TTL_MS;
          if ((state.reviewQueue && fresh) || state.reviewQueueLoading) return;
          state.reviewQueueLoading = true;
          state.reviewQueueErr = null;
          renderReviewQueueBrowse();
          fetchGuarded("/resolution/review-queue?limit=50", undefined, MATCH_RATE_TIMEOUT_MS)
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Review queue")))
            .then((body) => { state.reviewQueue = body; state.reviewQueueErr = null; state.reviewQueueAt = Date.now(); })
            .catch((e) => { state.reviewQueueErr = e; })
            .finally(() => { state.reviewQueueLoading = false; renderReviewQueueBrowse(); });
        }
        $("reviewq-browse").addEventListener("toggle", function () {
          if (this.open) loadReviewQueue();
        });

        // ===== lazy /lab/promotion-distance loader (B1 — Lab view only) ======
        // Distance to the trusted-CLV EVIDENCE threshold, per (sport, market).
        // Never fetched in the boot-time loadOnce() cycle: setView("lab")
        // triggers it, with the same single-fetch guard + TTL as match-rate.
        // A point estimate renders ONLY at/above the min-n bar — the payload
        // nulls sub-floor estimates at the source and this render re-guards it.
        const PROMO_TTL_MS = 5 * 60 * 1000;
        function renderPromotionDistance() {
          const box = $("promo-distance"); box.replaceChildren();
          const p0 = document.createElement("p"); p0.className = "muted";
          if (state.promoLoading && !state.promo) { p0.textContent = "Loading promotion distance…"; box.appendChild(p0); return; }
          if (state.promoErr && !state.promo) { p0.textContent = "Could not load promotion distance."; box.appendChild(p0); return; }
          if (!state.promo) { p0.textContent = "Not loaded yet."; box.appendChild(p0); return; }
          if (state.promoErr) box.appendChild(staleNoticeEl("Could not refresh promotion distance — showing last loaded data."));
          const okN = Number(state.promo.ok_n) || 30;
          const cells = state.promo.cells || [];
          if (cells.length === 0) { p0.textContent = "No settled sport/market cells yet."; box.appendChild(p0); return; }
          cells.slice(0, 12).forEach((c) => {
            const r = document.createElement("div"); r.className = "kickoff-row";
            const nm = document.createElement("span"); nm.className = "kr-t mono";
            nm.textContent = String(fmt(c.sport)).replace(/_/g, " ") + " · " + marketLabel(c.market);
            const prog = document.createElement("span"); prog.className = "kr-s mono";
            prog.textContent = fmt(c.n_trusted) + " / " + okN + " trusted closes";
            const s = document.createElement("span"); s.className = "kr-s";
            if (c.status === "ok" && Number(c.n_trusted) >= okN && c.mean_clv_log != null) {
              s.textContent = "threshold met · trusted CLV " + fmtSignedPct(clvPctFromLog(c.mean_clv_log) / 100);
            } else if (c.status === "ok") {
              s.textContent = "threshold met";
            } else {
              s.textContent = "accruing · days to threshold " + (c.est_days_to_threshold != null
                ? "~" + Math.ceil(Number(c.est_days_to_threshold)) + "d"
                : "—");
            }
            r.append(nm, prog, s); box.appendChild(r);
          });
          // Task 1 (2026-07-12): ADR-0022 crit-2 promotion-readiness rows, one
          // per accruing sport/market cell. source_agreement + freshness are
          // NOT YET INSTRUMENTED — reported null, never fabricated; a cell can
          // never read READY until they are wired AND every condition holds.
          const pr = state.promo.promotion_readiness;
          if (pr && Array.isArray(pr.cells) && pr.cells.length > 0) {
            const hdr = document.createElement("p"); hdr.className = "muted"; hdr.style.marginTop = "10px";
            hdr.textContent = "Promotion readiness (ADR-0022) — source agreement + freshness "
              + "are not yet instrumented (null, never fabricated):";
            box.appendChild(hdr);
            const neededN = Number(pr.needed_n) || 50;
            pr.cells.slice(0, 12).forEach((c) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const nm = document.createElement("span"); nm.className = "kr-t mono";
              nm.textContent = "Promotion readiness — " + String(fmt(c.sport)).replace(/_/g, " ")
                + " · " + marketLabel(c.market);
              const prog = document.createElement("span"); prog.className = "kr-s mono";
              prog.textContent = "n " + (Number(c.n_trusted) || 0) + "/" + neededN
                + " · " + (c.ci_low_gt_zero === true ? "CI>0 met"
                  : c.ci_low_gt_zero === false ? "CI>0 no" : "CI>0 pending")
                + " · cov " + fmtNum(c.coverage_pct, 0) + "%";
              const s = document.createElement("span"); s.className = "kr-s";
              s.textContent = c.ready === true ? "READY" : "NOT READY";
              r.append(nm, prog, s); box.appendChild(r);
            });
          }
        }
        function loadPromotionDistance() {
          const fresh = state.promoAt != null && Date.now() - state.promoAt < PROMO_TTL_MS;
          if ((state.promo && fresh) || state.promoLoading) return;
          state.promoLoading = true;
          if (activeView === "lab") renderPromotionDistance();
          fetchGuarded("/lab/promotion-distance")
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Promotion distance")))
            .then((body) => { state.promo = body; state.promoErr = null; state.promoAt = Date.now(); })
            .catch((e) => { state.promoErr = e; })
            .finally(() => { state.promoLoading = false; if (activeView === "lab") renderPromotionDistance(); });
        }

        // ===== lazy /bankroll loader (B7 — Lab view only) =====================
        // HYPOTHETICAL running-balance ledger: line chart + running-peak line
        // (the drawdown read) + text stats. Informational only — no money
        // moves, and nothing here feeds staking. Same single-fetch guard +
        // TTL as promotion-distance; never fetched in the boot-time
        // loadOnce() cycle: setView("lab") triggers it.
        const BANKROLL_TTL_MS = 5 * 60 * 1000;
        const SVG_NS = "http://www.w3.org/2000/svg";
        function bankrollChartEl(series) {
          const vals = series.map((e) => numOf(e.balance_after)).filter((v) => isFinite(v));
          if (vals.length < 2) return null;
          const peaks = [];
          let pk = vals[0];
          vals.forEach((v) => { pk = Math.max(pk, v); peaks.push(pk); });
          const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, peaks);
          const W = 320, H = 96, PAD = 6, span = hi - lo || 1;
          const x = (i) => PAD + (i * (W - 2 * PAD)) / (vals.length - 1);
          const y = (v) => H - PAD - ((v - lo) * (H - 2 * PAD)) / span;
          const pts = (arr) => arr.map((v, i) => x(i).toFixed(1) + "," + y(v).toFixed(1)).join(" ");
          const line = (arr, stroke, dashed, width) => {
            const el = document.createElementNS(SVG_NS, "polyline");
            el.setAttribute("points", pts(arr));
            el.setAttribute("fill", "none");
            el.setAttribute("stroke", stroke);
            el.setAttribute("stroke-width", width);
            if (dashed) el.setAttribute("stroke-dasharray", "3 3");
            return el;
          };
          const svg = document.createElementNS(SVG_NS, "svg");
          svg.setAttribute("viewBox", "0 0 " + W + " " + H);
          svg.setAttribute("width", "100%");
          svg.setAttribute("height", "96");
          svg.setAttribute("role", "img");
          svg.setAttribute("aria-label", "Hypothetical running balance (solid) with running peak (dashed)");
          svg.append(line(peaks, "var(--warm)", true, "1"), line(vals, "var(--cyan)", false, "1.5"));
          return svg;
        }
        function renderBankroll() {
          const box = $("bankroll-body"); box.replaceChildren();
          const p0 = document.createElement("p"); p0.className = "muted";
          if (state.bankrollLoading && !state.bankroll) { p0.textContent = "Loading bankroll…"; box.appendChild(p0); return; }
          if (state.bankrollErr && !state.bankroll) { p0.textContent = "Could not load bankroll."; box.appendChild(p0); return; }
          if (!state.bankroll) { p0.textContent = "Not loaded yet."; box.appendChild(p0); return; }
          if (state.bankrollErr) box.appendChild(staleNoticeEl("Could not refresh bankroll — showing last loaded data."));
          const b = state.bankroll;
          if (b.active !== true) { p0.textContent = "Bankroll ledger is not configured."; box.appendChild(p0); return; }
          const series = b.series || [];
          if (series.length === 0) { p0.textContent = "No ledger entries yet."; box.appendChild(p0); return; }
          const chart = bankrollChartEl(series);
          if (chart) {
            box.appendChild(chart);
            const legend = document.createElement("p"); legend.className = "muted";
            legend.textContent = "Solid — hypothetical balance · dashed — running peak (drawdown reference).";
            box.appendChild(legend);
          }
          const ml = document.createElement("div"); ml.className = "metric-list";
          ml.appendChild(metricEl("Current balance", fmtNum(b.current_balance)));
          const start = numOf(b.starting_balance), cur = numOf(b.current_balance);
          const pnl = isFinite(start) && isFinite(cur) ? cur - start : NaN;
          ml.appendChild(metricEl("Total settled P&L", isFinite(pnl) ? (pnl >= 0 ? "+" : "") + pnl.toFixed(2) : "—"));
          ml.appendChild(metricEl("Max drawdown", b.max_drawdown != null ? fmtPct(b.max_drawdown) : "—"));
          ml.appendChild(metricEl("Ledger entries", String(b.n_entries || series.length)));
          box.appendChild(ml);
        }
        function loadBankroll() {
          const fresh = state.bankrollAt != null && Date.now() - state.bankrollAt < BANKROLL_TTL_MS;
          if ((state.bankroll && fresh) || state.bankrollLoading) return;
          state.bankrollLoading = true;
          if (activeView === "lab") renderBankroll();
          fetchGuarded("/bankroll")
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Bankroll")))
            .then((body) => { state.bankroll = body; state.bankrollErr = null; state.bankrollAt = Date.now(); })
            .catch((e) => { state.bankrollErr = e; })
            .finally(() => { state.bankrollLoading = false; if (activeView === "lab") renderBankroll(); });
        }

        // ===== lazy /resolution/match-ceiling browse (B3 — Sources disclosure)
        // Collapsed by default; fetched ONLY on first expand (timeout-guarded
        // like match-rate). Live decomposition — never a static artifact.
        function renderMatchCeiling() {
          const status = $("ceiling-status");
          const tb = $("ceiling-rows"); tb.replaceChildren();
          if (state.ceilingLoading && !state.ceiling) { status.textContent = "Loading match ceiling…"; return; }
          if (state.ceilingErr && !state.ceiling) { status.textContent = "Could not load match ceiling."; return; }
          if (!state.ceiling) { status.textContent = "Not loaded yet."; return; }
          const sports = isRecord(state.ceiling.sports) ? state.ceiling.sports : {};
          const keys = Object.keys(sports).sort();
          const cachePrefix = state.ceilingErr
            ? "Could not refresh match ceiling — showing last loaded data. "
            : state.ceilingLoading ? "Refreshing match ceiling — showing last loaded data. " : "";
          if (keys.length === 0) { status.textContent = cachePrefix + "No events in the window."; return; }
          status.textContent = cachePrefix + "Live decomposition — window " + fmt(state.ceiling.window_days) + " days.";
          keys.forEach((k) => {
            const b = sports[k];
            const tr = document.createElement("tr");
            const corrected = b.corrected_match_rate_lower != null
              ? fmtPct(b.corrected_match_rate_lower) + " – " + (b.corrected_match_rate_upper != null ? fmtPct(b.corrected_match_rate_upper) : "—")
              : "—";
            [k.replace(/_/g, " "), b.events, b.matched, b.structural, b.addressable, b.unknown_league, corrected].forEach((v, i) => {
              const td = document.createElement("td");
              if (i >= 1 && i <= 5) td.className = "r";
              td.textContent = String(fmt(v));
              tr.appendChild(td);
            });
            tb.appendChild(tr);
          });
        }
        function loadMatchCeiling() {
          const fresh = state.ceilingAt != null && Date.now() - state.ceilingAt < MATCH_RATE_TTL_MS;
          if ((state.ceiling && fresh) || state.ceilingLoading) return;
          state.ceilingLoading = true;
          state.ceilingErr = null;
          renderMatchCeiling();
          fetchGuarded("/resolution/match-ceiling?days=30", undefined, MATCH_RATE_TIMEOUT_MS)
            .then((res) => readJson(res, (body) => expectObjectPayload(body, "Match ceiling")))
            .then((body) => { state.ceiling = body; state.ceilingErr = null; state.ceilingAt = Date.now(); })
            .catch((e) => { state.ceilingErr = e; })
            .finally(() => { state.ceilingLoading = false; renderMatchCeiling(); });
        }
        $("ceiling-browse").addEventListener("toggle", function () {
          if (this.open) loadMatchCeiling();
        });

        // ===== header: system pill + popover ==================================
        function setPillText(text, mode) {
          $("pill-text").textContent = text;
          const pill = $("system-pill");
          pill.classList.remove("degraded", "stale");
          if (mode) pill.classList.add(mode);
        }
        function renderPill() {
          const health = state.health;
          const condition = systemCondition(health);
          const age = health && health.newest_poll_age_seconds != null ? fmtRelAge(new Date(Date.now() - Number(health.newest_poll_age_seconds) * 1000).toISOString()) : "—";
          setPillText(condition.label + " · data age " + age, condition.pillMode);

          const body = $("popover-body");
          body.replaceChildren();
          const row = (k, v) => {
            const d = document.createElement("div"); d.className = "metric-row";
            const a = document.createElement("span"); a.textContent = k;
            const b = document.createElement("b"); b.textContent = v;
            d.append(a, b); body.appendChild(d);
          };
          row("Poll freshness", age);
          row("Verified window", fmtNum(verifiedWindowMs(health) / 60000, 0) + "m");
          const pool = health && health.proxy_pool;
          row("Proxy verdict", pool ? fmt(pool.verdict) : "—");
        }
        function setSystemPopover(open, restoreFocus) {
          const pop = $("system-popover"), pill = $("system-pill");
          pop.hidden = !open;
          pill.setAttribute("aria-expanded", String(open));
          if (open) requestAnimationFrame(() => pop.focus({ preventScroll: true }));
          else if (restoreFocus) requestAnimationFrame(() => pill.focus({ preventScroll: true }));
        }
        $("system-pill").addEventListener("click", () => {
          setSystemPopover($("system-popover").hidden, false);
        });
        document.addEventListener("click", (ev) => {
          const pop = $("system-popover"), pill = $("system-pill");
          if (!pop.hidden && !pop.contains(ev.target) && !pill.contains(ev.target)) {
            setSystemPopover(false, false);
          }
        });
        document.addEventListener("keydown", (ev) => {
          if (ev.key === "Escape" && !$("system-popover").hidden) {
            ev.preventDefault();
            setSystemPopover(false, true);
          }
        });
        $("logout-btn").addEventListener("click", () => {
          fetchGuarded("/logout", { method: "POST" })
            .then((res) => releaseResponseGuard(res))
            .catch(() => {})
            .finally(() => window.location.assign("/login"));
        });

        function renderStaleBanner() {
          const banner = $("stale-banner");
          banner.classList.toggle("show", dataIsStale(state.health));
          // Task F 2026-07-26 #2: copy tracks the diagnosis — visibility gating above is unchanged.
          banner.textContent = staleIsCoverageOnly(state.health)
            ? COVERAGE_INCOMPLETE_COPY
            : "Odds data is stale. Picks should not be treated as current.";
        }

        // Fix 2026-07-10 #9/#13 — ONE shared row-open mechanism: navigating to
        // #/edges/<id> drives the existing hash router (setView("edges") +
        // select + the SAME detail drawer the Edges tab opens). A row without
        // a resolvable pick record stays NON-interactive (no cursor, no hover,
        // no tabindex — never an empty panel).
        function makeRowOpenPick(row, pick) {
          if (!pick || pick.id == null) return;
          row.classList.add("row-link");
          row.tabIndex = 0;
          row.setAttribute("role", "button");
          row.dataset.focusKey = "pick-" + String(pick.id);
          const open = (ev) => {
            const targetHash = "#/edges/" + String(pick.id);
            selectEdge(pick.id, targetHash, ev.currentTarget);
          };
          row.addEventListener("click", open);
          row.addEventListener("keydown", (ev) => {
            if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
              ev.preventDefault(); open();
            }
          });
        }

        // ===== TODAY ===========================================================
        function renderToday() {
          const health = state.health;
          const premium = state.picks.filter((p) => tierOf(p) === "premium");
          // Audit 2026-07-10: count the FULL qualified set before slicing the
          // display list to 5 — the KPI tile could never show more than 5.
          const qualified = premium.filter((p) => isActionable(p, health));
          const qualificationAvailable = state.premiumErr === null && healthIsTrusted(health);
          const actionable = qualified
            .sort((a, b) => (numOf(b.current_edge == null ? b.edge : b.current_edge) - numOf(a.current_edge == null ? a.edge : a.current_edge)))
            .slice(0, 5);
          $("actionable-count").textContent = qualificationAvailable ? String(qualified.length) : "—";
          const box = $("actionable-now"); box.replaceChildren();
          if (state.premiumErr !== null) {
            const p0 = document.createElement("p"); p0.className = "muted stale-cache";
            p0.textContent = premium.length > 0
              ? "Could not refresh premium picks — cached rows are non-actionable."
              : "Could not load picks.";
            box.appendChild(p0);
          } else if (!healthIsTrusted(health)) {
            const p0 = document.createElement("p"); p0.className = "muted stale-cache";
            p0.textContent = "Qualification unavailable while data health is unverified.";
            box.appendChild(p0);
          } else if (actionable.length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "No pick currently qualifies.";
            box.appendChild(p0);
          } else {
            actionable.forEach((p) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const t = document.createElement("span"); t.className = "kr-t mono"; t.textContent = fmtCountdown(p.starts_at);
              const e = document.createElement("span"); e.className = "kr-e"; e.textContent = eventLabel(p.event);
              const s = document.createElement("span"); s.className = "kr-s"; s.textContent = selLabel(p) + " @ " + fmtOdds(p.decimal_odds) + " · " + fmtSignedPct(p.current_edge == null ? p.edge : p.current_edge);
              r.append(t, e, s); box.appendChild(r);
            });
            const go = document.createElement("button"); go.type = "button"; go.textContent = "Open in Edges →";
            go.dataset.focusKey = "today-open-edges";
            go.addEventListener("click", () => setView("edges"));
            box.appendChild(go);
          }

          // needs-attention derived queue
          const attn = [];
          if (health && health.status === "degraded") attn.push("Source Degraded — ingestion health degraded.");
          if (dataIsStale(health)) attn.push(staleIsCoverageOnly(health)
            ? COVERAGE_INCOMPLETE_COPY
            : "Source Degraded — odds data is stale.");
          if (health && health.proxy_pool && health.proxy_pool.verdict === "Proxy pool degraded") attn.push("Source Degraded — proxy pool degraded.");
          // Fix 2026-07-10 #10: the two "Low Evidence" lines merely restated
          // the Qualified Now empty state / evidence-position panel — this
          // queue is reserved for real actionable alerts.
          const attnBox = $("needs-attention"); attnBox.replaceChildren();
          if (attn.length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Nothing needs attention right now.";
            attnBox.appendChild(p0);
          } else {
            attn.forEach((line) => {
              const r = document.createElement("div"); r.className = "attn-row";
              const t = document.createElement("span"); t.className = "attn-text"; t.textContent = line;
              r.appendChild(t); attnBox.appendChild(r);
            });
          }

          // top tracked edges (any tier, positive edge, pre-kickoff, best first) —
          // fills the Today view with the strongest live edges even when nothing
          // clears the premium gate (most are shadow / tracked-informational).
          const topEdges = state.picks
            .filter((p) => hasFutureKickoff(p) && isRankable(p))
            .sort((a, b) => numOf(b.current_edge == null ? b.edge : b.current_edge) - numOf(a.current_edge == null ? a.edge : a.current_edge))
            .slice(0, 8);
          const teBox = $("top-edges"); teBox.replaceChildren();
          if ((state.premiumErr || state.volumeErr) && state.picks.length > 0) {
            const notice = document.createElement("p"); notice.className = "muted stale-cache";
            notice.textContent = "Some pick data could not refresh — showing last loaded rows.";
            teBox.appendChild(notice);
          }
          if (topEdges.length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted";
            p0.textContent = state.picks.length === 0 && state.picksErr !== null ? "Could not load picks." : "No tracked edges yet.";
            teBox.appendChild(p0);
          } else {
            topEdges.forEach((p) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const t = document.createElement("span"); t.className = "kr-t mono";
              t.textContent = fmtSignedPct(p.current_edge == null ? p.edge : p.current_edge);
              const e = document.createElement("span"); e.className = "kr-e"; e.textContent = eventLabel(p.event);
              const s = document.createElement("span"); s.className = "kr-s";
              s.textContent = selLabel(p) + " @ " + fmtOdds(p.decimal_odds) + " · " + (tierOf(p) === "premium" ? "Premium" : "Shadow");
              r.append(t, e, s); makeRowOpenPick(r, p); teBox.appendChild(r);
            });
          }

          // Fix 2026-07-10 #25: the edge-magnitude bar chart was removed at
          // the operator's request — the ranked numeric list above carries
          // the same information.

          // next kickoffs (any status, pre-kickoff, both tiers, soonest first).
          // Audit 2026-07-10: one row per EVENT, not per pick — an event with 5
          // open picks rendered as 5 identical lines. Premium wins the tier tag
          // when the event carries both tiers; a >1 pick count is shown.
          const upcomingPicks = state.picks.filter((p) => hasFutureKickoff(p));
          const byEvent = new Map();
          upcomingPicks.forEach((p) => {
            const k = `${p.event}|${p.starts_at}`;
            const cur = byEvent.get(k);
            if (!cur) byEvent.set(k, { pick: p, n: 1, premium: tierOf(p) !== "volume" });
            else { cur.n += 1; cur.premium = cur.premium || tierOf(p) !== "volume"; }
          });
          const upcoming = [...byEvent.values()]
            .sort((a, b) => new Date(a.pick.starts_at) - new Date(b.pick.starts_at)).slice(0, 6);
          const nk = $("next-kickoffs"); nk.replaceChildren();
          if (upcoming.length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "No upcoming kickoffs.";
            nk.appendChild(p0);
          } else {
            upcoming.forEach(({ pick: p, n, premium }) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const t = document.createElement("span"); t.className = "kr-t mono"; t.textContent = fmtCountdown(p.starts_at);
              const e = document.createElement("span"); e.className = "kr-e";
              e.textContent = eventLabel(p.event) + (n > 1 ? ` · ${n} picks` : "");
              const s = document.createElement("span"); s.className = "kr-s"; s.textContent = premium ? "Premium" : "Shadow";
              // per-EVENT aggregate: open the first pick of the event (the
              // detail drawer is per-pick; the event's other picks sit beside
              // it in the Edges list).
              r.append(t, e, s); makeRowOpenPick(r, p); nk.appendChild(r);
            });
          }

          // recent settled results (auto-graded or manual), newest first
          const settled = state.picks.filter((p) => p.outcome != null || p.provisional_outcome != null)
            .sort((a, b) => new Date(b.starts_at || 0) - new Date(a.starts_at || 0)).slice(0, 4);
          const rr = $("recent-results"); rr.replaceChildren();
          if (settled.length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "No settled picks yet.";
            rr.appendChild(p0);
          } else {
            settled.forEach((p) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const t0 = document.createElement("span"); t0.className = "kr-t mono";
              t0.textContent = fmt(p.score == null ? p.scraped_score : p.score);
              const e = document.createElement("span"); e.className = "kr-e"; e.textContent = eventLabel(p.event);
              const s = document.createElement("span"); s.className = "kr-s mono";
              const oc = String(p.outcome == null ? p.provisional_outcome : p.outcome).replace("_", " ");
              // Audit 2026-07-09: route through selLabel like every other
              // pick render site — a bare BTTS "Yes"/"No" is ambiguous
              // without its market qualifier.
              const sel = p.selection ? selLabel(p) : "";
              s.textContent = (sel ? sel + " · " : "") + oc.toUpperCase() + (p.outcome == null ? " (provisional)" : "");
              r.append(t0, e, s); makeRowOpenPick(r, p); rr.appendChild(r);
            });
          }

          // evidence position statement
          const perf = state.perf;
          const evEl = $("evidence-position");
          evEl.replaceChildren();
          if (!perf) {
            evEl.textContent = "Could not load performance data.";
          } else {
            const n = Number(perf.n_sharp_close) || 0, floor = Number(perf.min_headline_n) || 50;
            const evTxt = document.createElement("span"); evTxt.className = "ev-pos-text";
            evTxt.textContent = perf.sharp_status === "ok"
              ? n + " of " + floor + " trusted sharp closes settled — evidence threshold met."
              : n + " of " + floor + " trusted sharp closes settled — Low Evidence, still accruing.";
            evEl.appendChild(evTxt);
            // Fix 2026-07-10 #11: earn the panel height — an n-of-floor
            // progress bar built from the SAME fields as the sentence above.
            evEl.appendChild(progressBarEl(n, floor, "Trusted sharp closes"));
          }

          // ---- at-a-glance KPI strip (all live, no fabricated metrics) ----
          $("today-asof").textContent = state.lastOkAt
            ? "updated " + state.lastOkAt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
            : "";
          const trackedOpen = state.picks.filter((p) => tierOf(p) === "volume" && hasFutureKickoff(p) && p.status === "alerted").length;
          const next24 = state.picks.filter((p) => {
            if (hasStarted(p) || p.starts_at == null) return false;
            const ms = new Date(p.starts_at).getTime() - Date.now();
            return ms >= 0 && ms <= 24 * 3.6e6;
          }).length;
          const settledCount = state.picks.filter((p) => p.outcome != null || p.provisional_outcome != null).length;
          const nSharp = perf ? Number(perf.n_sharp_close) || 0 : null;
          const floorN = perf ? Number(perf.min_headline_n) || 50 : null;
          const ageTxt = health && health.newest_poll_age_seconds != null
            ? fmtRelAge(new Date(Date.now() - Number(health.newest_poll_age_seconds) * 1000).toISOString())
            : "—";
          const stripBox = $("today-stats"); stripBox.replaceChildren();
          stripBox.setAttribute("aria-busy", "false");
          const mkStat = (val, label, cls, primary) => {
            const c = document.createElement("div"); c.className = "stat" + (primary ? " primary" : "");
            const v = document.createElement("div"); v.className = "sv" + (cls ? " " + cls : ""); v.textContent = val;
            const k = document.createElement("div"); k.className = "sk"; k.textContent = label;
            c.append(v, k); return c;
          };
          stripBox.appendChild(mkStat(qualificationAvailable ? String(qualified.length) : "—", "Qualified now",
            !qualificationAvailable ? "neg" : qualified.length > 0 ? "pos" : null, true));
          stripBox.appendChild(mkStat(state.volumeErr ? "—" : String(trackedOpen), "Tracked open",
            state.volumeErr ? "warn" : null));
          stripBox.appendChild(mkStat(String(next24), "Next 24h", (state.premiumErr || state.volumeErr) ? "warn" : null));
          stripBox.appendChild(mkStat(String(settledCount), "Settled (loaded)", (state.premiumErr || state.volumeErr) ? "warn" : null));
          stripBox.appendChild(mkStat(perf ? nSharp + "/" + floorN : "—", "Trusted sharp closes",
            state.perfErr ? "warn" : perf && perf.sharp_status === "ok" ? "pos" : "warn"));
          stripBox.appendChild(mkStat(
            gamesUnavailableWithoutCache() ? "—" : String(state.games.length),
            gamesPendingWithoutCache() ? "Fixtures loading" :
              state.gamesErr && state.gamesLastGoodAt ? "Fixtures cached" : "Fixtures tracked",
            state.gamesErr || gamesPendingWithoutCache() ? "warn" : null));

          // ---- system-health strip ----
          const healthBox = $("today-health"); healthBox.replaceChildren();
          const mkCell = (label, dotLvl, main, sub) => {
            const c = document.createElement("div"); c.className = "health-cell";
            const k = document.createElement("div"); k.className = "hk"; k.textContent = label;
            const v = document.createElement("div"); v.className = "hv";
            if (dotLvl) { const d = document.createElement("span"); d.className = "health-dot " + dotLvl; v.appendChild(d); }
            v.appendChild(document.createTextNode(main));
            const s = document.createElement("div"); s.className = "hs"; s.textContent = sub;
            c.append(k, v, s); healthBox.appendChild(c);
          };
          const conditionH = systemCondition(health);
          const staleH = dataIsStale(health);
          mkCell("Data freshness", conditionH.dot, conditionH.label,
            "Newest odds " + ageTxt + " · window " + fmtNum(verifiedWindowMs(health) / 60000, 0) + "m");
          const pool = health && health.proxy_pool;
          const pc = classifyProxyPool(pool);
          mkCell("Proxy pool", pc.level, pc.label,
            pool ? "healthy " + fmt(pool.healthy) + "/" + fmt(pool.configured) + " · dead " + fmt(pool.dead) : pc.detail);
          const sportCount = health && health.polls ? Object.keys(health.polls).length : 0;
          const fixtureValue = gamesUnavailableWithoutCache() ? "—" : String(state.games.length);
          const fixtureLabel = gamesPendingWithoutCache() ? "loading fixtures" :
            fixtureValue + (state.gamesErr && state.gamesLastGoodAt ? " cached fixtures" : " fixtures");
          mkCell("Sources", state.gamesErr || gamesPendingWithoutCache() ? "warn" : "ok",
            fixtureLabel,
            sportCount > 0 ? sportCount + " sport poll" + (sportCount === 1 ? "" : "s") + " active" : ((health && health.odds_source) || "odds") + " feed");
          mkCell("Odds staleness", staleH ? "warn" : "ok",
            staleH ? "Stale — picks not current" : "Within freshness window",
            "Auto-refresh every 60s");

          // ---- desk ticker (head vitals) ----
          const tick = $("desk-ticker"); tick.replaceChildren();
          const mkTick = (dot, key, val) => {
            const t = document.createElement("div"); t.className = "tk";
            if (dot) { const d = document.createElement("span"); d.className = "tkdot " + dot; t.appendChild(d); }
            const kk = document.createElement("span"); kk.className = "tkk"; kk.textContent = key;
            const b = document.createElement("b"); b.textContent = val;
            t.append(kk, b); tick.appendChild(t);
          };
          mkTick(conditionH.dot, "feed", conditionH.label.toLowerCase() + " · " + ageTxt);
          mkTick(state.gamesErr || gamesPendingWithoutCache() ? "warn" : null, "sources", fixtureLabel);
          mkTick(pc.level, "proxy", pc.label);
          if (perf) mkTick(perf.sharp_status === "ok" ? "ok" : "warn", "sharp clv", nSharp + "/" + floorN);

          // ---- systems footer (persistent picks-only framing + vitals) ----
          const foot = $("desk-foot"); foot.replaceChildren();
          const feedName = (health && health.odds_source) ? String(health.odds_source) : "odds";
          const sep = () => { const s = document.createElement("span"); s.className = "fsep"; s.textContent = "·"; return s; };
          const fMeta = (label, val) => { const s = document.createElement("span"); const b = document.createElement("b"); b.textContent = val; s.append(document.createTextNode(label + " "), b); return s; };
          const safe = document.createElement("span"); safe.className = "fsafe"; safe.textContent = "Picks-only · informational · never places bets";
          foot.append(safe, sep(), fMeta("feed", feedName), sep(),
            fMeta("window", fmtNum(verifiedWindowMs(health) / 60000, 0) + "m"), sep(),
            fMeta("data age", ageTxt));
          $("view-today").setAttribute("aria-busy", "false");
        }

        // ===== EDGES — list ====================================================
        // Task 4 (2026-07-11): volume-tier demotion-note chips. The pipeline
        // appends demotion notes to reason_summary as " | slug: …" / " | slug (…)"
        // segments (stake_zero, ml-filter, steam, non-major league, the
        // per-market premium floor …). Client-side DISPLAY parse only — no
        // schema or staking change; defensive against unknown segment shapes.
        function demotionChips(p) {
          if (tierOf(p) !== "volume" || !p.reason_summary) return [];
          const segs = String(p.reason_summary).split(" | ").slice(1);
          const out = [];
          segs.forEach((seg) => {
            const cut = seg.search(/[:(]/);
            let slug = (cut > 0 ? seg.slice(0, cut) : seg).trim();
            if (slug.length > 28) slug = slug.slice(0, 27) + "…";
            if (slug && out.indexOf(slug) === -1) out.push(slug);
          });
          return out.slice(0, 4);
        }
        function demotionChipsEl(p) {
          const chips = demotionChips(p);
          if (chips.length === 0) return null;
          const wrap = document.createElement("span"); wrap.className = "er-chips";
          chips.forEach((c) => {
            const t = document.createElement("span"); t.className = "tag tag-warm tag-dashed";
            t.textContent = c;
            t.title = "demotion note from the reason summary — display only";
            wrap.appendChild(t);
          });
          return wrap;
        }
        // Task 5 (2026-07-11): same-game correlation. Two or more OPEN premium
        // picks sharing an event_id are correlated exposure — flagged so the
        // operator sees it before betting both. Display only, staking unchanged.
        function correlatedPremiumCount(p) {
          if (tierOf(p) !== "premium" || p.status !== "alerted" || !hasFutureKickoff(p) || p.event_id == null) return 0;
          const n = state.picks.filter((q) =>
            tierOf(q) === "premium" && q.status === "alerted" && hasFutureKickoff(q) &&
            q.event_id != null && String(q.event_id) === String(p.event_id)).length;
          return n >= 2 ? n : 0;
        }
        function correlationChipEl(p) {
          const n = correlatedPremiumCount(p);
          if (n < 2) return null;
          const wrap = document.createElement("span"); wrap.className = "er-chips";
          const t = document.createElement("span"); t.className = "tag tag-warm";
          t.textContent = "correlated: " + n + " picks this game";
          t.title = "same-game premium picks rise and fall together — display only, staking unchanged";
          wrap.appendChild(t);
          return wrap;
        }
        function trustGlyph(p) {
          if (isTrustedClv(p)) return "●";
          if (p.anchor_type === "pinnacle" || p.anchor_type === "sharp") return "◐";
          return "○";
        }
        function edgeRowEl(p, health) {
          const row = document.createElement("button");
          row.type = "button"; row.className = "edge-row " + (tierOf(p) === "volume" ? "tier-shadow" : "tier-premium");
          row.dataset.id = String(p.id);
          row.dataset.focusKey = "pick-" + String(p.id);
          if (String(p.id) === String(selectedId)) row.classList.add("active");
          const stale = tierOf(p) === "premium" && p.status === "alerted" && !hasStarted(p) &&
            (!hasFutureKickoff(p) || !isRevalidationFresh(p, health));
          if (stale || dataIsStale(health)) row.classList.add("is-stale");
          const ko = document.createElement("span"); ko.className = "er-ko mono"; ko.textContent = hasStarted(p) ? "started" : fmtCountdown(p.starts_at);
          const ev = document.createElement("span"); ev.className = "er-event"; ev.textContent = eventLabel(p.event);
          const tier = document.createElement("span"); tier.className = "er-tier tag " + (tierOf(p) === "volume" ? "tag-warm" : "tag-cyan");
          tier.textContent = tierOf(p) === "volume" ? "Shadow" : "Premium";
          const sel = document.createElement("span"); sel.className = "er-sel mono";
          // Task F 2026-07-26 #1: a closed/settled/void row shows its MINT
          // edge (+ settled P&L when known) — a live re-priced current_edge
          // on a dead market is meaningless and floated void picks to the top.
          const closed = edgeGroupOf(p) === "closed";
          const eff = closed ? numOf(p.edge) : numOf(p.current_edge == null ? p.edge : p.current_edge);
          sel.appendChild(document.createTextNode(selLabel(p) + " @ " + fmtOdds(p.decimal_odds) + " · "));
          // Fix 2026-07-10 #3: a negative edge keeps its minus sign and is
          // styled as negative — never rendered like a normal positive edge.
          const effEl = document.createElement("span");
          effEl.className = "er-edge" + (isFinite(eff) && eff < 0 ? " neg" : "");
          effEl.textContent = (closed ? "mint " : "") + fmtSignedPct(eff);
          sel.appendChild(effEl);
          if (closed) {
            const pnl = p.pnl != null ? p.pnl : p.provisional_pnl;
            if (pnl != null && isFinite(Number(pnl))) {
              sel.appendChild(document.createTextNode(" · P&L " + (Number(pnl) >= 0 ? "+" : "") + Number(pnl).toFixed(2)));
            }
          }
          const trust = document.createElement("span"); trust.className = "er-trust"; trust.textContent = trustGlyph(p); trust.title = anchorLabel(p);
          row.append(ko, ev, tier, sel, trust);
          // Task 5: same-game correlation chip (premium); Task 4: demotion-note
          // chips (volume). Both display only.
          const corr = correlationChipEl(p);
          if (corr) row.appendChild(corr);
          const demo = demotionChipsEl(p);
          if (demo) row.appendChild(demo);
          // Audit 2026-07-09: surface the server's stored-impossible flag —
          // isActionable already refuses these rows; the tag says WHY.
          // Fix 2026-07-10 #3 — QUARANTINE, never hide: structurally
          // inconsistent rows grey out with an explicit excluded badge, and
          // negative-edge Closed rows get their own muted treatment.
          if (p.structural_sane !== true) {
            row.classList.add("is-quarantined");
            const bad = document.createElement("span");
            bad.className = "er-danger tag tag-danger tag-dashed";
            bad.textContent = "Internally inconsistent — excluded, do not bet";
            row.appendChild(bad);
          } else if (edgeGroupOf(p) === "closed" && isFinite(eff) && eff < 0) {
            row.classList.add("is-neg-closed");
          }
          row.addEventListener("click", () => selectEdge(p.id, null, row));
          return row;
        }
        function renderEdgesList() {
          const box = $("edge-list");
          box.replaceChildren();
          if (state.picks.length === 0 && state.picksErr !== null) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Could not load picks.";
            box.appendChild(p0); return;
          }
          if ((state.premiumErr || state.volumeErr) && state.picks.length > 0) {
            const notice = document.createElement("p"); notice.className = "muted stale-cache";
            notice.textContent = "Could not refresh all pick tiers — showing cached rows; cached premium rows cannot qualify.";
            box.appendChild(notice);
          }
          const q = ($("eq-search").value || "").trim().toLowerCase();
          const tierWant = $("eq-tier").value;
          const statusWant = $("eq-status").value;
          const health = state.health;
          // ---- edges overview ticker (all picks, by group) ----
          const sumBox = $("edges-summary");
          if (sumBox) {
            sumBox.replaceChildren();
            const all = state.picks;
            const cQual = all.filter((p) => isActionable(p, health)).length;
            const cTrack = all.filter((p) => edgeGroupOf(p) === "tracked").length;
            const cClosed = all.filter((p) => edgeGroupOf(p) === "closed").length;
            // Audit 2026-07-09: the "stale" chip must count exactly the rows
            // the "Stale — awaiting re-price" group below it shows — derive
            // both from the one edgeGroupOf predicate (like cTrack/cClosed).
            const cStale = all.filter((p) => edgeGroupOf(p) === "stale").length;
            const mkS = (dot, k, v) => {
              const t = document.createElement("div"); t.className = "tk";
              if (dot) { const d = document.createElement("span"); d.className = "tkdot " + dot; t.appendChild(d); }
              const kk = document.createElement("span"); kk.className = "tkk"; kk.textContent = k;
              const b = document.createElement("b"); b.textContent = String(v);
              t.append(kk, b); sumBox.appendChild(t);
            };
            mkS(cQual > 0 ? "ok" : null, "qualified", cQual);
            mkS(null, "tracked", cTrack);
            mkS(null, "closed", cClosed);
            if (cStale > 0) mkS("warn", "stale", cStale);
          }
          const matches = (p) => {
            if (tierWant && tierOf(p) !== tierWant) return false;
            const group = edgeGroupOf(p);
            if (statusWant && group !== statusWant) return false;
            if (!q) return true;
            return (String(p.event || "").toLowerCase().indexOf(q) !== -1) ||
              (String(p.selection || "").toLowerCase().indexOf(q) !== -1) ||
              (String(p.league || "").toLowerCase().indexOf(q) !== -1);
          };
          const rows = state.picks.filter(matches);
          const groups = [
            ["actionable", "Actionable"],
            ["stale", "Stale — awaiting re-price"],
            ["tracked", "Tracked — informational"],
            ["closed", "Closed"],
          ];
          const sortMode = ($("eq-sort") && $("eq-sort").value) || "edge";
          // Task F 2026-07-26 #1: closed rows sort by MINT edge — never by a
          // live re-priced current_edge on a finished market.
          const edgeVal = (p) => { const v = numOf(edgeGroupOf(p) === "closed" || p.current_edge == null ? p.edge : p.current_edge); return Number.isFinite(v) ? v : -1e9; };
          const koVal = (p) => new Date(p.starts_at || 8e15).getTime();
          // Fix 2026-07-10 #17: sport precedence first, then the selected
          // comparator within each sport.
          const sortList = (arr) => arr.slice().sort((a, b) =>
            (sportRank(a) - sportRank(b)) ||
            (sortMode === "kickoff" ? koVal(a) - koVal(b)
            : sortMode === "odds" ? (numOf(b.decimal_odds) || 0) - (numOf(a.decimal_odds) || 0)
            : edgeVal(b) - edgeVal(a)));
          groups.forEach(([key, label]) => {
            const list = sortList(rows.filter((p) => edgeGroupOf(p) === key));
            const h = document.createElement("div"); h.className = "edge-group-h";
            const t = document.createElement("span"); t.className = "egt"; t.textContent = label;
            const n = document.createElement("span"); n.className = "egn"; n.textContent = "(" + list.length + ")";
            h.append(t, n); box.appendChild(h);
            if (list.length === 0) {
              const e = document.createElement("div"); e.className = "edge-group-empty";
              e.textContent = key === "actionable" ? "No pick currently qualifies." : "None right now.";
              box.appendChild(e); return;
            }
            const cap = edgeCaps[key] || EDGE_CHUNK;
            const grid = document.createElement("div"); grid.className = "edge-grid";
            // Fix 2026-07-10 #28: the #17 sport ordering must be LEGIBLE —
            // a small mono subheader precedes each sport's rows (serialized
            // sport_label vocabulary: Football/Basketball/Tennis/NFL). The
            // list is already sportRank-sorted, so labels change in order;
            // sports with no rows never render a header.
            let lastSportLbl = null;
            list.slice(0, cap).forEach((p) => {
              const sportLbl = String(p.sport_label || p.sport || "").replace(/_/g, " ");
              if (sportLbl && sportLbl !== lastSportLbl) {
                const sh = document.createElement("div"); sh.className = "edge-sport-h mono";
                sh.textContent = sportLbl.toUpperCase();
                grid.appendChild(sh);
                lastSportLbl = sportLbl;
              }
              grid.appendChild(edgeRowEl(p, health));
            });
            box.appendChild(grid);
            if (list.length > cap) {
              const remaining = list.length - cap;
              const more = document.createElement("button"); more.type = "button"; more.className = "edge-more";
              more.dataset.focusKey = "edge-more-" + key;
              more.textContent = "Show " + Math.min(EDGE_CHUNK, remaining) + " more · " + remaining + " hidden";
              more.addEventListener("click", () => { edgeCaps[key] = cap + EDGE_CHUNK; renderEdgesList(); });
              box.appendChild(more);
            }
          });
        }
        function csvSafeCell(value) {
          let cell = value == null ? "" : String(value);
          // Spreadsheet programs may evaluate cells beginning with these
          // characters as formulas. Prefix after any leading control/space
          // characters, then apply normal RFC 4180 quoting.
          if (/^[\s\u0000-\u001f]*[=+\-@]/.test(cell)) cell = "'" + cell;
          return /[",\r\n]/.test(cell) ? '"' + cell.replace(/"/g, '""') + '"' : cell;
        }
        function exportEdgesCsv() {
          const q = ($("eq-search").value || "").trim().toLowerCase();
          const tierWant = $("eq-tier").value, statusWant = $("eq-status").value;
          const sortMode = ($("eq-sort") && $("eq-sort").value) || "edge";
          // Task F 2026-07-26 #1: CSV keeps both edge columns, but a closed
          // row's primary (sort) edge is its MINT edge, matching the list.
          const edgeVal = (p) => numOf(edgeGroupOf(p) === "closed" || p.current_edge == null ? p.edge : p.current_edge);
          const match = (p) => {
            if (tierWant && tierOf(p) !== tierWant) return false;
            if (statusWant && edgeGroupOf(p) !== statusWant) return false;
            if (!q) return true;
            return [p.event, p.selection, p.league].some((v) => String(v || "").toLowerCase().indexOf(q) !== -1);
          };
          const rows = state.picks.filter(match).sort((a, b) =>
            sortMode === "kickoff" ? new Date(a.starts_at || 8e15) - new Date(b.starts_at || 8e15)
            : sortMode === "odds" ? (numOf(b.decimal_odds) || 0) - (numOf(a.decimal_odds) || 0)
            : (edgeVal(b) || -1e9) - (edgeVal(a) || -1e9));
          const cols = ["group", "tier", "status", "event", "league", "market", "selection", "decimal_odds", "edge", "current_edge", "confidence", "anchor_type", "starts_at", "revalidated_at"];
          const lines = [cols.map(csvSafeCell).join(",")];
          rows.forEach((p) => lines.push(cols.map((c) => csvSafeCell(c === "group" ? edgeGroupOf(p) : p[c])).join(",")));
          const blob = new Blob([lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          a.download = "edges-" + new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-") + ".csv";
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }

        let drawerOpener = null;
        let drawerOpenerKey = null;
        function rememberDrawerOpener(id, opener) {
          const candidate = opener instanceof HTMLElement ? opener : document.activeElement;
          drawerOpener = candidate instanceof HTMLElement && candidate !== document.body ? candidate : null;
          drawerOpenerKey = (drawerOpener && drawerOpener.dataset.focusKey) || "pick-" + String(id);
        }
        function selectEdge(id, targetHash, opener) {
          rememberDrawerOpener(id, opener);
          selectedId = String(id);
          const nextHash = targetHash || "#/edges/" + selectedId;
          if (location.hash === nextHash) {
            setView("edges");
            syncDrawerFromRoute(true);
          } else {
            location.hash = nextHash;
          }
        }
        function restoreDrawerOpener() {
          let target = drawerOpener && drawerOpener.isConnected && drawerOpener.offsetParent !== null
            ? drawerOpener : null;
          if (!target && drawerOpenerKey) {
            target = Array.from(document.querySelectorAll("[data-focus-key]")).find((el) =>
              el.dataset.focusKey === drawerOpenerKey && el.offsetParent !== null
            ) || null;
          }
          if (!target && activeView === "edges") target = $("eq-search");
          if (target) target.focus({ preventScroll: true });
          drawerOpener = null;
          drawerOpenerKey = null;
        }
        function openSheetIfMobile(shouldFocus) {
          // The detail is a right-docked drawer at all widths. Modal state,
          // backdrop visibility, scroll containment, and focus move together.
          const detail = $("edge-detail");
          const newlyOpened = detail.getAttribute("aria-hidden") !== "false";
          detail.classList.add("visible");
          detail.classList.add("open");
          detail.setAttribute("aria-hidden", "false");
          $("edge-backdrop").hidden = false;
          document.body.style.overflow = "hidden";
          if ((shouldFocus !== false || newlyOpened) && !detail.contains(document.activeElement)) {
            requestAnimationFrame(() => $("edge-back").focus({ preventScroll: true }));
          }
        }
        function closeSheet(restoreOpener) {
          const detail = $("edge-detail");
          detail.classList.remove("open");
          detail.classList.remove("visible");
          detail.setAttribute("aria-hidden", "true");
          $("edge-backdrop").hidden = true;
          document.body.style.overflow = "";
          if (restoreOpener) requestAnimationFrame(restoreDrawerOpener);
        }
        function requestDrawerClose() {
          if (location.hash !== "#/edges") {
            location.hash = "#/edges";
            return;
          }
          selectedId = null;
          closeSheet(true);
          renderEdgesList();
        }
        $("edge-back").addEventListener("click", requestDrawerClose);
        $("edge-backdrop").addEventListener("click", requestDrawerClose);
        document.addEventListener("keydown", (ev) => {
          const detail = $("edge-detail");
          if (!detail.classList.contains("open")) return;
          if (ev.key === "Escape") {
            ev.preventDefault();
            requestDrawerClose();
            return;
          }
          if (ev.key !== "Tab") return;
          const focusable = Array.from(detail.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
          )).filter((el) => el.offsetParent !== null && el.getAttribute("aria-hidden") !== "true");
          if (focusable.length === 0) {
            ev.preventDefault();
            detail.focus();
            return;
          }
          const first = focusable[0], last = focusable[focusable.length - 1];
          if (ev.shiftKey && (document.activeElement === first || !detail.contains(document.activeElement))) {
            ev.preventDefault(); last.focus();
          } else if (!ev.shiftKey && (document.activeElement === last || !detail.contains(document.activeElement))) {
            ev.preventDefault(); first.focus();
          }
        });

        function currentPick() {
          if (!selectedId) return null;
          return state.picks.find((p) => String(p.id) === String(selectedId)) || null;
        }

        function metricEl(label, value) {
          const d = document.createElement("div"); d.className = "metric";
          const k = document.createElement("span"); k.className = "mk"; k.textContent = label;
          const v = document.createElement("span"); v.className = "mv";
          if (value instanceof Node) v.appendChild(value); else v.textContent = value;
          d.append(k, v); return d;
        }
        // Fix 2026-07-10 #24 — subtle "not available" for empty ticket fields,
        // with a short title tooltip where the reason is knowable.
        function naEl(title) {
          const s = document.createElement("span"); s.className = "na";
          s.textContent = "not available";
          if (title) s.title = title;
          return s;
        }
        // Fix 2026-07-10 #19 — labelled ticket section header (mono eyebrow).
        function ticketSec(label) {
          const h = document.createElement("div"); h.className = "eyebrow ticket-sec"; h.textContent = label;
          return h;
        }
        // Fix 2026-07-10 #11/#14 — shared n-of-floor progress bar built from
        // values ALREADY rendered beside it (never new data).
        function progressBarEl(n, floor, label) {
          const bar = document.createElement("div"); bar.className = "mini-progress";
          bar.setAttribute("role", "progressbar");
          bar.setAttribute("aria-valuemin", "0");
          bar.setAttribute("aria-valuemax", String(floor));
          bar.setAttribute("aria-valuenow", String(Math.min(n, floor)));
          bar.setAttribute("aria-label", label + " " + n + " of " + floor);
          const fill = document.createElement("span"); fill.className = "mini-progress-fill";
          fill.style.width = (floor > 0 ? Math.min(100, Math.round((n / floor) * 100)) : 0) + "%";
          bar.appendChild(fill);
          return bar;
        }

        function buildTimeline(p) {
          const wrap = document.createElement("div"); wrap.className = "timeline";
          const steps = [
            { label: "Created", done: true },
            { label: "Alerted", done: p.status === "alerted" || p.status === "settled" || p.status === "superseded" },
            { label: "Kickoff", done: hasStarted(p) },
            { label: "Settled", done: p.status === "settled" },
          ];
          steps.forEach((s) => {
            const el = document.createElement("span"); el.className = "tl-step" + (s.done ? " done" : ""); el.textContent = s.label;
            wrap.appendChild(el);
          });
          return wrap;
        }

        function validateSettlementPayload(value) {
          const result = expectObjectPayload(value, "Settlement");
          if (!Number.isInteger(result.settled) || result.settled < 0 ||
              !Number.isInteger(result.skipped) || result.skipped < 0) {
            throw responseError("Settlement payload has invalid counts.", "SchemaError");
          }
          return result;
        }
        function buildResultForm(p) {
          const form = document.createElement("form");
          form.className = "result-form";
          form.noValidate = true;
          form.dataset.dirty = "false";
          form.setAttribute("aria-label", "Record final score");
          const suffix = String(p.event_id == null ? p.id : p.event_id).replace(/[^a-zA-Z0-9_-]/g, "-");
          const h = document.createElement("div"); h.className = "eyebrow"; h.textContent = "Record result"; form.appendChild(h);
          const row = document.createElement("div"); row.className = "result-inputs";
          const mkField = (labelText, id) => {
            const f = document.createElement("label"); f.className = "field"; f.htmlFor = id;
            const l = document.createElement("span"); l.className = "field-lbl"; l.textContent = labelText;
            const i = document.createElement("input");
            i.type = "number"; i.min = "0"; i.max = "250"; i.step = "1"; i.inputMode = "numeric";
            i.autocomplete = "off"; i.id = id; i.className = "field-input"; i.dataset.focusKey = id;
            f.append(l, i); return { f, i };
          };
          const home = mkField("Home score", "result-home-" + suffix);
          const away = mkField("Away score", "result-away-" + suffix);
          const scraped = typeof p.scraped_score === "string"
            ? p.scraped_score.match(/^\s*(\d+)\s*[-–:]\s*(\d+)\s*$/) : null;
          if (scraped) {
            home.i.value = scraped[1];
            away.i.value = scraped[2];
          }
          row.append(home.f, away.f);
          const submit = document.createElement("button");
          submit.type = "submit"; submit.id = "result-submit-" + suffix; submit.textContent = "Submit result";
          const note = document.createElement("span");
          note.className = "result-note"; note.id = "result-note-" + suffix;
          note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
          home.i.setAttribute("aria-describedby", note.id);
          away.i.setAttribute("aria-describedby", note.id);

          const clearError = () => {
            note.textContent = "";
            note.classList.remove("err");
            home.i.removeAttribute("aria-invalid");
            away.i.removeAttribute("aria-invalid");
          };
          [home.i, away.i].forEach((input) => input.addEventListener("input", () => {
            form.dataset.dirty = "true";
            clearError();
          }));
          const invalid = (input, message) => {
            form.dataset.dirty = "true";
            note.textContent = message;
            note.classList.add("err");
            input.setAttribute("aria-invalid", "true");
            input.focus();
          };
          const scoreOf = (input, label) => {
            const raw = input.value.trim();
            if (!raw) {
              invalid(input, "Enter the " + label.toLowerCase() + ".");
              return null;
            }
            if (!/^\d+$/.test(raw) || Number(raw) > 250) {
              invalid(input, label + " must be a whole number from 0 to 250.");
              return null;
            }
            return Number(raw);
          };
          form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            clearError();
            const homeScore = scoreOf(home.i, "Home score");
            if (homeScore === null) return;
            const awayScore = scoreOf(away.i, "Away score");
            if (awayScore === null) return;
            form.dataset.dirty = "true";
            submit.disabled = true;
            note.textContent = "Recording result…";
            try {
              const result = await fetchGuarded("/events/" + p.event_id + "/result", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ home_score: homeScore, away_score: awayScore }),
              }).then((res) => readJson(res, validateSettlementPayload));
              form.dataset.dirty = "false";
              note.textContent = "Result recorded — " + result.settled + " picks settled.";
              setTimeout(() => load(), 800);
            } catch (e) {
              note.classList.add("err");
              if (e && e.message === "Authentication required.") return;
              if (isTimeoutErr(e)) {
                note.textContent = "Could not record result. No answer within 15s.";
              } else if (e && e.httpStatus) {
                note.textContent = "Could not record result. (HTTP " + e.httpStatus + ")";
              } else if (e && (e.name === "SchemaError" || e.name === "ResponseFormatError" || e.name === "PayloadTooLargeError")) {
                note.textContent = "Could not record result. Server returned invalid data.";
              } else {
                note.textContent = "Could not record result. Network error.";
              }
              submit.disabled = false;
            }
          });
          form.append(row, submit, note);
          return form;
        }

        function renderEdgeDetail() {
          const p = currentPick();
          const detail = $("edge-detail");
          if (!p) { closeSheet(false); return; }
          detail.classList.add("visible");
          $("edge-detail-title").textContent = eventLabel(p.event);

          const body = $("edge-detail-body"); body.replaceChildren();

          // ==== trade-ticket header (fix 2026-07-10 #19): selection + price
          // emphasized, tier badge alongside; the event name is the drawer
          // title above this block. ====
          const head = document.createElement("div"); head.className = "ticket-head";
          const selLine = document.createElement("div"); selLine.className = "ticket-sel mono";
          selLine.textContent = selLabel(p) + " @ " + fmtOdds(p.decimal_odds);
          head.appendChild(selLine);
          const tierTag = document.createElement("span");
          tierTag.className = "tag " + (tierOf(p) === "volume" ? "tag-warm" : "tag-cyan");
          tierTag.textContent = tierOf(p) === "volume" ? "Shadow — tracked, informational only" : "Premium";
          head.appendChild(tierTag);
          // Audit 2026-07-09: the server flags stored-impossible rows
          // (structural_sane=false) so the dashboard can refuse to present
          // them as bettable — badge the detail pane in danger style.
          if (p.structural_sane !== true) {
            const bad = document.createElement("span");
            bad.className = "tag tag-danger tag-dashed";
            bad.textContent = "Internally inconsistent — excluded, do not bet";
            head.appendChild(bad);
          }
          body.appendChild(head);
          body.appendChild(buildTimeline(p));

          // ==== trust state (fix 2026-07-10 #21) — strictly the OR of states
          // the code already computes elsewhere; no new trust logic. ====
          const staleNow = p.status === "alerted" && hasFutureKickoff(p) && !isRevalidationFresh(p, state.health);
          const invalidLiveKickoff = p.status === "alerted" && !hasStarted(p) && !hasFutureKickoff(p);
          const lacksSharpAnchor = !(p.anchor_type === "pinnacle" || p.anchor_type === "sharp");
          const amcVal = numOf(p.anchor_match_confidence);
          const matchMissing = !isFinite(amcVal) || !p.anchor_match_method;
          // Audit 2026-07-10 (operator report: Partick Over 4.5 banner):
          // close-trust applies only once the pick has STARTED — an open
          // pick's clv_log is a PROVISIONAL revalidation stamp with no
          // close to trust yet, so testing it here false-fired the
          // untrusted banner on fresh, sharp-anchored qualifying picks.
          const untrustedClose = hasStarted(p) && p.clv_log != null && !isTrustedClv(p);
          const untrusted = staleNow || invalidLiveKickoff || lacksSharpAnchor || matchMissing || untrustedClose ||
            p.structural_sane !== true || dataIsStale(state.health);
          if (untrusted) {
            const warn = document.createElement("div"); warn.className = "trust-banner";
            warn.setAttribute("role", "note");
            warn.textContent = "Untrusted / stale pricing — indicative only, not a recommended bet.";
            body.appendChild(warn);
          }

          // ==== key metrics row (fix 2026-07-10 #19/#22/#23) ====
          const eff = p.current_edge == null ? numOf(p.edge) : numOf(p.current_edge);
          const evNum = numOf(p.ev);
          // #22: extreme EV (>+100%) or an untrusted/inconsistent pick mutes
          // the figures and flags them — values are NEVER recomputed/clamped.
          const figsIndicative = untrusted || (isFinite(evNum) && evNum > 1);
          const kpis = document.createElement("div");
          kpis.className = "ticket-kpis" + (figsIndicative ? " indicative" : "");
          const mkKpi = (label, valText, subText) => {
            const c = document.createElement("div"); c.className = "tkpi";
            const v = document.createElement("div"); v.className = "tkpi-v mono"; v.textContent = valText;
            const k = document.createElement("div"); k.className = "tkpi-k"; k.textContent = label;
            c.append(v, k);
            if (subText) {
              const s0 = document.createElement("div"); s0.className = "tkpi-sub"; s0.textContent = subText;
              c.appendChild(s0);
            }
            return c;
          };
          // Task 3 (2026-07-11): provenance labels — EV is FIXED at mint;
          // Edge (and Fair odds below) are live re-priced values.
          kpis.appendChild(mkKpi("Edge (now)", fmtSignedPct(eff), figsIndicative ? "indicative, unverified" : null));
          kpis.appendChild(mkKpi("EV (at mint)", fmtSignedPct(p.ev), figsIndicative ? "indicative, unverified" : null));
          const sf = numOf(p.recommended_stake_fraction);
          // Show the actual fractional-Kelly stake (as a % of bankroll) — the
          // label promises a stake figure, not a confidence rating. A missing or
          // zero fraction (e.g. a cap-denied pick) reads "—".
          const stakeTxt = isFinite(sf) && sf > 0 ? (sf * 100).toFixed(2) + "% of bankroll" : "—";
          // #23: never an actionable-looking stake on an untrusted/stale/
          // shadow pick — de-emphasized and explicitly not applicable.
          const stakeGated = !isActionable(p, state.health);
          const stakeCell = mkKpi("Suggested stake (informational)", stakeTxt,
            stakeGated
              ? (untrusted ? "informational only — not applicable while untrusted"
                : "informational only — not applicable unless currently qualified")
              : null);
          if (stakeGated) stakeCell.classList.add("gated");
          kpis.appendChild(stakeCell);
          body.appendChild(kpis);
          // Task 5 correlation chip (premium) + Task 4 demotion chips (volume)
          // on the ticket too — same builders as the list rows, display only.
          const tCorr = correlationChipEl(p);
          if (tCorr) body.appendChild(tCorr);
          const tDemo = demotionChipsEl(p);
          if (tDemo) body.appendChild(tDemo);

          // ==== Pricing (fix 2026-07-10 #19) ====
          body.appendChild(ticketSec("Pricing"));
          const ml = document.createElement("div"); ml.className = "metric-list";
          // FIX 5: the banner's Fair odds, Edge, and Min-acceptable must all
          // reference ONE fair. Prefer the reconciled server-serialized fair_odds
          // (1/(closing_fair_probability ?? model_probability)); fall back to the
          // same computation client-side — NEVER 1/fair_probability, which on
          // value picks equals the offered odds (the "OFFERED == FAIR" symptom).
          let fairOdds = fmtOdds(p.fair_odds);
          if (fairOdds === "—") {
            const cf = numOf(p.closing_fair_probability), mf = numOf(p.model_probability);
            const fair = isFinite(cf) && cf > 0 ? cf : mf;
            fairOdds = isFinite(fair) && fair > 0 && fair < 1 ? (1 / fair).toFixed(2) : "—";
          }
          ml.appendChild(metricEl("Offered odds", fmtOdds(p.decimal_odds)));
          ml.appendChild(metricEl("Fair odds (now)",
            fairOdds === "—" ? naEl("no reconciled fair price on this row") : fairOdds));
          const minAcc = fmtOdds(p.min_acceptable_odds);
          ml.appendChild(metricEl("Min acceptable odds",
            minAcc === "—" ? naEl("no minimum-odds guard recorded") : minAcc));
          const floor = edgeFloorOf(p, state.health);
          ml.appendChild(metricEl("Edge vs tier floor",
            isFinite(floor) ? fmtSignedPct(eff - floor) : naEl("no tier floor available")));
          body.appendChild(ml);
          // Task 3: provenance one-liner — where "mint" vs "now" figures live.
          const prov = document.createElement("p"); prov.className = "muted";
          prov.textContent = "Mint-time fair/edge are archived in the raw reason summary below; Edge (now) and Fair odds (now) are re-priced live.";
          body.appendChild(prov);

          body.appendChild(ticketSec("Status"));
          if (p.status === "settled" || (hasStarted(p) && p.status === "alerted")) {
            const settledMl = document.createElement("div"); settledMl.className = "metric-list";
            const oc = p.outcome || p.provisional_outcome;
            settledMl.appendChild(metricEl("Result", outcomeLabel(oc) || (p.status === "settled" ? "Settled" : "Pending")));
            settledMl.appendChild(metricEl("Score", fmt(p.score || p.scraped_score)));
            const pnl = p.pnl != null ? p.pnl : p.provisional_pnl;
            settledMl.appendChild(metricEl("P&L", pnl != null ? (Number(pnl) >= 0 ? "+" : "") + Number(pnl).toFixed(2) : "—"));
            body.appendChild(settledMl);
          } else {
            const statusRow = document.createElement("div"); statusRow.className = "metric";
            const k = document.createElement("span"); k.className = "mk"; k.textContent = "Status";
            const v = document.createElement("span"); v.className = "mv"; v.textContent = p.status === "alerted" ? "Pending" : fmt(p.status);
            statusRow.append(k, v); body.appendChild(statusRow);
          }

          if (edgeGroupOf(p) === "closed" && p.status !== "settled" && p.status !== "superseded" && p.event_id != null) {
            body.appendChild(buildResultForm(p));
          }

          // ---- evidence pane (fix 2026-07-10 #19: labelled sections) ----
          const ev = $("edge-evidence-body"); ev.replaceChildren();
          // Section header is the DIMENSION name, not a value — the tag below
          // carries the actual anchor (Sharp/Pinnacle/Consensus). The old
          // hardcoded "Consensus Anchor" header rendered a contradictory
          // "CONSENSUS ANCHOR / SHARP ANCHOR" pair on sharp-anchored tickets.
          ev.appendChild(ticketSec("Fair-Value Anchor"));
          const anchorTag = document.createElement("span");
          const at = p.anchor_type;
          anchorTag.className = "tag " + (at === "pinnacle" || at === "sharp" ? "tag-cyan" : at === "consensus" ? "tag-neutral" : "tag-danger tag-dashed");
          anchorTag.textContent = anchorLabel(p);
          ev.appendChild(anchorTag);

          if (isWeakMatch(p)) {
            const wm = document.createElement("span"); wm.className = "tag tag-warm tag-dashed"; wm.style.marginLeft = "6px";
            wm.textContent = "Weak Match"; ev.appendChild(wm);
          }

          const evml = document.createElement("div"); evml.className = "metric-list"; evml.style.marginTop = "10px";
          const amc = numOf(p.anchor_match_confidence);
          evml.appendChild(metricEl("Match confidence",
            isFinite(amc) ? amc.toFixed(2) : naEl("no consensus match recorded")));
          evml.appendChild(metricEl("Match method",
            p.anchor_match_method ? fmt(p.anchor_match_method) : naEl("no consensus match method recorded")));
          // Freshness applies to any live (alerted, pre-kickoff) pick — the
          // revalidation loop runs for BOTH tiers, so a shadow/volume pick (e.g.
          // an inline-Betfair-canonical match) has a real revalidated_at too.
          // Started / settled / superseded picks are past the live window -> n/a.
          const liveWindow = p.status === "alerted" && hasFutureKickoff(p);
          evml.appendChild(metricEl("Freshness", liveWindow
            ? (isRevalidationFresh(p, state.health) ? "Fresh" : "Stale")
            : naEl(p.revalidated_at == null ? "no revalidation yet" : "past the live revalidation window")));
          ev.appendChild(evml);

          ev.appendChild(ticketSec("Provenance"));
          const clvTag = document.createElement("span"); clvTag.style.display = "inline-block";
          clvTag.className = "tag " + (isTrustedClv(p) ? "tag-cyan" : "tag-neutral tag-dashed");
          const clvState = clvStateLabel(p);
          // Fix 2026-07-10 #24: a bare "—" tag reads as broken — name the state.
          clvTag.textContent = clvState === "—" ? "no close recorded" : clvState;
          if (clvState === "—") clvTag.title = "no closing line captured for this pick yet";
          ev.appendChild(clvTag);
          if (p.clv_log != null && !clvExclusionOf(p)) {
            const clvVal = document.createElement("span"); clvVal.className = "mono"; clvVal.style.marginLeft = "8px";
            clvVal.textContent = fmtSignedPct(clvPctFromLog(p.clv_log) / 100);
            ev.appendChild(clvVal);
          }

          const details = document.createElement("details"); details.className = "raw-reason";
          const summary = document.createElement("summary"); summary.textContent = "Raw reason summary";
          const pre = document.createElement("pre"); pre.textContent = fmt(p.reason_summary);
          details.append(summary, pre); ev.appendChild(details);
        }

        $("edge-copy").addEventListener("click", async () => {
          const p = currentPick();
          const note = $("edge-copy-note");
          if (!p) { note.textContent = "Nothing selected."; return; }
          // AH-3 (audit 2026-07-26): same closed-pick guard as the list —
          // mint edge for closed picks, never a live re-priced current_edge
          // on a finished market.
          const eff = edgeGroupOf(p) === "closed" || p.current_edge == null ? p.edge : p.current_edge;
          const lines = [
            eventLabel(p.event),
            selLabel(p) + " @ " + fmtOdds(p.decimal_odds),
            "Edge: " + fmtSignedPct(eff),
            "Tier: " + (tierOf(p) === "volume" ? "Shadow" : "Premium"),
            "Anchor: " + anchorLabel(p),
          ];
          try {
            await navigator.clipboard.writeText(lines.join("\n"));
            note.textContent = "Copied.";
          } catch (e) {
            note.textContent = "Could not copy.";
          }
        });

        let edgeSearchT = null;
        $("eq-search").addEventListener("input", () => {
          clearTimeout(edgeSearchT);
          edgeSearchT = setTimeout(() => { edgeCaps = {}; renderEdgesList(); }, 150);
        });
        $("eq-tier").addEventListener("change", () => { edgeCaps = {}; renderEdgesList(); });
        $("eq-status").addEventListener("change", () => { edgeCaps = {}; renderEdgesList(); });
        $("eq-sort").addEventListener("change", () => { edgeCaps = {}; renderEdgesList(); });
        $("eq-export").addEventListener("click", exportEdgesCsv);

        // ===== RADAR ============================================================
        // Fills the Radar / Lab / Sources desk-header tickers from global state so
        // each view carries the same live-vitals signature as Today and Edges.
        function renderViewHeaders() {
          const health = state.health, perf = state.perf;
          const ageTxt = health && health.newest_poll_age_seconds != null
            ? fmtRelAge(new Date(Date.now() - Number(health.newest_poll_age_seconds) * 1000).toISOString()) : "—";
          const sportCount = health && health.polls ? Object.keys(health.polls).length : 0;
          const pc = classifyProxyPool(health && health.proxy_pool);
          const staleH = dataIsStale(health);
          const feedName = (health && health.odds_source) ? String(health.odds_source) : "odds";
          const fill = (id, cells) => {
            const box = $(id); if (!box) return; box.replaceChildren();
            cells.forEach((c) => {
              const t = document.createElement("div"); t.className = "tk";
              if (c[0]) { const d = document.createElement("span"); d.className = "tkdot " + c[0]; t.appendChild(d); }
              const kk = document.createElement("span"); kk.className = "tkk"; kk.textContent = c[1];
              const b = document.createElement("b"); b.textContent = String(c[2]);
              t.append(kk, b); box.appendChild(t);
            });
          };
          fill("radar-summary", [
            [state.gamesErr || gamesPendingWithoutCache() ? "warn" : "ok", "fixtures",
              gamesPendingWithoutCache() ? "loading" :
                gamesUnavailableWithoutCache() ? "—" : state.games.length + (state.gamesErr ? " cached" : "")],
            [null, "sport polls", sportCount || "—"],
            [null, "feed", feedName],
          ]);
          const nSharp = perf ? Number(perf.n_sharp_close) || 0 : null;
          const floorN = perf ? Number(perf.min_headline_n) || 50 : null;
          const settledCount = state.picks.filter((p) => p.outcome != null || p.provisional_outcome != null).length;
          fill("lab-summary", [
            [perf ? (perf.sharp_status === "ok" ? "ok" : "warn") : "warn", "sharp clv", perf ? nSharp + "/" + floorN : "—"],
            [null, "settled", settledCount],
          ]);
          fill("sources-summary", [
            [pc.level, "proxy", pc.label],
            [staleH ? "warn" : "ok", "feed", (staleH ? "stale" : "verified") + " · " + ageTxt],
          ]);
        }
        function renderRadar() {
          loadMatchRate();
          setCacheNotice("radar-cache-notice", state.gamesErr && state.gamesLastGoodAt
            ? "Could not refresh games — showing last loaded fixtures" + lastGoodSuffix(state.gamesLastGoodAt) + "."
            : null);
          const covEl = $("coverage-summary");
          covEl.replaceChildren();
          if (state.matchRate) {
            // Fix 2026-07-10 #16: prefer already-cached data over a blank
            // placeholder while a background refresh is in flight.
            // 2026-07-12: lead with SLATE coverage (sharp events / soft-scraped
            // events, denominator shown) — the operator's model. It replaces the
            // old dedicated-capture headline that could read a confident "100%"
            // over a 3-fixture denominator. Fall back to the old summary only if
            // the new field is absent (older cached payload).
            const slate = state.matchRate.slate_sharp_coverage;
            const headline = fmt(
              (slate && slate.headline)
              || (state.matchRate.coverage_summary && state.matchRate.coverage_summary.headline)
            );
            covEl.textContent = state.matchRateErr
              ? "Could not refresh coverage — showing last loaded data. " + headline
              : headline;
          } else if (state.matchRateErr) {
            covEl.textContent = "Could not load coverage summary.";
          } else {
            // Fix 2026-07-10 #16: skeleton shimmer (console-styled) while the
            // slow /resolution/match-rate fetch runs — display-only.
            covEl.setAttribute("role", "status");
            covEl.setAttribute("aria-label", "Loading coverage summary…");
            const sk = document.createElement("span"); sk.className = "skeleton-line";
            const sk2 = document.createElement("span"); sk2.className = "skeleton-line short";
            covEl.append(sk, sk2);
          }

          const box = $("radar-bands"); box.replaceChildren();
          if (gamesPendingWithoutCache()) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Loading fixtures…";
            box.appendChild(p0); return;
          }
          if (state.gamesErr !== null && state.gamesLastGoodAt === null) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Could not load games.";
            box.appendChild(p0); return;
          }
          const now = Date.now();
          // Fix 2026-07-10 #13: map fixtures to their pick (event + kickoff)
          // so a radar row can open the SAME Edges detail drawer; fixtures
          // without a pick stay non-interactive (no misleading hover).
          const pickByEvent = new Map();
          state.picks.forEach((p) => {
            if (p.starts_at == null) return;
            const k = String(p.event) + "|" + new Date(p.starts_at).getTime();
            if (!pickByEvent.has(k)) pickByEvent.set(k, p);
          });
          const bands = [
            ["Next 2h", (ms) => ms >= 0 && ms <= 2 * 3.6e6],
            ["Today", (ms) => ms > 2 * 3.6e6 && ms <= 24 * 3.6e6],
            ["Tomorrow+", (ms) => ms > 24 * 3.6e6],
          ];
          bands.forEach(([label, pred]) => {
            const rows = state.games.filter((g) => {
              if (g.starts_at == null) return false;
              const ms = new Date(g.starts_at).getTime() - now;
              return pred(ms);
            });
            const h = document.createElement("div"); h.className = "radar-band-h"; h.textContent = label + " (" + rows.length + ")";
            const band = document.createElement("div"); band.className = "radar-band"; band.appendChild(h);
            rows.slice(0, 40).forEach((g) => {
              const r = document.createElement("div"); r.className = "radar-row";
              const tm = document.createElement("span"); tm.className = "rr-t mono"; tm.textContent = fmtLocal(g.starts_at);
              const ev = document.createElement("span"); ev.className = "rr-ev"; ev.textContent = eventLabel(g.event);
              const lg = document.createElement("span"); lg.className = "rr-lg mono"; lg.textContent = fmt(g.league);
              const bc = document.createElement("span"); bc.className = "rr-n mono"; bc.textContent = "books " + fmt(g.bookmaker_count);
              const mc = document.createElement("span"); mc.className = "rr-n mono"; mc.textContent = "markets " + fmt(g.market_count);
              const fr = document.createElement("span"); fr.className = "rr-n mono"; fr.textContent = fmtRelAge(g.updated_at || g.last_captured_at);
              r.append(tm, ev, lg, bc, mc, fr);
              if (g.unvalidated === true || g.validated === false) {
                const tag = document.createElement("span"); tag.className = "tag tag-warm tag-dashed"; tag.textContent = "Display-only";
                tag.title = "model not validated — informational only";
                r.appendChild(tag);
                const note = document.createElement("span"); note.className = "rr-n"; note.textContent = "model not validated — informational only";
                r.appendChild(note);
              }
              const linkedPick = g.starts_at != null
                ? pickByEvent.get(String(g.event) + "|" + new Date(g.starts_at).getTime())
                : null;
              if (linkedPick) makeRowOpenPick(r, linkedPick);
              band.appendChild(r);
            });
            if (rows.length === 0) {
              const e = document.createElement("div"); e.className = "edge-group-empty"; e.textContent = "No fixtures in this window.";
              band.appendChild(e);
            }
            box.appendChild(band);
          });
        }

        function fmtDecisions(d) {
          if (!d || typeof d !== "object") return "—";
          const parts = Object.keys(d).sort().map((k) => k.replace(/_/g, " ") + " " + fmt(d[k]));
          return parts.length ? parts.join(" · ") : "—";
        }

        // ===== LAB ============================================================
        // B2: per-sport close-quality breakdown by persisted exclusion reason —
        // rendered straight from /performance (by_sport[*].clv_quality), no new
        // fetch. Feature-detected: sports without reason counts are omitted.
        function renderCloseQualityBySport() {
          const box = $("closeq-sport"); box.replaceChildren();
          const perf = state.perf;
          const p0 = document.createElement("p"); p0.className = "muted";
          if (!perf) { p0.textContent = "Could not load performance data."; box.appendChild(p0); return; }
          const bySport = perf.by_sport || {};
          const rows = Object.keys(bySport).sort().map((k) => {
            const q = bySport[k] && bySport[k].clv_quality;
            return {
              sport: k,
              known: q ? Number(q.n_close_reason_known) || 0 : 0,
              nSettled: q ? q.n_settled : null,
              reasons: (q && q.close_exclusion_reasons) || {},
            };
          }).filter((r) => r.known > 0);
          if (rows.length === 0) { p0.textContent = "No per-sport close-reason data yet."; box.appendChild(p0); return; }
          rows.forEach((r0) => {
            const row = document.createElement("div"); row.className = "kickoff-row";
            const nm = document.createElement("span"); nm.className = "kr-t mono"; nm.textContent = r0.sport.replace(/_/g, " ");
            const kn = document.createElement("span"); kn.className = "kr-s";
            kn.textContent = "reason known " + r0.known + " / " + fmt(r0.nSettled);
            const s = document.createElement("span"); s.className = "kr-s mono";
            s.textContent = Object.keys(r0.reasons)
              .sort((a, b) => Number(r0.reasons[b]) - Number(r0.reasons[a]))
              // Fix 2026-07-10 (Task 4, 2b): the persisted "trusted" stamp only
              // means no exclusion guard tripped at close-write — a looser read
              // than the trusted-sharp subset. Relabel so it cannot be misread
              // against the SLA / evidence-distance trusted counts.
              .map((k) => (k === "trusted" ? "no guard tripped" : k.replace(/_/g, " ")) + " " + r0.reasons[k]).join(" · ") || "—";
            row.append(nm, kn, s); box.appendChild(row);
          });
        }
        // CLOSE/FRESHNESS SLA (audit #8): per sport-market close coverage + SLA
        // verdict — rendered straight from /performance (close_coverage_sla), no
        // new fetch. When coverage is below the SLA the CLV/ROI number is built on
        // thin closing-line coverage, so the CLAIM is flagged unreliable (the
        // claim, not the picks). Report annotation only — nothing is demoted.
        function renderCloseCoverageSla() {
          const box = $("close-sla"); box.replaceChildren();
          const perf = state.perf;
          const p0 = document.createElement("p"); p0.className = "muted";
          if (!perf) { p0.textContent = "Could not load performance data."; box.appendChild(p0); return; }
          const rows = perf.close_coverage_sla || [];
          if (rows.length === 0) { p0.textContent = "No settled picks yet."; box.appendChild(p0); return; }
          // Fix 2026-07-10 #15: the SLA explanation reads ONCE as a legend —
          // every row keeps its data but carries a compact badge instead of
          // repeating the full sentence. Thresholds unchanged.
          const thr0 = rows[0] && rows[0].sla_threshold != null ? fmtPct(rows[0].sla_threshold, 0) : "—";
          const legend = document.createElement("p"); legend.className = "ops-cap";
          legend.textContent = "Below SLA = trusted-close coverage under " + thr0
            + ", so the CLV/ROI CLAIM for that market is CLV unreliable (the claim, not the picks).";
          box.appendChild(legend);
          rows.forEach((r0) => {
            const row = document.createElement("div"); row.className = "kickoff-row";
            const nm = document.createElement("span"); nm.className = "kr-t mono";
            nm.textContent = String(r0.sport).replace(/_/g, " ") + " · " + marketLabel(r0.market);
            const cov = document.createElement("span"); cov.className = "kr-s";
            cov.textContent = "coverage " + fmtPct(r0.close_coverage) + " (trusted "
              + (Number(r0.n_trusted_close) || 0) + " / " + fmt(r0.n_settled) + ")";
            const tag = document.createElement("span");
            const belowSla = !!r0.below_sla;
            const noData = r0.close_coverage === null || r0.close_coverage === undefined;
            tag.className = "tag " + (belowSla ? "tag-warm" : (noData ? "tag-neutral" : "tag-success"));
            tag.textContent = belowSla ? "below SLA" : (noData ? "no settled picks" : "meets SLA");
            row.append(nm, cov, tag); box.appendChild(row);
          });
        }
        // B4: steam shadow-verdict summary — counts + mint-week trend + the
        // trusted-CLV split by verdict, with the SAME min-n discipline as every
        // other aggregate: below the floor it reads "n=X — insufficient",
        // never a point estimate. Monitor-only; nothing is demoted.
        function renderSteamShadow() {
          const box = $("steam-shadow"); box.replaceChildren();
          const perf = state.perf;
          const p0 = document.createElement("p"); p0.className = "muted";
          if (!perf) { p0.textContent = "Could not load performance data."; box.appendChild(p0); return; }
          const ss = perf.steam_shadow;
          if (!ss) { p0.textContent = "Not yet reported."; box.appendChild(p0); return; }
          const ml = document.createElement("div"); ml.className = "metric-list";
          ml.appendChild(metricEl("Would demote", String(ss.would_demote || 0)));
          ml.appendChild(metricEl("Clear", String(ss.clear || 0)));
          ml.appendChild(metricEl("Unevaluated", String(ss.unevaluated || 0)));
          box.appendChild(ml);
          const weekly = ss.weekly || [];
          if (weekly.length > 0) {
            const wk = document.createElement("p"); wk.className = "muted"; wk.style.marginTop = "8px";
            wk.textContent = "Would-demote by mint week: " + weekly.slice(-6).map((w) => {
              const total = (Number(w.would_demote) || 0) + (Number(w.clear) || 0) + (Number(w.unevaluated) || 0);
              return w.week_start + " " + (Number(w.would_demote) || 0) + "/" + total;
            }).join(" · ");
            box.appendChild(wk);
          }
          const split = ss.settled_by_verdict;
          if (split) {
            const sm = document.createElement("div"); sm.className = "metric-list"; sm.style.marginTop = "8px";
            [["Trusted CLV — would demote", split.would_demote], ["Trusted CLV — clear", split.clear]].forEach(([label, agg]) => {
              if (!agg) return;
              const v = agg.sharp_status === "ok" && agg.sharp_stake_weighted_clv_log != null
                ? fmtSignedPct(clvPctFromLog(agg.sharp_stake_weighted_clv_log) / 100)
                : "n=" + (Number(agg.n_sharp_close) || 0) + " — insufficient";
              sm.appendChild(metricEl(label, v));
            });
            box.appendChild(sm);
          }
        }
        function claimRow(can, text) {
          const r = document.createElement("div"); r.className = "claim " + (can ? "can" : "cannot");
          const mk = document.createElement("span"); mk.className = "claim-mk"; mk.textContent = can ? "Can claim" : "Cannot claim yet";
          const t = document.createElement("span"); t.textContent = text;
          r.append(mk, t); return r;
        }
        function renderLab() {
          setCacheNotice("lab-cache-notice", state.perfErr && state.perfLastGoodAt
            ? "Could not refresh performance data — showing last loaded evidence" + lastGoodSuffix(state.perfLastGoodAt) + "."
            : null);
          const perf = state.perf;
          const ledger = $("claims-ledger"); ledger.replaceChildren();
          if (!perf) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Could not load performance data.";
            ledger.appendChild(p0);
          } else {
            const nSharp = Number(perf.n_sharp_close) || 0, floor = Number(perf.min_headline_n) || 50;
            ledger.appendChild(claimRow(perf.sharp_status === "ok",
              perf.sharp_status === "ok"
                ? "Edge vs sharp closes — supported by " + nSharp + " trusted closes (≥ " + floor + " floor)."
                : "Edge vs sharp closes — " + nSharp + " of " + floor + " trusted closes settled; below the floor."));
            ledger.appendChild(claimRow(perf.roi_status === "ok",
              perf.roi_status === "ok" ? "Settled ROI — " + fmtPct(perf.roi) + " on a reportable sample." : "Settled ROI — sample below the reporting floor."));
            const cal = perf.calibration;
            ledger.appendChild(claimRow(!!(cal && !cal.insufficient),
              cal && !cal.insufficient ? "Calibration — the fair-probability monitor is reporting." : "Calibration — sample below the reporting floor."));
          }

          const hero = $("sharp-clv-hero"); hero.replaceChildren();
          if (perf && perf.sharp_status === "ok" && perf.sharp_stake_weighted_clv_log != null) {
            const v = clvPctFromLog(perf.sharp_stake_weighted_clv_log);
            const num = document.createElement("div"); num.className = "hero-num" + (v < 0 ? " neg" : ""); num.textContent = (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
            hero.appendChild(num);
          } else {
            const num = document.createElement("div"); num.className = "hero-num accruing";
            num.textContent = perf ? (Number(perf.n_sharp_close) || 0) + " / " + (Number(perf.min_headline_n) || 50) : "—";
            hero.appendChild(num);
            const sub = document.createElement("p"); sub.className = "hero-sub"; sub.textContent = "Low Evidence — accruing trusted sharp closes.";
            hero.appendChild(sub);
            // Fix 2026-07-10 #14: earn the panel height — progress toward the
            // evidence floor, same values as the figure above.
            if (perf) {
              hero.appendChild(progressBarEl(
                Number(perf.n_sharp_close) || 0,
                Number(perf.min_headline_n) || 50,
                "Trusted sharp closes"));
            }
            // Task 2 (2026-07-11): "when will this move?" — the server-side
            // trusted_close_eta projection (every component nulled honestly).
            const eta = perf && perf.trusted_close_eta;
            if (eta) {
              const bits = [];
              bits.push(eta.projected_days != null
                ? "~" + Math.ceil(Number(eta.projected_days)) + "d to the floor at the current trusted rate"
                : "no honest ETA yet");
              bits.push(eta.trusted_rate != null
                ? "trusted rate " + Math.round(Number(eta.trusted_rate) * 100) + "% of the last " + (Number(eta.n_rate_window) || 0) + " settled"
                : "rate unknown (only " + (Number(eta.n_rate_window) || 0) + " recent settled)");
              bits.push((Number(eta.open_premium) || 0) + " open premium awaiting kickoff");
              const etaP = document.createElement("p"); etaP.className = "muted"; etaP.style.marginTop = "6px";
              etaP.textContent = "ETA: " + bits.join(" · ");
              hero.appendChild(etaP);
            }
          }
          if (perf && perf.stake_weighted_clv_log != null) {
            const ctx = document.createElement("p"); ctx.className = "muted"; ctx.style.marginTop = "10px";
            ctx.textContent = "All-closes CLV (context — not evidence): " + (clvPctFromLog(perf.stake_weighted_clv_log) >= 0 ? "+" : "") + clvPctFromLog(perf.stake_weighted_clv_log).toFixed(2) + "%.";
            hero.appendChild(ctx);
          }

          const cq = $("close-quality"); cq.replaceChildren();
          const q = perf && perf.clv_quality;
          if (!q) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Not yet reported.";
            cq.appendChild(p0);
          } else {
            const ml = document.createElement("div"); ml.className = "metric-list";
            ml.appendChild(metricEl("Tautological Close Excluded", String(q.clv_excluded_tautological || 0)));
            ml.appendChild(metricEl("Circular Close Excluded", String(q.clv_excluded_circular || 0)));
            ml.appendChild(metricEl("Fabricated excluded", String(q.clv_excluded_fabricated || 0)));
            // Fix 2026-07-10 #1: n_snapshot_close and n_fallback_close are two
            // INDEPENDENT counts, never a numerator/denominator — label each.
            ml.appendChild(metricEl("Close provenance", "snapshot " + (q.n_snapshot_close || 0) + " · fallback " + (q.n_fallback_close || 0)));
            ml.appendChild(metricEl("Close age (min)", "p50 " + fmtNum(q.close_age_p50_minutes, 0) + " · p90 " + fmtNum(q.close_age_p90_minutes, 0)));
            cq.appendChild(ml);
            const note = document.createElement("p"); note.className = "muted"; note.style.marginTop = "8px";
            note.textContent = "An excluded close is unusable evidence, not a loss. Close-age provenance accruing.";
            cq.appendChild(note);
            // Task 4 (2026-07-12): close-age histogram per CLOSE anchor source.
            // Age = kickoff − capture time (capture time vs kickoff — a
            // capture-time proxy, not the market's true close). Counts only.
            const hist = perf && perf.close_age_histogram;
            if (hist && hist.by_anchor && Number(hist.n) > 0) {
              const hb = document.createElement("div"); hb.className = "metric-list"; hb.style.marginTop = "10px";
              const buckets = hist.buckets || [];
              Object.keys(hist.by_anchor).sort().forEach((anchor) => {
                const cell = hist.by_anchor[anchor] || {};
                const parts = buckets.map((b) => b + " " + (Number(cell[b]) || 0)).join(" · ");
                hb.appendChild(metricEl("Close age — " + fmt(anchor), parts));
              });
              cq.appendChild(hb);
              const hn = document.createElement("p"); hn.className = "muted"; hn.style.marginTop = "6px";
              hn.textContent = "Close-age buckets per anchor source — age is capture time vs kickoff, "
                + "a capture-time proxy, not the market's true closing instant.";
              cq.appendChild(hn);
            }
          }

          const cs = $("calibration-summary"); cs.replaceChildren();
          const cal = perf && perf.calibration;
          if (!cal || cal.insufficient) {
            const p0 = document.createElement("p"); p0.className = "muted";
            // Fix 2026-07-10 #14: carry the sample size (an existing payload
            // field) so the single-line state earns its panel.
            p0.textContent = "Low Evidence — calibration sample too small."
              + (cal && cal.n != null ? " (n=" + cal.n + ")" : "");
            cs.appendChild(p0);
          } else {
            const ml = document.createElement("div"); ml.className = "metric-list";
            ml.appendChild(metricEl("n in band", String(cal.n)));
            ml.appendChild(metricEl("ECE", fmtNum(cal.ece, 3)));
            ml.appendChild(metricEl("Brier", fmtNum(cal.brier, 3)));
            cs.appendChild(ml);
          }

          const sr = $("sport-readiness"); sr.replaceChildren();
          const bySport = (perf && perf.by_sport) || {};
          const SPORT_STATUS = {
            soccer: ["Validated", "tag-cyan", "premium-eligible under the live gates"],
            basketball: ["Shadow", "tag-warm", "evidence accruing — promotion premature"],
            tennis: ["Shadow", "tag-warm", "settlement convention + capture parser pending"],
            american_football: ["Shadow", "tag-warm", "negligible capture — visibility only"],
          };
          const sportKeys = Object.keys(SPORT_STATUS).filter((k) => k === "soccer" || bySport[k]);
          if (sportKeys.length === 0) {
            const p1 = document.createElement("p"); p1.className = "muted";
            p1.textContent = "No per-sport data yet.";
            sr.appendChild(p1);
          } else {
            sportKeys.forEach((k) => {
              const info = SPORT_STATUS[k];
              const agg = bySport[k] || {};
              const row = document.createElement("div"); row.className = "kickoff-row";
              const nm = document.createElement("span"); nm.className = "kr-t mono"; nm.textContent = k.replace("_", " ");
              const st = document.createElement("span"); st.className = "tag " + info[1]; st.textContent = info[0];
              const note = document.createElement("span"); note.className = "kr-s";
              const n = Number(agg.n_settled);
              note.textContent = (Number.isFinite(n) && n > 0 ? n + " settled · " : "") + info[2];
              row.append(nm, st, note); sr.appendChild(row);
            });
          }

          const tb = $("tier-rows"); tb.replaceChildren();
          if (!perf) {
            const tr = document.createElement("tr"); const c = document.createElement("td"); c.colSpan = 4; c.className = "pending";
            c.textContent = "Could not load performance data."; tr.appendChild(c); tb.appendChild(tr);
          } else {
            [["Premium", perf], ["Shadow", perf.volume || {}]].forEach(([label, agg]) => {
              const tr = document.createElement("tr");
              const c0 = document.createElement("td"); c0.textContent = label; tr.appendChild(c0);
              const c1 = document.createElement("td"); c1.className = "r"; c1.textContent = String(agg.n_settled || 0); tr.appendChild(c1);
              // Fold Asian/quarter-line HALF settlements into the headline W-L
              // so the record reconciles to n_settled. The backend emits all six
              // outcome buckets (won, lost, push, void, half_won, half_lost);
              // dropping the two half buckets silently made W-L understate and
              // NOT sum to n_settled the moment any half settlement lands.
              const won = (Number(agg.won) || 0) + (Number(agg.half_won) || 0);
              const lost = (Number(agg.lost) || 0) + (Number(agg.half_lost) || 0);
              const pv = (Number(agg.push) || 0) + (Number(agg.void) || 0);
              // Integrity guard: displayed segments must partition n_settled. A
              // mismatch means an outcome bucket is unaccounted for — surface it
              // rather than silently drop it (diagnostic only, never throws).
              const nSettled = Number(agg.n_settled) || 0;
              if (won + lost + pv !== nSettled) {
                console.warn("tier record segments do not reconcile to n_settled", label, { won, lost, pv, nSettled });
              }
              const c2 = document.createElement("td"); c2.className = "r"; c2.textContent = won + "-" + lost + "-" + pv; tr.appendChild(c2);
              const c3 = document.createElement("td"); c3.className = "r pending"; c3.textContent = agg.roi_status === "ok" ? fmtPct(agg.roi) : "insufficient sample"; tr.appendChild(c3);
              tb.appendChild(tr);
            });
          }

          // Task 4 (2026-07-10): trusted-CLV-first scorecard — per-tier trusted
          // CLV with 95% CI + n, the CLV→yield calibration ratio vs the
          // RebelBetting public 0.8× benchmark, and the plain-language evidence
          // verdict. Reads straight from /performance (live_evidence.trusted_clv_ci
          // / clv_yield_ratio / evidence_verdict); estimates arrive nulled at the
          // source below the honesty floor, so the render only mirrors the state.
          const scv = $("tier-scorecard"); scv.replaceChildren();
          const le = perf && perf.live_evidence;
          if (!le || !le.trusted_clv_ci) {
            const p0 = document.createElement("p"); p0.className = "muted";
            p0.textContent = "Trusted-CLV scorecard not yet reported.";
            scv.appendChild(p0);
          } else {
            const ml = document.createElement("div"); ml.className = "metric-list"; ml.style.marginTop = "8px";
            const tierName = { premium: "Premium", volume: "Shadow" };
            // ONE shared CI-entry formatter: tier rows and the ADR-0022 cohort
            // rows render identically (estimates arrive nulled below the floor).
            const ciEntryText = (e) =>
              e.mean_clv_log != null && e.ci_low != null && e.ci_high != null
                ? fmtSignedPct(clvPctFromLog(e.mean_clv_log) / 100)
                  + " (95% CI " + fmtSignedPct(clvPctFromLog(e.ci_low) / 100)
                  + " … " + fmtSignedPct(clvPctFromLog(e.ci_high) / 100)
                  + ", n=" + (Number(e.n) || 0) + ")"
                : "n=" + (Number(e.n) || 0) + " — insufficient";
            const tiers = le.trusted_clv_ci.by_tier || {};
            const tierKeys = Object.keys(tiers).sort();
            if (tierKeys.length === 0) {
              ml.appendChild(metricEl("Trusted CLV", "n=0 — insufficient"));
            }
            tierKeys.forEach((k) => {
              ml.appendChild(metricEl("Trusted CLV — " + (tierName[k] || k), ciEntryText(tiers[k] || {})));
            });
            // ADR-0022 crit 3/4: the premium tier split into pre-/post-
            // selection-fix mint cohorts (boundary 2026-07-07T00:00:00Z).
            const cohorts = le.trusted_clv_ci.premium_cohorts || {};
            [["pre_fix", "Premium (pre-fix, minted < 2026-07-07)"],
             ["post_fix", "Premium (post-fix, minted ≥ 2026-07-07)"]].forEach(([k, label]) => {
              if (!cohorts[k]) return;
              ml.appendChild(metricEl("Trusted CLV — " + label, ciEntryText(cohorts[k])));
            });
            const yr = le.clv_yield_ratio;
            const rv = yr && yr.ratio != null
              ? fmtNum(yr.ratio, 2) + "× (benchmark 0.8×)"
              : "not computable — below floor or trusted CLV ≈ 0";
            ml.appendChild(metricEl("CLV→yield ratio", rv));
            // Task 8 probe: Monte Carlo zero-edge null (Buchdahl MCoB) — how
            // often pure luck at the offered prices does at least this well.
            const mc = le.mc_null;
            if (mc) {
              ml.appendChild(metricEl("Luck probe",
                mc.p_luck != null
                  ? "Record vs zero-edge null: p = " + Number(mc.p_luck).toFixed(2)
                    + " (n=" + (Number(mc.n) || 0) + ", " + (Number(mc.sims) || 0) + " resamples)"
                  : "Record vs zero-edge null: n=" + (Number(mc.n) || 0) + " — insufficient"));
            }
            scv.appendChild(ml);
            const verdict = document.createElement("p"); verdict.className = "muted"; verdict.style.marginTop = "8px";
            verdict.textContent = "Verdict: " + fmt(le.evidence_verdict);
            scv.appendChild(verdict);
            // Task 3 (2026-07-12): ADR-0022 crit-3 kill/keep gate — the post-fix
            // premium cohort's progress toward the n>=50 trusted-close floor.
            // The PROGRESS 95% CI (progress_ci_low/progress_ci_high) is sent by
            // the server only once n>=10, so we only show it when present.
            const postFix = (le.trusted_clv_ci.premium_cohorts || {}).post_fix;
            if (postFix) {
              const kg = document.createElement("p"); kg.className = "muted"; kg.style.marginTop = "6px";
              const needed = 50;
              let kgTxt = "Kill/keep gate: post-fix premium trusted closes "
                + (Number(postFix.n) || 0) + " / " + needed;
              if (postFix.progress_ci_low != null && postFix.progress_ci_high != null) {
                kgTxt += " · progress 95% CI "
                  + fmtSignedPct(clvPctFromLog(postFix.progress_ci_low) / 100) + " … "
                  + fmtSignedPct(clvPctFromLog(postFix.progress_ci_high) / 100);
              }
              kg.textContent = kgTxt;
              scv.appendChild(kg);
            }
            // Task 2 (2026-07-12): ADR-0022 crit-5 uncertainty-shrink 30-day
            // shadow review — one muted line (estimates nulled below n=10).
            const sr = perf && perf.shrink_review;
            if (sr) {
              const srp = document.createElement("p"); srp.className = "muted"; srp.style.marginTop = "6px";
              srp.textContent = "Shrink shadow review: " + (Number(sr.n_annotated) || 0)
                + " annotated · due " + fmt(sr.review_due);
              scv.appendChild(srp);
            }
          }

          renderCloseQualityBySport();
          renderCloseCoverageSla();
          renderSteamShadow();
          renderPromotionDistance();
          renderBankroll();
        }

        // ===== SOURCES ============================================================
        function sourceRow(name, freshness, coverage, verdict, notes, verdictClass) {
          // Fix 2026-07-10 #7: data-label per cell feeds the mobile stacked-
          // card layout (labels from the column headers, Notes included).
          const tr = document.createElement("tr");
          const c0 = document.createElement("td"); c0.dataset.label = "Source"; c0.textContent = name; tr.appendChild(c0);
          const c1 = document.createElement("td"); c1.dataset.label = "Freshness"; c1.textContent = freshness; tr.appendChild(c1);
          const c2 = document.createElement("td"); c2.dataset.label = "Coverage"; c2.textContent = coverage; tr.appendChild(c2);
          const c3 = document.createElement("td"); c3.dataset.label = "Verdict";
          const tag = document.createElement("span"); tag.className = "tag " + (verdictClass || "tag-neutral"); tag.textContent = verdict;
          c3.appendChild(tag); tr.appendChild(c3);
          const c4 = document.createElement("td"); c4.dataset.label = "Notes"; c4.textContent = notes; tr.appendChild(c4);
          return tr;
        }
        // Task F: honest proxy-pool semantics used by BOTH the Sources source
        // matrix and the dedicated proxy tile + the Today health strip.
        //   GREEN  = healthy AND spare capacity (headroom > 0).
        //   AMBER  = healthy but headroom <= 0 (a capacity hint, not a failure).
        //   RED    = verdict degraded, OR dead > 0, OR any slot quarantined/half-open.
        function proxyBadSlots(pool) {
          return ((pool && pool.slots) || []).filter(
            (s) => s.state === "quarantined" || s.state === "half_open"
          );
        }
        function classifyProxyPool(pool) {
          if (!pool) return { level: "warn", label: "Unknown", detail: "Not loaded yet." };
          const dead = Number(pool.dead) || 0;
          const degraded = pool.verdict === "Proxy pool degraded";
          const bad = proxyBadSlots(pool);
          const headroom = typeof pool.headroom === "number" ? pool.headroom : null;
          if (degraded || dead > 0 || bad.length > 0) {
            return {
              level: "bad",
              label: "Degraded",
              detail: "Pool degraded — scraping is slower, but no pick is minted from a stale price (the mint-time gate drops it). A live pick whose price later ages out is flagged Stale and leaves Qualified now until it re-prices.",
            };
          }
          if (headroom !== null && headroom <= 0) {
            return {
              level: "warn",
              label: "No spare capacity",
              detail: "Pool healthy, but no spare proxies above the " + fmt(pool.concurrency_floor) + " concurrent fetchers — add more to speed up scraping.",
            };
          }
          return { level: "ok", label: "Healthy", detail: "Healthy, with spare proxy capacity." };
        }
        function proxyTagClass(level) {
          return level === "ok" ? "tag-success" : level === "warn" ? "tag-warm" : "tag-danger";
        }
        function renderProxyRow(pool) {
          if (!pool) return sourceRow("Proxy pool", "—", "—", "—", "Not loaded yet.", "tag-neutral");
          const c = classifyProxyPool(pool);
          return sourceRow(
            "Proxy pool",
            "healthy " + fmt(pool.healthy) + " / " + fmt(pool.configured),
            "dead " + fmt(pool.dead) + " · quarantined " + fmt(pool.quarantined),
            c.label,
            c.detail,
            proxyTagClass(c.level)
          );
        }
        function renderProxyHealth(pool) {
          const box = $("proxy-health"); box.replaceChildren();
          const badge = $("proxyh-verdict");
          if (!pool) {
            badge.textContent = "—"; badge.className = "tag tag-neutral";
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "Not loaded yet.";
            box.appendChild(p0); return;
          }
          const c = classifyProxyPool(pool);
          badge.textContent = c.label; badge.className = "tag " + proxyTagClass(c.level);
          const stats = document.createElement("div"); stats.className = "proxy-stats";
          [["Configured", pool.configured, false], ["Healthy", pool.healthy, false],
           ["Dead", pool.dead, true], ["Quarantined", pool.quarantined, false]].forEach(([k, v, negIfPos]) => {
            const cell = document.createElement("div"); cell.className = "proxy-stat";
            const sv = document.createElement("div"); sv.className = "psv" + (negIfPos && Number(v) > 0 ? " neg" : "");
            sv.textContent = fmt(v);
            const sk = document.createElement("div"); sk.className = "psk"; sk.textContent = k;
            cell.append(sv, sk); stats.appendChild(cell);
          });
          box.appendChild(stats);
          const note = document.createElement("p"); note.className = "muted"; note.style.marginTop = "8px";
          note.textContent = c.detail; box.appendChild(note);
          if (typeof pool.headroom === "number") {
            const hr = document.createElement("p"); hr.className = "muted";
            hr.textContent = "Spare capacity over " + fmt(pool.concurrency_floor) + " concurrent fetchers: " +
              (pool.headroom > 0 ? "+" + pool.headroom : String(pool.headroom)) + ".";
            box.appendChild(hr);
          }
          if (pool.failovers_1h != null) {
            const fo = document.createElement("p"); fo.className = "muted";
            fo.textContent = "Failovers: " + fmt(pool.failovers_15m) + " in 15m · " + fmt(pool.failovers_1h) + " in 1h" +
              (pool.dominant_failure_class ? " · most common: " + pool.dominant_failure_class : "");
            box.appendChild(fo);
          }
          const bad = proxyBadSlots(pool);
          if (bad.length > 0) {
            const h = document.createElement("div"); h.className = "eyebrow"; h.style.marginTop = "10px";
            h.textContent = "Dead / quarantined slots"; box.appendChild(h);
            bad.forEach((s) => {
              const r = document.createElement("div"); r.className = "kickoff-row";
              const nm = document.createElement("span"); nm.className = "kr-t mono"; nm.textContent = "slot " + fmt(s.index);
              const st = document.createElement("span"); st.className = "tag tag-danger"; st.textContent = String(s.state).replace(/_/g, " ");
              const de = document.createElement("span"); de.className = "kr-s mono";
              de.textContent = (s.last_error_class || "—") + " · fails " + fmt(s.consecutive_failures) + " · " +
                (s.last_success_at ? "last ok " + fmtRelAge(s.last_success_at) : "never ok");
              r.append(nm, st, de); box.appendChild(r);
            });
          }
        }
        function renderSources() {
          loadMatchRate();
          const sourceCacheMessages = [];
          if (state.healthErr && state.healthLastGoodAt) sourceCacheMessages.push(
            "health " + lastGoodSuffix(state.healthLastGoodAt).replace(/^ \(|\)$/g, "")
          );
          if (state.gamesErr && state.gamesLastGoodAt) sourceCacheMessages.push(
            "fixtures " + lastGoodSuffix(state.gamesLastGoodAt).replace(/^ \(|\)$/g, "")
          );
          if (state.matchRateErr && state.matchRate) sourceCacheMessages.push("coverage cache");
          setCacheNotice("sources-cache-notice", sourceCacheMessages.length
            ? "Could not refresh " + sourceCacheMessages.join(" and ") + " — cached operations data is stale."
            : null);
          const health = state.health;
          const tb = $("source-rows"); tb.replaceChildren();
          const pollAge = health && health.newest_poll_age_seconds != null ? fmtNum(health.newest_poll_age_seconds / 60, 1) + "m" : "—";
          const oddsSrc = (health && health.odds_source) || null;
          const oddsSrcLabel = oddsSrc === "oddschecker" ? "OddsChecker scrape"
            : oddsSrc === "oddsportal" ? "OddsPortal scrape"
            : oddsSrc === "odds_api" ? "The Odds API" : "Odds feed";
          const sourceTrusted = healthIsTrusted(health) && state.gamesErr === null && !gamesPendingWithoutCache();
          const sourceFixtures = gamesPendingWithoutCache() ? "Loading fixtures…" :
            gamesUnavailableWithoutCache() ? "—" : String(state.games.length) + (state.gamesErr ? " cached fixtures" : " fixtures");
          const sourceStatus = gamesPendingWithoutCache() ? "Pending" : sourceTrusted ? "Nominal" : "Source Degraded";
          tb.appendChild(sourceRow(oddsSrcLabel, pollAge, sourceFixtures, sourceStatus, "Live odds feed — active provider: " + (oddsSrc || "—") + ".", sourceTrusted ? "tag-success" : gamesPendingWithoutCache() ? "tag-neutral" : "tag-danger"));

          const mr = state.matchRate;
          // 2026-07-12: coverage cells show SLATE coverage "X% (sharp/soft)" —
          // sharp-priced events over soft-scraped events, denominator visible.
          const slateCov = mr && mr.slate_sharp_coverage;
          const slateCell = (num, den, rate) =>
            slateCov && rate != null && den != null
              ? fmtPct(rate) + " (" + (Number(num) || 0) + "/" + (Number(den) || 0) + ")"
              : (mr ? "n/a (no soft slate)" : (state.matchRateErr ? "Could not load coverage." : "Computing coverage… (~15s)"));
          // Task F 2026-07-26 #3: the verdict chip must follow the coverage
          // rate — "Nominal" over "0.0% (0/14)" was a contradiction. 0% =
          // DARK (alert), <20% = Low Coverage (warn); no measurable rate
          // keeps the prior Nominal/Pending labels.
          const covVerdict = (rate) => {
            if (!mr) return ["Pending", "tag-neutral"];
            const r = numOf(rate);
            if (!isFinite(r)) return ["Nominal", "tag-success"];
            if (r <= 0) return ["DARK", "tag-danger"];
            if (r < 0.2) return ["Low Coverage", "tag-warm"];
            return ["Nominal", "tag-success"];
          };
          const pinRate = slateCell(slateCov && slateCov.pinnacle_events, slateCov && slateCov.soft_events, slateCov && slateCov.pinnacle_rate);
          const pinV = covVerdict(slateCov && slateCov.pinnacle_rate);
          tb.appendChild(sourceRow("Pinnacle ARCADIA", "—", pinRate, pinV[0], "Sharp-close archive · share of soft-scraped events also priced by Pinnacle.", pinV[1]));

          const bfRate = slateCell(slateCov && slateCov.betfair_events, slateCov && slateCov.soft_events, slateCov && slateCov.betfair_rate);
          const bfSrc = (health && health.betfair_source) || "Betfair EXCHANGE share of soft-scraped events (Sportsbook is soft, not counted).";
          const bfV = covVerdict(slateCov && slateCov.betfair_rate);
          tb.appendChild(sourceRow("Betfair Exchange", "—", bfRate, bfV[0], bfSrc, bfV[1]));

          const stale = mr && mr.betfair_staleness;
          tb.appendChild(sourceRow("Betfair API", "—", "—", "Monitor-only", stale ? "fresh decisions — " + fmtDecisions(stale.fresh_decisions) : "Monitor-only — not a pick-feeding read.", "tag-neutral"));

          tb.appendChild(renderProxyRow(health && health.proxy_pool));

          const links = mr && mr.links;
          tb.appendChild(sourceRow("Review queues", "—", links ? String(links.auto_linked) + " linked" : "—", links ? (links.weak_links > 0 ? "Low Evidence" : "Nominal") : "Pending", links ? links.review_queued + " queued · " + links.weak_links + " weak links" : "Not loaded yet.", links ? "tag-success" : "tag-neutral"));

          renderProxyHealth(health && health.proxy_pool);

          const staleBox = $("staleness-monitor"); staleBox.replaceChildren();
          if (!stale) {
            staleBox.textContent = state.matchRateErr ? "Could not load the staleness monitor." : "Computing coverage… (~15s)";
          } else {
            const cap = document.createElement("p"); cap.className = "ops-cap";
            cap.textContent = "Monitor-only check on the Betfair price freshness guard — how many inline prices were fresh, demoted as stale, or had no matching live API price. Not a pick-feeding read.";
            staleBox.appendChild(cap);
            const ml = document.createElement("div"); ml.className = "metric-list ops-list";
            const fd = stale.fresh_decisions || {};
            const DEC = { pass: "Passed (fresh)", demote: "Demoted (stale)", no_api_match: "No live API match", no_api_price: "No live API price" };
            Object.keys(fd).sort().forEach((k) => ml.appendChild(metricEl(DEC[k] || k.replace(/_/g, " "), String(fd[k]))));
            ml.appendChild(metricEl("Stale rows in window", fmt(stale.stale_rows)));
            staleBox.appendChild(ml);
          }

          const rq = $("review-queues"); rq.replaceChildren();
          if (!links) {
            rq.textContent = state.matchRateErr ? "Could not load review queues." : "Computing coverage… (~15s)";
          } else {
            const cap = document.createElement("p"); cap.className = "ops-cap";
            cap.textContent = "Cross-source event links. Auto-linked matched automatically; in-review is held for human triage; weak links matched below 0.95 confidence.";
            rq.appendChild(cap);
            const ml = document.createElement("div"); ml.className = "metric-list ops-list";
            ml.appendChild(metricEl("Auto-linked", fmt(links.auto_linked)));
            ml.appendChild(metricEl("In review queue", fmt(links.review_queued)));
            ml.appendChild(metricEl("Weak links (< 0.95)", fmt(links.weak_links)));
            rq.appendChild(ml);
          }

          const pmBox = $("per-market-summary"); pmBox.replaceChildren();
          const polls = (health && health.polls) || {};
          Object.keys(polls).sort().forEach((sport) => {
            const pm = polls[sport].per_market || {};
            const entries = Object.entries(pm).sort((a, b) => Number(b[1]) - Number(a[1])).slice(0, 5);
            const row = document.createElement("div"); row.className = "kickoff-row";
            const t = document.createElement("span"); t.className = "kr-t mono"; t.textContent = sport;
            const s = document.createElement("span"); s.className = "kr-s"; s.textContent = entries.map((e) => marketLabel(e[0]) + ": " + e[1]).join(" · ") || "—";
            row.append(t, s); pmBox.appendChild(row);
          });
          if (Object.keys(polls).length === 0) {
            const p0 = document.createElement("p"); p0.className = "muted"; p0.textContent = "No completed poll cycle yet.";
            pmBox.appendChild(p0);
          }

          const perf = state.perf;
          const h2 = $("h2-readiness");
          h2.textContent = perf && perf.sharp_status === "ok"
            ? "Ready to review — enough trusted sharp closes have settled to look at H2 validation. This is an informational readiness note, not an automated trigger."
            : "Not ready — still accruing trusted sharp closes before H2 validation can be reviewed.";
        }

        // ===== hash routing + nav wiring ======================================
        function parseHash() {
          const h = (location.hash || "").replace(/^#\/?/, "");
          const parts = h.split("/").filter(Boolean);
          return { view: parts[0] || "today", id: parts[1] || null };
        }
        function syncDrawerFromRoute(shouldFocus) {
          const route = parseHash();
          if (route.view !== "edges" || !route.id) {
            const hadSelection = selectedId !== null;
            selectedId = null;
            if (state.coreLoaded && hadSelection) renderEdgesList();
            closeSheet(Boolean(shouldFocus));
            return;
          }
          selectedId = String(route.id);
          if (!state.coreLoaded) {
            closeSheet(false);
            return;
          }
          if (!currentPick()) {
            closeSheet(drawerOpenerKey !== null);
            // Only normalize a genuinely missing bookmark after both pick
            // tiers loaded successfully. A partial outage may hide the row.
            if (state.premiumErr === null && state.volumeErr === null) {
              selectedId = null;
              history.replaceState(null, "", "#/edges");
              renderEdgesList();
            }
            return;
          }
          renderEdgesList();
          renderEdgeDetail();
          openSheetIfMobile(shouldFocus);
        }
        function setView(view) {
          if (VIEW_KEYS.indexOf(view) === -1) view = "today";
          activeView = view;
          if (view !== "edges" || selectedId == null) closeSheet(false);
          VIEW_KEYS.forEach((k) => { $("view-" + k).hidden = k !== view; });
          document.querySelectorAll("#rail button[data-view], #dock button[data-view]").forEach((b) => {
            b.setAttribute("aria-current", String(b.dataset.view === view));
          });
          if (location.hash.replace(/^#\/?/, "").split("/")[0] !== view) location.hash = "#/" + view;
          if (view === "radar") renderRadar();
          if (view === "sources") renderSources();
          if (view === "lab") { loadPromotionDistance(); loadBankroll(); }
          window.scrollTo(0, 0);
        }
        document.querySelectorAll("#rail button[data-view], #dock button[data-view]").forEach((b) =>
          b.addEventListener("click", () => setView(b.dataset.view)));
        window.addEventListener("hashchange", () => {
          const route = parseHash();
          if (route.view === "edges" && route.id) selectedId = route.id;
          setView(route.view);
          syncDrawerFromRoute(true);
        });

        function operatorIsEditingResult() {
          const detail = $("edge-detail");
          if (detail.getAttribute("aria-hidden") !== "false" || !detail.classList.contains("open")) {
            return false;
          }
          const form = detail.querySelector(".result-form");
          const active = document.activeElement;
          return !!(form && (form.dataset.dirty === "true" || form.contains(active)));
        }

        // ===== boot ============================================================
        const boot = parseHash();
        selectedId = boot.view === "edges" ? boot.id : null;
        setView(boot.view);
        setInterval(() => {
          if (document.hidden || !state.picks.length) return;
          rerenderLiveViewsPreservingFocus();
        }, 30000);
        setInterval(() => {
          if (document.hidden || operatorIsEditingResult()) return;
          load();
        }, 60000);
        document.addEventListener("visibilitychange", () => {
          if (document.hidden) return;
          if (state.picks.length) rerenderLiveViewsPreservingFocus();
          if (!operatorIsEditingResult()) load();
        });
        load();

        if ("serviceWorker" in navigator) {
          window.addEventListener("load", () => { navigator.serviceWorker.register("/sw.js").catch(() => {}); });
        }
      })();
