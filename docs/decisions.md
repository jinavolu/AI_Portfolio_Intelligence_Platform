# Project decisions

Decisions that change how signals may be produced. Each is recorded before the evidence it
depends on is seen.

---

## D1. Signal safeguards (agreed 2026-09-28)

1. **rules-1.0.0 is frozen.** It is backtested as it is. Any change is a new version.
2. **Long-term safeguard:** for a `LONG_TERM` holding, price or technical evidence alone can never
   produce SELL. The most it can produce is REVIEW or a signal.
3. **Thesis safeguard:** a horizon or reason assigned in bulk or from a stock's profile stays a
   **draft** until the owner confirms it ("Confirm as my thesis", or editing and saving on the Stock
   page). Every decision records `thesis_status`: `missing`, `draft` or `confirmed`.
4. **Recommendation safeguard:** unvalidated rules produce **signals**, never BUY/SELL **candidates**.
5. **Missing evidence stays missing.** Until the fundamentals engine exists, the system reports
   "fundamental evidence unavailable" (`decision.evidence_unavailable`) and never infers that
   fundamentals are deteriorating.

---

## D2. Backtest sequence and holdout (agreed 2026-09-28)

1. Download and check historical data (Backtest page → History).
2. Test rules-1.0.0 separately for SHORT, MEDIUM and LONG term on the **development window**:
   every trading day before **2024-09-28**. It is judged on the window's last 40% against
   `backend/backtest_criteria.json` (locked 2026-09-28 12:00 IST).
3. Compare each run against Nifty 50 buy-and-hold (the criterion) and an equal-weight buy-and-hold
   of the same stocks (a second baseline, for information).
4. Costs, taxes and split/bonus adjustments are included. Dividends and delisted stocks are not
   (no data); every report lists this under limitations.
5. **Sealed holdout:** 2024-09-28 to the latest day. It is used **once per rule version and
   horizon**, only after that version passes on the development window, and judged against
   `backend/holdout_criteria.json` (locked 2026-09-28 17:15 IST): excess CAGR ≥ 2%, drawdown no
   worse than Nifty's, ≥ 20 trades.
6. A rule version and horizon may be marked **validated** only after both passes. Only then do its
   BUY/SELL signals become candidates.
7. Runs are stored immutably, with the rule-version hash and the hashes of both criteria files.
8. Passing means meeting criteria fixed in advance. It does not predict future performance.

---

## D3. rules-1.1.0: specification (pre-registered 2026-09-28, before any real backtest)

Written before rules-1.0.0 has been backtested on real market data, so its results cannot shape
this design. rules-1.1.0 is built and tested **only if** rules-1.0.0 LONG_TERM fails. It must not
be changed after results are seen; a further change would be rules-1.2.0.

**Scope.** `LONG_TERM` only. `SHORT_TERM` and `MEDIUM_TERM` behave exactly as in rules-1.0.0. All
weights, thresholds (buy 0.6, hold 0.0, sell −0.6), freshness limits and gates 1, 2, 3, 5 and 6 are
unchanged.

**Evidence and maximum outcome for a LONG_TERM holding**

| Evidence | Maximum outcome |
| --- | --- |
| Price below the 200-day average alone | REVIEW |
| Technical deterioration alone (score ≤ −0.6) | REVIEW |
| Thesis invalidation condition met alone | REVIEW |
| Thesis broken **and** fundamentals deteriorating **and** long-term trend broken | Eligible for SELL evaluation |

**Exact rule**

- *SELL-eligible* means all three are true:
  1. at least one thesis invalidation condition is met (gate 4 would fire);
  2. fundamental evidence is **available** and its component score is ≤ −0.5. This score is defined
     by the future fundamentals engine (plan Phase 11). While it doesn't exist, this condition is
     **false** and the reason includes "fundamental evidence unavailable";
  3. `long_term_trend == BELOW_SMA200`.
- SELL-eligible and score ≤ −0.6 → `SELL_SIGNAL`, or `SELL_CANDIDATE` once validated.
- SELL-eligible and score > −0.6 → REVIEW.
- Not SELL-eligible, but gate 4 fires or score ≤ −0.6 → **REVIEW**, with the reason naming the
  evidence present and "price/technical evidence alone cannot produce SELL for a long-term holding".
- Otherwise the rules-1.0.0 outcome applies (BUY band, HOLD band, neutral band, DON'T ADD).

**Backtest treatment (stated in advance)**

- REVIEW is **no trade**: the position is held.
- Thesis conditions cannot be replayed historically, and fundamentals are unavailable, so condition
  1 and condition 2 are false in backtests. rules-1.1.0 LONG_TERM therefore never exits on a signal
  and closes positions only at the end of the window. This is expected: the backtest measures its
  **entry** selection plus holding, against Nifty 50 and equal-weight buy-and-hold.
- It faces the same development criteria and then the one-time sealed holdout.

---

## D5. rules-1.0.0 development results (runs #1–#3, 2026-09-29)

Nifty 500 (today's constituents), 10 years of history; development window 2017-08-17 → 2024-09-27;
out-of-sample from 2021-11-26. **All three horizons failed** the development criteria, so BUY/SELL
remain signals and the sealed holdout stays unused.

| Horizon | OOS CAGR | Nifty 50 | Equal-weight hold | OOS max DD (Nifty) | OOS trades | Regimes present |
| --- | --- | --- | --- | --- | --- | --- |
| LONG_TERM | 38.3% | 13.0% | 30.9% | −16.8% (−16.5%) ✗ | 44 ✗ | bull, sideways ✗ |
| MEDIUM_TERM | 47.2% | 13.0% | 30.9% | −20.3% (−16.5%) ✗ | 29 ✗ | bull, sideways ✗ |
| SHORT_TERM | 58.5% | 13.0% | 30.9% | −21.2% (−16.5%) ✗ | 30 ✗ | bull, sideways ✗ |

Findings:
- The returns are **not credible as stated**. Equal-weight buy-and-hold of the same stocks earned
  24.6% a year over the full window, against Nifty 50's 13.3%. Most of the apparent edge comes from
  survivorship: today's Nifty 500 is made of past winners. Momentum and trend rules benefit most from
  this bias, because many stocks joined the index *after* rising.
- No calendar year from 2018 to 2023 had a negative Nifty return, so the bear-regime test cannot be
  met by any rule set on this window. That is a property of the data, not of the rules.
- As pre-registered in D3, rules-1.0.0 LONG_TERM failed, which triggers building rules-1.1.0.

## D6. Backtesting paused (2026-09-29)

After D5 the owner chose to stop backtesting for now instead of building rules-1.1.0 or extending
history.

- rules-1.0.0 remains the live rule set, unvalidated: every BUY/SELL is a **signal**, never a
  candidate. The sealed holdout (from 2024-09-28) is still unused and stays sealed.
- The rules-1.1.0 specification in D3 stays pre-registered and unchanged, ready if backtesting resumes.
- Resume when point-in-time index constituents (free of survivorship bias) are available, or if the
  owner decides to use a longer history that includes bear years. Either choice is recorded here first.

## D7. Monitoring first; condition categories (agreed 2026-09-29)

The owner chose **direction B** (trustworthy monitoring of the 66 holdings) now, with direction A
(survivorship-free backtesting) kept as a later, independent project. Tax is out of scope for B.
Design: `docs/monitoring-design.md`.

1. **Three condition categories.** THESIS (the owner's reason for holding), BUSINESS (reported
   fundamentals) and TECHNICAL (price, trend, volume, position size).
2. **Only owner-confirmed (ACTIVE) THESIS and BUSINESS conditions reach gate 4.** Suggested
   conditions stay PROPOSED and are not evaluated until the owner accepts them. TECHNICAL conditions
   never reach gate 4 and never produce SELL; they raise a technical warning.
3. **Gate 4 and rules-1.0.0.** Gate 4's logic is unchanged; its input is now the ACTIVE THESIS and
   BUSINESS conditions. Conditions saved before D7 load as THESIS / OWNER / ACTIVE, so every
   existing decision is reproduced exactly. The rule version stays rules-1.0.0.
4. **Every condition result is MET, NOT_MET or CANNOT_CHECK.** Missing or stale data is
   CANNOT_CHECK with a reason, never read as met or as passed.
5. **Default technical warnings** are portfolio-wide settings with per-holding overrides, labelled
   "default warning, not your thesis": −25% from the 52-week high and −20% unrealised loss on cost.
6. **Technical warning severity:** `info` for LONG_TERM holdings, `medium` for SHORT and MEDIUM term.
7. **Business data freshness:** latest quarter ended within 150 days and fetched within 7 days;
   otherwise CANNOT_CHECK "financial data overdue". The quarter-end date is shown with every
   business value, and growth alerts show both raw quarters.
8. **Suggested thesis conditions** may be drafted from `why_bought`, always PROPOSED. If the reason
   is vague or missing, nothing is suggested.
9. **Business conditions are enabled only after** the owner has reviewed the MarketLens validation
   report for all 66 holdings.
10. Monitoring conditions never enter the rules-1.0.0 score, a backtest, or any rule validation, and
    are never presented as evidence that BUY/SELL signals work.

## D8. Fundamentals validation reviewed; business conditions enabled (2026-09-29)

The owner reviewed the MarketLens validation report for all 66 holdings (monitoring-design.md
section 4) and accepted the recommendations. This satisfies D7.9.

1. **P/E is not used.** The provider's P/E matched price / trailing EPS for only 12 of 50 holdings.
2. **Figures are standalone**, labelled as such wherever shown (confirmed against published June-2025
   results of HDFC Bank, ICICI Bank and HAL). For groups, standalone is not the group picture.
3. **Gaps are accepted and shown, never filled:** holdings the provider doesn't carry (8 of 66), and
   year-on-year metrics where no year-ago quarter is returned (25 of 58), are CANNOT_CHECK with the
   reason.
4. Used: revenue, profit and profit-before-tax growth (latest quarter vs year-ago), latest-quarter net
   profit, loss-making quarters among the last four, P/B, debt-to-equity (non-financials) and promoter
   holding.

## D9. rules-1.2.0: volume confirmation for BUY (agreed 2026-09-29)

The owner decided that a buy signal must be backed by trading volume. Recorded before implementation.

1. **Rule.** In the buy band (score ≥ 0.6), BUY requires the average volume of the **last 5 completed
   sessions** to be at least **1.0×** the average of the last 20 completed sessions (those 5
   included). Otherwise the state is **HOLD**, with the reason naming the volume shortfall. If volume
   data is missing, BUY is not confirmed and the state is HOLD.
2. **Completed sessions.** During market hours (before 15:30 IST) today's candle is incomplete and is
   excluded.
3. **Everything else is unchanged from rules-1.0.0:** weights, thresholds, gates 1–6, concentration
   (DON'T ADD still takes precedence in the buy band), SELL logic and all horizons.
4. **Unvalidated.** Backtesting is paused (D6), so every BUY/SELL from rules-1.2.0 is a signal, never a
   candidate. The backtest engine does not implement the volume filter yet and refuses to run
   rules-1.2.0 rather than mislabel results; implementing it is a precondition for resuming.
5. rules-1.0.0 stays in the history (its runs, D5). rules-1.1.0 (D3) stays pre-registered and unbuilt;
   if backtesting resumes, its relation to rules-1.2.0 must be recorded here first.

## D10. rules-1.3.0: heavy-volume breakouts and breakdowns drive signals (agreed 2026-09-29)

The owner decided that strong buying or selling on heavy volume should produce a signal, keeping
the long-term safeguard (D1.2). Recorded before implementation. rules-1.3.0 = rules-1.2.0 plus:

1. **Events.** A *breakout* is a session that closes above the highest high of the previous 20
   sessions on at least 1.5× the average volume of the previous 20 sessions; a *breakdown* closes
   below the lowest low on the same volume. (Same definitions as the technical engine.)
2. **Completed sessions only.** Before 15:30 IST the event is judged on the last completed session,
   never on today's unfinished candle (as D9.2). The event signal therefore lasts from the event's
   close until the next session completes; after that the score bands apply again.
3. **Breakdown** → **SELL signal** for SHORT_TERM and MEDIUM_TERM holdings, whatever the score. For
   LONG_TERM holdings → **REVIEW**: price and volume alone never produce SELL for a long-term holding
   (D1.2 stands).
4. **Breakout** → **BUY signal** on every horizon, whatever the score. It is volume-confirmed by
   definition, so the rules-1.2.0 five-session filter (D9) applies only to score-based buys. A
   concentration breach still turns it into DON'T ADD (gate 6).
5. Gates 1–5 come first, unchanged: stale or missing data, no thesis, a met thesis condition or an
   event window still decide before any of this.
6. **Unvalidated.** Signals only, never candidates; the backtest engine refuses rules-1.3.0 until it
   implements both filters (as D9.4). Heavy-volume breakouts and breakdowns often reverse; nothing
   here has been tested.

## D11. rules-1.4.0: news, in shadow mode first (agreed 2026-09-29)

The owner decided that official events should trigger review and that a news score should feed
buy/sell signals, and accepted a review that separated the two. Recorded before implementation. Plan:
`news-based signals` (Phase 9). rules-1.4.0 = rules-1.3.0 plus two independent pathways that share one
set of validated news records:

1. **Risk pathway → gate 5.** Only **official NSE disclosures** create events; press never does.
   Windows come from one table (`EVENT_WINDOWS`) and are computed from the event date at snapshot
   time, so an event discovered late still opens its window:
   - RESULTS (board meeting to consider results): 7 days before, 0 after;
   - RESULTS_ANNOUNCED: 0 before, 2 after;
   - MAJOR_EVENT (official item rated HIGH materiality with a material event type: rating change,
     KMP/auditor exit, pledge or default, fraud or investigation, regulatory action, merger or
     acquisition, large order, other material): 0 before, 7 after;
   - CORPORATE_ACTION: shown only.
   Sentiment never cancels a risk event.
2. **Scoring pathway → news component.** Items of the last 7 days (by publication date) count only
   if relevance is DIRECT_COMPANY or SUBSIDIARY, confirmation is CONFIRMED or REPORTED, and the item
   is official or content-verified press. Items are grouped into **event clusters** (same event type
   within 3 days and the same cluster key); each cluster contributes once, represented by its best
   item (official first, then highest materiality). With s = rating/2 (rating −2..+2), materiality
   m = 1.0 / 0.5 / 0.2 and recency r = 1.0 (0–1 days), 0.75 (2–3), 0.5 (4–5), 0.25 (6–7):
   **NewsScore = Σ s·m·r / Σ m·r**, in [−1, +1]. Press-only clusters carry at most 50% of Σ m·r when
   an official cluster exists. **Confidence** comes from evidence, never the model: HIGH with an
   official cluster, MEDIUM with at least two independent verified press publishers, LOW otherwise.
   LOW confidence, no qualifying items or stale data → the component is **None** (missing).
3. **Weights.** News takes 15% of the score (short and medium term) and 10% (long term); the existing
   weights are scaled by (1 − share). Because the score normalises by the weights present, a missing
   news component gives exactly the rules-1.3.0 score. Thresholds, gates, D9, D10 and D1.2 are
   unchanged. The shares are **experimental parameters**, not validated weights.
4. **Diagnostics** on every 1.4.0 decision: score with and without news, the news contribution, and
   the impact class NO_IMPACT / REINFORCED / MODIFIED / REVERSED / BLOCKED.
5. **Shadow mode.** The live rule set stays **rules-1.3.0**. Each snapshot also computes the 1.4.0
   decision as a shadow for every holding; it changes no signal, summary or signal alert. Activating
   1.4.0 is a separate decision after the owner reviews the shadow results. (Alerts about news itself,
   such as a new material official disclosure, are information and are raised regardless.)
6. **Untrusted content.** News text, PDFs and search results are data; instructions in them are never
   followed. Prompts carry no secrets. Model output is schema-validated; ratings must quote the item
   text verbatim as evidence; all numbers and decisions are computed in Python.
7. **Unvalidated.** News can't be backtested: signals only, never candidates; the backtest refuses
   rules-1.4.0.
8. Holdings without NSE disclosures (e.g. BSE-only) are **NOT_COVERED**, never read as "no news".
9. *Refinements found while building (same day, before any shadow results):* items that are routine
   filings, reports only about the share price (PRICE_MOVE: the price rules already measure it) and
   neutral items (sentiment 0: no direction) are shown but not scored.

## D4. Draft theses (2026-09-28)

All 66 real holdings received profile-based **draft** horizons (30 long, 29 medium, 7 short term),
assigned by the assistant, not inferred from the owner's reasons. Each draft's notes say so. They
unlock signals but remain `thesis_status = draft` until confirmed on the Stock page.

## D12. Accounts, and chart-based draft theses (2026-09-30)

One database serves every Kite user. Theses, trades, snapshots, daily snapshots, alerts and settings
are kept per `account` (the Kite `user_id` from `get_profile`); market data is shared. Rows from
before accounts were claimed by the first account whose holdings matched the old theses.

A holding with no thesis and at least 60 sessions of price history gets a **draft** thesis
automatically, so its chart-based signal shows. The horizon comes from annualised volatility of
daily returns over the last year: under 30% long term, 30–45% medium term, 45% or more short term.
The reasons use the D4 wording; the notes give the volatility. Drafts stay `thesis_status = draft`
until confirmed on the Stock page, and the thesis change raises no signal-change alert.

## D13. Telegram channel feed: read-only, never a signal input (2026-09-30)

Public Telegram channels chosen by the owner are shown on a Feed page, read from Telegram's public
web preview (no login; `PI_TELEGRAM_WEB_URL`, default telegram.me because t.me is blocked on some
networks). Holdings named by NSE symbol are highlighted. Posts are unverified opinions, so they are
never used in signals, alerts or the news score (D11).

Every NSE stock a post names is tagged, by symbol or by company name (from Kite's instrument list;
one-word names only when capitalised, standing alone and in a market post). Colour is the post's
own wording in that sentence: green buy/invest, red sell/exit, grey news or unclear; a post naming
three or more stocks never colours a stock from words elsewhere in the post. Held stocks are bold.
Posts that link to YouTube are not shown. This is the channel's view, not the app's.

Images (mostly screenshots of news-wire posts) are read offline with RapidOCR's English model, not
Gemini (owner's choice: no API cost). Each image is read once in the background and cached; Telugu in
images is not read. Text from an image is treated as news: a stock found there is coloured only for
tip wording ("BUY: X TGT 780", "SELL @ 410"), never for business words like "sell" or "investment".

## D14. Scanner: the live rules over the Nifty 500 (2026-10-01)

A Scanner page runs the live rule set on every stock in the stored index universe (Nifty 500),
including stocks the owner doesn't hold. Same code as a holding: `compute_technicals` → `decide()`
with `scan=True`, which only drops gate 2's "holding required"; freshness, events and scoring are
unchanged, and the horizon is the chart-based draft of D12. Prices come from the local EOD store;
"Refresh prices" asks Kite for the last 10 days per stock and merges them. For a stock not held,
a SELL state is shown as "Weak: not a buy now". The rule set is not back-tested, so the page says
buy signals are prompts to look, not recommendations, and are for personal use only (SEBI).

The Scanner also shows, per stock, what the saved Telegram channels said in their last ~60 posts
(counts of buy / sell / news mentions, the latest line, links) and tags "Telegram agrees" or
"Telegram disagrees" when a chart BUY/SELL meets the opposite or same post wording. Telegram never
changes the state or the score (D13); stocks named on Telegram outside the index are listed apart.
