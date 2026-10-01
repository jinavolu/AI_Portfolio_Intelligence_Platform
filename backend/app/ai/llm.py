"""LLM clients and the model router. Gemini explains; it never calculates or decides."""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import BaseModel

from app.config import Settings


class LLMResult(BaseModel):
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def generate(self, model: str, system: str, prompt: str) -> LLMResult: ...


class GeminiClient:
    def __init__(self, api_key: str):
        from google import genai  # optional dependency: uv pip install -e .[ai]

        self._client = genai.Client(api_key=api_key)

    def generate(self, model: str, system: str, prompt: str) -> LLMResult:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.2),
        )
        usage = resp.usage_metadata
        return LLMResult(
            text=resp.text or "",
            model=model,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
        )

    def generate_grounded(self, model: str, system: str, prompt: str) -> tuple[LLMResult, dict]:
        """Google Search grounding: the answer plus its sources. Grounding is DISCOVERY, not
        verification (the caller checks each source). Returns {"chunks": [{uri, title}],
        "supports": [{text, chunks: [index]}]}."""
        from google.genai import types

        resp = self._client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system, temperature=0.1, tools=[types.Tool(google_search=types.GoogleSearch())],
                # Search grounding runs server-side; no local function calling (also silences the SDK's AFC warning).
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        usage = resp.usage_metadata
        gm = resp.candidates[0].grounding_metadata if resp.candidates else None
        chunks = [{"uri": c.web.uri, "title": c.web.title} for c in (gm.grounding_chunks or []) if c.web] if gm else []
        supports = [{"text": s.segment.text or "", "chunks": list(s.grounding_chunk_indices or [])}
                    for s in (gm.grounding_supports or []) if s.segment] if gm else []
        return (LLMResult(text=resp.text or "", model=model,
                          input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
                          output_tokens=getattr(usage, "candidates_token_count", 0) or 0),
                {"chunks": chunks, "supports": supports})


class OfflineTemplateClient:
    """Not an LLM. Fills a fixed template from the context so the pipeline (cache, grounding,
    usage logging) can be exercised without an API key. Responses are labelled with its model name."""

    MODEL = "offline-template"

    def generate(self, model: str, system: str, prompt: str) -> LLMResult:
        ctx = json.loads(prompt.split("CONTEXT_JSON:", 1)[1])
        d, p, t = ctx["decision"], ctx["portfolio"], ctx.get("technical") or {}
        lines = [f"{ctx['symbol']} is {d['state']}: {d['reason']}."]
        lines.append(f"You hold {p['quantity']} shares at an average of ₹{p['average_price']}; "
                     f"the last price is ₹{p['last_price']}, for a P&L of ₹{p['pnl']}.")
        if t:
            lines.append(f"Trend is {t.get('trend')} and RSI(14) is {t.get('rsi14')}; "
                         f"EMA50 is {t.get('ema50')} and SMA200 is {t.get('sma200')}.")
        fired = [g for g in ctx["gates"] if g["status"] == "FIRED"]
        if fired:
            lines.append("Gates that fired: " + "; ".join(f"{g['name']} ({g['detail']})" for g in fired) + ".")
        return LLMResult(text=" ".join(lines), model=self.MODEL)


class LLMUnavailable(RuntimeError):
    """Every model in the chain was overloaded, rate-limited or failed."""


def is_transient(e: Exception) -> bool:
    """Overload / quota / server errors worth retrying on another model."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code in (429, 500, 502, 503, 504) or "UNAVAILABLE" in str(e) or "RESOURCE_EXHAUSTED" in str(e)


def refused(e: Exception) -> LLMUnavailable:
    """A non-transient failure (bad request, key not allowed, safety block, extra not installed): no
    other model will do better, and the API answers it like an outage rather than with a 500."""
    return LLMUnavailable(f"Gemini couldn't answer ({type(e).__name__}: {str(e)[:200]})")


def generate_with_fallback(llm: LLMClient, models: list[str], system: str, prompt: str) -> LLMResult:
    errors = []
    for model in dict.fromkeys(models):
        try:
            return llm.generate(model, system, prompt)
        except Exception as e:  # noqa: BLE001
            if not is_transient(e):
                raise refused(e) from e
            errors.append(f"{model}: {str(e)[:120]}")
    raise LLMUnavailable("Gemini is busy or over quota, try again shortly (" + "; ".join(errors) + ")")


def model_chain(feature: str, settings: Settings) -> list[str]:
    return list(dict.fromkeys([route_model(feature, settings), *settings.gemini_fallback_models]))


def route_model(feature: str, settings: Settings) -> str:
    """Fast model for per-holding explanations and extraction; strong model for
    portfolio-wide reasoning."""
    strong = {"portfolio_review", "chat"}
    return settings.gemini_strong_model if feature in strong else settings.gemini_fast_model


def build_llm_client(settings: Settings) -> LLMClient:
    if settings.gemini_api_key:
        return GeminiClient(settings.gemini_api_key.get_secret_value())
    return OfflineTemplateClient()
