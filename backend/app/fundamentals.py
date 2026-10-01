"""Fundamentals provider (monitoring-design.md section 4).

A replaceable provider returns a FundamentalsRecord with provenance: the raw response, what was
normalised, and every value it refused to trust (`issues`). Missing stays missing (D1.5): a zero or
implausible provider value becomes None with an issue, never a number.

Business conditions stay disabled until the owner has reviewed the validation report (D7.9).
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

# Sub-sectors where debt-to-equity is not meaningful (banks, NBFCs, insurers, exchanges).
FINANCIAL_SUBSECTORS = {"Financial Services"}


class Quarter(BaseModel):
    period_end: date
    total_income: float | None  # ₹ crore
    pbt: float | None  # ₹ crore
    net_profit: float | None  # ₹ crore


class FundamentalsRecord(BaseModel):
    symbol: str  # our symbol
    provider_symbol: str  # the provider's, e.g. TATAMOTORS -> TMPV
    provider: str
    fetched_at: datetime
    name: str | None = None
    sector: str | None = None
    sub_sector: str | None = None
    financial: bool = False  # debt-to-equity not meaningful
    quarters: list[Quarter] = []  # newest first
    ratios: dict[str, float | None] = {}  # pe_ratio, pb_ratio, debt_to_equity, promoter_holding (fraction)
    basis: Literal["STANDALONE", "CONSOLIDATED", "UNKNOWN"] = "UNKNOWN"
    issues: list[str] = []
    raw: dict = {}


class ProviderError(Exception):
    def __init__(self, symbol: str, kind: Literal["NOT_FOUND", "ERROR"], detail: str):
        super().__init__(f"{symbol}: {kind}: {detail}")
        self.symbol, self.kind, self.detail = symbol, kind, detail


class FundamentalsProvider(Protocol):
    name: str

    def fetch(self, symbol: str) -> FundamentalsRecord: ...


def load_aliases(path: Path) -> dict[str, str]:
    """Our symbol -> provider symbol, for renames. Keys starting with '_' are comments."""
    if not path.exists():
        return {}
    return {k.upper(): v.upper() for k, v in json.loads(path.read_text(encoding="utf-8")).items()
            if not k.startswith("_")}


def base_symbol(symbol: str) -> str:
    """Kite series suffixes (HFCL-BE) name the same company as the base symbol."""
    return symbol.rsplit("-", 1)[0] if "-" in symbol else symbol


# --------------------------------------------------------------------------- MarketLens


class MarketLensProvider:
    """NSE MarketLens (https://marketlens.nseindia.com), an undocumented beta. Units: quarterly figures
    in ₹ lakh, **standalone** (validation 2026-09-29: HDFC Bank, ICICI Bank and HAL June-2025 quarters
    match their published standalone results exactly); stock-level money fields in rupees; promoter
    holding in percent. The provider P/E is not reproducible from price and EPS and is not used."""

    name = "marketlens"
    BASE = "https://marketlens.nseindia.com"

    def __init__(self, aliases: dict[str, str] | None = None, pause_seconds: float = 1.0, clock=None):
        import httpx  # optional extra: pip install -e ".[fundamentals]"

        self.aliases = aliases or {}
        self.pause = pause_seconds
        self.now = clock or datetime.now
        self._last = 0.0
        self.http = httpx.Client(base_url=self.BASE, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36",
            "Accept": "application/json", "Referer": f"{self.BASE}/screener"})

    def _get(self, path: str) -> tuple[int, dict | None]:
        wait = self.pause - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)  # one request at a time, politely spaced
        self._last = time.monotonic()
        r = self.http.get(path)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None

    def fetch(self, symbol: str) -> FundamentalsRecord:
        psym = self.aliases.get(symbol.upper(), base_symbol(symbol.upper()))
        raw: dict = {}
        for key, path in (("stock", f"/api/stocks/{psym}"), ("quarters", f"/api/stocks/{psym}/quarterly-financials")):
            try:
                status, body = self._get(path)
            except Exception as e:  # network errors: report, never guess
                raise ProviderError(symbol, "ERROR", f"{path}: {e}") from e
            if status == 404:
                raise ProviderError(symbol, "NOT_FOUND", f"{psym} not found ({path})")
            if status != 200 or not body or not body.get("success"):
                raise ProviderError(symbol, "ERROR", f"{path}: HTTP {status} {str(body)[:200]}")
            raw[key] = body
        return normalise_marketlens(symbol, psym, raw, self.now())


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def normalise_marketlens(symbol: str, psym: str, raw: dict, fetched_at: datetime) -> FundamentalsRecord:
    s = raw["stock"]["data"]
    issues: list[str] = []
    sub = s.get("subSector")
    financial = sub in FINANCIAL_SUBSECTORS

    by_date: dict[date, list[Quarter]] = {}
    for q in raw["quarters"]["data"] or []:
        try:
            end = datetime.strptime(q["date"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            issues.append(f"quarter with unreadable date {q.get('date')!r} skipped")
            continue
        lakh = {k: _num(q.get(k)) for k in ("totalIncome", "profitBeforeTax", "netProfitLoss")}
        by_date.setdefault(end, []).append(Quarter(
            period_end=end, total_income=None if lakh["totalIncome"] is None else lakh["totalIncome"] / 100,
            pbt=None if lakh["profitBeforeTax"] is None else lakh["profitBeforeTax"] / 100,
            net_profit=None if lakh["netProfitLoss"] is None else lakh["netProfitLoss"] / 100))
    quarters = []
    for end, rows in by_date.items():
        figures = {(r.total_income, r.pbt, r.net_profit) for r in rows}
        if len(figures) == 1:
            quarters.append(rows[0])
            if len(rows) > 1:
                issues.append(f"quarter {end} listed {len(rows)} times with the same figures: kept once")
        else:
            # e.g. a half-year total labelled as a quarter: we can't tell which row is right.
            quarters.append(Quarter(period_end=end, total_income=None, pbt=None, net_profit=None))
            issues.append(f"quarter {end} listed with conflicting figures: treated as missing")
    quarters.sort(key=lambda q: q.period_end, reverse=True)
    for q in quarters:
        if q.total_income is not None and q.total_income <= 0:
            issues.append(f"total income {q.total_income} for {q.period_end} is not positive: treated as missing")
            q.total_income = None

    def positive(field: str, label: str) -> float | None:
        v = _num(s.get(field))
        if v is None or v <= 0:
            issues.append(f"{label} = {v}: treated as missing")
            return None
        return v

    ratios: dict[str, float | None] = {"pe_ratio": positive("peRatio", "P/E"), "pb_ratio": positive("pbRatio", "P/B")}

    de, debt = _num(s.get("debtToEquity")), _num(s.get("totalDebt"))
    if financial:
        ratios["debt_to_equity"] = None
        issues.append("financial company: debt-to-equity not meaningful")
    elif de is None or de < 0:
        ratios["debt_to_equity"] = None
        issues.append(f"debt-to-equity = {de}: treated as missing")
    elif de == 0 and debt != 0:
        ratios["debt_to_equity"] = None
        issues.append(f"debt-to-equity = 0 but total debt = {debt}: treated as missing")
    else:
        ratios["debt_to_equity"] = de

    promoter, public = _num(s.get("promoterHolding")), _num(s.get("publicHolding"))
    if promoter is None or promoter < 0 or promoter > 100:
        ratios["promoter_holding"] = None
        issues.append(f"promoter holding = {promoter}: treated as missing")
    elif promoter == 0 and public != 100:
        ratios["promoter_holding"] = None
        issues.append(f"promoter holding = 0 with public holding = {public}: treated as missing")
    else:
        ratios["promoter_holding"] = round(promoter / 100, 6)

    return FundamentalsRecord(symbol=symbol, provider_symbol=psym, provider="marketlens", fetched_at=fetched_at,
                              name=s.get("name"), sector=s.get("sector"), sub_sector=sub, financial=financial,
                              quarters=quarters, ratios=ratios, basis="STANDALONE", issues=issues, raw=raw)


# --------------------------------------------------------------------------- derived metrics


def year_ago(quarters: list[Quarter]) -> tuple[Quarter, Quarter] | None:
    """The latest quarter and the same quarter one year earlier (period end 350-380 days before)."""
    if not quarters:
        return None
    latest = quarters[0]
    for q in quarters[1:]:
        if 350 <= (latest.period_end - q.period_end).days <= 380:
            return latest, q
    return None


def yoy(now: float | None, then: float | None) -> float | None:
    """Growth from a positive base only: growth from a loss or zero is undefined."""
    if now is None or then is None or then <= 0:
        return None
    return round(now / then - 1, 4)


# --------------------------------------------------------------------------- snapshot view

# D7.7: business values count only while the latest quarter is recent and the data was fetched lately.
MAX_QUARTER_AGE_DAYS = 150
MAX_FETCH_AGE_DAYS = 7

_YOY = {"revenue_yoy": ("total_income", "total income"), "profit_yoy": ("net_profit", "net profit"),
        "pbt_yoy": ("pbt", "profit before tax")}


def business_view(rec: FundamentalsRecord) -> dict:
    """What the snapshot stores: metric values, the reason for every missing one, and the raw inputs
    behind each growth figure (both quarters), so an alert can be read later without the provider."""
    q = rec.quarters
    values: dict[str, float | None] = {}
    reasons: dict[str, str] = {}
    inputs: dict[str, dict] = {}
    pair = year_ago(q)
    for metric, (field, label) in _YOY.items():
        if pair is None:
            values[metric], reasons[metric] = None, "year-ago quarter not available from the provider"
            continue
        now, then = getattr(pair[0], field), getattr(pair[1], field)
        values[metric] = yoy(now, then)
        inputs[metric] = {"latest": {"period_end": pair[0].period_end.isoformat(), "value": now},
                          "year_ago": {"period_end": pair[1].period_end.isoformat(), "value": then},
                          "unit": "₹ crore", "label": label}
        if values[metric] is None:
            reasons[metric] = (f"{label} missing for one of the two quarters" if now is None or then is None
                               else f"year-ago {label} is not positive: growth from a loss is undefined")
    latest = q[0] if q else None
    values["net_profit_q"] = latest.net_profit if latest else None
    if values["net_profit_q"] is None:
        reasons["net_profit_q"] = "latest quarter's net profit not available"
    last4 = [x.net_profit for x in q[:4]]
    if len(last4) == 4 and None not in last4:
        values["loss_quarters_4"] = float(sum(1 for x in last4 if x < 0))
    else:
        values["loss_quarters_4"], reasons["loss_quarters_4"] = None, "fewer than four quarters available"
    # The normaliser's issue explains each missing ratio, e.g. "financial company: debt-to-equity not meaningful".
    issue_prefix = {"pb_ratio": ("p/b",), "debt_to_equity": ("financial", "debt"), "promoter_holding": ("promoter",)}
    for metric, prefixes in issue_prefix.items():
        values[metric] = rec.ratios.get(metric)
        if values[metric] is None:
            reasons[metric] = next((i for i in rec.issues if i.lower().startswith(prefixes)), "not provided")
    return {"status": "OK", "provider": rec.provider, "provider_symbol": rec.provider_symbol, "basis": rec.basis,
            "fetched_at": rec.fetched_at.isoformat(), "financial": rec.financial,
            "latest_quarter": latest.period_end.isoformat() if latest else None,
            "quarters": [x.model_dump(mode="json") for x in q], "values": values, "reasons": reasons,
            "inputs": inputs, "issues": rec.issues}


def freshness_problem(view: dict | None, today: date, now: datetime) -> str | None:
    """Why no business value may be used right now, or None when they may (D7.7)."""
    if view is None:
        return "fundamentals not fetched yet"
    if view["status"] == "NOT_FOUND":
        return f"no fundamentals: the provider doesn't cover this stock ({view.get('detail', '')})".rstrip(" ()")
    if view["status"] != "OK":
        return f"fundamentals fetch failed: {view.get('detail', '')}"
    fetched = datetime.fromisoformat(view["fetched_at"])
    if (now - fetched).days > MAX_FETCH_AGE_DAYS:
        return f"fundamentals not fetched recently (last {fetched.date().isoformat()})"
    if view["latest_quarter"] is None:
        return "no quarterly results available"
    age = (today - date.fromisoformat(view["latest_quarter"])).days
    if age > MAX_QUARTER_AGE_DAYS:
        return f"financial data overdue: latest quarter ended {view['latest_quarter']} ({age} days ago)"
    return None
