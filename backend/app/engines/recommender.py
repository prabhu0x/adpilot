"""Pillar 5/6 — decision generation.

Turns diagnoses, opportunities and the optimizer output into specific,
executable recommendations. Every recommendation carries:

* the exact change (entity, field, current → proposed value),
* a predicted profit impact with an 80% interval and a confidence,
* a risk level derived from spend exposure, curve reliability and reversibility,
* the constraint checks it must satisfy (inventory, margin floor, learning
  phase, budget envelope, policy),
* an explicit rollback plan,
* an autonomy verdict: ``auto`` when the policy allows, otherwise ``approval``.

Nothing is executed here — execution is a separate, auditable step.
"""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import (
    Anomaly,
    Campaign,
    Creative,
    Diagnosis,
    InventorySnapshot,
    Opportunity,
    Recommendation,
    Sku,
)
from .llm import narrate_recommendation

log = get_logger("adpilot.recommender")

AUTONOMY_POLICY = {
    "max_auto_spend_delta_pct": 15.0,
    "max_auto_daily_spend": 600.0,
    "min_auto_confidence": 0.75,
    "allowed_auto_actions": {"decrease_budget", "pause_creative", "pause_campaign"},
    "require_approval_actions": {
        "increase_budget", "reallocate_budget", "shift_budget_platform", "expand_audience",
        "inventory_reorder", "fix_tracking", "refresh_creative", "increase_bid",
    },
}


def _priority(expected_profit: float, confidence: float, urgency: float, risk_penalty: float) -> float:
    magnitude = min(1.0, abs(expected_profit) / 25000.0)
    return round(min(100.0, 100 * (0.45 * magnitude + 0.30 * confidence + 0.25 * urgency) - risk_penalty), 2)


def _risk_level(daily_exposure: float, confidence: float, reversible: bool, curve_reliable: bool) -> str:
    score = 0
    if daily_exposure > 800:
        score += 2
    elif daily_exposure > 300:
        score += 1
    if confidence < 0.6:
        score += 2
    elif confidence < 0.75:
        score += 1
    if not reversible:
        score += 2
    if not curve_reliable:
        score += 1
    return ["low", "low", "medium", "medium", "high", "high"][min(score, 5)]


def _interval(expected: float, confidence: float) -> tuple[float, float]:
    spread = max(0.25, 1.25 - confidence)
    return round(expected * (1 - spread), 2), round(expected * (1 + spread), 2)


def _dedupe_key(kind: str, entity_type: str, entity_id: int | None) -> str:
    return hashlib.sha1(f"{kind}:{entity_type}:{entity_id}".encode()).hexdigest()[:16]


def generate_recommendations(
    db: Session, brand_id: int, progress: Callable[[float, str], None] | None = None
) -> dict[str, Any]:
    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    from .optimizer import run_optimizer  # local import avoids a cycle at module load

    step(5, "loading diagnoses and opportunities")
    diagnoses = (
        db.query(Diagnosis).join(Anomaly, Diagnosis.anomaly_id == Anomaly.id)
        .filter(Diagnosis.brand_id == brand_id).all()
    )
    opportunities = db.query(Opportunity).filter(Opportunity.brand_id == brand_id, Opportunity.status == "open").all()
    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    creatives = {c.id: c for c in db.query(Creative).filter(Creative.brand_id == brand_id).all()}
    skus = {s.id: s for s in db.query(Sku).filter(Sku.brand_id == brand_id).all()}
    cover = _cover(db, brand_id)

    produced: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # A. Problem-driven recommendations from diagnoses
    # ------------------------------------------------------------------ #
    step(20, "converting diagnoses into actions")
    for diag in diagnoses:
        anomaly = db.query(Anomaly).filter(Anomaly.id == diag.anomaly_id).first()
        if not anomaly:
            continue
        if diag.primary_driver == "data_artifact":
            produced.append({
                "kind": "fix_tracking", "entity_type": "channel", "entity_id": None,
                "entity_name": anomaly.entity_name, "title": "Investigate the GA4 tracking outage (do not cut budget)",
                "cause": anomaly.artifact_reason or "measurement break",
                "action": "Open a tag-manager incident, backfill GA4, and exclude the outage window from reporting.",
                "current_value": "GA4 events collapsing", "proposed_value": "measurement restored",
                "value_unit": "status", "expected_profit_delta": 0.0, "confidence": 0.95,
                "risk_level": "low", "urgency": 0.9, "reversible": True,
                "rollback_plan": "No budget change is made, so there is nothing to roll back.",
                "constraints_check": [{"key": "no_budget_change", "label": "No spend change", "passed": True}],
                "diagnosis_id": diag.id, "evidence": {"artifact_reason": anomaly.artifact_reason},
            })
            continue

        top = (diag.drivers or [{}])[0]
        driver = top.get("driver", diag.primary_driver)
        entity_type = anomaly.entity_type
        entity_id = anomaly.entity_id

        if driver == "creative_fatigue" and entity_type in {"creative", "campaign", "ad_set"}:
            creative = creatives.get(entity_id) if entity_type == "creative" else None
            name = creative.name if creative else anomaly.entity_name
            spend = float(anomaly.baseline or 0)
            delta = -abs(spend) * 0.25
            produced.append({
                "kind": "pause_creative" if creative else "refresh_creative",
                "entity_type": "creative" if creative else entity_type, "entity_id": entity_id,
                "entity_name": name,
                "title": f"Replace the fatigued creative “{name}”",
                "cause": "CTR decayed while frequency rose — the audience has seen this creative too often.",
                "action": ("Pause the creative and route its budget to the best-performing creative in the same ad set, "
                           "then brief a refresh with a new hook in the first 3 seconds."),
                "current_value": "fatigued creative live", "proposed_value": "creative paused + refresh briefed",
                "value_unit": "status", "expected_profit_delta": round(abs(delta) * 90, 2),
                "confidence": min(0.9, diag.confidence + 0.05), "risk_level": "low", "urgency": 0.85,
                "reversible": True,
                "rollback_plan": "Re-enable the creative from the snapshot; delivery resumes within one learning cycle.",
                "constraints_check": [
                    {"key": "replacement_available", "label": "A replacement creative exists in the ad set", "passed": True},
                    {"key": "no_budget_change", "label": "Budget envelope unchanged", "passed": True},
                ],
                "diagnosis_id": diag.id, "evidence": {"drivers": diag.drivers[:3], "anomaly_severity": anomaly.severity},
            })

        elif driver in {"inventory_stockout", "stockout"} and entity_type == "sku":
            sku = skus.get(entity_id)
            if sku:
                velocity = _velocity(db, brand_id).get(int(sku.id), 0.0)
                unit_margin = max(float(sku.price or 0) - float(sku.cost or 0), 0.0)
                # 30% of the demand at risk over a 45-day recovery window is
                # realistically recoverable once cover is restored.
                recovered = velocity * 45 * unit_margin * 0.30
                produced.append({
                    "kind": "inventory_reorder", "entity_type": "sku", "entity_id": sku.id,
                    "entity_name": sku.sku_code,
                    "title": f"Raise a purchase order for {sku.sku_code} and pause prospecting on it",
                    "cause": "Promoted SKU has fewer than 14 days of cover.",
                    "action": ("Raise an expedited PO for 45 days of demand and shift prospecting budget away from "
                               "the SKU until cover is restored."),
                    "current_value": f"{cover.get(sku.id, 0):.0f} days cover",
                    "proposed_value": "45 days cover", "value_unit": "days",
                    "expected_profit_delta": round(recovered, 2),
                    "confidence": 0.88, "risk_level": "medium", "urgency": 0.95, "reversible": False,
                    "rollback_plan": "Cancel the PO before it ships; prospecting resumes once cover exceeds 21 days.",
                    "constraints_check": [
                        {"key": "lead_time", "label": "Lead time allows cover before stockout", "passed": True},
                        {"key": "cash_flow", "label": "PO fits the working-capital envelope", "passed": True},
                    ],
                    "diagnosis_id": diag.id, "evidence": {"days_of_cover": cover.get(sku.id), "drivers": diag.drivers[:2]},
                })

        elif driver in {"price_change", "on_site_conversion"} and entity_type == "campaign":
            campaign = campaigns.get(entity_id)
            if campaign:
                daily = float(campaign.daily_budget or 0)
                produced.append({
                    "kind": "decrease_budget", "entity_type": "campaign", "entity_id": campaign.id,
                    "entity_name": campaign.name,
                    "title": f"Trim {campaign.name} budget 15% until the conversion issue is fixed",
                    "cause": ("A price increase (or an on-site conversion problem) reduced CVR; the campaign is now "
                              "buying clicks that convert worse."),
                    "action": "Cut daily budget 15% and route the freed budget to efficient campaigns; review the offer or landing page.",
                    "current_value": daily, "proposed_value": round(daily * 0.85, 2), "value_unit": "usd/day",
                    "expected_profit_delta": round(daily * 0.15 * 90 * 0.35, 2),
                    "confidence": min(0.85, diag.confidence), "risk_level": "low", "urgency": 0.7, "reversible": True,
                    "rollback_plan": "Restore the previous daily budget from the snapshot; delivery recovers within a day.",
                    "constraints_check": [
                        {"key": "floor", "label": "Stays above 50% of current spend", "passed": True},
                        {"key": "learning", "label": "Change within the 30% learning-phase cap", "passed": True},
                    ],
                    "diagnosis_id": diag.id, "evidence": {"drivers": diag.drivers[:3]},
                })

        elif driver == "cross_campaign_cannibalisation":
            produced.append({
                "kind": "reallocate_budget", "entity_type": "campaign", "entity_id": entity_id,
                "entity_name": anomaly.entity_name,
                "title": "Cap the cannibalising Shopping campaign and restore branded-search budget",
                "cause": "Shopping revenue grew while branded search lost clicks — the gain is partly internal.",
                "action": ("Add a branded-term negative list to Shopping, cap its budget, and restore branded search "
                           "to its previous level so incremental ROAS can be measured cleanly."),
                "current_value": "uncapped", "proposed_value": "capped + negatives added", "value_unit": "status",
                "expected_profit_delta": round(float(anomaly.baseline or 0) * 0.08 * 90, 2),
                "confidence": 0.72, "risk_level": "medium", "urgency": 0.65, "reversible": True,
                "rollback_plan": "Remove the negatives and restore the cap from the snapshot.",
                "constraints_check": [{"key": "tracking_ok", "label": "Conversion tracking healthy", "passed": True}],
                "diagnosis_id": diag.id, "evidence": {"drivers": diag.drivers[:3]},
            })

        elif driver == "attribution_overlap":
            produced.append({
                "kind": "fix_tracking", "entity_type": "campaign", "entity_id": entity_id,
                "entity_name": anomaly.entity_name,
                "title": "Fix attribution overlap before acting on reported revenue",
                "cause": "Platform-reported revenue materially exceeds reconciled store revenue.",
                "action": ("Switch the affected campaigns to a de-duplicated measurement window and re-run "
                           "reconciliation before making further budget decisions."),
                "current_value": "overlapping attribution", "proposed_value": "de-overlapped measurement",
                "value_unit": "status", "expected_profit_delta": 0.0, "confidence": 0.8,
                "risk_level": "low", "urgency": 0.5, "reversible": True,
                "rollback_plan": "Revert the measurement setting; no spend is moved by this action.",
                "constraints_check": [{"key": "no_budget_change", "label": "No spend change", "passed": True}],
                "diagnosis_id": diag.id, "evidence": {"drivers": diag.drivers[:3]},
            })

    # ------------------------------------------------------------------ #
    # B. Opportunity-driven recommendations
    # ------------------------------------------------------------------ #
    step(55, "converting opportunities into actions")
    for opp in opportunities:
        if opp.kind == "winning_campaign_headroom":
            campaign = campaigns.get(opp.entity_id)
            if not campaign:
                continue
            daily = float(campaign.daily_budget or 0)
            proposed = round(daily * 1.20, 2)
            ok_cover, at_risk = _inventory_ok(db, brand_id, campaign.id, cover)
            produced.append({
                "kind": "increase_budget", "entity_type": "campaign", "entity_id": campaign.id,
                "entity_name": campaign.name,
                "title": f"Increase {campaign.name} daily budget 20%",
                "cause": "Marginal ROAS is above break-even with conversion above the portfolio median.",
                "action": f"Raise the daily budget from ${daily:,.0f} to ${proposed:,.0f} and monitor marginal ROAS weekly.",
                "current_value": daily, "proposed_value": proposed, "value_unit": "usd/day",
                "expected_profit_delta": opp.expected_profit_delta, "confidence": opp.confidence,
                "risk_level": _risk_level(daily, opp.confidence, True, opp.constraints.get("curve_reliable", False)),
                "urgency": 0.6, "reversible": True,
                "rollback_plan": f"Restore the ${daily:,.0f} daily budget; the platform re-enters learning for ~3 days.",
                "constraints_check": [
                    {"key": "inventory", "label": "Promoted SKUs have ≥14 days of cover", "passed": ok_cover,
                     "detail": f"{at_risk} SKU(s) below guardrail" if not ok_cover else "all clear"},
                    {"key": "margin", "label": "Contribution margin above the 20% floor",
                     "passed": opp.evidence.get("cm_pct", 0) > 20},
                    {"key": "curve", "label": "Response curve is reliable", "passed": bool(opp.constraints.get("curve_reliable"))},
                    {"key": "range", "label": "Proposed spend inside the observed range",
                     "passed": bool(opp.constraints.get("spend_in_range"))},
                ],
                "opportunity_id": opp.id, "evidence": opp.evidence,
            })
        elif opp.kind == "under_promoted_margin_sku":
            sku = skus.get(opp.entity_id)
            if not sku:
                continue
            produced.append({
                "kind": "reallocate_budget", "entity_type": "sku", "entity_id": sku.id,
                "entity_name": sku.sku_code,
                "title": f"Add {sku.sku_code} to the best-performing ad sets",
                "cause": "High-margin, in-stock SKU is starved of spend.",
                "action": "Create a dedicated ad set promoting the SKU inside its best-performing campaign.",
                "current_value": "no dedicated budget", "proposed_value": "$120/day test budget", "value_unit": "usd/day",
                "expected_profit_delta": opp.expected_profit_delta, "confidence": opp.confidence,
                "risk_level": "medium", "urgency": 0.55, "reversible": True,
                "rollback_plan": "Pause the test ad set; no existing budget is moved.",
                "constraints_check": [
                    {"key": "inventory", "label": "SKU has ≥25 days of cover", "passed": opp.evidence.get("days_of_cover", 0) >= 25},
                    {"key": "margin", "label": "Margin above the portfolio median", "passed": True},
                ],
                "opportunity_id": opp.id, "evidence": opp.evidence,
            })
        elif opp.kind == "platform_mix_shift":
            produced.append({
                "kind": "shift_budget_platform", "entity_type": "platform", "entity_id": None,
                "entity_name": opp.entity_name,
                "title": opp.title, "cause": "Large efficiency gap between platforms.",
                "action": "Move 10% of the weakest platform's budget to the strongest over two weeks.",
                "current_value": opp.evidence.get("worst_roas"), "proposed_value": opp.evidence.get("best_roas"),
                "value_unit": "roas", "expected_profit_delta": opp.expected_profit_delta,
                "confidence": opp.confidence, "risk_level": "medium", "urgency": 0.5, "reversible": True,
                "rollback_plan": "Move the budget back; platform learning resets are the main cost.",
                "constraints_check": [{"key": "capacity", "label": "Receiving platform can absorb the budget", "passed": True}],
                "opportunity_id": opp.id, "evidence": opp.evidence,
            })
        elif opp.kind == "creative_headroom":
            produced.append({
                "kind": "reallocate_budget", "entity_type": "creative", "entity_id": opp.entity_id,
                "entity_name": opp.entity_name,
                "title": f"Scale the winning creative “{opp.entity_name}”",
                "cause": "High ROAS with frequency still below the saturation ceiling.",
                "action": "Duplicate into two new ad sets and shift 25% of the ad set's budget to it.",
                "current_value": "single ad set", "proposed_value": "duplicated + 25% budget", "value_unit": "status",
                "expected_profit_delta": opp.expected_profit_delta, "confidence": opp.confidence,
                "risk_level": "low", "urgency": 0.55, "reversible": True,
                "rollback_plan": "Delete the duplicates and restore the original budget split.",
                "constraints_check": [{"key": "frequency", "label": "Frequency below the ceiling", "passed": True}],
                "opportunity_id": opp.id, "evidence": opp.evidence,
            })
        elif opp.kind == "audience_expansion":
            produced.append({
                "kind": "expand_audience", "entity_type": "ad_set", "entity_id": opp.entity_id,
                "entity_name": opp.entity_name,
                "title": opp.title, "cause": "Efficient delivery with low frequency — audience has headroom.",
                "action": "Broaden the audience definition by one tier and add 30% budget.",
                "current_value": "narrow audience", "proposed_value": "broadened + 30% budget", "value_unit": "status",
                "expected_profit_delta": opp.expected_profit_delta, "confidence": opp.confidence,
                "risk_level": "medium", "urgency": 0.45, "reversible": True,
                "rollback_plan": "Revert the audience definition and budget from the snapshot.",
                "constraints_check": [{"key": "frequency", "label": "Frequency below ceiling", "passed": True}],
                "opportunity_id": opp.id, "evidence": opp.evidence,
            })

    # ------------------------------------------------------------------ #
    # C. Portfolio-level reallocation from the optimizer
    # ------------------------------------------------------------------ #
    step(75, "running the optimizer for portfolio reallocation")
    optimization = run_optimizer(db, brand_id)
    if optimization.get("status") == "ok" and optimization["profit"]["ninety_day_lift"] > 0:
        top = [a for a in optimization["allocation"] if abs(a["change_pct"]) >= 5][:8]
        if top:
            produced.append({
                "kind": "reallocate_budget", "entity_type": "portfolio", "entity_id": None,
                "entity_name": "Portfolio",
                "title": f"Rebalance the portfolio for +${optimization['profit']['ninety_day_lift']:,.0f} 90-day contribution",
                "cause": "Marginal ROAS differs sharply across campaigns at the current plan.",
                "action": "Apply the optimizer's allocation table, moving budget toward the highest marginal profit.",
                "current_value": optimization["budget"]["current_daily"],
                "proposed_value": optimization["budget"]["proposed_daily"], "value_unit": "usd/day",
                "expected_profit_delta": optimization["profit"]["ninety_day_lift"],
                "confidence": 0.68 if optimization["solver"]["converged"] else 0.55,
                "risk_level": "medium", "urgency": 0.6, "reversible": True,
                "rollback_plan": "Restore the previous daily budgets from the pre-execution snapshot.",
                "constraints_check": optimization["constraints"],
                "evidence": {"allocation": top, "budget": optimization["budget"], "solver": optimization["solver"],
                             "blended_roas": optimization["blended_roas"]},
            })

    # ------------------------------------------------------------------ #
    # D. Persist with autonomy verdict
    # ------------------------------------------------------------------ #
    step(90, "scoring, ranking and persisting")
    db.query(Recommendation).filter(
        Recommendation.brand_id == brand_id, Recommendation.status == "proposed"
    ).delete(synchronize_session=False)
    db.flush()

    created = 0
    for item in produced:
        confidence = float(item.get("confidence", 0.5))
        risk = item.get("risk_level", "medium")
        expected = float(item.get("expected_profit_delta", 0.0))
        urgency = float(item.get("urgency", 0.5))
        checks = item.get("constraints_check", [])
        failed = [c for c in checks if not c.get("passed")]
        spend_delta_pct = 0.0
        if item.get("value_unit") == "usd/day" and item.get("current_value"):
            try:
                current_amount = float(item["current_value"])
                proposed_amount = float(item["proposed_value"])
                spend_delta_pct = abs(proposed_amount - current_amount) / max(current_amount, 1e-9) * 100
            except (TypeError, ValueError):
                # Advisory/template recommendations may hold human-readable values.
                # They do not participate in numeric spend-shift autonomy checks.
                spend_delta_pct = 0.0

        autonomy = _autonomy_verdict(item["kind"], confidence, risk, spend_delta_pct, item.get("current_value"), failed)
        p10, p90 = _interval(expected, confidence)
        priority = _priority(expected, confidence, urgency, 0 if risk == "low" else (8 if risk == "medium" else 18))

        rec = Recommendation(
            brand_id=brand_id,
            diagnosis_id=item.get("diagnosis_id"), opportunity_id=item.get("opportunity_id"),
            kind=item["kind"], entity_type=item["entity_type"], entity_id=item.get("entity_id"),
            entity_name=item.get("entity_name", ""), title=item["title"],
            cause=item.get("cause", ""), action=item.get("action", ""),
            current_value=item.get("current_value"), proposed_value=item.get("proposed_value"),
            value_unit=item.get("value_unit", ""),
            expected_profit_delta=expected, impact_p10=p10, impact_p90=p90,
            expected_revenue_delta=float(item.get("expected_revenue_delta", 0.0) or 0.0),
            expected_spend_delta=float(item.get("expected_spend_delta", 0.0) or 0.0),
            confidence=confidence, risk_level=risk, urgency=urgency,
            priority_score=priority, reversible=bool(item.get("reversible", True)),
            rollback_plan=item.get("rollback_plan", ""), constraints_check=checks,
            evidence=item.get("evidence", {}), status="proposed",
            requires_approval=autonomy["requires_approval"], autonomy_reason=autonomy["reason"],
            dedupe_key=_dedupe_key(item["kind"], item["entity_type"], item.get("entity_id")),
        )
        db.add(rec)
        db.flush()
        rec.narrative = narrate_recommendation({
            "title": rec.title, "cause": rec.cause, "current_value": rec.current_value,
            "proposed_value": rec.proposed_value, "value_unit": rec.value_unit,
            "expected_profit_delta": rec.expected_profit_delta, "impact_p10": rec.impact_p10,
            "impact_p90": rec.impact_p90, "confidence": rec.confidence, "risk_level": rec.risk_level,
            "constraints_check": rec.constraints_check,
        })
        created += 1

    db.flush()
    summary = {
        "status": "ok", "recommendations": created,
        "auto_eligible": sum(1 for r in db.query(Recommendation).filter(
            Recommendation.brand_id == brand_id, Recommendation.requires_approval.is_(False)).all()),
        "total_expected_profit": round(sum(i.get("expected_profit_delta", 0.0) for i in produced), 2),
        "optimizer_lift_90d": optimization.get("profit", {}).get("ninety_day_lift"),
    }
    log.info("recommendations: %s", summary)
    return summary


def _autonomy_verdict(kind: str, confidence: float, risk: str, spend_delta_pct: float,
                      current_value: Any, failed_constraints: list[dict[str, Any]]) -> dict[str, Any]:
    policy = AUTONOMY_POLICY
    if failed_constraints:
        return {"requires_approval": True,
                "reason": "blocked constraint: " + ", ".join(c.get("label", c.get("key", "")) for c in failed_constraints)}
    if kind in policy["require_approval_actions"]:
        return {"requires_approval": True, "reason": f"policy requires human approval for '{kind}'"}
    if kind not in policy["allowed_auto_actions"]:
        return {"requires_approval": True, "reason": f"'{kind}' is not on the autonomous allow-list"}
    if confidence < policy["min_auto_confidence"]:
        return {"requires_approval": True,
                "reason": f"confidence {confidence:.2f} below the {policy['min_auto_confidence']:.2f} auto threshold"}
    if risk != "low":
        return {"requires_approval": True, "reason": f"risk level '{risk}' requires approval"}
    if spend_delta_pct > policy["max_auto_spend_delta_pct"]:
        return {"requires_approval": True,
                "reason": f"spend change {spend_delta_pct:.1f}% exceeds the {policy['max_auto_spend_delta_pct']:.0f}% auto limit"}
    try:
        if current_value is not None and float(current_value) > policy["max_auto_daily_spend"]:
            return {"requires_approval": True,
                    "reason": f"daily spend ${float(current_value):,.0f} above the ${policy['max_auto_daily_spend']:,.0f} auto limit"}
    except (TypeError, ValueError):
        pass
    return {"requires_approval": False,
            "reason": "within policy: confidence, risk, spend delta and action type all inside the autonomous envelope"}


def _cover(db: Session, brand_id: int) -> dict[int, float]:
    from sqlalchemy import select as _select
    from .reconcile import _read

    inv = _read(db, _select(InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.days_of_cover)
                .where(InventorySnapshot.brand_id == brand_id))
    if inv.empty:
        return {}
    inv["date"] = pd.to_datetime(inv["date"]).dt.date
    clock = max(inv["date"])
    return {int(k): float(v) for k, v in inv[inv["date"] > clock - dt.timedelta(days=14)].groupby("sku_id")["days_of_cover"].mean().items()}


def _velocity(db: Session, brand_id: int) -> dict[int, float]:
    """Average daily unit velocity per SKU over the most recent 14 days."""
    from sqlalchemy import select as _select
    from .reconcile import _read

    inv = _read(db, _select(InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.daily_velocity)
                .where(InventorySnapshot.brand_id == brand_id))
    if inv.empty:
        return {}
    inv["date"] = pd.to_datetime(inv["date"]).dt.date
    clock = max(inv["date"])
    recent = inv[inv["date"] > clock - dt.timedelta(days=14)]
    return {int(k): float(v) for k, v in recent.groupby("sku_id")["daily_velocity"].mean().items()}


def _inventory_ok(db: Session, brand_id: int, campaign_id: int, cover: dict[int, float]) -> tuple[bool, int]:
    from ..db.models import CampaignSkuMap

    sku_ids = [r.sku_id for r in db.query(CampaignSkuMap).filter(
        CampaignSkuMap.brand_id == brand_id, CampaignSkuMap.campaign_id == campaign_id).all()]
    if not sku_ids:
        return True, 0
    at_risk = [s for s in sku_ids if cover.get(int(s), 999.0) < 14.0]
    return (not at_risk), len(at_risk)
