"""Suggested thesis conditions (decisions.md D7.8).

From the owner's OWN reason for holding, propose up to three invalidation conditions. Gemini proposes;
Python decides what survives: the metric must be in the catalogue and available for this holding,
the value must be sensible, and every suggestion must quote the owner's words it is based on
(checked verbatim), so nothing is invented. Suggestions are saved as PROPOSED and are never
evaluated until the owner accepts them. A vague, missing or app-written reason gets no suggestions.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from app.ai.llm import LLMClient, OfflineTemplateClient, generate_with_fallback, model_chain
from app.clock import IST, ist_day_start
from app.config import Settings
from app.db import Repository
from app.decision import DecisionInputs, condition_metrics
from app.snapshot import HoldingSnapshot
from app.thesis import (BUSINESS_METRICS, CONDITION_METRICS, ENUM_VALUES, FRACTION_METRICS, METRIC_CATEGORIES,
                        InvalidationCondition, Thesis)

FEATURE = "suggest_conditions"
PROMPT_VERSION = "suggest-conditions-v1"
MAX_SUGGESTIONS = 3

# Reasons the app wrote itself when assigning draft horizons (D4). Never a basis for "your" conditions.
APP_WRITTEN_REASONS = {
    "Core long-term holding: established, quality business held for compounding.",
    "Growth or cyclical theme (capex, manufacturing, consumption, recent listing) held for a multi-quarter run.",
    "High-volatility or turnaround position held for a shorter move.",
}

SYSTEM_PROMPT = """You help an investor turn their own reason for holding a stock into at most three
measurable "invalidation conditions": signs that their reason may no longer apply.

Return ONLY JSON: {"vague": true|false, "suggestions": [{"category": "THESIS"|"BUSINESS",
"metric": "<one of AVAILABLE_METRICS>", "op": "<"|"<="|">"|">="|"=="|"!=", "value": <number or allowed text>,
"description": "<short, plain words>", "based_on": "<exact words copied from the owner's text>"}]}

Rules:
- Base every suggestion on something the owner actually wrote; copy those words exactly into based_on.
- If the owner's text is too vague to measure anything (e.g. "good company"), return {"vague": true, "suggestions": []}.
- Use only metrics listed in AVAILABLE_METRICS; BUSINESS only for business metrics. Fractions are
  written as decimals (-0.2 means -20%).
- Choose thresholds that mean the reason is in doubt, not ordinary noise. Use the current values given
  to avoid thresholds that are already met, unless the owner's words clearly say so.
- Do not repeat EXISTING_CONDITIONS. Never suggest buying or selling."""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def owner_text(thesis: Thesis) -> str:
    return " ".join([thesis.why_bought, *thesis.assumptions, *thesis.metrics_to_monitor, thesis.notes or ""])


def not_suggestible(thesis: Thesis | None) -> str | None:
    """Why no suggestion may be made, or None."""
    if thesis is None:
        return "Record your reason for holding first."
    if thesis.draft or thesis.why_bought.strip() in APP_WRITTEN_REASONS:
        return "This reason was written by the app, not you. Write your own reason first."
    if len(thesis.why_bought.split()) < 4:
        return "Your reason is too short to suggest conditions from. Add a sentence on why you hold it."
    return None


def available_metrics(hs: HoldingSnapshot) -> dict[str, dict]:
    """Metrics a suggestion may use for THIS holding, with today's value. Business metrics only when
    the provider has a value (otherwise the condition could never be checked)."""
    inp = DecisionInputs(symbol=hs.symbol, now=datetime.now(timezone.utc), today=datetime.now(IST).date(),
                         holding=hs.portfolio, holding_as_of=None, technical=hs.technical, thesis=hs.thesis,
                         risk=hs.risk, events=hs.events, fundamental=hs.fundamental)
    current = condition_metrics(inp)
    out = {}
    for m, desc in CONDITION_METRICS.items():
        if m in BUSINESS_METRICS and current.get(m) is None:
            continue
        out[m] = {"description": desc, "categories": [c for c in METRIC_CATEGORIES[m] if c != "TECHNICAL"],
                  "unit": "fraction" if m in FRACTION_METRICS else "text" if m in ENUM_VALUES else "number",
                  "allowed_values": ENUM_VALUES.get(m), "current": current.get(m)}
    return out


def _parse(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0) if m else t)


def validate(raw: dict, thesis: Thesis, metrics: dict[str, dict]) -> tuple[list[InvalidationCondition], list[str]]:
    """Keep only suggestions that pass every check; say why the others were dropped."""
    words = _norm(owner_text(thesis))
    existing = {(c.metric, c.op) for c in thesis.invalidation_conditions if c.status != "DISMISSED"}
    kept, dropped = [], []
    for s in (raw.get("suggestions") or [])[:MAX_SUGGESTIONS * 2]:
        if not isinstance(s, dict):
            continue
        metric, based_on = s.get("metric"), str(s.get("based_on") or "").strip()
        if metric not in metrics:
            dropped.append(f"{metric}: not available for this holding")
            continue
        if not based_on or _norm(based_on) not in words:
            dropped.append(f"{metric}: not based on words you wrote")
            continue
        value = s.get("value")
        if metric in FRACTION_METRICS and not (isinstance(value, (int, float)) and -1 <= value <= 10):
            dropped.append(f"{metric}: implausible value {value!r}")
            continue
        if metric in ENUM_VALUES and value not in ENUM_VALUES[metric]:
            dropped.append(f"{metric}: value {value!r} not allowed")
            continue
        if (metric, s.get("op")) in existing:
            dropped.append(f"{metric}: you already have this condition")
            continue
        desc = str(s.get("description") or "").strip().rstrip(".")
        # Keep the owner's words as provenance, without repeating them when the description says the same.
        same = not desc or _norm(based_on) in _norm(desc) or _norm(desc) in _norm(based_on)
        description = f"{desc or based_on} (your words)" if same else f"{desc} (from your words: “{based_on}”)"
        try:
            c = InvalidationCondition(category=s.get("category"), metric=metric, op=s.get("op"), value=value,
                                      description=description, origin="SUGGESTED", status="PROPOSED")
        except ValidationError as e:
            dropped.append(f"{metric}: {e.errors()[0]['msg']}")
            continue
        existing.add((metric, c.op))
        kept.append(c)
        if len(kept) == MAX_SUGGESTIONS:
            break
    return kept, dropped


class Suggester:
    def __init__(self, repo: Repository, llm: LLMClient, settings: Settings, clock):
        self.repo, self.llm, self.settings, self.clock = repo, llm, settings, clock

    def suggest(self, hs: HoldingSnapshot, thesis: Thesis | None) -> dict:
        if why := not_suggestible(thesis):
            return {"suggestions": [], "note": why, "dropped": []}
        if isinstance(self.llm, OfflineTemplateClient):
            return {"suggestions": [], "dropped": [],
                    "note": "Suggestions need Gemini (no API key configured). Add conditions in the editor."}
        now = self.clock.now()
        day_start = ist_day_start(now)
        if self.repo.model_calls_since(FEATURE, day_start) >= self.settings.ai_daily_cap_per_feature:
            return {"suggestions": [], "dropped": [], "note": "Daily limit for suggestions reached; try tomorrow."}

        metrics = available_metrics(hs)
        context = {"symbol": hs.symbol, "sector": hs.portfolio.sector,
                   "horizon": thesis.horizon.value if thesis.horizon else None,
                   "owner_reason": thesis.why_bought, "owner_assumptions": thesis.assumptions,
                   "owner_metrics_to_monitor": thesis.metrics_to_monitor,
                   "EXISTING_CONDITIONS": [c.model_dump(include={"metric", "op", "value", "description"})
                                           for c in thesis.invalidation_conditions if c.status != "DISMISSED"],
                   "AVAILABLE_METRICS": metrics}
        result = generate_with_fallback(self.llm, model_chain(FEATURE, self.settings), SYSTEM_PROMPT,
                                        "CONTEXT_JSON:" + json.dumps(context, default=str))
        self.repo.log_usage(now, FEATURE, result.model, cache_hit=False, input_tokens=result.input_tokens,
                            output_tokens=result.output_tokens)
        try:
            raw = _parse(result.text)
        except (ValueError, json.JSONDecodeError):
            return {"suggestions": [], "dropped": [], "model": result.model,
                    "note": "The suggestion came back unreadable; try again or add conditions in the editor."}
        kept, dropped = validate(raw, thesis, metrics)
        note = ("Your reason is too general to measure. Add what would make you doubt it, e.g. “profit growth”, "
                "“low debt”, “staying above the 200-day average”." if raw.get("vague") and not kept else "")
        return {"suggestions": kept, "dropped": dropped, "note": note, "model": result.model,
                "current": {c.metric: metrics[c.metric]["current"] for c in kept}}
