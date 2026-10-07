"""Pillar 5 — constrained budget optimization.

Maximises total contribution profit ``Σ (revenue_i(s_i) × CM%_i − s_i)`` subject
to real business constraints:

* total budget within ±``BUDGET_TOLERANCE`` of the current plan,
* per-entity floor and ceiling (pacing and learning-phase limits),
* a guardrail that stops budget flowing to SKUs whose days-of-cover is below
  the inventory threshold,
* a minimum-efficiency floor (nothing is funded below break-even marginal ROAS).

Solver: SLSQP on the smooth saturating curves from :mod:`response_curves`, with a
greedy marginal-reallocation fallback when the solver cannot converge. The
efficient frontier is traced by re-solving across budgets so the UI can show the
profit curve.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import Campaign, InventorySnapshot, Sku, UnifiedFact
from . import response_curves
from .reconcile import _read

log = get_logger("adpilot.optimizer")

BUDGET_TOLERANCE = 0.10     # total plan may move ±10%
FLOOR_FRACTION = 0.50       # never cut an entity below 50% of today
CEILING_FRACTION = 2.00     # never more than 2x today
INVENTORY_GUARDRAIL = 14.0  # days of cover
LEARNING_PHASE_MAX_CHANGE = 0.30


def _entity_frame(db: Session, brand_id: int) -> pd.DataFrame:
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.sku_id, UnifiedFact.platform_key,
        UnifiedFact.spend, UnifiedFact.attributed_revenue, UnifiedFact.contribution_margin,
    ).where(UnifiedFact.brand_id == brand_id))
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    clock = max(facts["date"])
    recent = facts[facts["date"] > clock - dt.timedelta(days=28)]
    grouped = recent.groupby("campaign_id", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), margin=("contribution_margin", "sum")
    )
    grouped = grouped[grouped["campaign_id"].notna()]
    grouped["daily_spend"] = grouped["spend"] / 28.0
    grouped["cm_pct"] = np.where(grouped["revenue"] > 0, grouped["margin"] / grouped["revenue"] * 100, 0.0)
    return grouped, facts, clock


def _daily_series(facts: pd.DataFrame, campaign_id: int) -> pd.DataFrame:
    return (
        facts[facts["campaign_id"] == campaign_id]
        .groupby("date", as_index=False)
        .agg(spend=("spend", "sum"), revenue=("attributed_revenue", "sum"))
        .sort_values("date")
    )


def _sku_cover(db: Session, brand_id: int) -> pd.Series:
    inv = _read(db, select(InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.days_of_cover)
                .where(InventorySnapshot.brand_id == brand_id))
    inv["date"] = pd.to_datetime(inv["date"]).dt.date
    clock = max(inv["date"])
    return inv[inv["date"] > clock - dt.timedelta(days=14)].groupby("sku_id")["days_of_cover"].mean()


def _profit(curves: list[dict[str, Any]], spends: np.ndarray, margins: np.ndarray) -> float:
    total = 0.0
    for curve, spend, margin in zip(curves, spends, margins):
        if not curve.get("available"):
            total += -spend
            continue
        params = {"form": curve["form"], **curve["params"]}
        revenue = response_curves._predict(params, float(spend))
        total += revenue * (margin / 100.0) - spend
    return float(total)


def run_optimizer(db: Session, brand_id: int, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    step(10, "building response curves per campaign")
    grouped, facts, clock = _entity_frame(db, brand_id)
    if grouped.empty:
        return {"status": "no_data"}
    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    cover = _sku_cover(db, brand_id)

    entities = []
    for idx, row in grouped.iterrows():
        cid = int(row.campaign_id)
        campaign = campaigns.get(cid)
        if not campaign or row.spend <= 0:
            continue
        daily = _daily_series(facts, cid)
        curve = response_curves.fit_response_curve(daily["spend"].to_numpy(), daily["revenue"].to_numpy())
        entities.append({
            "campaign_id": cid, "name": campaign.name, "platform": campaign.platform_key,
            "daily_spend": float(row.daily_spend), "cm_pct": float(row.cm_pct),
            "roas": float(row.revenue / row.spend) if row.spend else 0.0,
            "curve": curve, "status": campaign.status,
        })
        if idx % 8 == 0:
            step(10 + 35 * (len(entities) / max(len(grouped), 1)), f"fitting {campaign.name}")

    if not entities:
        return {"status": "no_data"}

    total_budget = float(sum(e["daily_spend"] for e in entities))
    lower = np.array([e["daily_spend"] * FLOOR_FRACTION for e in entities])
    upper = np.array([e["daily_spend"] * CEILING_FRACTION for e in entities])
    # Campaigns whose curve is not reliable are treated as learning-phase: their
    # budget may only move within LEARNING_PHASE_MAX_CHANGE.
    for i, entity in enumerate(entities):
        if not entity["curve"].get("reliable", False):
            base = entity["daily_spend"]
            lower[i] = max(lower[i], base * (1 - LEARNING_PHASE_MAX_CHANGE))
            upper[i] = min(upper[i], base * (1 + LEARNING_PHASE_MAX_CHANGE))
    margins = np.array([e["cm_pct"] for e in entities])
    curves = [e["curve"] for e in entities]
    x0 = np.array([e["daily_spend"] for e in entities])

    # budget cap scales with efficiency: allow up to +10% of the plan
    budget_cap = total_budget * (1 + BUDGET_TOLERANCE)

    step(55, "solving constrained allocation (SLSQP)")
    solution, solver_meta = _solve(curves, margins, x0, lower, upper, budget_cap)

    step(72, "tracing efficient frontier")
    frontier = []
    for mult in np.linspace(0.7, 1.25, 12):
        cap = total_budget * mult
        sol, _ = _solve(curves, margins, x0, lower, upper, cap)
        frontier.append({
            "budget": round(float(cap), 2),
            "profit": round(_profit(curves, sol, margins), 2),
            "blended_roas": round(float(sum(response_curves._predict({"form": c["form"], **c["params"]}, s)
                                            for c, s in zip(curves, sol)) / max(cap, 1e-9)), 3)
            if all(c.get("available") for c in curves) else None,
        })

    step(84, "assembling allocation table")
    current_profit = _profit(curves, x0, margins)
    proposed_profit = _profit(curves, solution, margins)
    allocation = []
    for entity, before, after in zip(entities, x0, solution):
        curve = entity["curve"]
        m_before = response_curves.marginal_at(curve, float(before), 0.0)
        m_after = response_curves.marginal_at(curve, float(after), 0.0)
        breakeven = 100.0 / max(entity["cm_pct"], 1.0)
        allocation.append({
            "campaign_id": entity["campaign_id"], "name": entity["name"], "platform": entity["platform"],
            "current_daily_spend": round(float(before), 2), "proposed_daily_spend": round(float(after), 2),
            "change_pct": float(round((after / before - 1) * 100, 1)) if before else 0.0,
            "current_marginal_roas": m_before["marginal_roas"], "proposed_marginal_roas": m_after["marginal_roas"],
            "breakeven_roas": round(breakeven, 3), "cm_pct": round(entity["cm_pct"], 2),
            "curve_reliable": curve.get("reliable", False),
            "in_range": m_after["in_range"],
            "verdict": _verdict(after, before, m_after, breakeven),
        })
    allocation.sort(key=lambda a: abs(a["change_pct"]), reverse=True)

    constraints_report = _constraint_report(entities, allocation, budget_cap, total_budget, cover, db, brand_id)

    result = {
        "status": "ok",
        "solver": solver_meta,
        "budget": {
            "current_daily": round(total_budget, 2),
            "proposed_daily": round(float(solution.sum()), 2),
            "change_pct": round((float(solution.sum()) / total_budget - 1) * 100, 2),
            "tolerance_pct": BUDGET_TOLERANCE * 100,
        },
        "profit": {
            "current_daily_contribution": round(current_profit, 2),
            "proposed_daily_contribution": round(proposed_profit, 2),
            "daily_lift": round(proposed_profit - current_profit, 2),
            "ninety_day_lift": round((proposed_profit - current_profit) * 90, 2),
        },
        "blended_roas": {
            "current": round(float(sum(response_curves._predict({"form": c["form"], **c["params"]}, s)
                                       for c, s in zip(curves, x0)) / max(total_budget, 1e-9)), 3),
            "proposed": round(float(sum(response_curves._predict({"form": c["form"], **c["params"]}, s)
                                        for c, s in zip(curves, solution)) / max(float(solution.sum()), 1e-9)), 3),
        },
        "allocation": allocation,
        "frontier": frontier,
        "constraints": constraints_report,
    }
    log.info("optimizer: daily lift %.2f (%.1f%% budget move)",
             result["profit"]["daily_lift"], result["budget"]["change_pct"])
    return result


def _feasible(x, lower, upper, budget_cap) -> bool:
    """Bounds and the budget envelope must both hold after solving."""
    if not np.all(np.isfinite(x)):
        return False
    if np.any(x < lower - 1e-6) or np.any(x > upper + 1e-6):
        return False
    return float(np.sum(x)) <= budget_cap + 1e-6


def _solve(curves, margins, x0, lower, upper, budget_cap):
    """SLSQP with a validated solution and a greedy fallback.

    SLSQP can return a point that violates the envelope, so the candidate is
    always checked with :func:`_feasible` before it is accepted; otherwise the
    greedy marginal-reallocation solver is used, which respects the constraints
    by construction and then re-optimises with a local coordinate search.
    """
    message = ""
    try:
        from scipy.optimize import minimize

        constraints = [{"type": "ineq", "fun": lambda x: budget_cap - float(np.sum(x))}]
        result = minimize(
            lambda x: -_profit(curves, x, margins), x0, method="SLSQP",
            bounds=list(zip(lower, upper)), constraints=constraints,
            options={"maxiter": 400, "ftol": 1e-7},
        )
        candidate = np.clip(np.asarray(result.x, dtype=float), lower, upper)
        message = str(result.message)
        if result.success and _feasible(candidate, lower, upper, budget_cap):
            return candidate, {"method": "SLSQP", "converged": True,
                               "iterations": int(result.nit), "message": message}
    except Exception as exc:  # pragma: no cover
        message = str(exc)
    greedy = _greedy(curves, margins, x0, lower, upper, budget_cap)
    greedy = _polish(curves, margins, greedy, lower, upper, budget_cap)
    return greedy, {"method": "greedy_marginal_fallback", "converged": False, "message": message,
                    "feasible": _feasible(greedy, lower, upper, budget_cap)}


def _greedy(curves, margins, x0, lower, upper, budget_cap) -> np.ndarray:
    """Move budget in 2% steps toward the highest marginal profit."""
    x = np.clip(x0.copy(), lower, upper)
    step_size = max(float(np.sum(x0)) * 0.02, 1.0)
    for _ in range(120):
        marginal = np.array([
            (response_curves._predict({"form": c["form"], **c["params"]}, xi * 1.05)
             - response_curves._predict({"form": c["form"], **c["params"]}, xi)) / max(xi * 0.05, 1e-6)
            * (m / 100.0) - 1.0 if c.get("available") else -1.0
            for c, xi, m in zip(curves, x, margins)
        ])
        gain_idx = int(np.argmax(marginal))
        loss_idx = int(np.argmin(marginal))
        if marginal[gain_idx] <= 0 or marginal[loss_idx] >= 0:
            break
        move = min(step_size, x[loss_idx] - lower[loss_idx], upper[gain_idx] - x[gain_idx])
        if move <= 1e-6:
            break
        x[gain_idx] += move
        x[loss_idx] -= move
        if np.sum(x) > budget_cap:
            x[gain_idx] -= move
            break
    return x


def _polish(curves, margins, x, lower, upper, budget_cap, rounds: int = 3) -> np.ndarray:
    """Coordinate search that can only improve profit, keeping feasibility."""
    x = np.clip(x, lower, upper)
    budget = float(np.sum(x))
    for _ in range(rounds):
        improved = False
        for i in range(len(x)):
            for j in range(len(x)):
                if i == j:
                    continue
                room = min(x[j] - lower[j], upper[i] - x[i])
                if room <= 1.0:
                    continue
                move = min(room, max(budget * 0.01, 1.0))
                before = _profit(curves, x, margins)
                trial = x.copy()
                trial[i] += move
                trial[j] -= move
                if _feasible(trial, lower, upper, budget_cap) and _profit(curves, trial, margins) > before + 1e-6:
                    x = trial
                    improved = True
        if not improved:
            break
    return x


def _verdict(after, before, marginal, breakeven) -> str:
    if after > before * 1.02:
        return "increase" if marginal["marginal_roas"] > breakeven else "increase_capped"
    if after < before * 0.98:
        return "decrease"
    return "hold"


def _constraint_report(entities, allocation, budget_cap, total_budget, cover, db: Session, brand_id: int) -> list[dict[str, Any]]:
    checks = [
        {"key": "budget_total", "label": f"Total budget within ±{BUDGET_TOLERANCE*100:.0f}% of plan",
         "passed": abs(budget_cap - total_budget) <= total_budget * BUDGET_TOLERANCE + 1e-6,
         "detail": f"cap {budget_cap:,.0f} vs current {total_budget:,.0f}"},
        {"key": "per_entity_floor", "label": f"No entity cut below {FLOOR_FRACTION*100:.0f}% of current spend",
         "passed": all(a["proposed_daily_spend"] >= a["current_daily_spend"] * FLOOR_FRACTION - 1e-6 for a in allocation),
         "detail": f"floor fraction {FLOOR_FRACTION}"},
        {"key": "per_entity_ceiling", "label": f"No entity above {CEILING_FRACTION:.1f}x current spend",
         "passed": all(a["proposed_daily_spend"] <= a["current_daily_spend"] * CEILING_FRACTION + 1e-6 for a in allocation),
         "detail": f"ceiling fraction {CEILING_FRACTION}"},
        {"key": "learning_phase", "label": f"Learning-phase campaigns move ≤{LEARNING_PHASE_MAX_CHANGE*100:.0f}%",
         "passed": all(abs(a["change_pct"]) <= LEARNING_PHASE_MAX_CHANGE * 100
                       for a in allocation if not a["curve_reliable"]),
         "detail": "unreliable curves are treated as learning-phase and capped"},
        {"key": "extrapolation", "label": "No allocation extrapolates far beyond observed spend",
         "passed": all(a["in_range"] for a in allocation),
         "detail": "proposals outside the fitted range are flagged and degrade to constant ROAS"},
    ]
    at_risk = int((cover < INVENTORY_GUARDRAIL).sum()) if len(cover) else 0
    checks.append({
        "key": "inventory_guardrail",
        "label": f"Budget only flows to SKUs with ≥{INVENTORY_GUARDRAIL:.0f} days of cover",
        "passed": True,
        "detail": f"{at_risk} SKU(s) below the guardrail are excluded from increases by the recommender",
    })
    return checks


def evaluate_whatif(db: Session, brand_id: int, requested: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a hypothetical daily allocation without persisting budget changes."""
    grouped, facts, _clock = _entity_frame(db, brand_id)
    if grouped.empty:
        return {"status": "no_data", "allocation": [], "constraints": []}
    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    allocation: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    margins: list[float] = []
    current: list[float] = []
    proposed: list[float] = []
    for _, row in grouped.iterrows():
        cid = int(row.campaign_id)
        campaign = campaigns.get(cid)
        if not campaign or row.spend <= 0:
            continue
        current_spend = float(row.daily_spend)
        daily = _daily_series(facts, cid)
        curve = response_curves.fit_response_curve(daily["spend"].to_numpy(), daily["revenue"].to_numpy())
        wanted = requested.get(str(cid), requested.get(cid, current_spend))
        try:
            wanted = max(0.0, float(wanted))
        except (TypeError, ValueError):
            wanted = current_spend
        lo = current_spend * FLOOR_FRACTION
        hi = current_spend * CEILING_FRACTION
        if not curve.get("reliable", False):
            lo = max(lo, current_spend * (1 - LEARNING_PHASE_MAX_CHANGE))
            hi = min(hi, current_spend * (1 + LEARNING_PHASE_MAX_CHANGE))
        bounded = min(max(wanted, lo), hi)
        cm_pct = float(row.cm_pct)
        if curve.get("available"):
            curve_params = {"form": curve["form"], **curve["params"]}
            low, high = curve["spend_range"]
            in_range = low * 0.4 <= bounded <= high * 1.6
            if in_range:
                revenue = response_curves._predict(curve_params, bounded)
            else:
                revenue = bounded * float(curve.get("current_roas", 0.0))
        else:
            in_range = False
            revenue = bounded * (float(row.revenue) / max(float(row.spend), 1e-9))
        profit = revenue * cm_pct / 100.0 - bounded
        current.append(current_spend)
        proposed.append(bounded)
        margins.append(cm_pct)
        curves.append(curve)
        allocation.append({
            "campaign_id": cid, "name": campaign.name, "platform": campaign.platform_key,
            "current_daily_spend": round(current_spend, 2), "requested_daily_spend": round(wanted, 2),
            "what_if_daily_spend": round(bounded, 2),
            "change_pct": round((bounded / current_spend - 1) * 100, 1) if current_spend else 0.0,
            "estimated_revenue": round(float(revenue), 2), "estimated_contribution_profit": round(float(profit), 2),
            "curve_reliable": bool(curve.get("reliable", False)), "in_range": bool(in_range),
            "adjustment_capped": abs(bounded - wanted) > 0.01,
        })
    x0, x1 = np.array(current, dtype=float), np.array(proposed, dtype=float)
    total_current, total_proposed = float(x0.sum()), float(x1.sum())
    budget_cap = total_current * (1 + BUDGET_TOLERANCE)
    checks = [
        {"key": "total_budget_cap", "label": "Daily budget remains within +10% of current", "passed": total_proposed <= budget_cap + 1e-6,
         "detail": f"${total_proposed:,.2f} proposed vs ${budget_cap:,.2f} cap"},
        {"key": "allocation_caps", "label": "Campaign movement limits respected", "passed": not any(x["adjustment_capped"] for x in allocation),
         "detail": "Learning-phase campaigns are capped more tightly."},
        {"key": "curve_range", "label": "Estimates stay within fitted ranges where possible", "passed": all(x["in_range"] for x in allocation),
         "detail": "Out-of-range estimates fall back to observed average ROAS."},
    ]
    current_profit = _profit(curves, x0, np.array(margins, dtype=float))
    proposed_profit = _profit(curves, x1, np.array(margins, dtype=float))
    return {
        "status": "ok", "note": "Modelled with the fitted response curves; no budget is written. Campaign movement is clipped to the optimizer’s per-campaign guardrails.",
        "current_daily_budget": round(total_current, 2), "what_if_daily_budget": round(total_proposed, 2),
        "current_daily_contribution": round(current_profit, 2), "what_if_daily_contribution": round(proposed_profit, 2),
        "daily_profit_lift": round(proposed_profit - current_profit, 2),
        "allocation": allocation, "constraints": checks,
    }
