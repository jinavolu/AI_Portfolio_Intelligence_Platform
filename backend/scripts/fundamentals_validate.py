"""Step 4 validation (monitoring-design.md section 4, D7.9): fetch fundamentals for every holding once,
save the raw responses, and write a report for the owner to review before business conditions are
enabled.

    uv pip install -e ".[fundamentals]"
    uv run python scripts/fundamentals_validate.py [--broker kite_mcp] [SYMBOL ...]

Symbols default to the holdings with a thesis in the broker's database (no Kite login needed).
Output goes to fundamentals_payloads/<date>/ (git-ignored: it reveals your holdings).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BACKEND_DIR, Settings  # noqa: E402
from app.db import Repository  # noqa: E402
from app.fundamentals import (FundamentalsRecord, MarketLensProvider, ProviderError, load_aliases,  # noqa: E402
                              year_ago, yoy)

MAX_QUARTER_AGE_DAYS = 150  # D7.7


def check(rec: FundamentalsRecord, today: date) -> dict:
    """Per-holding findings: freshness, quarter spacing, YoY computability, provider consistency."""
    s = rec.raw["stock"]["data"]
    q = rec.quarters
    out: dict = {"quarters": len(q), "latest": q[0].period_end.isoformat() if q else None}
    out["age_days"] = (today - q[0].period_end).days if q else None
    out["overdue"] = out["age_days"] is None or out["age_days"] > MAX_QUARTER_AGE_DAYS
    gaps = [(a.period_end - b.period_end).days for a, b in zip(q, q[1:])]
    out["irregular_spacing"] = [g for g in gaps if not 80 <= g <= 100]
    pair = year_ago(q)
    out["yoy_pair"] = (pair[0].period_end.isoformat(), pair[1].period_end.isoformat()) if pair else None
    out["revenue_yoy"] = yoy(pair[0].total_income, pair[1].total_income) if pair else None
    out["profit_yoy"] = yoy(pair[0].net_profit, pair[1].net_profit) if pair else None
    out["loss_quarters_4"] = sum(1 for x in q[:4] if x.net_profit is not None and x.net_profit < 0) if len(q) >= 4 else None
    # Consistency. The provider's EPS should be the last four quarters' EPS (TTM); P/E should be about
    # price / that EPS; P/B about price / book value. Ratios near 1.0 mean consistent.
    price, eps, book = s.get("closePrice"), s.get("eps"), s.get("bookValue")
    pe, pb = rec.ratios.get("pe_ratio"), rec.ratios.get("pb_ratio")
    q_eps = [x.get("eps") for x in rec.raw["quarters"]["data"] or []]
    dates = [x.get("date") for x in rec.raw["quarters"]["data"] or []]
    ttm = sum(q_eps[:4]) if len(q_eps) >= 4 and len(set(dates[:4])) == 4 and None not in q_eps[:4] else None
    out["eps_vs_ttm"] = round(eps / ttm, 3) if eps and ttm and ttm > 0 else None
    out["pe_check"] = round(price / ttm / pe, 3) if pe and price and ttm and ttm > 0 else None
    out["pb_check"] = (round(price / book / pb, 3) if pb and price and book and book > 0 else None)
    # D/E should be about total debt / (book value per share * shares), shares = market cap / price.
    de, debt, mcap = rec.ratios.get("debt_to_equity"), s.get("totalDebt"), s.get("marketCap")
    out["de_check"] = (round(debt / (book * mcap / price) / de, 3)
                       if de and debt and mcap and price and book and book > 0 else None)
    # Provider-computed fields the design excludes: count how often they are zero.
    out["zero_fields"] = [k for k in ("returnOnEquity", "revenueGrowth", "avgRoce7Year", "industryPe", "netMargin",
                                      "ebitdaMargin", "patGrowth", "opRevenue") if s.get(k) in (0, None)]
    np_, margin = s.get("netProfit"), s.get("netMargin")
    out["margin_sign_mismatch"] = bool(np_ and margin and (np_ < 0) != (margin < 0))
    return out


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1%}"


def ratio_ok(v: float | None) -> str:
    return "—" if v is None else ("ok" if 0.9 <= v <= 1.1 else f"**{v}**")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--broker", default="kite_mcp")
    ap.add_argument("--pause", type=float, default=1.0)
    args = ap.parse_args()

    settings = Settings(broker=args.broker)
    symbols = [s.upper() for s in args.symbols] or sorted(Repository(settings.resolved_database_url()).latest_theses())
    provider = MarketLensProvider(load_aliases(BACKEND_DIR / "fundamentals_aliases.json"), pause_seconds=args.pause,
                                  clock=lambda: datetime.now(timezone.utc))
    today = date.today()
    out_dir = BACKEND_DIR / "fundamentals_payloads" / today.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    records, failures, findings = {}, {}, {}
    for i, sym in enumerate(symbols, 1):
        try:
            rec = provider.fetch(sym)
        except ProviderError as e:
            failures[sym] = (e.kind, e.detail)
            print(f"[{i}/{len(symbols)}] {sym}: {e.kind} {e.detail}")
            continue
        records[sym] = rec
        findings[sym] = check(rec, today)
        (out_dir / f"{sym}.json").write_text(rec.model_dump_json(indent=1), encoding="utf-8")
        print(f"[{i}/{len(symbols)}] {sym}: {len(rec.quarters)} quarters, latest {findings[sym]['latest']}, "
              f"{len(rec.issues)} issues")

    write_report(out_dir / "REPORT.md", symbols, records, failures, findings, today)
    (out_dir / "summary.json").write_text(json.dumps({"failures": failures, "findings": findings}, indent=1,
                                                     default=str), encoding="utf-8")
    print(f"\nReport: {out_dir / 'REPORT.md'}")


def write_report(path: Path, symbols, records, failures, findings, today: date) -> None:
    n = len(symbols)
    ok = [s for s in symbols if s in records]
    fresh = [s for s in ok if not findings[s]["overdue"]]
    yoy_ok = [s for s in ok if findings[s]["yoy_pair"]]
    L = [f"# Fundamentals validation report ({today.isoformat()})", "",
         "Provider: NSE MarketLens (undocumented beta). Generated by `scripts/fundamentals_validate.py`.",
         "Business conditions stay disabled until the owner has reviewed this report (D7.9).", "",
         "## Summary", "",
         "| Check | Result |", "| --- | --- |",
         f"| Holdings | {n} |",
         f"| Found | {len(ok)} |",
         f"| Not found | {sum(1 for k, _ in failures.values() if k == 'NOT_FOUND')}: "
         f"{', '.join(s for s, (k, _) in failures.items() if k == 'NOT_FOUND') or 'none'} |",
         f"| Errors | {sum(1 for k, _ in failures.values() if k == 'ERROR')}: "
         f"{', '.join(s for s, (k, _) in failures.items() if k == 'ERROR') or 'none'} |",
         f"| Latest quarter within {MAX_QUARTER_AGE_DAYS} days | {len(fresh)} of {len(ok)} |",
         f"| Year-ago quarter available (YoY computable) | {len(yoy_ok)} of {len(ok)} |",
         f"| Financial companies (debt-to-equity not used) | {sum(1 for s in ok if records[s].financial)} |",
         f"| Provider EPS equals the last four quarters' EPS (±10%) | "
         f"{sum(1 for s in ok if findings[s]['eps_vs_ttm'] and 0.9 <= findings[s]['eps_vs_ttm'] <= 1.1)} of "
         f"{sum(1 for s in ok if findings[s]['eps_vs_ttm'])} checkable |",
         f"| P/E consistent with price / last four quarters' EPS (±10%) | "
         f"{sum(1 for s in ok if findings[s]['pe_check'] and 0.9 <= findings[s]['pe_check'] <= 1.1)} of "
         f"{sum(1 for s in ok if findings[s]['pe_check'])} checkable |",
         f"| P/B consistent with price / book (±10%) | "
         f"{sum(1 for s in ok if findings[s]['pb_check'] and 0.9 <= findings[s]['pb_check'] <= 1.1)} of "
         f"{sum(1 for s in ok if findings[s]['pb_check'])} checkable |",
         f"| D/E consistent with total debt / book equity (±20%) | "
         f"{sum(1 for s in ok if findings[s]['de_check'] and 0.8 <= findings[s]['de_check'] <= 1.25)} of "
         f"{sum(1 for s in ok if findings[s]['de_check'])} checkable |",
         f"| Net margin sign contradicts net profit | {sum(1 for s in ok if findings[s]['margin_sign_mismatch'])} |",
         ""]
    zero = Counter(f for s in ok for f in findings[s]["zero_fields"])
    L += ["## Provider-computed fields that are zero or missing", "",
          "These are excluded by design; the counts show why.", "",
          "| Field | Zero or missing in |", "| --- | --- |"]
    L += [f"| `{f}` | {c} of {len(ok)} |" for f, c in zero.most_common()]
    latest = Counter(findings[s]["latest"] for s in ok)
    L += ["", "## Latest quarter reported", "", "| Quarter end | Holdings |", "| --- | --- |"]
    L += [f"| {d} | {c} |" for d, c in sorted(latest.items(), key=lambda x: x[0] or "", reverse=True)]
    L += ["", "## Year-ago quarters (published results, for checking units and basis)", "",
          "The quarter one year before the latest is already published. Compare these with the companies' "
          "reported results for that quarter, standalone and consolidated.", "",
          "| Holding | Quarter | Total income (₹ cr) | Net profit (₹ cr) |", "| --- | --- | --- | --- |"]
    for s in [s for s in ("HDFCBANK", "INFY", "ICICIBANK", "HINDALCO", "HAL") if s in records]:
        pair = year_ago(records[s].quarters)
        if pair:
            q = pair[1]
            L.append(f"| {s} | {q.period_end} | {q.total_income:,.0f} | {q.net_profit:,.0f} |")
    L += ["", "## Units check: please compare with published results", "",
          "Quarterly figures are read as ₹ lakh and shown here in ₹ crore. The basis (standalone or "
          "consolidated) is not stated by the provider. Compare two or three of these with the company's "
          "published results for the same quarter; if they match standalone figures, the basis is standalone.", "",
          "| Holding | Quarter | Total income (₹ cr) | Net profit (₹ cr) |", "| --- | --- | --- | --- |"]
    for s in sorted(ok, key=lambda s: -(records[s].quarters[0].total_income or 0) if records[s].quarters else 0)[:5]:
        q = records[s].quarters[0]
        L.append(f"| {s} | {q.period_end} | {q.total_income:,.0f} | {q.net_profit:,.0f} |"
                 if q.total_income is not None and q.net_profit is not None else f"| {s} | {q.period_end} | — | — |")
    L += ["", "## Per holding", "",
          "| Holding | Provider symbol | Sub-sector | Quarters | Latest | Age (d) | Revenue YoY | Profit YoY | "
          "Loss qtrs (of 4) | P/E | D/E | Promoter | P/E check | P/B check | Issues |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in symbols:
        if s in failures:
            k, d = failures[s]
            L.append(f"| {s} | — | — | — | — | — | — | — | — | — | — | — | — | — | **{k}**: {d} |")
            continue
        r, f = records[s], findings[s]
        age = f"**{f['age_days']}**" if f["overdue"] else str(f["age_days"])
        rt = r.ratios
        issues = list(r.issues)
        if f["irregular_spacing"]:
            issues.append(f"irregular quarter spacing {f['irregular_spacing']} days")
        if f["margin_sign_mismatch"]:
            issues.append("net margin sign contradicts net profit")
        L.append(" | ".join([
            f"| {s}", r.provider_symbol if r.provider_symbol != s else "same", r.sub_sector or "—", str(f["quarters"]),
            f["latest"] or "—", age, pct(f["revenue_yoy"]), pct(f["profit_yoy"]),
            "—" if f["loss_quarters_4"] is None else str(f["loss_quarters_4"]),
            "—" if rt.get("pe_ratio") is None else f"{rt['pe_ratio']:.1f}",
            "—" if rt.get("debt_to_equity") is None else f"{rt['debt_to_equity']:.2f}",
            "—" if rt.get("promoter_holding") is None else f"{rt['promoter_holding']:.0%}",
            ratio_ok(f["pe_check"]), ratio_ok(f["pb_check"]), "; ".join(issues) or "—"]) + " |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
