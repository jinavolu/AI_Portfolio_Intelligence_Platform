"""Portfolio-wide default technical warnings (decisions.md D7.5).

Generic thresholds that apply to every holding, with per-holding overrides. They are TECHNICAL
conditions: they raise a warning and never reach gate 4 or produce SELL. They are settings, not
rows copied into each thesis, so they are never mistaken for the owner's reason for holding.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.thesis import InvalidationCondition

# Fixed set; the owner can switch each off or change its threshold, portfolio-wide or per holding.
DEFAULT_WARNINGS = [
    InvalidationCondition(id="w_sma200", category="TECHNICAL", metric="long_term_trend", op="==",
                          value="BELOW_SMA200", description="Below the 200-day average"),
    InvalidationCondition(id="w_52w_high", category="TECHNICAL", metric="pct_from_52w_high", op="<",
                          value=-0.25, description="More than 25% below the 52-week high"),
    InvalidationCondition(id="w_cost_loss", category="TECHNICAL", metric="pnl_pct", op="<",
                          value=-0.20, description="Down more than 20% on cost"),
]
DEFAULT_IDS = {w.id for w in DEFAULT_WARNINGS}


class WarningSetting(BaseModel):
    enabled: bool | None = None  # None = inherit
    value: float | str | None = None  # None = inherit


class WarningSettings(BaseModel):
    defaults: dict[str, WarningSetting] = {}  # portfolio-wide changes to DEFAULT_WARNINGS
    overrides: dict[str, dict[str, WarningSetting]] = {}  # symbol -> warning id -> change


def _describe(w: InvalidationCondition, value: float | str) -> str:
    if value == w.value:
        return w.description
    if w.metric == "pct_from_52w_high":
        return f"More than {abs(float(value)):.0%} below the 52-week high"
    if w.metric == "pnl_pct":
        return f"Down more than {abs(float(value)):.0%} on cost"
    return f"{w.description} ({w.metric} {w.op} {value})"


def resolve(settings: WarningSettings, symbol: str | None = None) -> list[dict]:
    """Every default warning with its effective state: portfolio setting, then the holding's override."""
    out = []
    for w in DEFAULT_WARNINGS:
        base = settings.defaults.get(w.id, WarningSetting())
        own = settings.overrides.get(symbol, {}).get(w.id, WarningSetting()) if symbol else WarningSetting()
        portfolio_value = w.value if base.value is None else base.value
        value = portfolio_value if own.value is None else own.value
        enabled = own.enabled if own.enabled is not None else base.enabled if base.enabled is not None else True
        out.append({"id": w.id, "metric": w.metric, "op": w.op, "value": value, "enabled": enabled,
                    "description": _describe(w, value), "default_value": w.value,
                    "portfolio_value": portfolio_value,
                    "portfolio_enabled": base.enabled if base.enabled is not None else True,
                    "overridden": own.enabled is not None or own.value is not None})
    return out


def effective_warnings(settings: WarningSettings, symbol: str) -> list[InvalidationCondition]:
    """The enabled default warnings for one holding, as TECHNICAL conditions ready to evaluate."""
    by_id = {w.id: w for w in DEFAULT_WARNINGS}
    return [by_id[r["id"]].model_copy(update={"value": r["value"], "description": r["description"]})
            for r in resolve(settings, symbol) if r["enabled"]]


def validate_setting(warning_id: str, s: WarningSetting) -> None:
    if warning_id not in DEFAULT_IDS:
        raise ValueError(f"Unknown warning {warning_id!r}; allowed: {sorted(DEFAULT_IDS)}")
    w = next(w for w in DEFAULT_WARNINGS if w.id == warning_id)
    if s.value is not None and type(s.value) is not type(w.value) and not (
            isinstance(s.value, (int, float)) and isinstance(w.value, float)):
        raise ValueError(f"{warning_id} needs a {'number' if isinstance(w.value, float) else 'text'} value")
    if isinstance(w.value, str) and s.value is not None:
        raise ValueError(f"{warning_id} has no threshold to change; it can only be switched on or off")
