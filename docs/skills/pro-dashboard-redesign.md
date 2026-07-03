# Pro Dashboard Redesign — working design system + QA checklist

Governs the 2026-07-03 from-scratch rebuild of `app/api/dashboard.html`.
This is the checklist the build is verified against — not documentation
theater. Written with the `frontend-design` plugin skill process (brainstorm →
plan → critique → build → critique).

## Product thesis

A **decision terminal for one quant operator**: its single job is to answer
"what deserves money right now, and how much can I trust it" in under ten
seconds — with evidence honesty as the brand. It is a trading/research
instrument, not a betting promo page. Everything actionable must justify
itself; everything tracked-but-not-actionable must *look* tracked.

## Design concept: "the ledger terminal"

Signature element (the one memorable thing): the **evidence spine** — a
segmented rail on the left edge of every pick (and reused horizontally as the
trust meter in Evidence). Its four segments physically encode the evidence
chain: **anchor → freshness → match → close**. A full amber spine = premium,
fully evidenced. Broken/hollow segments show exactly which link is weak.
Trust is structural, not a badge sprinkle.

Second structural device: a monospace UTC **system line** at the very top
(terminal status line): one line, dot + verdict + data age. Everything else
stays quiet.

## Tokens

Palette (dark instrument panel — deep ink-blue, never pure black; explicitly
NOT the near-black + acid-green AI default, NOT casino neon):

| token | hex | role |
|---|---|---|
| `--ink` | `#0B1220` | page background |
| `--panel` | `#111C30` | surface |
| `--raised` | `#182740` | raised cards / rows |
| `--line` | `#24344F` | hairlines/borders |
| `--text` | `#DCE6F5` | primary text |
| `--muted` | `#8CA0BE` | secondary text |
| `--signal` | `#E8B34B` | the priced edge — premium accent, used sparingly |
| `--trust` | `#4FD1A1` | positive/settled-won/healthy |
| `--risk` | `#E06C75` | negative/lost/degraded |
| `--info` | `#6FA8DC` | neutral-informational (monitor-only, pending) |

Type (self-contained: system stacks only; personality comes from treatment):
- Display/UI: `system-ui` — weight discipline (600/700 headings, 400 body).
- Data voice: `ui-monospace, "SF Mono", Consolas, monospace` for ALL numbers,
  odds, times, ids; `font-variant-numeric: tabular-nums` globally.
- Eyebrows/labels: 11px, letterspaced 0.08em, uppercase, `--muted`.
- Body ≥ 14px mobile, 13px allowed only in dense desktop tables.

Motion: one orchestrated moment only — view-switch fade/slide (120ms) and
count-up on Overview tiles. `prefers-reduced-motion` disables both.

## Information architecture

Mobile (bottom nav, 5): **Overview · Picks · Games · Evidence · Ops**.
Compact system line on top (single row, sticky). No wasted header.

Desktop (≥1024px): left sidebar (nav + system line details), wide content
workspaces; Picks = dense table with expandable evidence drawer; Overview =
command deck grid.

Overview answers: healthy? fresh? premium available? evidence conclusive?
sources ok? what needs attention? → "Attention queue" panel is derived, never
decorative (empty = "Nothing needs attention").

## State vocabulary (all must render, none color-only — icon/text always)

Premium · Shadow (label "Tracked — not actionable") · Pending · Settled ·
Won/Lost/Push/Void · CLV Pending · CLV Updated · Stale · Display-only ·
Weak Match · Sharp Anchor · Consensus Anchor · Missing Anchor · Trusted CLV ·
Untrusted Close · Tautological Close Excluded · Circular Close Excluded ·
Low Evidence · Monitor-only.

Hard rule: Shadow/stale/weak/missing-anchor/display-only/untrusted must never
share Premium's amber. They render desaturated with explicit non-actionable
copy.

## Copy rules

Plain verbs, sentence case. Never: "lock", "guaranteed", "sure", "easy
money", "best bet". Missing value = `—` (never undefined/null/NaN). Empty
states direct ("No picks in this window. The next poll runs in ~Nm."). Errors
say what happened + what to do; no stack traces, no secrets, no proxy URLs.

## API discipline (verify per call)

path · method · auth · fields read · error shape · null handling · empty
state · loading state. Slow endpoint (`/resolution/match-rate`) loads lazily
in Ops only, never blocks Overview. `/health` is the eager source.

## QA gates (run before ship)

- [ ] Python tests green incl. dashboard content tests (IDs kept/updated)
- [ ] JS syntax: extracted `<script>` passes `node --check`
- [ ] Viewports 360/390/430/768/1280: no horizontal scroll of core pick info
- [ ] Touch targets ≥ 44px (nav, filters, expanders)
- [ ] Keyboard: tab order, visible `:focus-visible`, ESC closes drawers
- [ ] Headings ordered; nav has aria-labels; badges have text not just color
- [ ] Contrast: text ≥ 4.5:1 on its surface (muted #8CA0BE on #111C30 ok)
- [ ] No undefined/null/NaN in any rendered string path
- [ ] Secrets scan: no token/apiKey/authorization/proxy URL in payload paths
- [ ] Safety copy: stakes labeled informational; no execution affordances
