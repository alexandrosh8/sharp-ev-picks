# Whole-Internet Betting Research — Beyond GitHub (2026-07-10)

**Scope:** everything except GitHub repos (already covered by prior sweeps): professional
bettors' documented practice, the book canon, community intelligence 2024–26, the
commercial tooling landscape, fresh 2024–26 academia, non-GitHub methodology (Kaggle/
blogs/talks), and X (Twitter) discourse. **Method:** 7 parallel research agents over
Tavily/exa web search, each reading 5–10 sources in full; every claim carries a URL and
an established-vs-folklore label inside its lane briefing below.

**Signal-quality caveat (consistent across lanes):** the open web around betting is
heavily polluted — affiliate funnels (OddsJam/Outlier codes in ~90% of YouTube "EV"
content), Kalshi/Polymarket badge spam on X since Dec 2025, SEO lead magnets. The
high-signal residue: r/algobetting, arbusers.com, Circles Off transcripts, Unabated and
Pinnacle article archives, named practitioner blogs, regulator records (Massachusetts
Gaming Commission), and peer-reviewed 2024–26 work (esp. the Whelan/UCD corpus).

---

## Executive Synthesis — what the whole internet adds beyond the repos

**1. Execution is the moat; everything else is table stakes.** Every documented
professional (Spanky, Walters, Bloom/Starlizard, Benham, Voulgaris) built a
beard/broker/partner network as the core of the business — Benham: a model is worthless
"if you're just going into High Street bookies… they close you down." The limit
cascade is now quantified from three independent directions: practitioner tracking
(MGM 2 weeks, FanDuel 4 weeks, Caesars 2 months; Pinnacle-class 6+ months unlimited),
regulator data (MGC: 0.64% of accounts limited, ~58% cut to 1–24% of default stakes),
and an on-record operator admission that detection keys on bet-shape, not P&L
(Fanatics: ~half of limited accounts were net losers). A Massachusetts public comment
even names CLV explicitly as the trigger.

**2. Independent convergence on the sharp-anchor architecture.** The 2026 Kaggle March
Madness 3rd place blended market prices at 90% weight and the 1st place's stated top
regret was not using them; Circles Off's practitioner taxonomy calls top-down
price-taking the only realistic solo path ("pretty much no beginner is succeeding at
origination"); Unabated's flagship product IS a devigged weighted sharp blend. The
origination exception (Starlizard/Smartodds/Peabody) is a syndicate-scale game with
staff and execution networks. This triangulates the platform's exact architecture from
practitioners, Kagglers, and commercial products independently.

**3. The CLV doctrine has matured from "does it matter" to "domain of validity."**
Its own high priests now bound it: valid only where a true market-maker exists
(Pinnacle/Circa-listed), only devigged, weighted by when/why the line moved —
"CLV doesn't mean anything in props" (Captain Jack Andrews). The one public
large-sample calibration point: RebelBetting's 373,654-bet month showing realized
yield ≈ 0.8× measured CLV (+3.3% CLV → +2.7% yield). And the sharpest venue insight
from fresh academia: on Betfair, MAKERS earn +0.6%…+2.0% while TAKERS lose −2.2%…−2.5%
across ~3M identified bets (Whelan 2025) — crossing the spread is the cost that eats
marginal edges.

**4. Fresh 2024–26 academia rewrites three assumptions.** (a) Shin's insider parameter
z mostly measures bookmaker margin, not informed money (Whelan; independent booksum
null) — don't over-interpret Shin devig. (b) All FLB-correcting devigs (Shin variants,
power, new OO-EPC) systematically underestimate DRAW frequency in 1X2 — a testable QC
probe. (c) In-play prices don't leak goals beyond observable xG, and Betfair absorbs
goal news within ~2 minutes — the only in-play edge is faster event data, not price
patterns. Prediction markets are already HFT-efficient at top-of-book (7 executable
single-market arbs in a month of Polymarket NBA) but combinatorial mispricings persist
(~2/game).

**5. Where practitioners say edges persist (2025–26) — and what died.** Persisting:
prop/niche origination (no market-maker to copy), live betting (commercial money
agrees: BetBurger prices live access at 3.5× prematch), cross-venue prediction-market
divergence (Kalshi oscillates softer/sharper than books), maker-side exchange posture,
and speed-to-news. Died/dying: naive top-down scanner EV (tools commoditized —
"table stakes"), the mechanical Wong teaser (repriced), novelty props (killed because
only sharps bet them), Betfair liquidity (declining yearly; Premium Charge → Expert
Fee 20–40% above £25k/£100k), and the free public Pinnacle API (off 2025-07-23 —
aggregator-shown Pinnacle prices are now delayed; this platform's own Arcadia capture
is unaffected and is the correct posture).

**6. Sizing practice vs theory.** No documented professional runs textbook Kelly:
Walters used 1–3% edge-scaled with a hard cap; Voulgaris a probability floor; Andrews
argues "getting the money down trumps knowing the precise size." Fractional-Kelly-as-
norm is folklore with strong circumstantial support. Buchalter's "Theory of
Everything" (fractional Kelly + regression-to-market + CLV as one Bayesian
parameter-uncertainty story) is the best free unification found.

### Consolidated top actionable ideas (full lists in each lane)

1. **Anchor-quality weighting:** demote picks where edge is large AND the anchor is
   thin (arbusers: where Pinnacle's own limit is low, "edge vs Pinnacle" is usually
   fake); time-gate lower-league anchors to near-kickoff.
2. **Per-sport/market CLV-trust taxonomy** (Unabated doctrine) — codify CLV-meaningful
   vs CLV-unreliable markets in the trust layer; never grade props strategies on CLV.
3. **CLV→yield calibration stat**: report the platform's own realized-yield/CLV ratio
   against RebelBetting's public 0.8× benchmark.
4. **Draw-frequency devig probe** (shadow-only): test whether the shipped devig
   underestimates draws, per the 2026 arXiv result.
5. **Get-downability + latency telemetry**: per-pick executability score and per-book
   lag-behind-anchor league table (Spanky's top-down core, Andrews' speed>sizing).
6. **Maker-not-taker ticket copy** for exchange-informed picks (Whelan's +0.6/−2.5
   asymmetry; taker penalty concentrates in longshots — reinforces the odds ceiling).
7. **Monte Carlo record simulator + luck-vs-skill p-value** on the settled ledger
   (Buchdahl's Black Cat/MCoB machinery) beside the CLV CIs.
8. **Picks-per-eligible-events alarm** (Buchalter's bet-volume smoke detector): a
   strategy firing on too much of a liquid market is wrong, not the market.
9. **Two-stage regress-to-market** (Peabody): market-free model, then measured
   shrinkage toward the anchor weighted by demonstrated "power relative to market".
10. **Monitor the MA limit-notice corpus** (48h individualized explanations, live
    since 2026-06-01) — the first public dataset of book-stated limiting rationales.

---

# Professional Bettors: How They Actually Operate (2020–2026) — Research Briefing

## 1. Edge sourcing: origination vs. top-down price-taking

**Established.** The practitioner community itself now uses a three-way taxonomy, stated explicitly by Rob Pizzola and Johnny (betstamp) on **Circles Off Ep. 186** (Dec 2024): (1) *top-down* — trust a sharper book's number as truth and hit lagging books; (2) *origination* — build your own number ("extremely hard... pretty much no beginner is succeeding at that right now"); (3) leveraging tools/promos/boosts. Transcript: https://www.betstamp.com/education/circles-off-episode-186-how-to-actually-start-winning-at-sports

**Originators (documented):**
- **Rufus Peabody** (golf/NFL props, Massey-Peabody): "I'm basically making the market... you use the market as your foundation" — Wharton Moneyball, 2025 (https://www.youtube.com/watch?v=M1T0OlG3XEU). He describes the job as "akin to being a portfolio manager of a quantitative hedge fund... thin margins," has bet ~$1B on golf lifetime, and started at $25k/yr working for Vegas oddsmakers (Normal Sport Q&A No. 137: https://www.normalsport.com/newsletter/q-a-the-man-who-bet-1-billion-on-golf). Process detail: he models Sunday night, brother **Tom Peabody** handles distribution/execution all week, including taking "crosses" (private market offers) on Wednesdays (https://unabated.com/articles/inside-the-process-betting-the-open-with-tom-rufus-peabody). Props method: project game-state-dependent quantities (pass attempts, routes, target share), bet the *median* not the mean of skewed distributions (https://thepowerrank.com/the-craft-of-sports-betting-professionals-2).
- **Haralabos Voulgaris**: transitioned from a single massive insight (halftime-split totals mispricing) to the **Ewing** play-by-play simulator built over ~2 years by a hired quant ("the Whiz"), deployed 2008, Ewing 2.0 in 2009, 1,000+ bets/season; claimed ROI ~6% (2010–11), 5.14% (2011–12) — figures via Grokipedia aggregating ESPN, treat numbers as **unverified/lore-adjacent**. Nate Silver's firsthand account (*The Signal and the Noise*) is documented: bet threshold ≈54% win probability, hired assistants to chart every player's defensive positioning ("his own scouting service"), "a thousand little secrets" not one edge (PDF excerpt: http://faculty.bard.edu/hhaggard/teaching/sci127Sp20/notes/SilverSignalExcerpt.pdf). On My First Million (Feb 7, 2025) he says the model still runs fully automated and he refuses discretionary overrides: "I wanted to automate me... we're not making any changes" (https://www.youtube.com/watch?v=eQF1oLwdWcM). $100M+ lifetime claim is **self-reported**.
- **Tony Bloom / Starlizard** and **Matthew Benham / Smartodds**: pure origination at industrial scale. Benham at MIT Sloan (Mar 2026, his first public interview in years): the late-90s/2000s Asian market was "so inefficient... a very basic model with goals as your only input was enough to beat the market," and — key operational quote — "there's no use having a model if you're just going into High Street bookies, because you can only bet a tenner and... they close you down"; edge required Asian outlets that took size (https://trainingground.guru/matthew-benham-how-data-culture-and-smart-bets-built-brentfords-success). Smartodds (founded 2004) was built on the **Dixon-Coles** model; Benham hired Stuart Coles himself, and co-owns the **Matchbook** exchange (https://en.wikipedia.org/wiki/Matthew_Benham). Starlizard specializes in **Asian handicap, high-volume/low-margin**, monitors "thousands of variables," and deliberately bets obscure leagues where bookmaker attention is thin (BI 2016: https://www.businessinsider.com/inside-story-star-lizard-tony-bloom-2016-2).

**Top-down (documented):** **Gadoon "Spanky" Kyrollos** — middling, scalping, steam-chasing, and positional bets off market-vs-own-number differences (Las Vegas Review-Journal, Aug 5, 2023: https://www.reviewjournal.com/sports/betting/meet-spanky-who-left-rat-race-to-become-top-pro-sports-bettor-2882980). **Zeljko Ranogajec** is a different species: pari-mutuel pools/racing/Keno at maximum liquidity, tiny margins, colossal volume, **plus negotiated rebates of 8–10% from Tabcorp** — profitable even at a 2% gross betting loss (SMH, May 2018: https://www.smh.com.au/national/meet-the-joker-the-australian-who-is-the-biggest-gambler-in-the-world-20180515-p4zfhi.html; Wikipedia: his action ≈6–8% of Tabcorp's $10B turnover). His crew financed the 2023 Texas Lottery buyout, winning $57.8M (Houston Chronicle, Mar 2026).

## 2. Getting bets down — universally described as the binding constraint

**Established, across every named pro:**
- Spanky: "Once you know how to win... the problem is finding a bookmaker to accept your bets. That's the hard part... I have to rely on beards and different people... That's the most important part of the business" (Review-Journal 2023). The Ringer's embedded profile (Jun 5, 2019) documents the limit cascade empirically: fresh account → $1,000 limit → $300 → banned; Alan Denkenson: "most people are limited after four bets... if you laid 5 on a game and it closed 8 you're guaranteed to be a sharp" (https://www.theringer.com/2019/06/05/gambling/sports-betting-bettors-sharps-kicked-out-spanky-william-hill-new-jersey).
- Walters (*Gambler*, Aug 2023): "thousands of people throughout the world" as betting partners, not mere runners (Musburger interview: https://www.youtube.com/watch?v=6oRYtBxyQwg); ESPN documented mover Jack Mastronardo running 20 underlings for a 25% cut of winnings (http://www.espn.com/espn/feature/story/_/id/12280555/how-billy-walters-became-sports-most-successful-controversial-bettor).
- Starlizard: in-house "bet placers" work through chained Asian brokers to hide flow — "if they take a position, they will definitely move the entire market" (BI 2016). Dec 2025 High Court filings (Dudfield v. Bloom, $23M claim) allege "**secret exotic accounts**" — wealthy frontmen with losing reputations, allegedly including Nigel Farage aide George Cottrell (https://www.casino.org/news/inside-starlizard-high-court-filing-lifts-lid-on-tony-blooms-800m-betting-empire; The Athletic, Dec 6, 2025: https://www.nytimes.com/athletic/6865054/2025/12/06/tony-bloom-brighton-betting-starlizard-court-case). **Allegations, not adjudicated fact.**
- Voulgaris recruited beards at poker tables: he fronted the money and covered losses, beard kept 20% of profits (Ed Feng's summary of Voulgaris's own podcast statements: https://thepowerrank.com/the-craft-of-sports-betting-professionals-2).
- The Economist Christmas special (Dec 18, 2025), "The battle to stop clever people betting," covers the same restriction/evasion arms race EU-side (paywalled; contents unverified here): https://www.economist.com/christmas-specials/2025/12/18/the-battle-to-stop-clever-people-betting

## 3. Staking: do they actually use Kelly?

**Documented practice is cruder than the theory.** Walters's published rule is **1–3% of bankroll, scaled to edge, hard cap 3 units** — proportional-to-edge, i.e., Kelly-flavored but heuristic (Gambler; summaries: https://www.shortform.com/blog/billy-walters-gambler, Yahoo interview Aug 2023). Voulgaris used a probability floor (≥54% at -110) rather than published Kelly fractions (Silver excerpt). Captain Jack Andrews' contrarian, practitioner-grade position (Unabated, Jan 20, 2023): "**Getting the money down while a line is available trumps knowing the precise size**"; bankroll management cannot rescue -EV; and young bettors with replenishable rolls can rationally play up to full Kelly (https://unabated.com/articles/misconceptions-about-bankroll-management). Fractional (half/quarter) Kelly as the norm is **established folklore** — widely asserted, rarely attributed to a named pro with numbers. Pizzola's named failure mode: sharp bettors who don't shrink stakes at long odds ("$500 to win $600,000 on a $100k bankroll... should be $30–60") — Ep. 186 transcript above.

## 4. CLV usage — a QC instrument, not a religion

- Peabody's canonical anecdote (Oddstrader interview, https://www.youtube.com/watch?v=b6S8DzV0Uw0): model lost for 5 weeks; CLV was ≈ **−0.5%** → bounded the worst case ("we're going to lose half a percent going forward"), so he kept betting; season finished **+5% ROI per bet**. CLV as a *drawdown-vs-model-death discriminator*.
- Peabody has pushed past CLV publicly: "Beyond CLV and ROI: Analyze Bet Results Using Expected ROI" (Unabated, Feb 29, 2024, listed at https://unabated.com/education) — CLV stabilizes faster than ROI but both need luck-stripping.
- Counterpoint (documented): pro "Alan" (via Ed Feng) *does not* pursue CLV — "greatness doesn't come from copying"; if the market moves your way 90% of the time you're doing what everyone else does (powerrank link above). Voulgaris told a cautionary tale about *avoiding* CLV-visible behavior to preserve a $500k-limit outlet — account preservation can outrank line value.
- Pizzola's tout-evaluation checklist treats missing CLV/timing/market-access records as disqualifying (Circles Off: https://podcasts.apple.com/us/podcast/circles-off-sports-betting-podcasts/id1552575702).

## 5. Team structure

Starlizard: ~160 staff (Athletic 2020) — press claims of ~500 and "£600m in an average year" circulate (Guardian-derived; **unverified**); 3-hour math tests, NDAs, staff betting banned, 24/7 office for global kickoffs, in-running doubling-down on live positions; employees hold "stars" in the syndicate — bonuses up to £500k/6mo but **must top up losses** (BI 2016; theesk analysis Apr 2026: https://theesk.org/2026/04/09/anthony-grant-bloom-analysis-of-starlizard-the-brighton-model-and-the-legal-challenges-to-professional-gambling-integrity). Spanky: 14 employees + hundreds of worldwide partners, "hundreds of millions annually" wagered (https://www.boydsbets.com/professional-sports-bettor-lifestyle — secondary source). Ranogajec: ~300 people via contractor companies, nobody employed directly (Wikipedia). Peabody: essentially a two-brother shop — modeler + execution/distribution. Voulgaris: himself + one quant + charting assistants.

## 6. What kills aspiring pros (their words)

Documented, convergent: (a) **execution naïveté** — winning is step one; keeping outlets is the job (Spanky, Benham, Walters); (b) **chasing losses** — Walters: doubling up after a bad Sunday "is suicide"; (c) **overbetting long odds** (Pizzola); (d) **betting -EV with good bankroll management** (Andrews: no sizing scheme saves bad bets); (e) **abandoning a validated model on drawdown** — Peabody's partners quit his CFB second-half model right before it printed (powerrank); Voulgaris refuses team requests to hand-tune. Andrews' meta-lesson: "You never master it; you only continually get a little bit better" (Chicago Sun-Times, Aug 23, 2025: https://chicago.suntimes.com/casinos-gambling/2025/08/23/captain-jack-andrews-betbash-gadoon-spanky-kyrollos-sports-gambling-hall-of-fame-michael-roxy-roxborough-las-vegas-atlantic-city).

---

## (a) Five sharpest takeaways
1. **Execution, not edge detection, is the moat** — every named pro (Spanky, Walters, Bloom, Benham, Voulgaris) built beard/broker/partner networks; Benham says a model without a size outlet is worthless.
2. **CLV is a bounded-loss instrument**: Peabody's −0.5% CLV floor justified staying in a losing model that returned +5%; but pros also deliberately sacrifice CLV optics to preserve high-limit accounts.
3. **Nobody documented runs textbook full Kelly**: Walters' 1–3% edge-scaled cap and Andrews' "speed trumps precise sizing" is the actual practice; fractional-Kelly-as-norm is folklore with strong circumstantial support.
4. **Origination is a syndicate-scale game now** (Pizzola: "pretty much no beginner is succeeding"); top-down/price-taking off sharp anchors is the realistic solo path — exactly the platform's architecture.
5. **Discipline failures beat math failures**: chasing, drawdown-abandonment, and longshot over-staking are the named killers, not bad devigging.

## (b) Actionable ideas for the platform
- Add a **Peabody-style expected-ROI layer** beside trusted CLV (luck-stripped grading vs close), and a "CLV worst-case floor" readout per strategy: if trusted CLV ≥ −0.5%, drawdowns are survivable noise — formalizes the keep/kill decision.
- Add a **get-downability score** per pick (count of books at/near the price, exchange liquidity, freshness) — pros treat executability as first-class; the operator bets manually and needs it.
- Enforce **odds-conditional stake shrink** (Pizzola's longshot rule) beyond the existing 4.0 ceiling: Kelly fraction decaying with odds/model uncertainty.
- Track **which books lag the sharp anchor and by how many seconds/minutes** (Spanky's top-down core, per https://www.bettored.org/post/case-study-spanky-top-down-betting) — latency league tables per bookmaker would sharpen alert prioritization.
- Treat **alert latency as a KPI** (Andrews: speed > sizing precision).

## (c) Unknowns / unverifiable
- Starlizard's true P&L (£600m/yr and ~500-staff figures are press/court claims; Bloom has never confirmed); "exotic accounts" allegations are untested in court.
- Voulgaris's $100M+ and per-season ROI figures — self-reported / aggregator-sourced.
- Ranogajec's Keno 40-of-44 jackpot claim — disputed by Tabcorp; his rebate percentages are "industry insider" estimates.
- Whether any major syndicate uses formal Kelly internally — no primary documentation found either way.
- Economist Dec 2025 piece contents (paywalled) — likely the best recent EU-facing account of limit evasion; worth manual retrieval.

---

# The Betting-Strategy Book Canon — What Each Actually Adds (research briefing, 2026-07-10)

## 1. Joseph Buchdahl — the measurement trilogy

**How to Find a Black Cat in a Coal Cellar (2013)** — the track-record-evaluation book. Load-bearing ideas: (1) a betting/tipster record is a hypothesis test, not a testimonial — Buchdahl walks through applying a **t-test to a returns record** to ask "could luck alone produce this?" (author-page framing: https://www.amazon.de/-/en/Joseph-Buchdahl-ebook/dp/B00B1R97PU); (2) the tipster industry is structurally selection-biased — survivorship, hidden losing streaks, vague/retro-fitted claims are the tells (https://www.goodreads.com/book/show/17434590-how-to-find-a-black-cat-in-a-coal-cellar); (3) even a "significant" p-value on a record is weak evidence given the multiple-comparisons ocean of tipsters. **Established** as the standard reference for verifying anyone's record. *Empirical caveat (established in later Buchdahl writing): t-tests on records are far less diagnostic than closing-line comparison — a point he himself operationalized in his Pinnacle "closing line as skill test" articles (https://www.pinnacle.com/betting-resources/en/author/joseph-buchdahl).*

**Squares & Sharps, Suckers & Sharks (2016, rev. 2021)** — the "why markets beat you" book. Load-bearing: (1) the **sharp vs recreational bookmaker dichotomy** (winners-welcome low-margin books vs. restrict-and-profile retail books) as the organizing fact of the industry; (2) **wisdom-of-crowds**: the (Pinnacle) closing price is the best available estimate of true probability, so value = deviation from it; (3) betting psychology — loss aversion, overconfidence from early wins, dopamine anticipation — as the mechanism that funds the market (Buchdahl interview recap covering exactly these themes: https://www.youtube.com/watch?v=g0-w6Y_-i2c). He publishes a live "Wisdom of the (Pinnacle) Crowd" methodology on his own site (https://www.football-data.co.uk/wisdom_of_crowd_bets.php). **Established** for soccer 1X2/AH at sharp books; **contested at the tails** — the favourite–longshot bias means naive "Pinnacle-fair vs soft price" EV is inflated at long odds (practitioner systems built on it acknowledge this, e.g. https://godsofodds.com/en/previews/using-pinnacle-s-odds-to-build-a-profitable-betting-system; your own BSP/odds-ceiling findings replicate it).

**Monte Carlo or Bust (2021)** — the luck-vs-skill quantification book. Load-bearing: (1) **simulate your record** — Monte Carlo resampling of a bet history gives the distribution of outcomes a zero-skill bettor would produce, the honest replacement for eyeballing ROI; (2) staking-plan comparisons under simulation (why progressive staking is variance cosplay, why fractional Kelly dominates full Kelly under estimation error); (3) a worked **Weibull goal-distribution model** for football as a "where the sharps are" example (BashCast ep. 182 chapter list confirms Weibull + simulation focus: https://bashcast.podbean.com/e/the-bashcast-episode-182-joseph-buchdahl-monte-carlo-and-bust). Caan Berry's 10/10 review stresses it teaches "the correct way to approach probability and data," not a strategy (https://caanberry.com/monte-carlo-or-bust-book-review). **Established** methodology; no serious empirical challenge — it *is* the challenge machinery.

## 2. Ed Miller & Matthew Davidow — how US books actually price

**The Logic of Sports Betting (2019)** — Load-bearing: (1) US books mostly **copy lines** (from market-makers/screen) rather than originate, and hold is an artifact of book structure, not omniscience; (2) the **0% synthetic hold** concept — combine best prices on opposite sides across two books to synthesize a hold near/below zero; the Sports Trading Network review calls it "the crux of the strategy… it doesn't make you better at sports betting, it just limits your losses when you're wrong" (https://www.sportstradingnetwork.com/article/book-review-the-logic-of-sports-betting); (3) the sportsbook's structural advantages (hold, limits, bans) vs the bettor's (bet selection, timing, line shopping); (4) betting as a **multiplayer trading game**, not you-vs-house (https://www.befreed.ai/book/the-logic-of-sports-betting-by-ed-miller). Davidow's authority is operational — he co-founded in-play pricing firm Deck Prism (https://www.youtube.com/watch?v=xHdSP3kAVoI). **Established**; practitioners note the zero-hold-synthetic exposition confuses readers but the concept is sound (https://www.reddit.com/r/algobetting/comments/zzp0dg/questions_on_the_logic_of_sportsbetting_by).

**Interception (2023)** — the derivatives/modern-product book. Load-bearing: (1) modern books are "very sophisticated technology products held together with duct tape and crazy glue" — SGPs, in-play, player props are priced by correlated-simulation engines and vendor feeds with **exploitable seams**; (2) **12 concrete angles**: arbitrage, house-rules-vs-model-rules mismatches, in-play props, data errors, etc.; (3) **account-longevity doctrine** — bet the products books believe are unbeatable (SGPs, in-play) at break-even to camouflage sharp action ("Making break-even bets on products that modern sportsbooks believe are unbeatable is the most effective way to ensure the longevity of an account") — all per Unabated's review (https://unabated.com/articles/interception-book). Fuldapocalypse calls it "the best sports betting book I've read" while noting most facts are known to insiders (https://fuldapocalypsefiction.com/2024/01/05/review-interception). **Established** as the only book-length treatment of derivative-pricing seams; individual angles decay fast by nature.

## 3. Elihu Feustel — model origination

**Conquering Risk: Attacking Wall Street and Vegas (2010, with George Howard)** — Load-bearing: (1) four fully-worked **winning regression models (MLB, NFL, NCAA Football, WNBA)** — rare published examples of actual origination rather than market-relative betting (https://books.google.com/books/about/Conquering_Risk_Attacking_Wall_Street_an.html?id=qzf619W_LUsC); (2) the assumption-audit habit — every model input is a falsifiable assumption; (3) "hard work and lots of modeling" as the honest cost of origination (author's own framing: http://www.elihufeustel.com/PUBLICATIONS.html). Still cited in practitioner canon lists (https://www.reddit.com/r/sportsbook/comments/1oa0r5/). **Empirically challenged by time**: the specific 2010 models are widely regarded as arbed-out; the Bettor Ed canon survey lists a Feustel follow-up, *Beyond the Odds* (2024) (https://www.bettored.org/post/the-evolution-of-sports-betting-books-25-years-later) — **I could not independently verify that 2024 title's contents; treat as unverified**.

## 4. Stanford Wong — Sharp Sports Betting (2001)

Load-bearing: (1) gave US bettors the **EV/breakeven-percentage vocabulary**; (2) **key numbers** in NFL (3 and 7) as the market's granularity; (3) the **Wong teaser**: 2-team 6-point teasers crossing the 3–7 corridor, avoiding road favorites (Bettor Ed: "the book that started it all," https://www.bettored.org/post/the-evolution-of-sports-betting-books-25-years-later; Fezzik's restatement of the rules: https://x.com/FezzikSports/status/1845365641160818925). **Empirically challenged**: the classic teaser edge assumed −110/−120 pricing; books have repriced teasers to −130/−140 and shade key-number legs, so the *mechanical* Wong teaser is dead-to-marginal and modern guides publish "updated" criteria requiring payout and leg filters (https://www.covers.com/nfl/teaser-strategy, https://oddsindex.com/guides/wong-teaser-strategy). The key-numbers insight itself remains fully established.

## 5. Poundstone — Fortune's Formula (2005)

Load-bearing: (1) the **Kelly criterion as information rate** — Kelly proved optimal log-growth equals the capacity of the bettor's private "noisy channel" (American Scientist's review "Bettor Math" summarizes this precisely: https://www.americanscientist.org/article/bettor-math); (2) Thorp's demonstration that **mean-variance and geometric-mean maximization are partially incompatible** (1969) — i.e., Kelly is not "risk-adjusted optimal" in the Markowitz sense (https://moontowermeta.com/insights-from-fortunes-formula); (3) the sociology: **Samuelson's decades-long attack** on Kelly (log utility is *a* utility, not *the* utility; "eventually almost surely ahead" ≠ rational for finite horizons) is the canonical empirical/theoretical challenge, and the book narrates it rather than resolves it. Practitioner resolution — fractional Kelly because edge estimates are noisy — is what Buchdahl's simulations and every serious operator (including your platform) actually implement. **Established history; full-Kelly-as-practice is folklore/refuted.**

## 6. Billy Walters — Gambler (2023), operational chapters

Load-bearing (per Shortform's chapter summary, https://www.shortform.com/summary/gambler-summary-billy-walters): (1) **power ratings → predicted spread**, with explicit point values: home-field ≈ 3 points, key-player injuries ≈ 1 point each — he publishes his valuation grammar even if not the full model; (2) the operation was **origination + execution as separate disciplines** — handicapping (Computer Group-descended modeling) plus a runner/beard network to get volume down at the best numbers before markets moved; (3) situational variables most modelers underweight (readers single out his **travel-distance** section: https://www.reddit.com/r/sportsbetting/comments/17ze7ev/billy_walters_book_gambler); (4) sizing tiers ("star" system) mapped to edge size. **Established as testimony** (30-year verified winner: https://en.wikipedia.org/wiki/Billy_Walters_(gambler)) but **unreplicable as method** — the book gives values, not the model, and the execution network is the real moat.

## 7. Notable 2023–2026 additions

- **Interception** (2023) and **Gambler** (2023) — above.
- **Andrew Mack** — *Statistical Sports Models in Excel* Vol 1–2 (2019/2020) "bridged intuition and data science for non-coders" (https://www.bettored.org/post/the-evolution-of-sports-betting-books-25-years-later); his **Bayesian Sports Models in R** (2024-reviewed) is the current origination-textbook step-up ("learn to originate profitable sports bets with statistical computer models" — https://x.com/SmokeTheBooks/status/1816853167608582225); long-form process interview: Circles Off #185, Dec 2024 (https://www.youtube.com/watch?v=Iu1JgQJXUWU).
- **Ronald Lockington, Secrets of Sports Betting (2025)** — pitched as the information-overload-era synthesis (promos, derivatives, scaling) per Bettor Ed (same URL); **note Bettor Ed appears to be promoting this title — treat its praise as conflicted/unverified**.
- Canon-adjacent standbys still on practitioner shelves: King Yao's *Weighing the Odds*, "Poker Joe's" *Sharper* (https://www.goodreads.com/shelf/show/sports-betting).

## Empirically challenged claims — scorecard

| Claim | Status |
|---|---|
| Wong's classic 6-pt teaser is +EV | **Refuted at modern pricing** (−130+, shaded legs) — needs updated filters (covers.com, oddsindex) |
| Full Kelly is optimal in practice | **Refuted** for noisy edges (Samuelson critique + fractional-Kelly consensus; americanscientist.org, moontowermeta) |
| Pinnacle close = true probability | **Established on average, biased at tails** (favourite-longshot); CLV vs. soft close overstates EV |
| Beating the close ⇒ long-run profit | **Being qualified**: Unabated's "You're Using Closing Line Value Wrong" (Jan 2026) proposes a 3-part test for when CLV is a valid signal — market liquidity, genuine close, vig-adjusted (https://www.youtube.com/watch?v=MIkpsiJzbcA); OddsJam stresses no-vig close as the only fair benchmark (https://oddsjam.com/betting-education/importance-of-closing-line-value) |
| Feustel's 2010 models still win | **Assumed decayed**; value is the method, not the coefficients |

## (a) Five sharpest takeaways
1. The canon splits cleanly into **market-relative** (Buchdahl, Miller/Davidow: price vs. sharp consensus) and **origination** (Feustel, Mack, Walters: build the number) — the second is the only durable edge, the first is the only measurable one.
2. **CLV must be no-vig, sharp-source, and liquidity-qualified** or it's a false signal (Unabated 2026; OddsJam) — the field has converged on exactly the guards your platform already implements.
3. **Synthetic hold** (Logic) is the cheapest risk reduction in betting: cross-book two-sided pricing bounds your loss when your model is wrong.
4. **Derivatives/SGP/in-play pricing seams** (Interception) are where 2023+ edges live — vendor-feed patchwork, house-rules-vs-model-rules mismatches, data errors.
5. Walters' explicit **point-value grammar** (HFA≈3, injury≈1/player, travel distance) is the only published sizing of situational adjustments from a verified 30-year winner.

## (b) Actionable ideas for the platform
- Add a **Monte Carlo record simulator** (Buchdahl MCoB): resample the settled-pick ledger under H0 (no skill) and show the operator's percentile — a stronger dashboard stat than raw ROI/CLV, and consistent with your trusted-CLV CI discipline.
- Implement **synthetic-hold display** per pick: best opposing price across captured books → the pick's true worst-case cost; a cheap column given you already store multi-book snapshots.
- Buchdahl's **Weibull goal model** is a candidate diversity term next to Dixon-Coles (penaltyblog already ships alternatives) — walk-forward it, don't trust it.
- Interception's angle taxonomy suggests a **rules-vs-settlement audit**: your settlement engine already distinguishes sport-aware grading; formalizing "house rules vs model rules" mismatch detection is a display-only research lane for tennis/NFL.
- Black Cat's t-test framing: expose a **luck-vs-skill p-value per strategy/tier**, pre-registered, to complement CLV CIs.

## (c) Unknowns
- Contents/quality of Feustel's *Beyond the Odds* (2024) — single conflicted source, unverified.
- Whether Lockington (2025) contains anything non-derivative — the only substantive coverage found is self-promotional.
- Quantitative post-mortems of Wong-teaser EV at current EU/US pricing exist behind paywalls (Unabated Premium); the exact break-even payout by season is unverified here.
- Walters' actual model features beyond the anecdotal point values — never published; all reconstructions are folklore.
- The precise author of American Scientist's "Bettor Math" review and its full Kelly critique details were not re-verified beyond the article page itself.

---

# Community Intelligence Briefing: What Working Bettors Report, 2024–2026

**Signal quality caveat up front:** the open-web community signal has degraded measurably since ~2023. r/sportsbook is now dominated by promo/affiliate content; r/EVbetting is heavily tool-marketing; YouTube "EV betting" content is ~90% affiliate-funded (OddsJam/AVO/Outlier codes visible in almost every video surveyed). The highest remaining open signal: **r/algobetting**, **arbusers.com**, and **Circles Off podcast transcripts** (Rob Pizzola/betstamp — sponsored by Pinnacle/betstamp/ProphetX, but the practitioner content is real). Reddit blocks most scraper fetches, so several threads were readable only partially — flagged below.

## 1. Which edge types practitioners say still work (2025–2026)

**Prop origination — the most consistent "still works" claim (established, multi-source).** Circles Off ep. 192 (2025): "the prop market is easy enough to beat if you're truly originating it… sometimes there's edges big enough that you don't have to take best price," which also extends account life ([betstamp transcript](https://www.betstamp.com/education/circles-off-episode-192-how-to-actually-fin-your-edge-in-sports-)). Ep. 186 gives the canonical 2025 hierarchy of viable edges — (1) origination in niche/prop markets, (2) top-down (market-as-north-star) betting, (3) being faster to news/injury info — with the warning that top-down "tools have become table stakes" like early baseball analytics ([betstamp](https://www.betstamp.com/education/circles-off-episode-186-how-to-actually-start-winning-at-sports)). Even Pinnacle is described as beatable "every single day on props" (ep. 185, [YouTube](https://www.youtube.com/watch?v=Iu1JgQJXUWU)).

**Niche/obscure markets.** Sharp guest Thon Misser (Circles Off, June 2026) built his reputation on Japanese baseball, reality-TV markets, and new prediction-market platforms ([iHeart episode listing](https://www.iheart.com/podcast/269-circles-off-sports-betting-100834994)). Folklore-grade but repeated: lower soccer leagues carry more pricing errors ([bet2invest](https://bet2invest.com/blog/Top-Football-Betting-Strategies-That-Actually-Work-in-2025)); arbusers counterpoint below.

**Prediction markets / exchange-style venues — the big 2025 migration (established).** Kalshi did $23.8B and Polymarket $27B+ volume in 2025; >90% of Kalshi's October-2025 volume was sports ([AIBM](https://aibm.org/policy/prediction-markets-regulation-risks-and-areas-of-research), [Lothian/LinkedIn](https://www.linkedin.com/posts/johnjlothian_prediction-markets-are-witnessing-explosive-activity-7387123867544326145-lXxL)). The pitch drawing sharps: peer-to-peer = no operator limiting, and break-even edge drops from ~4.5% (–110 vig) to sometimes <1% ([propsbot](https://propsbot.ai/best-prediction-markets-sports)). Pricing is NOT uniformly sharp: Kalshi NFL ML/totals were worse than books after fees in fall 2025, better than books in March Madness 2026 (AIBM, above) — i.e., a real cross-venue relative-value edge exists and mean-reverts. Novig/ProphetX got CFTC approval (June 2026) and market themselves explicitly at limited sharps ([Action Network](https://www.actionnetwork.com/online-sports-betting/reviews/prediction-market-apps)).

**Live/in-play.** Practitioner reports are thinner and more anecdotal: r/algobetting arbers say they hit live only for "mispriced middles or late arb" ([limits thread](https://www.reddit.com/r/algobetting/comments/1oqxgk6/i_tracked_my_limits_across_12_sportsbooks_here)); inplayLIVE (Andrew Pace) still teaches live +EV via Pinnacle's own academy ([Pinnacle YouTube, Apr 2025](https://www.youtube.com/watch?v=pM5TmN2SYyM)) — but that's marketing-adjacent. Label: **plausible, weak open-web verification**.

**Anchor-book value betting (EU style) still endorsed.** arbusers, Oct 2025: "Pinnacle is definitely a sharp bookie even today… reliable reference for doing value betting and the good results are backing these claims" — with the caveat that its **early lines on lower-tier leagues are not sharp**, only near kickoff ([Pinnacle limiting winners thread](https://arbusers.com/pinnacle-limiting-winners-t10780)). A long-running arbusers thread reports getting "absolutely crushed" value-betting against Pinnacle in small markets where other Asian books post higher limits — the poster's heuristic: where Pinnacle's limit is lower than IBC/Sing/ISN, the "edge vs Pinnacle" is likely fake ([thread](https://arbusers.com/value-betting-against-pinnacle-on-smaller-markets-t6703)). **Directly relevant to your sharp-anchor gate: anchor trustworthiness scales with the anchor's own limit, and biggest-edge-vs-anchor is a red flag, not a signal.**

## 2. What died recently, and why

- **First-player-to-score-basket type novelty props**: killed/nerfed because only sharps bet them — "if it's not fun to the casual bettor, how long is it really going to last?" (Circles Off ep. 192, above). Generalizable heuristic for prop-market lifespan.
- **Naive top-down/tool-driven +EV**: multiple 2025 sources describe shrinking edges as scanner tools commoditized ("Positive EV Betting Is Dead? The Real Problem", 8rainbets, Jun 2025, [YouTube](https://www.youtube.com/watch?v=jgWGHPGkwGI); Circles Off "tools become table stakes"). r/algobetting's Aug-2025 "reality check" thread: "Limits, bans, odds adjustments — they'll find a way to shut you down… the edge is razor[-thin]" ([thread](https://www.reddit.com/r/algobetting/comments/1mg9mqb/algobetting_isnt_what_people_think_it_is_a), snippet only).
- **Betfair Premium Charge (up to 60%) — abolished Jan 6, 2025**, replaced by "Expert Fee" (rolling 52-week gross profit, max 40%); Betfair claims ~80% pay less, half pay nothing ([Racing Post](https://www.racingpost.com/news/britain/betfair-exchange-to-introduce-new-commission-system-for-2025-as-premium-charge-is-dropped-a7wbg0v4GCAJ), [Pinnacle Odds Dropper](https://www.pinnacleoddsdropper.com/blog/betfair-exchange-switch-to-new-commission-structure-for-2025)). The driver: exchange liquidity has fallen every year since 2016 in UK racing win markets, worsened by affordability/AML checks pushing big layers off ([Racing Post], [LinkedIn industry commentary](https://www.linkedin.com/posts/bradallen21_why-betfair-died-84-comments-on-aluns-activity-7414683244698324992-fobC)). **Established.** Exchange trading as an edge class is shrinking with the liquidity, even as fees improved.
- **Boost/promo EV in mature US states**: still exists but diminished and increasingly gated to primed accounts; the boosted-play-loser thread on r/algobetting ([2024](https://www.reddit.com/r/algobetting/comments/1fvrtrf/losing_consistently_over_time_making_only)) also flags a discount-worthy pattern — people mis-devigging boosts. Label: **folklore-leaning, consistent across sources**.

## 3. Limiting timelines — the hardest numbers found

From the r/algobetting "I tracked my limits across 12 sportsbooks" post (Nov 2025; arb + +EV, $200–500 bets, ~$50k/mo volume) ([thread](https://www.reddit.com/r/algobetting/comments/1oqxgk6/i_tracked_my_limits_across_12_sportsbooks_here)): **MGM 2 weeks; FanDuel 4 weeks; Caesars 2 months; BetRivers 10 weeks; never limited after 6+ months: Pinnacle (14 mo), Bookmaker (11 mo), Heritage (8 mo), Bet105 (7 mo), BetOnline (6 mo, minor prop limits)**. Corroborating anecdotes: Pizzola, Dec 2024: "Fanatics… limited within 3 bets" ([X](https://x.com/robpizzola?lang=en)); Green Means Go's account-by-account 2025 video (DraftKings 3-tier limiting, $80 max on NBA ML at worst tier; Caesars shut him off shortly after comping a Vegas trip) ([YouTube](https://www.youtube.com/watch?v=hgcXSm3gseU)); r/algobetting consensus that **Kambi-network books limit fastest**, FanDuel/DraftKings slower ([thread](https://www.reddit.com/r/algobetting/comments/1epz0fb/how_viablerewarding_is_ev_betting)). EU/UK side: bet365 greyhound/early-racing CLV-beaters limited "within a couple of weeks" (Smart Sports Trader, Mar 2025, [YouTube](https://www.youtube.com/watch?v=0wfTu6rW6gY)). New arbusers wrinkle (Oct 2025): **even Pinnacle-via-broker is degraded** — PS3838 white-labels give ~half Pinnacle's maxbets, and brokers are suspected of profiling winners and *mirroring their bets* ("half limits so they can bet alongside you") ([thread](https://arbusers.com/pinnacle-limiting-winners-t10780)). Label: single-source claims from a reputable forum regular — **plausible, unverified**.

## 4. Tooling people actually pay for

Verified price points: **OddsJam** Gold/Sharp-Money $199.99/mo each, Global $399.99, Platinum ~$500 ([Bet Hero pricing table](https://betherosports.com/blog/oddsjam-alternative), [Trustpilot complaints](https://www.trustpilot.com/review/oddsjam.com)); **Unabated** from $99/mo; **RebelBetting** from ~€49.99; **Trademate-class EU scanners** ~€89+. Betstamp Pro is the prop/top-down feed Circles Off pushes. Recurring community math: at $199/mo the tool eats the whole edge for bankrolls under ~$3–5k ([8-month OddsJam review](https://www.youtube.com/watch?v=1fItK9hAaCg)). r/algobetting's most-upvoted advice remains "pay for data and tools you control; never buy picks" (secondhand via [SportBot roundup](https://www.sportbotai.com/blog/nfl-betting-reddit-sharp-lessons) — secondary source, treat as folklore).

## 5. Model-based vs price-based consensus

The 2025–26 community consensus is strongly **price-first**: predict the market/line, not the game; CLV is the scoreboard and the fastest edge-death detector ("CLV is the fastest signal you have that your edge is dying. Your P&L won't tell you for months" — full-time bettor AMA, [r/algobetting](https://www.reddit.com/r/algobetting/comments/1r2hqhh/ive_been_betting_as_my_only_source_of_income_for), snippet only). But there is an active minority counter-thread: +CLV with –ROI over ~450 plays ([thread](https://www.reddit.com/r/algobetting/comments/1swqtva/over_a_large_sample_has_anyone_had_clv_but_roi)) and "CLV vs win rate" debates ([thread](https://www.reddit.com/r/algobetting/comments/1rp54ks/clv_vs_win_rate_what_actually_matters_when)). Pure-model origination is treated as the only durable path but with brutal attrition — a Nov-2025 poster brute-forced ~1,000 model configs across 34 markets and reported "almost everything failed out-of-sample" ([r/algobetting](https://www.reddit.com/r/algobetting), listing visible, thread not fetchable).

## 6. Scam / selection-bias patterns to discount

- **Blogabet live-odds-delay exploit (confirmed by platform):** tipster "terroa" used odds-API delay to post picks after outcomes were known (e.g., over 6.5 corners after the 7th corner), building a fake winning portfolio — account closed ([Blogabet's own Trustpilot reply, Jul 2025](https://www.trustpilot.com/review/blogabet.com)). If verification platforms have exploitable verification delays, so do "verified" track records generally.
- **Blogabet/Tipstrr structural survivorship:** tipsters can hide picks or restart after bad runs; failed accounts vanish from rankings ([freetipsbet analysis](https://freetipsbet.com/best-free-betting-tipsters)); Blogabet itself is rated 1.7/5 with recurring non-payment/account-closure disputes (Trustpilot, above). Tipstrr fronts show classic odds-availability inflation (a top tipster's own Pinnacle-priced record is **–18.5% ROI** while soft-book prices show +25% — [example page](https://tipstrr.com/tipster/the-low-service)).
- **Screenshot-tout forensics:** Circles Off documented a serial scammer reusing identical ticket-screenshot framing across years ([betstamp](https://www.betstamp.com/education/rob-pizzola-got-betrayed-in-the-latest-sports-betting-spaces-pre)).
- **Tool-vendor selection bias:** Unabated's "96% of members became winning bettors" ([unabated.com](https://unabated.com)) and OddsJam's "$500–$1000+ weekly" are marketing, not cohort data. Discount all Pikkit-verified testimonial profit claims — survivors self-select.

---

### (a) Five sharpest takeaways
1. **The durable 2025–26 edges per practitioners: prop origination, niche-market origination, and speed-to-news** — top-down scanner betting is commoditized and mostly pays the tool vendor and the limit desk (Circles Off eps. 186/192).
2. **Limits are the binding constraint and are now quantified:** US recreational books limit +EV players in 2 weeks–3 months; only sharp-facing offshore/Pinnacle survive 6+ months; even Pinnacle-via-broker is half-limits and possibly mirrored (r/algobetting Nov 2025; arbusers Oct 2025).
3. **The sharp money's structural migration is to prediction markets/exchanges** (Kalshi/Polymarket/Novig/ProphetX; ~$50B combined 2025 volume) because peer-to-peer removes limiting — but their prices oscillate between softer- and sharper-than-books, which is itself the edge.
4. **Anchor-quality heuristic from arbusers:** Pinnacle's early lower-league lines aren't sharp, and where Pinnacle's limits are unusually low, "edge vs Pinnacle" tends to be spurious — biggest-edge picks against a thin anchor lose.
5. **CLV is community-consensus as the edge-death early-warning system**, with a live minority debate about +CLV/–ROI divergence on soft-book prop prices.

### (b) Actionable ideas for the platform
- **Weight the sharp anchor by its own limit/liquidity at the time of capture** (arbusers small-market finding) — you already floor exchange liquidity; consider an equivalent "anchor maxbet" proxy and demote picks where edge is large *and* anchor limit is thin (matches your existing edge-shrink/tail agenda).
- **Time-gate lower-league anchors:** treat Pinnacle-style anchors as sharp only near kickoff for minor soccer leagues; early anchor snapshots there should get a trust haircut.
- **Add a "book survivability" annotation to picks** (which venue class a user could realistically get down at: exchange/Pinnacle-class vs soft), using the community limit timelines as priors.
- **Prediction-market cross-venue module (display-only):** Kalshi/Polymarket vs book divergence is a documented, oscillating inefficiency and a possible extra anchor/consensus input for basketball/NFL.
- **Tipster-forensics rules for any external signal ingestion:** reject records lacking timestamp-before-close proof; recompute records at sharp prices (the Tipstrr Pinnacle-vs-soft ROI flip is the template).

### (c) Unknowns / unverifiable
- Broker bet-mirroring of winners (single arbusers voice; no second source).
- True live/in-play edge persistence in 2025 — open-web signal is almost entirely vendor-sponsored.
- Betting-Discord intelligence (Hammer Discord, Unabated Premium Discord) — paywalled, no public writeups found; genuinely dark to open-web research.
- Reddit thread bodies increasingly unfetchable programmatically (several key threads cited from snippets only — the two "12 sportsbooks" limit lists and CLV quotes were captured verbatim, the rest partially).
- Exact current Kambi/soft-EU limiting behavior for *soccer-only* CLV-beaters (most quantified reports are US-books, arb-pattern volume).

---

# Commercial Betting-Tools Landscape 2025–2026 — Idea-Mining Briefing

## 1. The +EV screen incumbents

**OddsJam (Gambling.com Group)** — *Established.* Acquired by Gambling.com Group effective 1 Jan 2025 for $80M upfront + $80M earnout; 2024 revenue ~$26M, adj. EBITDA ~$12M ([BusinessWire](https://www.businesswire.com/news/home/20241212903186/en/), [SEC exhibit](https://www.sec.gov/Archives/edgar/data/1839799/000183979924000084/exhibit991-pressreleasexde.htm)). The earnout was amended Dec 2025 — $40M paid early for 2025 performance ([NEXT.io](https://next.io/news/investment/gambling-com-amends-oddsjam-earnout-structure)), i.e., the retail-tool business is *hitting* growth targets. Ingests ~300 sportsbooks at "1M+ requests/sec." Core products: +EV screen (devigged consensus/sharp-anchored fair price vs. every book), arbitrage, middles, low-hold (rollover clearing), promo-conversion finder claiming "95–98% efficiency" on promo→cash ([RotoWire review](https://www.rotowire.com/betting/oddsjam-review)). Devig: their education page documents multiplicative, additive, Shin, and "worst-case" methods and explicitly tells users the right method is market- and strategy-dependent ([OddsJam](https://oddsjam.com/betting-education/uncovering-true-outcome-probabilities)); the default EV feed is a weighted average of "hundreds of books" devigged two-way (per their own tutorial video, [YouTube, May 2025](https://www.youtube.com/watch?v=6ItkDdWY2SY)). Pricing is regionally variable; a comparison pegs the Global plan around $12/day billed monthly ([On Pattison](https://onpattison.com/news/2026/jan/26/betburger-vs-oddsjam-surebet-services-comparison-in-2025)). Track record: influencer-style "$700k EV betting" claims are *unverifiable marketing*.

**OpticOdds** (OddsJam's B2B twin) — *Established.* Enterprise push/pull/queue feeds, 200+ books, sub-second latency, SGP odds, injuries/lineups in-feed, automated bet settlement (Won/Lost/Refunded/Half-Won/Half-Lost), and "AI-powered consensus pricing" — one calibrated price per market distilled from all sources ([opticodds.com](https://opticodds.com), [developer docs](https://developer.opticodds.com/docs/odds-api-getting-started-guide)). Plans reportedly start ~$5,000/mo, sales-gated ([SGO comparison](https://sportsgameodds.com/blog/optic-odds-vs-sports-game-odds)). Clients include BetMGM, PrizePicks, Kalshi, Entain, Kambi — i.e., **books themselves buy the aggregation layer used to beat books**.

**The cat-and-mouse is now litigation, not just limiting** — *Established.* Swish Analytics sued OddsJam + OpticOdds (San Francisco Superior Court, Dec 2024) for $100M+, alleging unauthorized scraping/API access of Swish's proprietary odds off licensee sites (FanDuel, bet365) and resale ([Gambling News](https://www.gamblingnews.com/news/swish-analytics-sues-odds-providers-oddsjam-and-opticsodds-over-alleged-misappropriation), [Wallach LinkedIn](https://www.linkedin.com/posts/daniel-wallach-a959a77_breaking-leading-b2b-sports-betting-oddsmaker-activity-7278726231313809408-SL_Y)). In Dec 2025 the judge kept **all five claims alive, including "hot news" misappropriation** — ruling the doctrine still exists in California ([NEXT.io](https://next.io/news/betting/swish-data-suit-to-proceed-against-oddsjam)). This is the first serious legal threat to the scrape-and-resell model our own free-first stack rhymes with (we scrape but don't resell — materially different posture, still worth watching).

## 2. Unabated — the "fair line as product" model

*Established (methodology documented, weights proprietary).* Built by Rufus Peabody and Captain Jack Andrews. The flagship **Unabated Line** is a **vig-free, sport-by-sport weighted blend of market-maker books** (Bookmaker, Circa, 3et, and "Sharp Book P" — a real-time Pinnacle approximation), with weights re-derived per sport because "no one book is sharpest at every sport" ([What Is The Unabated Line?](https://unabated.com/articles/what-is-the-unabated-line), [Premium guide](https://unabated.com/articles/how-to-get-the-most-out-of-your-premium-membership)). Their CLV doctrine is unusually precise: CLV% = (close_prob − bet_prob)/bet_prob, **against a devigged close**, and that number *is* your EV; they explicitly warn CLV is near-meaningless on props ("very few market-making books… sharp money moves the market more than it should") and early-season college markets ([Getting Precise About CLV](https://unabated.com/articles/getting-precise-about-closing-line-value)). Tools: props simulators (10k-sim Poisson-priced projections with cumulative-probability curves usable for live betting), alternate-line/partial-game derivative calculators, NFL season simulator, Edge Tool (per-book edge vs. Unabated Line turning green at +EV), Rusher line-move alerts, per-market line history on double-click ([OddsPlays review](https://oddsplays.com/reviews/unabated)). Pricing: Props+ $99/mo, Premium $199/mo ($167 annual), NBA projections add-on $249/mo ([BetHero comparison, Mar 2026](https://betherosports.com/blog/unabated-alternative)). Their "96% of members are winning bettors" claim is *unverifiable marketing/survivorship*.

## 3. EU value-betting services — the only ones publishing aggregate CLV

**RebelBetting (Clarobet AB)** — *Established numbers, self-reported.* Publishes monthly community results: Jan 2025 = 373,654 bets, €251,648 profit, **CLV +3.3% vs. realized yield +2.7%**, avg stake €25 ([RebelBetting](https://www.rebelbetting.com/customer-results/value-betting-results-in-january-2025-a-strong-start-to-the-year)). That CLV-vs-yield gap (~0.6pp) across 370k bets is a rare public calibration datapoint: realized yield ≈ 80% of CLV. They even report per-sport splits (handball negative yield but +4.2% CLV; e-sports +9.6% yield). Pricing ~€99/mo Starter, €199/mo Pro ([Sharkbetting comparison](https://www.sharkbetting.com/rebelbetting-alternative)); claimed median member profit €417/mo (Starter) to €728/mo (Pro) ([community forum](https://community.rebelbetting.com/t/is-the-pro-version-worth-it/13239)) — *self-reported, survivorship-prone*. "Profit Guarantee" = free extra month if unprofitable — a marketing device that's only cheap to offer if the median month is genuinely positive.

**Trademate Sports** — *Established product, deteriorating reputation.* Same soft-book value model (Pinnacle-anchored). Trustpilot 2/5: 2025 reviews cite outdated software, slowness vs. market, missing live coverage, and an €80 charge to export your own trade data; one user reports 3k trades/2 months → €440 profit ([Trustpilot](https://www.trustpilot.com/review/tradematesports.com)). RebelBetting's own comparison claims 3.6% vs 2.52% yield advantage ([RebelBetting](https://www.rebelbetting.com/valuebetting/best-tradematesports-alternative)) — *competitor-published, treat as folklore*.

**BetBurger** — *Established.* 280+ books/40+ sports, prematch + live surebets/valuebets/middles; Prematch €79.99/mo, Live €279.99/mo, Full €319.99/mo; **API pushing up to 1,800 arbs+valuebets/minute** for bot builders; bundled EV+/Kelly/no-vig calculators ([betburger.com](https://www.betburger.com), [On Pattison](https://onpattison.com/news/2026/jan/26/betburger-vs-oddsjam-surebet-services-comparison-in-2025), [SGP overview](https://www.sportsgamblingpodcast.com/2025/09/24/overview-of-the-top8-arbitrage-betting-scanners)). Live-mode pricing at 3.5x prematch tells you where they think the money is: **live markets are the soft underbelly** — books are slower to sync in-play, and detection of sharps is harder there (echoed by arbers: live arbs extend account life, [OddsJam's own video](https://www.youtube.com/watch?v=yRvv3scX-68&vl=en)).

## 4. Trackers — CLV as consumer product

**Pikkit** (free + Pro) — auto-syncs 30+ US books; Pro adds per-bet CLV with **selectable close reference**: best-available close, no-vig fair close, custom book set, or the book you bet at — but notably **no Pinnacle-devigged option** ([Pinnacle Odds Dropper review](https://www.pinnacleoddsdropper.com/blog/pikkit-pro-review)); converts CLV into "Expected Profit"; misgrade detection (flags book settlement errors — a genuinely clever feature); scenario/exposure analysis across correlated positions ([pikkit.com](https://pikkit.com/closing-line-value), [BettoRed comparison](https://www.bettored.org/post/best-bet-tracker-2026)). **Juice Reel** — 300+ book sync, 250k users, CLV analytics, plus a "transparent handicapper marketplace": pick-sellers are ranked **only by their synced, verified bet history** ([App Store](https://apps.apple.com/us/app/juice-reel-bet-tracker-tips/id1527960097)) — the verified-track-record idea applied to tout-killing. **BetStamp** — tracking + odds comparison + bankroll/deposit-withdrawal tracking ([BettorEdge roundup](https://www.bettoredge.com/post/top-bet-tracking-apps)).

## 5. Prop modeling — Establish The Run

*Established, with published records.* ETR sells in-season NFL props at **$49.99/week** (deliberately weekly, price floats with value), publishing multi-year records: 216-118 (2021), 308-222 (2022), 253-225 (2023), 228-158 (2024), 203-164 / +6.03% ROI (2025-26 NFL), 262-163 / +15.04% ROI (2025-26 NBA partial) ([ETR FAQ](https://establishtherun.com/etr-in-season-nfl-props-faq), [category page](https://establishtherun.com/category/levitan-player-props)). *Records are self-graded but consistent and multi-year.* Their 2025 practitioner talk ([YouTube](https://www.youtube.com/watch?v=oi5v8ilc8So)) is candid: realistic ROI expectation 6–9% (backtest ~10% ceiling), **only 32% of their bets go in at openers — most volume lands Friday/Saturday** when limits are bettable, and they assume books monitor their subscription. Props edge persists because props lack market-makers — the exact reason Unabated says props CLV is unreliable.

## 6. What the market structure implies

- **Books tolerate a tiered ecosystem, not the users.** Soft books limit fast (bet365 within 1–2 weeks of sharp pattern; DK/FD 4–12 weeks per practitioner tables at [Claw Arbs](https://clawarbs.com/blog/avoid-sportsbook-limits)); the tooling response is now productized "account longevity" engines — stake jitter, round-number stakes, leg delays, per-venue throttles, mug-betting — that deliberately *exclude* sharp venues (Pinnacle, exchanges, Kalshi/Polymarket/Cloudbet/SX/PS3838) where randomization only costs EV. Detection heuristics books use: rapid line-shopping, one-sided specific patterns, betting just before moves, cross-book IP/device linking ([XCLSV](https://xclsvmedia.com/how-sportsbooks-detect-arbitrage-bettors-and-how-to-stay-under-the-radar)).
- **The escape valve is fee-model venues**: exchanges/peer-to-peer (Novig, ProphetX, Kalshi, Polymarket) monetize volume not losses, never limit, and are now the standard second leg in US arb content ([OddsJam-affiliated commentary](https://www.youtube.com/watch?v=1m4Ajg1P7y0)).
- **The edge these tools sell is latency + coverage, not modeling.** OddsJam/Unabated/BetBurger all monetize "a sharp moved, a soft book is 30 minutes slow" ([XCLSV Unabated review](https://xclsvmedia.com/unabated-review-2026-premium-sharp-bettor-tool-worth-it)). Nobody at retail scale sells a true originating model except props shops (ETR, Unabated simulators) — precisely where market-maker anchors don't exist.

---

## (a) Five sharpest takeaways
1. **Sport-specific weighted sharp blends beat single-anchor fair prices** — Unabated's core IP is per-sport market-maker weights over a Pinnacle-approximation + Circa + Bookmaker + 3et, devigged ([source](https://unabated.com/articles/what-is-the-unabated-line)). Our single-sharp-anchor gate is the v1 of this.
2. **RebelBetting's 370k-bet month gives a public CLV→yield conversion factor: realized ≈ 0.8× CLV at 3.3% CLV** ([source](https://www.rebelbetting.com/customer-results/value-betting-results-in-january-2025-a-strong-start-to-the-year)) — a sanity band for our own trusted-CLV → expected-ROI mapping.
3. **CLV is explicitly disavowed on props by its own high priests** ("doesn't mean anything in props" — Andrews, [Unabated](https://unabated.com/articles/getting-precise-about-closing-line-value)) — validates our sport/market-aware CLV trust rules; never grade a props strategy on CLV.
4. **Live is where commercial money says the edge is**: BetBurger prices live at 3.5× prematch; live betting also degrades book sharp-detection. Our soccer-live focus is on the right side of that.
5. **"Hot news" misappropriation survived a motion to dismiss** against OddsJam/OpticOdds ([NEXT.io](https://next.io/news/betting/swish-data-suit-to-proceed-against-oddsjam)) — scraped-odds *resale* now carries real legal risk in the US; private decision-support use remains distinguishable but the doctrine's revival matters for any future data-sharing feature.

## (b) Actionable ideas for our platform (free-first, in-house)
- **Weighted multi-sharp fair line per sport** (Unabated-style): learn per-sport weights over our available sharp signals (Betfair exchange, Pinnacle when captured, sharp-book consensus) by regressing against realized outcomes/close — replaces the binary sharp-anchor gate with a blended anchor. Fits our pure-math boundary; walk-forward validated per existing policy.
- **Pikkit's "misgrade detection" inverted → settlement-audit alerts**: flag picks whose our-grade vs. provider-result disagree, or whose settlement source changed — we already fought settlement bugs; make disagreement a first-class dashboard signal.
- **CLV → "Expected Profit" translation on the dashboard** (Pikkit): render trusted CLV as € expected per stake-unit, not just a log-ratio — better operator ergonomics.
- **Scenario/exposure panel** (Pikkit Pro): show correlated open-pick exposure per event/market with max-loss; cheap to compute, prevents accidental stacking.
- **Per-sport CLV-trust taxonomy** (Unabated doctrine): codify "CLV-meaningful" vs "CLV-unreliable" (props, early-season, thin leagues) markets in the CLV trust layer rather than one global rule.
- **Line-history-on-click** (Unabated odds screen): per-pick sparkline of anchor + soft odds from snapshot history — we have the snapshots already.
- **RebelBetting-style monthly self-report**: auto-generate a monthly picks/CLV/yield/per-sport report with the CLV-vs-realized gap as the headline calibration stat.
- **ETR's timing lesson**: log and analyze *when in the pre-KO window* our picks are minted vs. their CLV (we already found h2h >24h is neg-CLV) — their "Friday/Saturday, after openers settle" finding suggests a mint-timing optimum, not just a freshness floor.

## (c) Unknowns / unverifiable
- Unabated Line weights and "Sharp Book P" construction (proprietary); their "96% winning members" claim (marketing).
- OddsJam's actual EV-feed devig default in production (education pages describe options; default appears weighted-average multiplicative — unconfirmed).
- RebelBetting/Trademate member-profit medians (self-reported, survivorship); RebelBetting-vs-Trademate yield comparison (competitor-published).
- ETR records are self-graded (multi-year consistency lends credibility, no third-party audit).
- OpticOdds $5k/mo floor (single third-party source); Swish case outcome (discovery stage as of Dec 2025).
- Claw Arbs' time-to-limit tables (practitioner folklore, no controlled data).

---

# Fresh Academic & Quasi-Academic Work 2024–2026 — Betting Markets Sweep (net-new vs. known list)

## 1. The Whelan/Hegarty UCD corpus — the single densest new vein
Karl Whelan's page (https://www.karlwhelan.com/sports-betting-research) indexes ~10 papers from 2024–2026 beyond the AH-vs-1X2 work already known:

- **"Agreeing to Disagree: The Economics of Betting Exchanges" (Whelan, 2025, UCD WP2025/22**, https://www.ucd.ie/economics/t4media/WP2025_22.pdf). Betfair **full order book at 1-second resolution**, maker/taker side identified per trade — 902,568 pre-play 1X2 bets across 152,102 soccer matches: **Taker mean return −2.5%, Maker +0.6% (t=11.8)**; totals markets (2.09M bets): Taker −2.2%, Maker +2.0%. In-play, longshot losses deepen for Takers as matches progress, and late in matches even Makers lose on longshot-side quotes while profits emerge for those *accepting* offers on favorites. **Established** (huge n, clean identification). Directly validates a "be the maker / never cross the spread on longshots" exchange posture.
- **"On Estimates of Insider Trading in Sports Betting" (Manchester School, 2025)**: prior Shin-z "insider share" estimates are **really just measuring bookmaker margins**, not insiders. Companion: **"How Does Inside Information Affect Sports Betting Odds?" (Scottish J. Pol. Econ., 2025)** — in a realistic model even a tiny insider fraction collapses the market. **Established**, and a direct caution against over-interpreting Shin-z as an insider signal in your devig stack.
- **"Market Structure and Prices in Online Betting Markets" (Hegarty & Whelan, Oxford Economic Papers, 2026)** — how bookmakers actually set odds, 150k matches; **"Estimating Expected Loss Rates in Betting Markets" (Applied Economics, 2025)** — average-bet loss rates exceed the naive overround calculation; **"Risk Aversion and Favourite–Longshot Bias" (Economica, 2024)** — FLB derived from bookmaker risk aversion, not bettor preferences; **"Forecasting Soccer Matches with Betting Odds" (IJF, 2025)** — 1X2 odds biased, Asian Handicap odds unbiased (journal consolidation of the AH-vs-1X2 result you know); **"On Optimal Betting Strategies With Multiple Mutually Exclusive Outcomes" (Bulletin of Econ. Research, 2025)** — Kelly extended to 1X2-style mutually exclusive outcome sets.

## 2. In-play efficiency & goal anticipation (Bielefeld group)
- **"Do Betting Markets Sense a Goal Coming? Evidence from the German Bundesliga" (Ötting et al., arXiv:2505.21275, 2025**, https://arxiv.org/html/2505.21275v1). Unique **1 Hz bookmaker odds + stakes** feed, Bundesliga 2018/19. Effect sizes: own red card −12pp implied win prob, opponent red card +17pp; in-match xG per minute is priced. Key negative result: **bookmakers do not anticipate goals beyond observable xG** — the minutes-to-goal term is insignificant. Anticipation (e.g., from tracking/momentum data faster than the odds feed) is left open as the bettor's edge channel. **Established** method, single-season/single-book caveat.
- **Winkelmann, Vienken, Deutscher & Langrock, "Betting Against Integrity: Identifying Match-Fixing Through In-Play Market Dynamics" (arXiv:2605.30209, 2026**, https://arxiv.org/html/2605.30209v1). High-frequency live-betting data, **Italian Serie B 2018/19–2020/21** (seasons with known fixes). State-space model predicts *expected in-play betting volumes* conditional on match state (odds-move "surprisingness", xG diff, halftime, and stake-concentration Gini terms — Gini enters strongly, α≈3.76); outliers vs. expectation flag suspicious windows. This is the first credible **in-play (not pre-game) volume-anomaly fix-detection** framework. **Established as method; detection performance is exploratory.**
- Related: **Winkelmann, Ötting, Deutscher & Makarewicz, "Are Betting Markets Inefficient? Evidence From Simulations and Real Data" (J. Sports Economics 25(1):54–97, 2024)** — shows simulation-vs-real-data tests of inefficiency claims (https://ideas.repec.org/a/bla/kyklos/v75y2022i2p294-316.html cites it); and **Fischer & Schmal, "Pricing in Response to New Information: The Case of Betting Markets" (Economic Inquiry 63(1):236–264, 2025)** — quasi-natural-experiment info shock with a public replication package (https://www.openicpsr.org/openicpsr/project/207770).
- **Calibrated Weibull in-play model vs Betfair (arXiv:2605.16066, 2026)**: over 140 matches (2024–25 H2), calibrating *any* reasonable hazard model to pre-match market prices dominates model structure; measured that **99% of Betfair in-play price reactions to goals are absorbed within 2 minutes** (470 goals). Useful hard number for staleness windows. **Established measurement, small eval set.**

## 3. Devig / probability-conversion advances
- **"Forecast Sports Outcomes under Efficient Market Hypothesis" (arXiv:2604.17194, 2026**, https://arxiv.org/html/2604.17194v1). Introduces an odds-only conversion (**OO-EPC**) that beats multiplicative, numerical Shin (Štrumbelj), **analytical Shin (Kizildemir et al., 2025 — a new closed-form Shin variant allowing insiders on any outcome)**, and power on log-loss for most of five bookmakers (numerical Shin + power win on Bet365; power also wins on William Hill). Two findings that matter for your stack: (a) **no significant correlation between booksum and odds accuracy**, i.e. no empirical support for Shin's insider mechanism (consistent with Whelan's Manchester School critique); (b) **all FLB-correcting methods (Shin variants, power, OO-EPC) systematically underestimate draw frequency** in 1X2. **Established results, venue/peer-review status unverified** — treat effect ordering per-book as data-dependent.

## 4. Prediction-market microstructure (Kalshi/Polymarket sports)
- **"Arbitrage Analysis in Polymarket NBA Markets" (arXiv:2605.00864, 2026)**: 75.1M on-chain LOB snapshots, 173 NBA games (Feb 4–Mar 4, 2026). **Single-market arb is essentially extinct: 7 executable in-game episodes across 3,042 markets, median lifetime 3.6 s** (below the 3.6–5.5 s polling cadence). Combinatorial moneyline-vs-spread inefficiencies are more common: **290 executable episodes, median 2 per game**. Polymarket NBA is now HFT-grade efficient at top-of-book. **Established (lower-bound caveat from polling latency).**
- **Saguillo et al., "Unravelling the Probabilistic Forest: Arbitrage in Prediction Markets" (AFT 2025**, https://suarez-tangil.networks.imdea.org/papers/2025aft-arbitrage.pdf): full Polymarket on-chain record Apr 2024–Apr 2025 incl. sports; uses **LLM prompt-engineering to extract logical dependencies across related markets** and detect cross-market (intra-exchange) arbitrage — a genuinely new detection method. Profits real but thin vs inter-exchange arb.
- **Bürgi, Deng & Whelan, "Makers and Takers: The Economics of the Kalshi Prediction Market" (UCD WP25/19, 2025**, https://www.ucd.ie/economics/t4media/WP2025_19.pdf): 300k+ contracts, transaction-level — **strong longshot bias; low-priced contracts produce large losses**.
- **Becker, "The Microstructure of Wealth Transfer in Prediction Markets" (quasi-academic**, https://jbecker.dev/research/prediction-market-microstructure): Kalshi 5¢ contracts win 4.18% (−16.4% mispricing); everything <20¢ underperforms, >80¢ outperforms; maker-vs-taker excess-return gap **flipped from −2.9pp (takers winning) pre-2024-election to +2.5pp (makers winning) after**, i.e. professional liquidity arrived with volume. Consistent with **Reichenbach & Walther, "Exploring Decentralized Prediction Markets: Accuracy, Skill, and Bias on Polymarket" (SSRN 5910522, 2025, 10+ cites)** and Bloomberg's finding that losing Polymarket users place 56% of trades at extreme prices vs 28% for top-0.1% earners; **$131M profit concentrated in 823 accounts** (https://finance.yahoo.com/markets/crypto/articles/100-000-polymarket-accounts-booked-062234759.html). **Established pattern across three independent datasets; Becker's piece itself is unrefereed.**
- Practitioner survey: **QuantPedia, "Systematic Edges in Prediction Markets" (Nov 2025**, https://quantpedia.com/systematic-edges-in-prediction-markets) — catalogues inter-exchange arb, intra-exchange dependency arb, and longshot-bias harvesting with citations. **Folklore-adjacent but well-sourced.**

## 5. LLM/ML forecasting with credible OOS discipline
- **"LLM-as-a-Prophet / Prophet Arena" (arXiv:2510.17638, 2025)**: live benchmark trading LLM forecasts against real prediction-market prices — **profitability against markets remains negative/marginal even for o3-class models**; best models' edge is calibration in the extreme bins. **"Pitfalls in Evaluating Language Model Forecasters" (arXiv:2506.00723, 2025)** — required reading: shows most "LLM beats the market" claims are leakage/circularity (LLMs can copy human market priors; back-generated questions bias toward "Yes"). **ForecastBench** (https://forecastingresearch.substack.com/p/ai-llm-forecasting-model-forecastbench-benchmark): LLMs passed the public crowd in 2025; superforecaster parity projected ~late 2026. **Established.** Nothing yet shows an LLM generating positive CLV in sports.
- **Walsh & Joshi journal version** (Machine Learning with Applications 19:100627, 2025) formalizes the calibration>accuracy result you know; among its citing papers, **"Risk Parity vs. Kelly: An Empirical Evaluation of Bankroll Allocation in Football Value Betting" (LNCS, 2026)** is new but I could not verify its content (**unverified**).
- **van Ours, "Non-Transitive Patterns in Sports Match Outcomes: A Profitable Anomaly" (Empirical Economics 69(6):4057–4087, 2025)** — documents exploitable non-transitivity (A beats B beats C beats A) that odds under-price. **Peer-reviewed; magnitude modest, pre-cost.**

---

### (a) Five sharpest takeaways
1. **Maker/taker asymmetry is the cleanest quantified edge of the sweep**: on Betfair soccer, posting (+0.6% to +2.0%) vs taking (−2.2% to −2.5%) across ~3M identified bets (Whelan 2025). Crossing the spread *is* the cost that eats marginal edges.
2. **Shin-z ≠ insiders**: two independent 2025–26 results (Whelan Manchester School; arXiv:2604.17194's booksum-accuracy null) say Shin's parameter measures margin, not informed money — and all FLB-corrected devigs underestimate draws.
3. **In-play prices don't leak goals** (Ötting 2025) and Betfair absorbs goal news within ~2 minutes (arXiv:2605.16066) — the only in-play edge is *faster event data*, not price patterns.
4. **Polymarket/Kalshi sports are already microstructurally efficient at top-of-book** (7 single-market arbs in a month of NBA), but **combinatorial/dependency mispricings persist** (~2/game; LLM-extracted dependency arb at AFT 2025).
5. **In-play volume-anomaly modeling now exists for integrity/steam detection** (Winkelmann et al. 2026): expected-volume state-space + outlier detection, with stake-concentration (Gini) as the strongest covariate.

### (b) Actionable ideas for the platform
- **Exchange fills:** when the read-only Betfair layer informs a manual pick, recommend limit-style prices (maker side) in the ticket copy; Whelan's decile plots imply the taker penalty concentrates in low-probability selections — reinforces your odds-ceiling-4.0 change.
- **Devig QC:** add a draw-frequency calibration probe for the 1X2 devig (all FLB-adjusting methods including power underestimate draws); consider benchmarking goto_conversion vs the analytical Shin (Kizildemir 2025) and OO-EPC on your own tar before touching frozen params — as a *shadow probe only*, given ADR-0021 is signed.
- **Staleness window:** the "99% absorbed in 2 min post-goal" number is a usable empirical bound for flagging stale exchange anchors around scoring events.
- **Anomaly telemetry:** a cheap version of Winkelmann's expected-volume/Gini outlier score over your snapshot stream could double as a steam-vs-noise and wrong-market-data guard (their code approach: distributional regression on match-state covariates).
- **Fischer & Schmal's ECIN replication package** (openICPSR 207770) is free labeled data for information-shock price-response calibration.

### (c) Unknowns / unverifiable
- Winkelmann et al. 2026 report no precision/recall on labeled fixes — detection performance unquantified.
- OO-EPC paper's authorship/venue and generalization beyond its five books: unverified.
- "Risk Parity vs Kelly" (LNCS 2026) and the IEEE Access "Sports Betting as Financial Asset" paper: existence confirmed, content unread.
- Whether Polymarket combinatorial-arb persistence survives fee/latency for an EU-facing observer, and whether the NBA-month result generalizes to soccer markets, is explicitly open (authors' own caveat).
- Reichenbach & Walther's Polymarket skill decomposition: abstract-level only (SSRN paywall friction); the Bloomberg 823-accounts figure is journalism, not peer-reviewed.

---

# Web-Research Briefing — Non-GitHub Code & Methodology Knowledge (Kaggle / Blogs / YouTube)

## 1. Kaggle: what actually wins March Madness & soccer comps (2023–2026)

**2026, 1st place — Matthias Kullowatz (0.1097 Brier, 1st/3,485).** Writeup: https://www.kaggle.com/c/march-machine-learning-mania-2026/writeups/march-machine-learning-mania-2026-1st-place-solut ; video interview: https://www.youtube.com/watch?v=AT7XsqkkUuY. **Established.** Key mechanics:
- Deliberately **shallow XGBoost (max_depth=2)**, separate men/women hyperparams — he explicitly found the models "struggled to get signal out of anything that wasn't a major driver," so simple wins.
- **Training-window expansion trick:** tournament games are too few (~2.5k men / ~1.5k women), so he trained on all games from February onward, adding a home/neutral indicator, using late-March-as-holdout. More data > tournament-only purity.
- **Leave-One-Season-Out CV (2003–2025)** + **isotonic regression calibration** (clipped to [0.001, 0.999]), with quantified gain: total CV Brier 0.1620 → 0.1590 (~1.9% improvement from calibration alone).
- Listed "incorporating betting/prediction-market odds" as his top missed opportunity.

**2026, 3rd place — "LR + Triple-Market Blend" (0.1160).** https://www.kaggle.com/competitions/march-machine-learning-mania-2026/writeups/3rd-place-solution-march-machine-learning-mania. **Established, and the sharpest finding of the sweep:** the solution incrementally added Elo, Four Factors, Barttorvik, Massey composite, KenPom, EvanMiya, Colley — then finished by blending **ESPN BPI + Vegas moneylines + Kalshi prediction-market prices at 90% market weight for men's Round 1 and 75% for women's Round 1**. When market prices exist, the model is nearly discarded. This is the Kaggle community independently rediscovering what your sharp-anchor gate encodes.

**2025, 1st — Mohammad Odeh:** plain XGBoost over seeds/Elo/"team quality" box-score aggregates, tuned, plus **manual domain-knowledge overrides** on specific matchups. https://www.kaggle.com/competitions/march-machine-learning-mania-2025/writeups/mohammad-odeh-first-place-solution. **Established** but note: single-year wins in this comp carry huge variance.

**2024, 2nd (Brier 0.05437) — the metric-gaming confession.** https://www.kaggle.com/competitions/march-machine-learning-mania-2024/discussion/492761. He openly states the round-probability Brier scoring "incentivizes gambling… specifically to predict the champion with 100% probability" and that he **overrode UConn/South Carolina to ~certainty**; the 8th-place finisher confirms doing the same. Earlier log-loss years were gamed with probability clipping. **Lesson (established): Kaggle leaderboard placement in these comps ≠ probabilistic skill; read winning writeups for the *base model*, ignore the final gambit layer.** The canonical old-school version is Andrew Landgraf's 1st place, won by *modeling other competitors' submissions* and optimizing P(top-5) via 10k simulated leaderboards (15%→25%): https://medium.com/kaggle-blog/march-machine-learning-mania-1st-place-winners-interview-andrew-landgraf-f18214efc659.

**2023, 1st — "RustyB"** (walkthrough: https://www.youtube.com/watch?v=9Y_rr_OfxPY): a non-data-scientist who **reused raddar's 2018 feature set** (GLMM "team quality"), showed post-hoc that averaging the model over 10 seeds instead of 3 improved score, and that the borrowed feature selection was worth ~10 leaderboard places. **Established:** seed-averaging and stolen-but-proven feature sets beat novelty.

**Soccer comps.** The Octosport/Sportmonks "Football Match Probability Prediction" (150k matches, 10-match team sequences, log-loss): https://www.kaggle.com/competitions/football-match-probability-prediction — winners used sequence models (organizer's RNN/LSTM companion writeup: https://medium.datadriveninvestor.com/predicting-football-match-with-rnn-99c334b5f10). The academic **2023 Soccer Prediction Challenge** postmortem (Machine Learning journal, 2024) found "relatively simple learning algorithms perform remarkably well compared to more complex algorithms" and domain-knowledge encoding is the differentiator: https://link.springer.com/article/10.1007/s10994-024-06625-9. **Established, converges with your penaltyblog/Dixon-Coles spine.**

**Datasets with provenance (established):**
- *Beat The Bookie odds-series* (Kaggle, from the 2017 Kaupčík/Getty "beating the bookies with their own numbers" study): https://www.kaggle.com/datasets/austro/beat-the-bookie-worldwide-football-dataset — time-series of odds movement, good for CLV-style replay. The strategy's real-world death (account limiting) is the classic implementability caveat.
- *NFL scores + betting data 1966–2024* (spreadspoke) with documented per-era line sources (Pro-Football-Reference, sportsline, aussportsbetting): https://www.kaggle.com/datasets/tobycrabtree/nfl-scores-and-betting-data — relevant to your NFL display-only lane.
- Generic "football odds" Kaggle dumps (e.g. https://www.kaggle.com/datasets/eladsil/football-games-odds) are single-anonymous-bookmaker with no timestamps guarantee — **weak provenance, avoid for CLV work.**

## 2. Substack/Medium/blogs with real methodology

**Plus EV Analytics — Matthew Buchalter (actuary).** The densest free material found. "Building a Bayesian Model" series (Part 1: https://plusevanalytics.wordpress.com/2021/02/26/building-a-bayesian-model-part-1/) walks a naive NBA defensive-shooting model to a full Bayesian one, with an honest backtest of the naive version: **1,512 bets, −52.5 units, −3.2% ROI**, and the operationally transferable heuristic: *"the bet volume is a big red flag — it's unlikely that a market as liquid as NBA sides would be that wrong, that often."* He binomial-tests the 756-735 record (p≈0.30) before believing anything. His publications page (https://plusevanalytics.wordpress.com/publications) indexes: **"The Real Kelly"** (generalized Kelly for simultaneous bets/hedging, Pinnacle 2017) and **"Toward a Theory of Everything"** (fractional Kelly, regression-to-market, and CLV unified as one Bayesian parameter-uncertainty story — directly relevant to how you shrink edges) plus "meta-probabilities" in NFL line movement. He now teaches at Analytics.bet with Harry Crane (https://analytics.bet/courses/art-of-sports-betting-analytics). **Established, high-signal.**

**Andrew Mack (Mack Analytics, @Gingfacekillah).** Author of *Statistical Sports Models in Excel* vols 1–2; bio/interviews at https://unabated.com/articles/to-find-edge-question-certainty and Pinnacle podcast transcript https://www.pinnacle.com/betting-resources/en/educational/a-guide-to-modelling-in-sports-betting-pinnacle-betting-podcast/sgu2uhjma6q8dzw7. Consistent theme: modeling vs the market is an **arms race of relative strength**; edge comes from pricing uncertainty, not point estimates. **Established** (his code lives on GitHub — out of scope, but the interviews carry the methodology).

**Matter of Stats — Tony Corke (AFL, since ~2008).** http://www.matterofstats.com. **MoSHBODS** (separate offensive/defensive team ratings) and MoSHPlay forecasting equations, with weekly public wagers/tips and *published review-and-recalibration posts* (http://www.matterofstats.com/mafl-stats-journal). ABC covered his 2025 season (76 correct tips called "exceptional"): https://www.abc.net.au/news/2025-08-31/computers-algorithms-afl-tipping-random-outcomes/105716120. **Established** — the value is his 15+ years of honest model-revision logs, a template for your research-log discipline.

**Rufus Peabody (Unabated / Bet the Process).** Two substantive video/audio sources: https://www.youtube.com/watch?v=b6S8DzV0Uw0 (Data to Find Angles) and Wharton Jan 2026 (https://www.youtube.com/watch?v=M1T0OlG3XEU). Transferable specifics: he builds models **with zero market inputs, then regresses model output to market** based on measured "power relative to the market" — a clean two-stage design that avoids circularity (identical concern to your CLV-fabrication guards). For props: project game-state-conditional quantities, then price the **full distribution; the market price is the median while means are skew-inflated** (summary: https://thepowerrank.com/the-craft-of-sports-betting-professionals-2). Wharton episode covers prediction-market market-making, queue priority, and sizing in illiquid books. **Established.**

**"The Crafty Bettor": UNVERIFIABLE.** Searches across Tavily/Substack surfaced no substantive publication under this name (craftybettor.com failed to resolve; no archive found). Either defunct, renamed, or too small to index. Do not treat it as a known source; the nearest live equivalents found are Buchalter's blog and BowTiedBettor's Substack (concept-tier, coding-inclusive directory: https://www.blog.bowtiedbettor.com/p/new-to-bowtiedbettor-start-here — **mixed quality, pseudonymous, unaudited**).

**StatsBomb/Opta.** The StatsBomb blog archive's *"Match Simulation: Score Effects and Beyond"* (https://blogarchive.statsbomb.com/articles/soccer/match-simulation-score-effects-and-beyond) covers xG→match-probability simulation including score effects — the standard correction naive Poisson simulators miss; their shot-impact-height xG release notes "gamblers everywhere" as customers (https://blogarchive.statsbomb.com/news/statsbomb-release-expected-goals-with-shot-impact-height). **Established but pre-2023 archive; StatsBomb's current betting-grade models are commercial and unpublished.**

## 3. YouTube/talks with substance

- **Kaggle's own "Winners Walkthroughs"** playlist (RustyB 2023, Kullowatz 2026 above) — the only place winners discuss what *didn't* work. **Established.**
- **SSAC22 FanDuel workshop, "How to Win at Sports Betting: Building Models and Pricing Odds with Data Science"** — a sportsbook's internal pipeline (ingestion → features → pricing → trader adjustment), *with published code and dataset*: video https://www.youtube.com/watch?v=4iagGljHCOA, event page with code link https://www.sloansportsconference.com/event/how-to-win-at-sports-betting-building-models-and-pricing-odds-with-data-science-presented-by-fanduel. Rare look at the adversary's side. **Established.**
- **Sloan prediction-markets panel** (Nate Silver + Susquehanna's Head of Prediction Markets): https://www.sloansportsconference.com/event/prediction-markets-at-the-crossroads-sports-regulation-and-what-comes-next — professional-trader takeover of prediction markets, relevant to Betfair/Kalshi-style anchor quality. **Established (panel exists; video via 42 Analytics channel https://www.youtube.com/user/42analytics).**
- **Folklore/marketing tier to avoid:** OddsJam model tutorials (https://www.youtube.com/watch?v=6HN-d9mC0DI) — the math shown (no-vig fair odds, weighted book averages) is correct but the content is affiliate-funnel; PropsBot's "218,826 graded props verified CLV" (https://propsbot.ai/glossary/closing-line-value) and Bet Hero's suspiciously tidy +2.7%-CLV-forever worked example (https://betherosports.com/blog/closing-line-value-explained) are **unaudited marketing numbers — survivorship/fabrication artifacts.** Pinnacle's own survivorship-bias piece is the right antidote: https://www.pinnacle.com/betting-resources/en/betting-strategy/what-is-survivorship-bias/3dy2t5gerne7gjyh.

---

### (a) Five sharpest takeaways
1. **Markets-as-features won 2026:** 3rd place blended Kalshi + Vegas + BPI at 90% market weight; the 1st-place winner's stated biggest regret was *not* using market odds. Independent convergence on your sharp-anchor architecture.
2. **Calibration is worth ~2% Brier, quantified:** isotonic on LOSO out-of-fold predictions (0.1620→0.1590) with clipping — a cheap, measured layer, matching your "identity beat recalibration" finding as something to re-probe per-sport.
3. **Shallow models + expanded training windows beat deep models on sparse sports data** (max_depth=2; Feb-onward games with venue indicator) — 2023, 2025, 2026 winners and the academic soccer challenge all agree.
4. **Bet-volume as model smoke-detector** (Buchalter): if a strategy fires on >X% of a liquid market's events, the prior should be "model is wrong," not "market is wrong." Trivially computable per-strategy telemetry.
5. **Kaggle leaderboards are gamed** (champion-override at ~100%, historical log-loss clipping): mine writeups for base models and CV design, never for headline scores.

### (b) Actionable ideas for the platform
- Add a **picks-per-eligible-events ratio** alarm per strategy/league (Buchalter red flag), alongside existing EV-sanity gates.
- Prototype an **isotonic calibration probe on out-of-fold Dixon-Coles/basketball outputs** with clipping [0.001,0.999], shadow-only, measuring Brier delta like Kullowatz did — revisit-trigger for your shipped "calibration haircut not warranted" verdict.
- Peabody's **two-stage regress-to-market** (market-free model, then measured shrinkage toward anchor) is a formal alternative to your pending edge-shrink item; his "model power relative to market" weighting is estimable from your CLV history.
- The **SSAC22 FanDuel workshop code/dataset** is a free look at bookmaker pricing pipelines — useful for anticipating soft-book line-setting behavior your value gate exploits.
- *Beat The Bookie* odds-series dataset = free odds-*movement* corpus for validating your CLV/close-capture replay logic off-production.

### (c) Unknowns / unverifiable
- "The Crafty Bettor" — existence/content unverified; possibly defunct or misnamed.
- Octosport competition winner details (exact winning architecture) — leaderboard-topper writeups not retrievable this pass; only the organizer's RNN companion post confirmed.
- All commercial CLV/ROI track-record claims (PropsBot, sharpfootballanalysis "15–25% annual ROI improvement") — unaudited, treat as marketing.
- Whether Kullowatz's calibration gain transfers to soccer 1X2 (different class structure, draw mass) — needs your own walk-forward probe.

---

# X (Twitter) Betting-Analytics Discourse Briefing — 2024–2026 sweep (2026-07-10)

Method note: all via Tavily open-web search + x.com extraction (no X API). x.com profile extracts return sparse, non-chronological timelines; several handles are unreachable. Confidence labels: **[EST]** established (primary post text or multiple corroborating sources), **[FOLK]** folklore/uncorroborated.

## 1. Buchdahl (@12Xpert) — the flatline post, and what he posted since

- **Original post found**: https://x.com/12Xpert/status/1929480767987450233 (Jun 2, 2025, 52.2K views): "Sadly, Wisdom of (Pinnacle) Crowd has now flatlined for 3 seasons. A word of warning to those wanting fast success/profits from betting. This underperformance over 4,000+ matches is not statistically significant and could quite easily be just bad luck." **[EST]** Replies not retrievable via open web.
- **Since — highly platform-relevant**: Jul 29, 2025: "So @Pinnacle, is your sharp book model slowly dying...? 23rd July you turned off general public access to your API, meaning odds comparisons are now showing out of date odds." Aug 12, 2025: Pinnacle confirmed "our real-time API is no longer publicly available... delayed version... to certain commercial partners. This has evidently impacted the odds comparison, including Oddsportal." (both visible at https://x.com/12Xpert) **[EST]**. This means any Pinnacle price seen via OddsPortal-type aggregators after 2025-07-23 may be delayed — it corroborates the platform's own observation that football-data Pinnacle columns died, and matters for sharp-anchor freshness assumptions.
- Aug 26, 2025: losing-streak expectations differ less between sharps and squares than people think; "inability to understand losing" is the biggest failure cause **[EST]**. Oct 26, 2025: flagged a (now-suspended) recreational book publicly mocking a limited customer **[EST]**. He also mirrors on Bluesky (12xpert.bsky.social) — partial X exodus signal. Long-standing position (sharpbetting.co.uk interview): "absence of CLV doesn't always imply absence of skill; it's a much debated metric"; personal limit stories (Blue Square cut him to £0.70 after one winning £50 bet) **[EST]**.

## 2. The CLV-skeptic debate — both sides, named

- **Kirk's Hammer, Jun 27 (2025)** (https://thehammer.bet/article/kirk-s-hammer-closing-line-value-political-betting-and-knowable-events): documents an X debate between **Shipper** and **Pads** — Shipper: don't stop evaluating a bet at the close; the realized result should inform bet quality (results contain information the close missed, esp. "knowable" events). Kirk/counter: that reintroduces exactly the outcome bias CLV exists to remove; degree-of-close matters ("Kamala at 37% closing 90% = good bet regardless of result; 37% closing 42% is genuinely contested"). His poll on that hypothetical split the credible quants — "the smart people disagreed," with confident assertions both ways. **Kostya** (= Kostya Medvedovsky, @kmedved) argued the knowable-events side for elections vs sports **[EST]**.
- **Captain Jack Andrews** (Unabated, May 9, 2025, https://unabated.com/articles/getting-precise-about-closing-line-value): CLV is "a guidepost, not gospel"; **CLV "doesn't mean anything in props"** — few market makers, thin liquidity, sharp money over-moves the line; how you calculate matters (devig, timing of the move). Unabated video rule of thumb: **if the market isn't on Pinnacle/Circa, CLV is close to meaningless**; and "the golden goose for any originator is to generate EV without generating CLV" (untracked edge = unlimited account) **[EST]**.
- **BettingIsCool, Oct 27, 2025**: public callout of @betstamp — "That's NOT how to calculate CLV... If you're not removing the bookmaker's margin you're creating false positives"; Oct 29: published a free Python devig script + trackabet.streamlit.app tracker **[EST]**. Directly validates this platform's devig-before-CLV discipline.

## 3. Limiting — data points and timelines

- **Massachusetts Gaming Commission**: staff study found **0.64% of MA accounts limited Dec 2024–Sep 2025; ~58% of those cut to 1–24% of default stakes**; 5-0 vote (Dec 18, 2025) requiring notice within 48h + a specific, per-market explanation, compliance by Jun 1, 2026 (https://www.covers.com/industry/massachusetts-bettors-could-soon-have-wagerning-limitations-explained-dec-18-2025) **[EST]**. Beware: secondary YouTube coverage garbled this as "64% of accounts limited" — that figure is wrong. Public comment record includes a bettor limited by BetMGM MA within weeks "even though I never made a dime... simply because I was getting CLV" (massgaming.com meeting packet) **[EST]** — regulator-grade confirmation that CLV is the limiting trigger.
- **Spanky** (@spanky — note @MagicianSpanky 404s; correct handle is @spanky): argued the MA framing hides sharp targeting; once limited you never get limits back; proposed a **max-bet ratio cap** (if one player can bet 50K, any player gets ≥2K, ~4%); Shipper's rebuttal — any non-insane ratio makes recreational books unprofitable (covered in Circles Off/Kalshi-sponsored roundtables; exact tweet not retrievable) **[FOLK on exact wording, EST on positions]**. Spanky's Be Better Bettors pod ran a Twitter Q&A Nov 26, 2025.
- UK: The Economist (via @TrungTPhan, quote-tweeted by @Polymarket): **4.3% of active UK accounts had stake restrictions in 2025** **[EST]**.

## 4. Prediction-market migration discourse

- **@Polymarket official, Jan 16, 2026**: "Sharps are welcome on Polymarket. No limit. No bans. Unlike sportsbooks, we want you to win." (https://x.com/Polymarket/status/2012151648207147208) **[EST]** — recruiting sharps is now explicit marketing.
- **Rufus Peabody (@RufusPeabody)** is the flagship migrant: ESPN (May 2025): prediction markets "serve an underserved part of the market: the sharps, the semi-sharps, the price-sensitive"; Bloomberg Big Take "Pro Betters Flock to Prediction Markets"; Wharton interview (Jan 30, 2026): Kalshi **taker** fees are bad (~1.44% at 50c + spread ≈ paying 52.4 on a 50/51 market), so he **makes markets** off his model instead; sharp consensus = prediction markets are politically safe ~3 years **[EST]**.
- Counter-signal: **Kalshi affiliate badges are polluting X** — The Athletic (Dec 29, 2025): fake-insider accounts with Kalshi/Polymarket badges; @AllbrightNFL: "the majority of the people that have these prediction markets badges are awful and Kalshi is by far the worst"; @DustinGouker similar **[EST]**. Also NoVig voided winning NFL SGPs (Dec 2025 X backlash), and the OBBBA **90% gambling-loss deduction** (Rufus Jul 2, 2025 worked example of taxable phantom income; @ClosingDime Jul 3, 2025 thread, 1.5M views, "$55,000 owed on ~$31k profit"; Titus FAIR BET Act stalled as of Dec 2025) — a structural US-pro headwind pushing volume to 1256-treated exchanges **[EST]**.

## 5. Edges persist/died + methodology nuggets

- **PlusEVAnalytics (Andrew Mack)**: Aug 4, 2025: hypothesis testing should start with "a pivot table with game-by-game temperature", not XGBoost/regression; Aug 22, 2024: "don't use player name as a join key"; pinned: betting is not a contest of sports knowledge. Speaking at Bet Bash '26. No substantive new devig math threads surfaced via open web **[EST for quoted posts]**.
- **Market menu is shrinking at the exploitable end**: NFL memo (Nov 13, 2025) limiting/prohibiting player props; MLB $200 cap on pitch-level props post Clase/Ortiz indictments; NBA (Rozier/Billups arrests Oct-Nov 2025) + NBPA favor prop limits **[EST]**. Consistent with Captain Jack's long-held claim that NFL sides/totals are the world's most efficient market and edge lives in unmodeled/no-market-maker markets (his BetBash V line: share less in bigger circles; build small info-sharing groups — Chicago Sun-Times, Aug 23, 2025).
- **BettingIsCool**: Real-Kelly Excel solver (implementing Mack's Pinnacle article, Jan 2, 2025); CLV tracker backed by 1.5M fixtures/41 sports; 2025 "$10k challenge" betting Pinnacle steam (ChasingSteamers) — steam-following as a *public* strategy persists in EU soft-book land even as this platform's own backtests rejected steam as a model feature **[EST]**.
- **nishikoripicks**: canonical tennis-CLV thread is Nov 27, 2023 (n=4,511 own bets, does CLV apply to tennis; 156K views); 2025 output is mostly tennis color commentary, not methodology **[EST]**.

## Sharpest takeaways

1. **Pinnacle killed public real-time API access on 2025-07-23** — aggregator-sourced "Pinnacle" prices (incl. OddsPortal) can be delayed; anything downstream treating them as live sharp anchors inherits staleness risk (Buchdahl, Jul 29/Aug 12, 2025).
2. The credible CLV debate is no longer "does CLV matter" but **domain of validity**: valid only where a true market maker exists (Pinnacle/Circa-listed), only devigged, and weighted by when/why the line moved; props/derivatives/illiquid CLV is noise (Andrews, BettingIsCool, Kirk/Shipper/Pads).
3. Regulators now confirm on the record that **CLV is the limiting trigger** (MA public comments), and MA's 48h-notice + explanation rule (eff. Jun 2026) will generate the first public dataset of limiting rationales.
4. Sharp liquidity is visibly migrating to exchanges/prediction markets — with the specific mechanics that **taking is fee-expensive; the sharp play is market-making off a model** (Peabody), while the OBBBA tax asymmetry accelerates the exit from books.
5. Buchdahl's flatline post is best read with his own caveat — 3 seasons of top-down Pinnacle-anchored underperformance is **not statistically significant** — but combined with API closure, the free top-down soccer edge is being squeezed on both data access and price efficiency.

## Actionable for the platform

- Audit assumed freshness of Pinnacle-derived anchors post-2025-07-23; prefer own-capture (Arcadia) timestamps over aggregator claims.
- The MA rule creates a forthcoming corpus of book-stated limiting reasons — worth monitoring for which signals (CLV %, market mix, timing) books admit to using.
- BettingIsCool's public devig-CLV standard and trackabet app are useful external validators for the platform's CLV math; his betstamp callout is a ready-made cautionary example for the dashboard's "no fabricated CLV" stance.
- Exchange/prediction-market making (not taking) is where sharps say the residual edge is — consistent with keeping Betfair read-only capture as a first-class close source.

## Where X signal was too degraded to trust

- Direct x.com extraction returns sparse/randomized timelines; replies/threads (e.g., flatline-post replies, Spanky's exact ratio-cap tweet) unretrievable; @MagicianSpanky 404s (real handle @spanky, effectively unreadable). threadreaderapp site-search returned zero via Tavily.
- Since ~Dec 2025 the Kalshi/Polymarket affiliate-badge flood has measurably degraded X betting discourse itself (Athletic-documented), on top of the usual OddsJam/Outlier/POD/PickTheOdd SEO tool funnels dominating search. @PickTheOdd is a promo funnel, not methodology. Nothing substantive surfaced for @teddy_covers or @UnabatedSports beyond their content-arm output; "Shipper"/"Pads" exact handles unverified — treat those attributions as secondhand via thehammer.bet.

---
