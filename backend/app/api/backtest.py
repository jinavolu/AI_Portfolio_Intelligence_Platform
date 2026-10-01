"""Backtesting (Phase 10): history download, runs, the sealed holdout, validated rule versions."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.context import ApiContext
from app.backtest import holdout_gate, mark_validated, revoke_validated
from app.backtest.data import read_manifest
from app.backtest.engine import BacktestParams, load_criteria, load_holdout
from app.decision import load_validated_rule_versions
from app.thesis import Horizon


class HistoryIn(BaseModel):
    universe: Literal["nifty50", "nifty100", "nifty500"] = "nifty100"
    # ≥10: the development window ends at the sealed date and needs warm-up plus several full years.
    years: int = Field(default=10, ge=10, le=20)


class BacktestIn(BaseModel):
    horizon: Horizon = Horizon.LONG_TERM
    window: Literal["development", "holdout"] = "development"


def router(ctx: ApiContext) -> APIRouter:
    r = APIRouter()
    repo, service, history_job, backtest_job = ctx.repo, ctx.service, ctx.history_job, ctx.backtest_job
    validated_file = ctx.settings.resolved_validated_rules_file

    @r.get("/api/backtest/criteria")
    def backtest_criteria():
        criteria, digest = load_criteria()
        return {**criteria, "sha256": digest}

    @r.get("/api/backtest/data")
    def backtest_data_status():
        m = read_manifest()
        return {"job": {k: v for k, v in history_job.state.items() if k not in ("symbols", "members")},
                "stored": None if not m else {k: m[k] for k in ("universe", "years", "fetched_on", "missing")}
                | {"members": len(m["members"])}}

    @r.post("/api/backtest/data")
    def backtest_fetch(body: HistoryIn):
        history_job.today = ctx.clock.today_ist()
        return {k: v for k, v in history_job.start(body.universe, body.years).items() if k not in ("symbols", "members")}

    @r.post("/api/backtest/run")
    def backtest_run(body: BacktestIn):
        if history_job.state.get("running"):
            raise HTTPException(409, "History download still running")
        if not read_manifest():
            raise HTTPException(409, "Download history first")
        try:
            return backtest_job.start(BacktestParams(horizon=body.horizon), body.window)
        except PermissionError as e:
            raise HTTPException(409, str(e)) from e

    @r.get("/api/backtest/holdout")
    def backtest_holdout():
        holdout, digest = load_holdout()
        return {**holdout, "sha256": digest,
                "eligible": {h.value: holdout_gate(repo, service.rules, h.value) for h in Horizon}}

    @r.get("/api/backtest/job")
    def backtest_job_status():
        return backtest_job.state

    @r.get("/api/backtest/runs")
    def backtest_runs():
        return repo.list_backtests()

    @r.get("/api/backtest/runs/{run_id}")
    def backtest_get(run_id: int):
        run = repo.get_backtest(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run

    @r.get("/api/backtest/validated")
    def backtest_validated():
        return {"current_rules": service.rules.version,
                "validated": sorted(load_validated_rule_versions(validated_file()))}

    @r.post("/api/backtest/runs/{run_id}/validate")
    def backtest_validate(run_id: int):
        run = repo.get_backtest(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        try:
            entry = mark_validated(run, repo, service.rules, validated_file())
        except ValueError as e:
            raise HTTPException(409, str(e)) from e
        return {"validated": entry}

    @r.delete("/api/backtest/validated/{entry}")
    def backtest_revoke(entry: str):
        revoke_validated(entry, validated_file())
        return {"revoked": entry}

    return r
