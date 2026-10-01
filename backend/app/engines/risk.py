"""Risk Engine: owns concentration limits and portfolio-level risk measures.
The Decision Engine only reads these outputs."""

from __future__ import annotations

from pydantic import BaseModel

from app.engines.portfolio import PortfolioReport

ENGINE_VERSION = "risk-1.0.0"


class HoldingRisk(BaseModel):
    position_weight: float
    sector: str
    sector_weight: float
    position_limit: float
    sector_limit: float
    position_breach: bool
    sector_breach: bool


class RiskReport(BaseModel):
    engine_version: str = ENGINE_VERSION
    max_position_weight: float
    max_sector_weight: float
    top_position_weight: float
    top5_weight: float
    herfindahl_index: float
    breaches: list[str]
    holdings: dict[str, HoldingRisk]


def compute_risk(portfolio: PortfolioReport, max_position_weight: float, max_sector_weight: float) -> RiskReport:
    weights = sorted((h.weight for h in portfolio.holdings), reverse=True)
    per: dict[str, HoldingRisk] = {}
    breaches: list[str] = []
    for h in portfolio.holdings:
        sw = portfolio.sector_allocation.get(h.sector, 0.0)
        pr = HoldingRisk(
            position_weight=h.weight,
            sector=h.sector,
            sector_weight=sw,
            position_limit=max_position_weight,
            sector_limit=max_sector_weight,
            position_breach=h.weight > max_position_weight,
            sector_breach=sw > max_sector_weight,
        )
        per[h.symbol] = pr
        if pr.position_breach:
            breaches.append(f"{h.symbol} weight {h.weight:.1%} > {max_position_weight:.0%}")
    for sector, sw in portfolio.sector_allocation.items():
        if sw > max_sector_weight:
            breaches.append(f"Sector {sector} weight {sw:.1%} > {max_sector_weight:.0%}")
    return RiskReport(
        max_position_weight=max_position_weight,
        max_sector_weight=max_sector_weight,
        top_position_weight=weights[0] if weights else 0.0,
        top5_weight=round(sum(weights[:5]), 6),
        herfindahl_index=round(sum(w * w for w in weights), 6),
        breaches=breaches,
        holdings=per,
    )
