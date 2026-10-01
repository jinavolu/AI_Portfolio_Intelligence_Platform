"""Theses (one holding, in bulk, AI-suggested conditions) and the default technical warnings."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import tech_warnings
from app.ai.llm import LLMUnavailable
from app.api.context import ApiContext
from app.thesis import BulkThesisIn, ThesisIn, metric_catalogue


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    service, repo, clock = ctx.service, ctx.repo, ctx.clock

    @r.get("/api/theses")
    def theses():
        return repo.latest_theses()

    @r.get("/api/theses/metrics")
    def thesis_metrics():
        return metric_catalogue()

    @r.put("/api/theses/{symbol}")
    def save_thesis(symbol: str, body: ThesisIn):
        now = clock.now()
        return repo.save_thesis(symbol.upper(), body.confirm_conditions(now), now)

    @r.post("/api/theses/{symbol}/suggest")
    def suggest_conditions(symbol: str):
        """Propose up to three conditions from the owner's own reason (D7.8). Saved as PROPOSED in a new
        thesis version; nothing is evaluated until the owner accepts."""
        sym = symbol.upper()
        hs = service.current().holdings.get(sym)
        if hs is None:
            raise HTTPException(404, f"{symbol} is not a current holding")
        thesis = repo.latest_theses().get(sym)
        try:
            out = ctx.suggester.suggest(hs, thesis)
        except LLMUnavailable as e:
            raise HTTPException(503, str(e)) from e
        version = thesis.version if thesis else None
        if out["suggestions"]:
            body = ThesisIn(**thesis.model_dump(exclude={"symbol", "version", "updated_at", "invalidation_conditions"}),
                            invalidation_conditions=[*thesis.invalidation_conditions, *out["suggestions"]])
            version = repo.save_thesis(sym, body, clock.now()).version
        return {**out, "suggestions": [c.model_dump(mode="json") for c in out["suggestions"]], "version": version}

    @r.post("/api/theses/bulk")
    def save_theses_bulk(body: BulkThesisIn):
        if body.thesis.horizon is None:
            raise HTTPException(422, "A bulk thesis needs a horizon")
        existing = repo.latest_theses()
        # Conditions applied in bulk are not the owner's per-holding judgement: they stay suggestions
        # until accepted on each Stock page (D7.2).
        suggested = [c.model_copy(update={"origin": "SUGGESTED", "status": "PROPOSED", "confirmed_at": None})
                     for c in body.thesis.invalidation_conditions]
        draft = body.thesis.model_copy(update={"draft": True, "invalidation_conditions": suggested})
        saved, skipped = [], []
        for symbol in dict.fromkeys(s.upper() for s in body.symbols):
            if body.skip_existing and symbol in existing:
                skipped.append(symbol)
                continue
            repo.save_thesis(symbol, draft, clock.now())
            saved.append(symbol)
        return {"saved": saved, "skipped": skipped}

    # --- default technical warnings

    @r.get("/api/warnings")
    def warnings_get(symbol: str | None = None):
        """Default warnings with their effective state, portfolio-wide or for one holding."""
        return tech_warnings.resolve(service.warning_settings(), symbol.upper() if symbol else None)

    @r.put("/api/warnings/defaults")
    def warnings_put_defaults(body: dict[str, tech_warnings.WarningSetting]):
        return _save_warnings(body, None)

    @r.put("/api/warnings/overrides/{symbol}")
    def warnings_put_override(symbol: str, body: dict[str, tech_warnings.WarningSetting]):
        """Replaces this holding's overrides; an empty body resets it to the portfolio defaults."""
        return _save_warnings(body, symbol.upper())

    def _save_warnings(body: dict[str, tech_warnings.WarningSetting], symbol: str | None):
        try:
            for wid, s in body.items():
                tech_warnings.validate_setting(wid, s)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        current = service.warning_settings()
        changes = {k: v for k, v in body.items() if v.enabled is not None or v.value is not None}
        if symbol is None:
            current.defaults = changes
        elif changes:
            current.overrides[symbol] = changes
        else:
            current.overrides.pop(symbol, None)
        service.save_warning_settings(current)
        return tech_warnings.resolve(current, symbol)

    return r
