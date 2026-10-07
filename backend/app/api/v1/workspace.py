"""Workspace APIs that power the local AdPilot front end.

These routes expose existing persisted models and analysis engines. They are
brand-scoped and intentionally use the SQLite/Postgres ORM already configured
by the application.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import ApiError, write_audit
from ...core.events import bus
from ...db.models import (
    Ad,
    AdMetricsDaily,
    AdSet,
    AgentRun,
    AgentStep,
    Anomaly,
    AuditLog,
    AutonomySetting,
    BusinessObjective,
    Campaign,
    Constraint,
    Creative,
    DataSource,
    Diagnosis,
    Execution,
    Guardrail,
    InventorySnapshot,
    ModelFeedback,
    ModelRegistry,
    Opportunity,
    Outcome,
    Recommendation,
    Role,
    SimulationState,
    Sku,
    UnifiedFact,
)
from ...db.session import get_db
from ...engines import copilot, execution, learning, optimizer, outcomes, recommender, simulation
from ...engines.reconcile import _read
from ..deps import Principal, get_principal, resolve_brand

router = APIRouter(tags=["workspace"])


def _brand(db: Session, principal: Principal, brand_id: int | None) -> int:
    return resolve_brand(db, principal, brand_id)


def _json(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    return value


def _rec(rec: Recommendation) -> dict[str, Any]:
    return {
        "id": rec.id, "title": rec.title, "summary": rec.summary, "kind": rec.kind,
        "entity_type": rec.entity_type, "entity_id": rec.entity_id, "entity_name": rec.entity_name,
        "platform_key": rec.platform_key, "sku_id": rec.sku_id, "anomaly_id": rec.anomaly_id,
        "diagnosis_id": rec.diagnosis_id, "opportunity_id": rec.opportunity_id, "cause": rec.cause,
        "current_value": rec.current_value, "proposed_value": rec.proposed_value, "value_unit": rec.value_unit,
        "expected_profit_delta": rec.expected_profit_delta, "expected_revenue_delta": rec.expected_revenue_delta,
        "expected_roas_delta": rec.expected_roas_delta, "impact_p10": rec.impact_p10,
        "impact_p90": rec.impact_p90, "impact_distribution": rec.impact_distribution or [],
        "confidence": rec.confidence, "risk_level": rec.risk_level, "priority_score": rec.priority_score,
        "priority_formula": rec.priority_formula, "constraints_check": rec.constraints_check or [],
        "constraints_passed": rec.constraints_passed, "time_to_impact_days": rec.time_to_impact_days,
        "rollback_plan": rec.rollback_plan or {}, "execution_payload": rec.execution_payload or {},
        "advisory_only": rec.advisory_only, "status": rec.status, "autonomy_level": rec.autonomy_level,
        "rejection_reason": rec.rejection_reason, "approved_by": rec.approved_by,
        "approved_at": _json(rec.approved_at), "action": rec.action, "expected_spend_delta": rec.expected_spend_delta,
        "urgency": rec.urgency, "reversible": rec.reversible, "evidence": rec.evidence or {},
        "requires_approval": rec.requires_approval, "autonomy_reason": rec.autonomy_reason,
        "narrative": rec.narrative, "execution_id": rec.execution_id,
        "executed_at": _json(rec.executed_at), "created_at": _json(rec.created_at),
    }


@router.get("/analytics/performance")
def performance(
    brand_id: int | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    platform_key: str | None = Query(None),
    campaign_id: int | None = Query(None),
    sku_id: int | None = Query(None),
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db),
) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    stmt = select(
        UnifiedFact.date, UnifiedFact.platform_key, UnifiedFact.campaign_id, UnifiedFact.sku_id,
        UnifiedFact.spend, UnifiedFact.impressions, UnifiedFact.clicks, UnifiedFact.attributed_revenue,
        UnifiedFact.attributed_orders, UnifiedFact.contribution_margin, UnifiedFact.inventory_days_of_cover,
    ).where(UnifiedFact.brand_id == brand)
    if platform_key:
        stmt = stmt.where(UnifiedFact.platform_key == platform_key)
    if campaign_id:
        stmt = stmt.where(UnifiedFact.campaign_id == campaign_id)
    if sku_id:
        stmt = stmt.where(UnifiedFact.sku_id == sku_id)
    frame = _read(db, stmt)
    if frame.empty:
        return {"window": {}, "summary": {}, "series": [], "platforms": [], "campaigns": [], "sku_mix": [], "rows": []}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    end = max(frame.date)
    frame = frame[frame.date > end - dt.timedelta(days=days)]
    frame["profit"] = frame["contribution_margin"].fillna(0) - frame["spend"].fillna(0)
    daily = frame.groupby("date", as_index=False).agg(spend=("spend", "sum"), revenue=("attributed_revenue", "sum"),
        profit=("profit", "sum"), orders=("attributed_orders", "sum"))
    daily["roas"] = daily.revenue / daily.spend.replace(0, pd.NA)
    platforms = frame.groupby("platform_key", as_index=False).agg(spend=("spend", "sum"),
        revenue=("attributed_revenue", "sum"), profit=("profit", "sum"), orders=("attributed_orders", "sum"))
    platforms["roas"] = platforms.revenue / platforms.spend.replace(0, pd.NA)
    campaign_rows = db.query(Campaign).filter(Campaign.brand_id == brand).all()
    cname = {c.id: c.name for c in campaign_rows}
    by_campaign = frame.groupby("campaign_id", as_index=False).agg(spend=("spend", "sum"),
        revenue=("attributed_revenue", "sum"), profit=("profit", "sum"), orders=("attributed_orders", "sum"))
    by_campaign["roas"] = by_campaign.revenue / by_campaign.spend.replace(0, pd.NA)
    by_campaign["campaign_name"] = by_campaign.campaign_id.map(cname)
    skuname = {s.id: s.sku_code for s in db.query(Sku).filter(Sku.brand_id == brand).all()}
    by_sku = frame.groupby("sku_id", as_index=False).agg(spend=("spend", "sum"),
        revenue=("attributed_revenue", "sum"), profit=("profit", "sum"), orders=("attributed_orders", "sum"))
    by_sku["sku_code"] = by_sku.sku_id.map(skuname)
    def records(df, cols):
        return [{k: _json(row[k]) for k in cols if k in row} for row in df.fillna(0).to_dict("records")]
    spend, revenue, profit = float(frame.spend.sum()), float(frame.attributed_revenue.sum()), float(frame.profit.sum())
    return {
        "window": {"start": str(end - dt.timedelta(days=days)), "end": str(end), "days": days},
        "summary": {"spend": spend, "revenue": revenue, "profit": profit,
                    "roas": revenue / spend if spend else 0,
                    "orders": int(frame.attributed_orders.sum()),
                    "cpa": spend / frame.attributed_orders.sum() if frame.attributed_orders.sum() else 0},
        "series": records(daily, ["date", "spend", "revenue", "profit", "roas", "orders"]),
        "platforms": records(platforms, ["platform_key", "spend", "revenue", "profit", "orders", "roas"]),
        "campaigns": records(by_campaign.sort_values("spend", ascending=False).head(100),
                             ["campaign_id", "campaign_name", "spend", "revenue", "profit", "orders", "roas"]),
        "sku_mix": records(by_sku.sort_values("profit", ascending=False).head(100),
                            ["sku_id", "sku_code", "spend", "revenue", "profit", "orders"]),
        "rows": records(frame.groupby(["date", "platform_key", "campaign_id", "sku_id"], as_index=False).agg(
            spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), profit=("profit", "sum"),
            orders=("attributed_orders", "sum")).sort_values("date", ascending=False).head(500),
            ["date", "platform_key", "campaign_id", "sku_id", "spend", "revenue", "profit", "orders"]),
    }


@router.get("/anomalies")
def anomalies(brand_id: int | None = Query(None), status: str | None = Query(None),
              limit: int = Query(100, ge=1, le=500), principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    q = db.query(Anomaly).filter(Anomaly.brand_id == brand)
    if status:
        q = q.filter(Anomaly.status == status)
    rows = q.order_by(Anomaly.severity.desc(), Anomaly.detected_at.desc()).limit(limit).all()
    result = []
    for a in rows:
        d = db.query(Diagnosis).filter(Diagnosis.anomaly_id == a.id).first()
        rec = db.query(Recommendation).filter(Recommendation.anomaly_id == a.id).order_by(Recommendation.priority_score.desc()).first()
        result.append({"id": a.id, "metric": a.metric, "entity_type": a.entity_type, "entity_id": a.entity_id,
            "entity_name": a.entity_name, "platform_key": a.platform_key, "granularity": a.granularity,
            "detected_at": _json(a.detected_at), "window_start": _json(a.window_start), "window_end": _json(a.window_end),
            "severity": a.severity, "severity_label": a.severity_label, "direction": a.direction,
            "magnitude_pct": a.magnitude_pct, "baseline": a.baseline, "observed": a.observed,
            "expected": a.expected, "lower_bound": a.lower_bound, "upper_bound": a.upper_bound,
            "p_value": a.p_value, "method_scores": a.method_scores or {}, "methods_agreeing": a.methods_agreeing,
            "is_data_artifact": a.is_data_artifact, "artifact_reason": a.artifact_reason,
            "status": a.status, "scenario_tag": a.scenario_tag,
            "diagnosis": {"primary_driver": d.primary_driver, "confidence": d.confidence,
                          "drivers": d.drivers or [], "narrative": d.narrative} if d else None,
            "recommendation_id": rec.id if rec else None})
    return {"items": result, "total": q.count()}


@router.get("/diagnoses/{anomaly_id}")
def diagnosis_detail(anomaly_id: int, brand_id: int | None = Query(None),
                     principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    a = db.query(Anomaly).filter(Anomaly.id == anomaly_id, Anomaly.brand_id == brand).first()
    if not a:
        raise ApiError(404, "not_found", "anomaly not found")
    d = db.query(Diagnosis).filter(Diagnosis.anomaly_id == a.id, Diagnosis.brand_id == brand).first()
    steps = []
    if d and d.agent_run_id:
        steps = db.query(AgentStep).filter(AgentStep.run_id == d.agent_run_id).order_by(AgentStep.idx).all()
    recs = db.query(Recommendation).filter(Recommendation.anomaly_id == a.id).order_by(Recommendation.priority_score.desc()).all()
    return {"anomaly": {"id": a.id, "metric": a.metric, "entity_type": a.entity_type, "entity_id": a.entity_id,
        "entity_name": a.entity_name, "platform_key": a.platform_key, "severity": a.severity,
        "severity_label": a.severity_label, "direction": a.direction, "magnitude_pct": a.magnitude_pct,
        "baseline": a.baseline, "observed": a.observed, "expected": a.expected,
        "window_start": _json(a.window_start), "window_end": _json(a.window_end),
        "is_data_artifact": a.is_data_artifact, "artifact_reason": a.artifact_reason,
        "method_scores": a.method_scores or {}, "scenario_tag": a.scenario_tag},
        "diagnosis": {"id": d.id, "primary_driver": d.primary_driver, "confidence": d.confidence,
            "drivers": d.drivers or [], "decomposition": d.decomposition or {}, "causal": d.causal or {},
            "evidence": d.evidence or {}, "narrative": d.narrative, "narrative_source": d.narrative_source,
            "artifact_flags": d.artifact_flags or [], "recommended_actions": d.recommended_actions or []} if d else None,
        "agent_steps": [{"id": s.id, "idx": s.idx, "phase": s.phase, "tool": s.tool, "thought": s.thought,
            "arguments": s.arguments or {}, "result_summary": s.result_summary, "result": s.result or {},
            "duration_ms": s.duration_ms} for s in steps],
        "recommendations": [_rec(r) for r in recs]}


@router.get("/opportunities")
def opportunities(brand_id: int | None = Query(None), kind: str | None = Query(None),
                  principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    q = db.query(Opportunity).filter(Opportunity.brand_id == brand)
    if kind:
        q = q.filter(Opportunity.kind == kind)
    rows = q.order_by(Opportunity.score.desc()).limit(300).all()
    return {"items": [{"id": o.id, "entity_type": o.entity_type, "entity_id": o.entity_id,
        "entity_name": o.entity_name, "platform_key": o.platform_key, "sku_id": o.sku_id, "kind": o.kind,
        "profit_lower": o.profit_lower, "profit_upper": o.profit_upper, "expected_profit_delta": o.expected_profit_delta,
        "expected_revenue_delta": o.expected_revenue_delta, "win_probability": o.win_probability,
        "availability_factor": o.availability_factor, "confidence": o.confidence, "score": o.score,
        "headroom_pct": o.headroom_pct, "marginal_roas": o.marginal_roas, "current_roas": o.current_roas,
        "contribution_margin_pct": o.contribution_margin_pct, "days_of_cover": o.days_of_cover,
        "shap_factors": o.shap_factors or [], "response_curve": o.response_curve or {},
        "status": o.status, "title": o.title, "description": o.description, "evidence": o.evidence or []} for o in rows],
        "total": q.count()}


@router.get("/recommendations")
def recommendations(brand_id: int | None = Query(None), status: str | None = Query(None),
                    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    q = db.query(Recommendation).filter(Recommendation.brand_id == brand)
    if status:
        q = q.filter(Recommendation.status == status)
    rows = q.order_by(Recommendation.priority_score.desc()).limit(500).all()
    return {"items": [_rec(r) for r in rows], "total": q.count()}


class RejectBody(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class EditBody(BaseModel):
    proposed_value: Any | None = None
    notes: str = ""


def _get_rec(db: Session, brand: int, rid: int) -> Recommendation:
    rec = db.query(Recommendation).filter(Recommendation.id == rid, Recommendation.brand_id == brand).first()
    if not rec:
        raise ApiError(404, "not_found", "recommendation not found")
    return rec


@router.post("/recommendations/{rid}/approve")
def approve(rid: int, brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    rec = _get_rec(db, brand, rid)
    if rec.status != "proposed":
        raise ApiError(409, "invalid_state", f"cannot approve a recommendation in '{rec.status}' state")
    failed = [c for c in (rec.constraints_check or []) if not c.get("passed")]
    if failed:
        raise ApiError(409, "constraints_failed", "one or more guardrails block approval", failed)
    rec.status, rec.approved_by, rec.approved_at = "approved", principal.email, dt.datetime.utcnow()
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role, action="approve_recommendation",
                entity_type=rec.entity_type, entity_id=rec.entity_id, before={"status": "proposed"},
                after={"status": "approved", "recommendation_id": rid})
    db.commit()
    bus.publish("recommendation", {"recommendation_id": rid, "status": rec.status})
    return _rec(rec)


@router.post("/recommendations/{rid}/reject")
def reject(rid: int, payload: RejectBody, brand_id: int | None = Query(None),
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    rec = _get_rec(db, brand, rid)
    if rec.status not in {"proposed", "approved"}:
        raise ApiError(409, "invalid_state", f"cannot reject a recommendation in '{rec.status}' state")
    rec.status, rec.rejection_reason = "rejected", payload.reason
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role, action="reject_recommendation",
                entity_type=rec.entity_type, entity_id=rec.entity_id,
                before={"status": "proposed", "title": rec.title}, after={"status": "rejected", "reason": payload.reason})
    db.add(ModelFeedback(brand_id=brand, model_name="recommendation_policy", source="rejection",
                         ref_type="recommendation", ref_id=rid, adjustment={"kind": rec.kind},
                         note=payload.reason[:255]))
    db.commit()
    bus.publish("recommendation", {"recommendation_id": rid, "status": rec.status})
    return _rec(rec)


@router.patch("/recommendations/{rid}")
def edit_recommendation(rid: int, payload: EditBody, brand_id: int | None = Query(None),
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    rec = _get_rec(db, brand, rid)
    if rec.status != "proposed":
        raise ApiError(409, "invalid_state", "only proposed recommendations can be edited")
    before = {"proposed_value": rec.proposed_value}
    if payload.proposed_value is not None:
        rec.proposed_value = payload.proposed_value
        rec.edited_before_approval = True
    rec.evidence = {**(rec.evidence or {}), "operator_notes": payload.notes}
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role,
                action="edit_recommendation", entity_type=rec.entity_type, entity_id=rec.entity_id,
                before=before, after={"proposed_value": rec.proposed_value, "notes": payload.notes})
    db.commit()
    return _rec(rec)


@router.post("/recommendations/{rid}/execute")
def execute(rid: int, brand_id: int | None = Query(None), mode: str = Query("simulate"),
            principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    if mode not in {"simulate", "live"}:
        raise ApiError(400, "invalid_mode", "mode must be simulate or live")
    brand = _brand(db, principal, brand_id)
    rec = _get_rec(db, brand, rid)
    if rec.status != "approved":
        raise ApiError(409, "approval_required", "approve this recommendation before executing it")
    state = simulation.get_state(db, brand)
    if state.kill_switch:
        raise ApiError(423, "kill_switch_active", "the global kill switch is active")
    if mode == "live":
        raise ApiError(409, "live_connector_not_enabled", "live execution is not enabled in this local simulator")
    result = execution.execute_recommendation(db, brand, rid, actor=principal.email, mode="simulate")
    db.commit()
    return result


@router.post("/executions/{execution_id}/rollback")
def rollback(execution_id: int, brand_id: int | None = Query(None),
             principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    result = execution.rollback_execution(db, brand, execution_id, actor=principal.email)
    if result.get("status") == "error":
        raise ApiError(409, result["error"]["code"], result["error"]["message"])
    db.commit()
    return result


@router.get("/executions")
def executions(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    rows = db.query(Execution).filter(Execution.brand_id == brand).order_by(Execution.id.desc()).limit(500).all()
    return {"items": [{"id": e.id, "recommendation_id": e.recommendation_id, "platform_key": e.platform_key,
        "connector": e.connector, "mode": e.mode, "idempotency_key": e.idempotency_key, "status": e.status,
        "attempts": e.attempts, "dry_run": e.dry_run, "validation": e.validation or {}, "error": e.error,
        "executed_by": e.executed_by, "executed_at": _json(e.executed_at), "rolled_back_at": _json(e.rolled_back_at),
        "payload": e.payload or [], "kind": e.kind, "entity_type": e.entity_type, "entity_id": e.entity_id,
        "entity_name": e.entity_name, "actor": e.actor, "pre_state": e.pre_state or {},
        "push_response": e.push_response or {}, "rollback_plan": e.rollback_plan,
        "measurement_window_start": _json(e.measurement_window_start),
        "measurement_window_end": _json(e.measurement_window_end)} for e in rows]}


@router.get("/audit")
def audit(brand_id: int | None = Query(None), limit: int = Query(200, ge=1, le=1000),
          principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    rows = db.query(AuditLog).filter(AuditLog.brand_id == brand).order_by(AuditLog.id.desc()).limit(limit).all()
    return {"items": [{"id": a.id, "actor": a.actor, "actor_role": a.actor_role, "action": a.action,
        "entity_type": a.entity_type, "entity_id": a.entity_id, "before": a.before or {}, "after": a.after or {},
        "context": a.context or {}, "immutable_hash": a.immutable_hash,
        "created_at": _json(a.created_at)} for a in rows]}


@router.post("/optimizer/run")
def run_optimizer(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("analyst")
    brand = _brand(db, principal, brand_id)
    result = optimizer.run_optimizer(db, brand)
    db.commit()
    return _json(result)


@router.post("/optimizer/apply")
def apply_optimizer(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    optimized = optimizer.run_optimizer(db, brand)
    recs = recommender.generate_recommendations(db, brand)
    db.commit()
    return {"optimizer": _json(optimized), "recommendations": _json(recs)}


@router.post("/whatif")
def whatif(payload: dict[str, Any], brand_id: int | None = Query(None),
           principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    return _json(optimizer.evaluate_whatif(db, brand, payload.get("allocations") or {}))


@router.get("/inventory")
def inventory(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    clock = db.query(func.max(InventorySnapshot.date)).filter(InventorySnapshot.brand_id == brand).scalar()
    if not clock:
        return {"items": [], "date": None}
    snaps = db.query(InventorySnapshot).filter(InventorySnapshot.brand_id == brand,
        InventorySnapshot.date == clock).all()
    sku_by_id = {s.id: s for s in db.query(Sku).filter(Sku.brand_id == brand).all()}
    spend_frame = _read(db, select(UnifiedFact.sku_id, UnifiedFact.spend).where(
        UnifiedFact.brand_id == brand, UnifiedFact.date > clock - dt.timedelta(days=14)))
    spend = spend_frame.groupby("sku_id")["spend"].sum().to_dict() if not spend_frame.empty else {}
    return {"date": str(clock), "items": [{"sku_id": r.sku_id,
        "sku_code": sku_by_id[r.sku_id].sku_code if r.sku_id in sku_by_id else str(r.sku_id),
        "name": sku_by_id[r.sku_id].name if r.sku_id in sku_by_id else "Unknown SKU",
        "price": sku_by_id[r.sku_id].price if r.sku_id in sku_by_id else 0,
        "cost": sku_by_id[r.sku_id].cost if r.sku_id in sku_by_id else 0,
        "margin_pct": sku_by_id[r.sku_id].margin_pct if r.sku_id in sku_by_id else 0,
        "on_hand": r.on_hand, "inbound": r.inbound, "days_of_cover": r.days_of_cover,
        "daily_velocity": r.daily_velocity, "reorder_point": r.reorder_point,
        "stockout": r.stockout_flag, "promoted_spend_14d": float(spend.get(r.sku_id, 0))} for r in snaps]}


@router.get("/creatives")
def creatives(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    rows = db.query(Creative).filter(Creative.brand_id == brand).order_by(Creative.fatigue_score.desc()).all()
    ads = db.query(Ad).filter(Ad.brand_id == brand).all()
    aset_by_id = {a.id: a for a in db.query(AdSet).filter(AdSet.brand_id == brand).all()}
    campaign_by_id = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand).all()}
    ads_by_creative: dict[int, list[Ad]] = {}
    for ad in ads:
        ads_by_creative.setdefault(ad.creative_id, []).append(ad)
    return {"items": [{"id": c.id, "external_id": c.external_id, "name": c.name,
        "format": c.format, "hook_type": c.hook_type, "theme": c.theme, "angle": c.angle,
        "asset_url": c.asset_url, "thumbnail_url": c.thumbnail_url, "launched_at": _json(c.launched_at),
        "retired_at": _json(c.retired_at), "fatigue_score": c.fatigue_score,
        "attributes": c.attributes or {}, "campaigns": [campaign_by_id[a.campaign_id].name for a in ads_by_creative.get(c.id, []) if a.campaign_id in campaign_by_id],
        "status": "retired" if c.retired_at else "active"} for c in rows]}


@router.get("/learning")
def learning_summary(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    outcomes_rows = db.query(Outcome).filter(Outcome.brand_id == brand).order_by(Outcome.measured_at).all()
    models = db.query(ModelRegistry).order_by(ModelRegistry.trained_at.desc()).all()
    feedback = db.query(ModelFeedback).filter(ModelFeedback.brand_id == brand).order_by(ModelFeedback.id.desc()).limit(100).all()
    wins = sum(1 for o in outcomes_rows if o.verdict in {"as_predicted", "better_than_predicted", "win"})
    return {"summary": {"measured_outcomes": len(outcomes_rows), "win_rate": wins / len(outcomes_rows) if outcomes_rows else None,
        "actual_profit_uplift": sum(o.actual_profit_delta for o in outcomes_rows),
        "predicted_profit_uplift": sum(o.predicted_profit_delta for o in outcomes_rows),
        "mean_absolute_pct_error": sum(abs(o.pct_error) for o in outcomes_rows) / len(outcomes_rows) if outcomes_rows else None},
        "outcomes": [{"id": o.id, "execution_id": o.execution_id, "recommendation_id": o.recommendation_id,
            "window_start": _json(o.window_start), "window_end": _json(o.window_end),
            "predicted_profit_delta": o.predicted_profit_delta, "actual_profit_delta": o.actual_profit_delta,
            "incremental_profit": o.incremental_profit, "pct_error": o.pct_error,
            "confidence": o.confidence, "verdict": o.verdict, "narrative": o.narrative} for o in outcomes_rows],
        "models": [{"id": m.id, "name": m.name, "version": m.version, "kind": m.kind,
            "metrics": m.metrics or {}, "trained_at": _json(m.trained_at), "is_active": m.is_active,
            "notes": m.notes} for m in models],
        "feedback": [{"id": f.id, "model_name": f.model_name, "source": f.source, "ref_type": f.ref_type,
            "ref_id": f.ref_id, "adjustment": f.adjustment or {}, "note": f.note,
            "created_at": _json(f.created_at)} for f in feedback]}


@router.get("/simulation")
def simulation_state(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    state = simulation.get_state(db, brand)
    db.commit()
    return {"clock_date": str(state.clock_date), "real_start_date": str(state.real_start_date),
        "days_advanced": state.days_advanced, "kill_switch": state.kill_switch,
        "last_tick_at": _json(state.last_tick_at)}


class AdvanceBody(BaseModel):
    days: int = Field(default=7, ge=1, le=30)


@router.post("/simulation/advance")
def advance_simulation(payload: AdvanceBody, brand_id: int | None = Query(None),
                       principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    result = simulation.advance_clock(db, brand, days=payload.days)
    if result.get("status") != "ok":
        raise ApiError(400, result.get("error", {}).get("code", "advance_failed"),
                       result.get("error", {}).get("message", "simulation advance failed"))
    db.commit()
    return _json(result)


@router.post("/copilot/chat")
def copilot_chat(payload: dict[str, Any], brand_id: int | None = Query(None),
                 principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    question = str(payload.get("question") or "").strip()
    if not question or len(question) > 2000:
        raise ApiError(422, "invalid_question", "question must contain 1–2000 characters")
    answer = copilot.ask(db, brand, question, context=payload.get("context") or {})
    db.commit()
    return _json(answer)


@router.get("/settings")
def settings_get(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = _brand(db, principal, brand_id)
    objective = db.query(BusinessObjective).filter(BusinessObjective.brand_id == brand,
        BusinessObjective.is_active.is_(True)).first()
    autonomy = db.query(AutonomySetting).filter(AutonomySetting.brand_id == brand).all()
    guardrails = db.query(Guardrail).filter(Guardrail.brand_id == brand).all()
    constraints = db.query(Constraint).filter(Constraint.brand_id == brand).all()
    state = simulation.get_state(db, brand)
    sources = db.query(DataSource).filter(DataSource.brand_id == brand).all()
    roles = [{"name": r.name, "description": r.description, "permissions": r.permissions or {}}
             for r in db.query(Role).all()]
    return {"objectives": ({"id": objective.id, "name": objective.name,
        "total_monthly_budget": objective.total_monthly_budget, "total_daily_budget": objective.total_daily_budget,
        "target_blended_roas": objective.target_blended_roas, "target_mer": objective.target_mer,
        "min_contribution_margin_pct": objective.min_contribution_margin_pct, "max_cpa": objective.max_cpa,
        "max_daily_budget_shift_pct": objective.max_daily_budget_shift_pct,
        "min_days_of_inventory_cover": objective.min_days_of_inventory_cover,
        "objective_weights": objective.objective_weights or {}, "per_platform_min": objective.per_platform_min or {},
        "per_platform_max": objective.per_platform_max or {}, "protect_learning_phase": objective.protect_learning_phase,
        "horizon_days": objective.horizon_days} if objective else None),
        "autonomy": [{"id": a.id, "scope": a.scope, "scope_ref": a.scope_ref, "level": a.level,
            "auto_execute_max_daily_spend_delta": a.auto_execute_max_daily_spend_delta,
            "auto_execute_min_confidence": a.auto_execute_min_confidence, "auto_execute_max_risk": a.auto_execute_max_risk} for a in autonomy],
        "guardrails": [{"id": g.id, "key": g.key, "label": g.label, "enabled": g.enabled,
            "value": g.value, "unit": g.unit, "config": g.config or {}} for g in guardrails],
        "constraints": [{"id": c.id, "key": c.key, "scope": c.scope, "scope_ref": c.scope_ref,
            "operator": c.operator, "value": c.value, "hard": c.hard, "label": c.label, "is_active": c.is_active} for c in constraints],
        "connectors": [{"key": s.key, "name": s.name, "status": s.status, "mode": s.mode,
            "quality_score": s.quality_score, "last_sync_at": _json(s.last_sync_at)} for s in sources],
        "roles": roles, "kill_switch": state.kill_switch}


class ObjectivePatch(BaseModel):
    target_blended_roas: float | None = Field(default=None, ge=0)
    target_mer: float | None = Field(default=None, ge=0)
    min_contribution_margin_pct: float | None = Field(default=None, ge=0, le=100)
    max_cpa: float | None = Field(default=None, ge=0)
    max_daily_budget_shift_pct: float | None = Field(default=None, ge=0, le=100)
    min_days_of_inventory_cover: float | None = Field(default=None, ge=0)
    total_daily_budget: float | None = Field(default=None, ge=0)


@router.patch("/settings/objectives")
def settings_objectives(payload: ObjectivePatch, brand_id: int | None = Query(None),
                        principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("brand_manager")
    brand = _brand(db, principal, brand_id)
    obj = db.query(BusinessObjective).filter(BusinessObjective.brand_id == brand,
        BusinessObjective.is_active.is_(True)).first()
    if not obj:
        raise ApiError(404, "objectives_missing", "no active business objective configured")
    before = {k: getattr(obj, k) for k in payload.model_fields_set}
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(obj, key, value)
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role,
        action="update_business_objectives", entity_type="business_objective", entity_id=obj.id,
        before=before, after=payload.model_dump(exclude_unset=True))
    db.commit()
    return {"status": "ok", "objectives": _json({k: getattr(obj, k) for k in payload.model_fields_set})}


class KillSwitchBody(BaseModel):
    enabled: bool


@router.put("/settings/kill-switch")
def kill_switch(payload: KillSwitchBody, brand_id: int | None = Query(None),
                principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict[str, Any]:
    principal.require("admin")
    brand = _brand(db, principal, brand_id)
    state = simulation.get_state(db, brand)
    before = state.kill_switch
    state.kill_switch = payload.enabled
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role,
        action="enable_kill_switch" if payload.enabled else "disable_kill_switch",
        entity_type="simulation_state", entity_id=state.id, before={"kill_switch": before}, after={"kill_switch": payload.enabled})
    db.commit()
    bus.publish("guardrail", {"kill_switch": payload.enabled, "brand_id": brand})
    return {"kill_switch": state.kill_switch}


@router.get("/events")
async def events():
    """Server-Sent Events stream for pipeline, sync, notification and execution events."""
    import asyncio
    from fastapi.responses import StreamingResponse
    from ...core.events import bus as event_bus

    async def stream():
        queue = event_bus.subscribe()
        try:
            for event in event_bus.history(10):
                yield event_bus.format_sse(event)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                    yield event_bus.format_sse(event)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            event_bus.unsubscribe(queue)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/methodology")
def methodology():
    return {"sections": [
        {"title": "Reconciliation", "summary": "Cross-source spend and revenue are normalized to a daily campaign grain. Attribution overlap is discounted, then store orders, GA events and platform conversions are reconciled into unified facts.", "caveat": "Attribution is an estimate; channel totals can differ from finance systems."},
        {"title": "Anomaly detection", "summary": "Seasonal residuals, robust median absolute deviation, change-point detection and forecast-bound breaches form an explainable ensemble with an effect-size floor.", "caveat": "A statistical flag is a prompt to investigate, not proof of causality."},
        {"title": "Diagnosis & causal estimates", "summary": "Metric and mix decomposition quantify candidate drivers; matched controls and difference-in-differences estimate counterfactual impact when suitable comparisons exist.", "caveat": "Causal estimates depend on parallel-trend and no-unobserved-confounder assumptions."},
        {"title": "Budget optimization", "summary": "Campaign response curves estimate marginal returns. Allocation is bounded by budget, per-campaign movement limits, learning-phase protection and stock cover.", "caveat": "Demo response curves are fitted on seeded/simulated history, not a guarantee of future results."},
        {"title": "Learning", "summary": "Measured execution outcomes and rejection feedback update model calibration and policy records.", "caveat": "A simulated execution is not a change to a live advertising account."},
    ]}
