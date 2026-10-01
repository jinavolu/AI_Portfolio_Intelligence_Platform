"""Phase 10: backtesting the deterministic rules. See engine.py for the controls.

Sequence (docs/decisions.md): development window → passes development criteria →
one sealed-holdout evaluation → passes holdout criteria → the owner may mark the rule
version + horizon validated. Runs are stored immutably; nothing edits or deletes them.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.backtest.engine import BacktestParams, load_criteria, load_holdout, run_backtest
from app.clock import Clock
from app.db import Repository
from app.decision import RuleSet


def holdout_gate(repo: Repository, rules: RuleSet, horizon: str) -> str | None:
    """Why a holdout run is not allowed now, or None when it is."""
    _, dev_hash = load_criteria()
    runs = [r for r in repo.list_backtests() if r["rules_version"] == rules.version and r["horizon"] == horizon]
    if any(r["window"] == "holdout" for r in runs):
        return f"{rules.version}:{horizon} already had its one holdout evaluation; that result is final"
    if not any(r["window"] == "development" and r["passed"] and r["criteria_hash"] == dev_hash for r in runs):
        return f"{rules.version}:{horizon} must pass the development criteria before the sealed holdout is used"
    return None


class BacktestJob:
    def __init__(self, repo: Repository, clock: Clock, rules: RuleSet):
        self.repo, self.clock, self.rules = repo, clock, rules
        self.state: dict = {"running": False}
        self._lock = threading.Lock()

    def start(self, params: BacktestParams, window: str = "development") -> dict:
        with self._lock:
            if self.state.get("running"):
                return self.state
            if window == "holdout" and (why := holdout_gate(self.repo, self.rules, params.horizon.value)):
                raise PermissionError(why)
            self.state = {"running": True, "horizon": params.horizon.value, "window": window, "run_id": None,
                          "error": None, "started_at": self.clock.now().isoformat()}
        threading.Thread(target=self._run, args=(params, window), daemon=True, name="backtest").start()
        return self.state

    def _run(self, params: BacktestParams, window: str) -> None:
        try:
            result = run_backtest(params, self.rules, window)
            self.state["run_id"] = self.repo.save_backtest(result, self.clock.now())
        except Exception as e:  # noqa: BLE001 - reported to the UI
            self.state["error"] = str(e)[:300]
        finally:
            self.state["running"] = False


def mark_validated(run: dict, repo: Repository, rules: RuleSet, validated_file: Path) -> str:
    """Graduate a rule version for one horizon. Needs a passed HOLDOUT run on the current holdout
    criteria, a passed development run on the current development criteria, and the rule version
    currently in use."""
    _, dev_hash = load_criteria()
    _, holdout_hash = load_holdout()
    if run.get("window") != "holdout":
        raise ValueError("Only a sealed-holdout run can validate rules; pass the development run first, then run the holdout")
    if not run["passed"]:
        raise ValueError("This holdout run did not pass every criterion")
    if run["criteria_hash"] != holdout_hash:
        raise ValueError("The holdout criteria file changed after this run")
    if run["rules_version"] != rules.version:
        raise ValueError(f"Run tested {run['rules_version']}, but the app now uses {rules.version}")
    dev = [r for r in repo.list_backtests() if r["window"] == "development" and r["passed"]
           and r["rules_version"] == run["rules_version"] and r["horizon"] == run["horizon"]
           and r["criteria_hash"] == dev_hash]
    if not dev:
        raise ValueError("No passed development run on the current criteria for this rule version and horizon")
    entry = f"{run['rules_version']}:{run['horizon']}"
    data = json.loads(validated_file.read_text(encoding="utf-8")) if validated_file.exists() else {}
    validated = set(data.get("validated", []))
    validated.add(entry)
    evidence = data.get("evidence", {})
    evidence[entry] = {"holdout_run_id": run["id"], "development_run_id": dev[0]["id"],
                       "holdout_criteria_hash": holdout_hash, "development_criteria_hash": dev_hash}
    validated_file.write_text(json.dumps({"validated": sorted(validated), "evidence": evidence}, indent=1),
                              encoding="utf-8")
    return entry


def revoke_validated(entry: str, validated_file: Path) -> None:
    if not validated_file.exists():
        return
    data = json.loads(validated_file.read_text(encoding="utf-8"))
    data["validated"] = [v for v in data.get("validated", []) if v != entry]
    data.get("evidence", {}).pop(entry, None)
    validated_file.write_text(json.dumps(data, indent=1), encoding="utf-8")
