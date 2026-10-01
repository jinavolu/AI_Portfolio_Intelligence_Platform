# Monitoring (direction B): data model and condition catalogue

Status: **agreed** (2026-09-29). The owner's decisions on the open questions are frozen in
`decisions.md` D7, which also records the direction and its separation from backtesting.

Goal: tell the owner **when a holding needs attention and why**, in terms of the reason they own it.
It does not predict prices and it produces no trading rule.

---

## 1. What exists today, and the gaps

| Area | Today | Gap |
| --- | --- | --- |
| Theses | Versioned, append-only (`theses` table). 66 real drafts, **no conditions**. | Conditions have no category, origin or confirmation state. |
| Condition evaluation | Deterministic (`decision.condition_metrics`, `evaluate_condition`). Missing metric = unevaluable, never met. | Only price/technical/position metrics. |
| Gate 4 | Any invalidation condition met → REVIEW. | Cannot tell an owner's thesis condition from a generic warning. |
| Alerts | Transitions between snapshots; each alert stores `snapshot_id`. | `THESIS_CONDITION` details hold only `{"gate": 4}`: no condition, value, source or as-of. No category. |
| Fundamentals | None (`HoldingSnapshot.fundamental = None`). | Provider, validation, business metrics. |
| Stock page | Thesis form; conditions edited as raw JSON. | Condition editor with categories; review queue. |

---

## 2. Three categories

| Category | Based on | Who creates it | Effect when met |
| --- | --- | --- | --- |
| **THESIS** | The owner's reason for holding | Owner, or the assistant as a suggestion the owner must accept | Gate 4 → REVIEW; alert `THESIS_CONDITION` (high) |
| **BUSINESS** | Reported fundamentals (revenue, profit, debt, valuation) | Owner, or accepted suggestion | Gate 4 → REVIEW; alert `BUSINESS_CONDITION` (high) |
| **TECHNICAL** | Price, trend, volume, position size | Default warnings, or owner | Alert `TECHNICAL_WARNING` (medium) and a review-queue item. **Never gate 4. Never SELL.** |

Rules:

1. **Only owner-confirmed THESIS and BUSINESS conditions reach gate 4.** A suggested condition is
   shown as a suggestion and is not evaluated until the owner accepts it.
2. **Generic defaults are always TECHNICAL** and labelled "default warning, not your thesis". They
   are portfolio-wide settings, not rows copied into each of the 66 theses.
3. The **owner may put a technical metric in a THESIS condition** (e.g. "my reason was momentum; if
   it closes below the 200-day average my reason is gone"). Only the owner can do that; the app never
   promotes a technical warning to a thesis condition. For `LONG_TERM` holdings D1.2 still applies:
   the most it produces is REVIEW.
4. Every evaluation has exactly one of three results, shown explicitly in the UI:
   **MET**, **NOT_MET**, or **CANNOT_CHECK** with its reason (data missing, data stale, financial
   data overdue). CANNOT_CHECK is never displayed or counted as met or as passed.

### Condition record

Stored inside the thesis JSON (so it stays versioned with the thesis):

```text
InvalidationCondition
  id            str       stable, e.g. "c1"; survives edits
  category      THESIS | BUSINESS | TECHNICAL
  metric        str       must be in the catalogue (section 3), and allowed for the category
  op            < <= > >= == !=
  value         float | str
  description   str       the owner's words, shown in alerts
  origin        OWNER | SUGGESTED
  status        ACTIVE | PROPOSED | DISMISSED
                          SUGGESTED starts as PROPOSED; only ACTIVE is evaluated
  confirmed_at  datetime | None   set when the owner accepts or writes it
```

Migration: existing conditions (only in the fixture/sample theses) become `THESIS / OWNER / ACTIVE`,
so gate 4 behaves exactly as before. The 66 real theses have none and are unchanged, including their
profile-assigned draft horizons (D4).

**Rule-version note.** rules-1.0.0 is frozen (D1.1). Gate 4 keeps its logic ("an owner invalidation
condition is met → REVIEW"); what changes is which conditions are its input. Existing behaviour is
preserved by the migration above. This is recorded in D7 so it is not a silent change.

### Portfolio-wide technical warnings (defaults)

Settings, editable once for all holdings, with per-holding overrides (disable or change a threshold):

Severity of every technical warning: **`info` for LONG_TERM holdings, `medium` for SHORT and
MEDIUM term**, so long-term holdings stay visible without urgency.

| Warning | Metric | Default | Existing alert it replaces |
| --- | --- | --- | --- |
| Below 200-day average | `long_term_trend == BELOW_SMA200` | on | `LONG_TERM_TREND_BROKEN` |
| Far below 52-week high | `pct_from_52w_high < x` | x = −0.25 | new |
| Loss on cost | `pnl_pct < x` | x = −0.20 | new |
| Support broken / breakdown | existing detectors | on | `SUPPORT_BROKEN`, `BREAKDOWN` |
| Position / sector over limit | existing risk limits | on | `CONCENTRATION`, `SECTOR_CONCENTRATION` |

The existing technical alert types keep working; they are tagged `TECHNICAL`.

---

## 3. Metric catalogue

Every metric has a unit, a source, an as-of date and a freshness limit. A value older than its limit
counts as **missing**.

### Technical and position (existing, source: Kite + computed)

| Metric | Meaning | Unit | As-of | Freshness | Categories |
| --- | --- | --- | --- | --- | --- |
| `last_price` | Last traded price | ₹ | holdings fetch | 18 h | TECHNICAL, THESIS |
| `pnl_pct` | Unrealised P&L on cost | fraction | holdings fetch | 18 h | TECHNICAL, THESIS |
| `weight` / `sector_weight` | Share of portfolio | fraction | holdings fetch | 18 h | TECHNICAL, THESIS |
| `rsi14`, `macd_hist`, `ema20`, `ema50`, `sma200` | Indicators | as named | last candle | 4 days | TECHNICAL, THESIS |
| `volume_ratio` | Volume / 20-session average | ratio | last candle | 4 days | TECHNICAL, THESIS |
| `pct_from_52w_high` | Distance from 52-week high | fraction | last candle | 4 days | TECHNICAL, THESIS |
| `close_vs_sma200` | Close / SMA200 − 1 | fraction | last candle | 4 days | TECHNICAL, THESIS |
| `trend`, `long_term_trend` | Trend labels | enum | last candle | 4 days | TECHNICAL, THESIS |

### Business (new, source: fundamentals provider, section 4)

All growth figures are computed by us from the quarterly series, **latest quarter vs the same
quarter a year earlier**, so seasonality cancels. The alert always shows both raw values and both
period labels.

| Metric | Meaning | Unit | Missing when | Categories |
| --- | --- | --- | --- | --- |
| `revenue_yoy` | Total income, latest quarter vs year-ago quarter | fraction | either quarter absent; base ≤ 0 | BUSINESS, THESIS |
| `profit_yoy` | Net profit, same comparison | fraction | either quarter absent; **base ≤ 0** (growth from a loss is undefined) | BUSINESS, THESIS |
| `pbt_yoy` | Profit before tax, same comparison | fraction | as above | BUSINESS, THESIS |
| `net_profit_q` | Net profit, latest quarter | ₹ crore | absent | BUSINESS, THESIS |
| `loss_quarters_4` | Loss-making quarters among the last 4 | count | fewer than 4 quarters | BUSINESS, THESIS |
| `pe_ratio` | Price / earnings | ratio | 0 or negative | BUSINESS, THESIS |
| `pb_ratio` | Price / book | ratio | 0 or negative | BUSINESS, THESIS |
| `debt_to_equity` | Debt / equity | ratio | **banks and NBFCs** (not meaningful); 0 when total debt is not also reported as 0 | BUSINESS, THESIS |
| `promoter_holding` | Promoter stake | fraction | 0 when the company is not known to be promoter-less | BUSINESS, THESIS |

Freshness: the **latest quarter must have ended within 150 days** (results are due 45–60 days after
quarter end, so this allows one late filing), and the data must have been **fetched within 7 days**.
Otherwise every business condition is **CANNOT_CHECK: financial data overdue** (or "not fetched
recently"), never a pass. The quarter-end date is shown prominently next to every business value.

Excluded on purpose: EPS growth (distorted by bonuses and splits, e.g. HDFC Bank's June 2025 EPS),
and provider-computed fields (ROE, ROCE, revenue growth, net margin, profit growth). In the sample
they were zero or plainly wrong.

**One-off items.** A quarter with a one-off gain or loss (e.g. TMPV's demerger quarter) distorts
YoY growth. The app does not try to detect this; it shows the two raw quarters so the owner can see
it, and the owner can dismiss that alert.

---

## 4. Fundamentals provider

```text
FundamentalsProvider (interface)
  name: str
  fetch(symbol) -> FundamentalsRecord | ProviderError

FundamentalsRecord
  symbol, provider_symbol          e.g. TATAMOTORS -> TMPV (alias table, owner-editable)
  provider, fetched_at
  quarters: [{period_end, total_income, pbt, net_profit}]   normalised to ₹ crore
  ratios:   {pe_ratio, pb_ratio, debt_to_equity, promoter_holding}  None when missing
  basis:    STANDALONE | CONSOLIDATED | UNKNOWN
  raw:      the untouched response, kept for audit
  issues:   ["debt_to_equity=0 treated as missing", ...]
```

- First implementation: **NSE MarketLens** (`/api/stocks/{symbol}` and
  `/api/stocks/{symbol}/quarterly-financials`). Undocumented beta; one fetch per holding per day,
  with a pause between requests. Replaceable without touching conditions or alerts.
- **Validation before use.** Fetch all 66 once, save the raw responses as fixtures, and produce a
  validation report: coverage (found / not found / alias needed), fields that are zero or implausible,
  units (checked against a few known published results), and basis. Business metrics are enabled
  only after the owner has seen that report.
- Stored in the snapshot as `HoldingSnapshot.fundamental`, so every alert's evidence is replayable.

### Validation result (2026-09-29, all 66 holdings)

Full report: `backend/fundamentals_payloads/2026-09-29/REPORT.md` (git-ignored; regenerate with
`scripts/fundamentals_validate.py`). **Reviewed and accepted by the owner (D8).**

| Finding | Result | Consequence |
| --- | --- | --- |
| Coverage | 58 of 66 found. Not found: BONDADA, EVIETF, HFCL-BE, NSDL, NSE, RELINFRA-BE, SPICEJET, STLTECH-BE (the provider doesn't carry them; search finds nothing) | Those 8 show CANNOT_CHECK "no fundamentals from provider" |
| Freshness | All 58 report the quarter ended 2026-06-30 (91 days) | Within the 150-day limit |
| Units and basis | Quarterly figures are ₹ lakh, **standalone**: HDFC Bank, ICICI Bank and HAL June-2025 quarters equal their published standalone results | Every business value is labelled "standalone". For groups (e.g. Bharti Airtel, Hindalco, Tata Power, Jio Financial) standalone ≠ group |
| Year-ago quarter | Available for 33 of 58; the rest return only 2–4 quarters | `revenue_yoy`, `profit_yoy`, `pbt_yoy` are CANNOT_CHECK "year-ago quarter not available" for 25 holdings |
| Duplicate / conflicting quarters | 2 duplicated (kept once); 1 conflicting (TRANSRAILL Sep-2025, likely a half-year total): treated as missing | Handled in `normalise_marketlens` |
| P/B | Consistent with price / book value for 51 of 51 | Keep `pb_ratio` |
| Debt-to-equity | Consistent with total debt / book equity for 35 of 35 non-financials | Keep `debt_to_equity` (non-financials only) |
| P/E | Consistent with price / last four quarters' EPS for only 12 of 50; provider EPS also inconsistent | **Proposed: drop `pe_ratio`** from the catalogue |
| Provider-computed fields | ROE, revenue growth, 7-year ROCE, industry P/E are zero for all 58 | Excluded, as designed |
| Structural breaks | e.g. TMPV profit −98.6% YoY (demerger), BAJAJELEC +2,824% (tiny base) | Not detected automatically; alerts show both raw quarters |

---

## 5. Alert evidence

Every condition alert stores, in `details`:

```text
category        THESIS | BUSINESS | TECHNICAL
condition       the full condition record (id, metric, op, value, description, origin)
thesis_version  version of the thesis it came from (None for portfolio-wide warnings)
actual          the value that met it
period          e.g. "quarter ended 30-Jun-2026 vs 30-Jun-2025"   (business only)
inputs          the raw values used, e.g. both quarters' net profit
source          kite | computed | marketlens
source_as_of    candle date, holdings fetch time, or quarter end
fetched_at      when the source was read
```

Together with the existing `snapshot_id`, this makes each alert self-explaining after the fact.

---

## 6. Review queue

One list, newest first, grouped by holding. Each item answers three questions:

| Question | Shown as |
| --- | --- |
| What changed? | The alert message with actual value, threshold, period and as-of |
| Why does it matter? | Category label, and for THESIS/BUSINESS the owner's reason for holding and the condition's own description |
| What now? | Links: open Stock page, edit thesis, accept / dismiss a suggestion, acknowledge |

Separate section: **Suggestions awaiting your decision** (PROPOSED conditions), and **cannot check**
items (conditions whose data is missing or stale). Technical warnings are visually distinct from
thesis and business conditions.

---

## 7. Out of scope

Tax (lot terms, estimated tax) is not part of B. The existing tradebook and tax code is left as it is.

---

## 8. Separation from backtesting (direction A)

- Monitoring conditions and warnings never enter the rules-1.0.0 score, a backtest, or the
  validation of any rule version.
- Nothing in B is presented as evidence that BUY/SELL signals work.
- A fundamentals **score** (D3 condition 2) is not built in B.

---

## 9. Implementation order

| # | Step | Status today |
| --- | --- | --- |
| 1 | Preserve the 66 drafts and their horizons | Done; nothing to change |
| 2 | Condition record (categories, origin, status) + migration; condition editor on the Stock page replacing raw JSON | **Done 2026-09-29.** Also: MET / NOT_MET / CANNOT_CHECK results in each snapshot, per-category condition alerts with evidence, suggestions accept/dismiss |
| 3 | Portfolio-wide technical warnings as settings; tag existing technical alerts | **Done 2026-09-29.** Defaults `w_sma200`, `w_52w_high`, `w_cost_loss` in `app/tech_warnings.py`, stored in `app_settings`; edited on the Alerts page (portfolio) and Stock page (per holding). `LONG_TERM_TREND_BROKEN` is replaced by `w_sma200`. Support-broken and breakdown alerts now follow D7.6 severity; concentration stays medium (a risk limit, not a price move). Alerts fire only on a market change: not on a default's first evaluation, and not when its threshold was just edited |
| 4 | Provider interface + MarketLens + validation report for all 66 | **Done 2026-09-29** (`app/fundamentals.py`, `scripts/fundamentals_validate.py`). Report awaiting the owner's review; results in section 4 |
| 5 | Business metrics in the catalogue and in deterministic evaluation | **Done 2026-09-29** (D8). Fetched daily at 07:00 IST by the scheduler or on demand (`app/fundamentals_service.py`), stored in `fundamentals`; snapshots read the stored fetch only. Business conditions are CANNOT_CHECK with the reason when data is missing, overdue or not covered. Stock page Fundamentals card; business alerts carry both raw quarters. `pe_ratio` dropped. The rules-1.0.0 `fundamental` score component stays unbuilt |
| 6 | Alert evidence fields (section 5) | Partly exists (`snapshot_id`) |
| 7 | Review queue page | New (alerts list exists) |
| 9 | News (Phase 9, D11) | **Built 2026-09-29, in shadow mode.** `app/news.py` (NSE client, events), `app/news_score.py` (deterministic score), `app/ai/news_rater.py` (grounded discovery, page verification, rating), `app/news_service.py`; Stock page News card, `NEWS_MATERIAL` alert, shadow table on the Backtest page. Live signals stay rules-1.3.0 |
| 8 | Suggested thesis conditions (D7.8) | **Done 2026-09-29.** All 66 drafts carried one of three app-written template reasons (D4), so suggestions start from the owner's own words: the owner's own reason (the separate "Your reasons" page was removed at the owner's request) feeds `POST /api/theses/{symbol}/suggest` (`app/ai/suggester.py`) proposes up to three conditions. Each must quote the owner's words verbatim, use a catalogue metric that has a value for that holding, and pass value checks; otherwise it is dropped with the reason shown. App-written, vague (< 4 words) or missing reasons get none. Saved as SUGGESTED / PROPOSED; the page warns when a suggestion is already met today |

---

## 10. Owner decisions (frozen in D7)

| # | Question | Decision |
| --- | --- | --- |
| 1 | Default technical thresholds | −25% from 52-week high and −20% unrealised loss on cost, configurable, portfolio-wide with per-holding overrides |
| 2 | Technical warnings on LONG_TERM holdings | `info`; `medium` for SHORT and MEDIUM term. Never gate 4 |
| 3 | Business freshness | 150 days after quarter end and fetched within 7 days; quarter-end date shown prominently; otherwise CANNOT_CHECK "financial data overdue" |
| 4 | Suggested thesis conditions | Drafted from `why_bought`, always PROPOSED until accepted. Vague or missing reason → no suggestion |

Refinements agreed with them: MET / NOT_MET / CANNOT_CHECK are explicit in the UI (section 2,
rule 4), and every growth alert shows both raw quarters with their period labels (section 3).
Business conditions stay disabled until the owner has reviewed the MarketLens validation report.
