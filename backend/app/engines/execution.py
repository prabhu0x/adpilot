"""Pillar 6 — execution with full auditability.

Execution is deliberately boring and reversible:

1. validate the recommendation is still actionable and inside policy,
2. capture a **pre-execution snapshot** of every field that will change,
3. build the exact platform payload and store the dry-run preview,
4. push through the connector (or mark as a manual task when the platform has
   no write API),
5. record an immutable audit entry with actor, timestamp, payload and rollback
   plan, and schedule the outcome measurement window.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from ..core.errors import write_audit
from ..core.events import bus
from ..core.logging import get_logger
from ..db.models import (
    AuditLog,
    Campaign,
    Creative,
    DataSource,
    Execution,
    Recommendation,
    Sku,
)
from .ingestion import get_connector

log = get_logger("adpilot.execution")

MEASUREMENT_WINDOW_DAYS = 14


def _snapshot(db: Session, rec: Recommendation) -> dict[str, Any]:
    """Capture the current values of everything the action will touch."""
    snapshot: dict[str, Any] = {"captured_at": dt.datetime.utcnow().isoformat(), "entity_type": rec.entity_type}
    if rec.entity_type == "campaign" and rec.entity_id:
        campaign = db.query(Campaign).filter(Campaign.id == rec.entity_id).first()
        if campaign:
            snapshot.update({
                "campaign_id": campaign.id, "name": campaign.name, "status": campaign.status,
                "daily_budget": campaign.daily_budget, "bid_strategy": campaign.bid_strategy,
                "target_roas": campaign.target_roas,
            })
    elif rec.entity_type == "creative" and rec.entity_id:
        creative = db.query(Creative).filter(Creative.id == rec.entity_id).first()
        if creative:
            snapshot.update({
                "creative_id": creative.id, "name": creative.name,
                "status": (creative.attributes or {}).get("status", "active"),
                "fatigue_score": creative.fatigue_score,
                "format": creative.format, "launched_at": str(creative.launched_at or ""),
            })
    elif rec.entity_type == "sku" and rec.entity_id:
        sku = db.query(Sku).filter(Sku.id == rec.entity_id).first()
        if sku:
            snapshot.update({"sku_id": sku.id, "sku_code": sku.sku_code, "price": sku.price, "cost": sku.cost,
                             "stock_on_hand": sku.stock_on_hand})
    return snapshot


def _payload(rec: Recommendation, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the exact platform call(s) this action would make."""
    if rec.kind in {"increase_budget", "decrease_budget"} and snapshot.get("campaign_id"):
        return [{
            "operation": "update_campaign_budget",
            "campaign_id": snapshot["campaign_id"],
            "fields": {"daily_budget": rec.proposed_value},
            "previous": {"daily_budget": rec.current_value},
            "note": "daily budget in account currency",
        }]
    if rec.kind == "pause_campaign" and snapshot.get("campaign_id"):
        return [{"operation": "update_campaign_status", "campaign_id": snapshot["campaign_id"],
                 "fields": {"status": "PAUSED"}, "previous": {"status": snapshot.get("status")}}]
    if rec.kind == "pause_creative" and snapshot.get("creative_id"):
        return [{"operation": "update_creative_status", "creative_id": snapshot["creative_id"],
                 "fields": {"status": "PAUSED"}, "previous": {"status": snapshot.get("status")}}]
    if rec.kind == "reallocate_budget" and rec.entity_type == "portfolio":
        allocation = (rec.evidence or {}).get("allocation", [])
        return [{
            "operation": "update_campaign_budget", "campaign_id": a["campaign_id"],
            "fields": {"daily_budget": a["proposed_daily_spend"]},
            "previous": {"daily_budget": a["current_daily_spend"]},
        } for a in allocation]
    if rec.kind == "shift_budget_platform":
        return [{"operation": "reallocate_platform_budget",
                 "fields": {"from": (rec.evidence or {}).get("worst_platform"),
                            "to": (rec.evidence or {}).get("best_platform"),
                            "amount": (rec.evidence or {}).get("shifted_spend")}}]
    if rec.kind == "inventory_reorder" and snapshot.get("sku_id"):
        return [{"operation": "create_purchase_order", "sku_code": snapshot.get("sku_code"),
                 "fields": {"quantity_days": 45}, "system": "erp"}]
    if rec.kind == "expand_audience":
        return [{"operation": "update_audience_definition", "entity_id": rec.entity_id,
                 "fields": {"audience_tier": "broadened"}}]
    if rec.kind == "refresh_creative":
        return [{"operation": "create_creative_brief", "entity_id": rec.entity_id,
                 "fields": {"brief": rec.action}}]
    if rec.kind == "fix_tracking":
        return [{"operation": "create_incident", "system": "tag_manager", "fields": {"summary": rec.action}}]
    return [{"operation": "manual_task", "fields": {"description": rec.action}}]


def _connector_for(db: Session, brand_id: int, platform_key: str | None):
    key = {
        "meta": "meta_ads", "google": "google_ads", "amazon": "amazon_ads",
        "tiktok": "tiktok_ads", "programmatic": "programmatic_dsp",
    }.get(platform_key or "", "store_orders")
    source = db.query(DataSource).filter(DataSource.brand_id == brand_id, DataSource.key == key).first()
    return get_connector(source) if source else None


def execute_recommendation(
    db: Session, brand_id: int, recommendation_id: int, actor: str = "operator",
    mode: str = "simulate", force: bool = False, notes: str = "",
) -> dict[str, Any]:
    rec = db.query(Recommendation).filter(
        Recommendation.id == recommendation_id, Recommendation.brand_id == brand_id
    ).first()
    if not rec:
        return {"status": "error", "error": {"code": "not_found", "message": "recommendation not found"}}
    if rec.status in {"executed", "measured"} and not force:
        return {"status": "error", "error": {"code": "already_executed",
                                             "message": f"recommendation already {rec.status}"}}
    failed = [c for c in (rec.constraints_check or []) if not c.get("passed")]
    if failed and not force:
        return {"status": "error", "error": {
            "code": "constraint_violation",
            "message": "blocked constraints: " + ", ".join(c.get("label", c.get("key", "")) for c in failed),
        }}

    snapshot = _snapshot(db, rec)
    payload = _payload(rec, snapshot)
    connector = _connector_for(db, brand_id, None if rec.entity_type == "portfolio" else None)
    push_result: dict[str, Any] = {}
    if connector:
        try:
            push_result = connector.push_changes(payload, dry_run=(mode != "live"))
        except Exception as exc:  # pragma: no cover
            push_result = {"error": str(exc)}

    applied = False
    if mode == "live":
        applied = _apply_to_simulator(db, rec, snapshot)

    idempotency_key = f"rec-{rec.id}-{rec.dedupe_key or 'x'}-{int(dt.datetime.utcnow().timestamp())}"
    execution = Execution(
        brand_id=brand_id, recommendation_id=rec.id, kind=rec.kind, entity_type=rec.entity_type,
        idempotency_key=idempotency_key, connector=(connector.key if connector else ""),
        platform_key=(connector.platform_key if connector and hasattr(connector, "platform_key") else None),
        dry_run=(mode != "live"), attempts=1, executed_by=actor,
        entity_id=rec.entity_id, entity_name=rec.entity_name, mode=mode, status="executed" if applied or mode != "live" else "failed",
        actor=actor, pre_state=snapshot, payload=payload, push_response=push_result,
        rollback_plan=rec.rollback_plan, notes=notes,
        measurement_window_start=dt.datetime.utcnow() + dt.timedelta(days=1),
        measurement_window_end=dt.datetime.utcnow() + dt.timedelta(days=MEASUREMENT_WINDOW_DAYS),
    )
    db.add(execution)
    db.flush()
    rec.status = "executed"
    rec.executed_at = dt.datetime.utcnow()
    rec.execution_id = execution.id

    write_audit(
        db, brand_id=brand_id, actor=actor, actor_role="operator",
        action=f"execute:{rec.kind}", entity_type=rec.entity_type, entity_id=rec.entity_id,
        before=snapshot, after={"mode": mode, "payload": payload, "execution_id": execution.id},
        context={"reason": rec.title, "risk_level": rec.risk_level, "confidence": rec.confidence},
    )
    db.flush()
    bus.publish("execution", {"execution_id": execution.id, "kind": rec.kind, "mode": mode,
                              "entity": rec.entity_name})
    log.info("executed %s (%s) on %s", rec.kind, mode, rec.entity_name)
    return {"status": "ok", "execution_id": execution.id, "mode": mode, "applied": applied,
            "payload": payload, "pre_state": snapshot, "push_response": push_result,
            "measurement_window": [execution.measurement_window_start.isoformat(),
                                   execution.measurement_window_end.isoformat()]}


def _apply_to_simulator(db: Session, rec: Recommendation, snapshot: dict[str, Any]) -> bool:
    """Apply the change to the simulated world so later data reflects it."""
    if rec.entity_type == "campaign" and rec.entity_id:
        campaign = db.query(Campaign).filter(Campaign.id == rec.entity_id).first()
        if not campaign:
            return False
        if rec.kind in {"increase_budget", "decrease_budget"}:
            campaign.daily_budget = float(rec.proposed_value or campaign.daily_budget)
        elif rec.kind == "pause_campaign":
            campaign.status = "paused"
        return True
    if rec.entity_type == "creative" and rec.entity_id:
        creative = db.query(Creative).filter(Creative.id == rec.entity_id).first()
        if creative and rec.kind == "pause_creative":
            attributes = dict(creative.attributes or {})
            attributes["status"] = "paused"
            attributes["paused_at"] = dt.date.today().isoformat()
            creative.attributes = attributes
            creative.retired_at = dt.date.today()
            return True
    if rec.entity_type == "portfolio" and rec.kind == "reallocate_budget":
        for item in (rec.evidence or {}).get("allocation", []):
            campaign = db.query(Campaign).filter(Campaign.id == item.get("campaign_id")).first()
            if campaign:
                campaign.daily_budget = float(item.get("proposed_daily_spend", campaign.daily_budget))
        return True
    if rec.entity_type == "sku" and rec.entity_id and rec.kind == "inventory_reorder":
        sku = db.query(Sku).filter(Sku.id == rec.entity_id).first()
        if sku:
            sku.stock_on_hand = float(sku.stock_on_hand or 0) + 45 * 40  # 45 days of an assumed 40/day
            return True
    return False


def rollback_execution(db: Session, brand_id: int, execution_id: int, actor: str = "operator") -> dict[str, Any]:
    execution = db.query(Execution).filter(Execution.id == execution_id, Execution.brand_id == brand_id).first()
    if not execution:
        return {"status": "error", "error": {"code": "not_found", "message": "execution not found"}}
    if execution.status == "rolled_back":
        return {"status": "error", "error": {"code": "already_rolled_back", "message": "already rolled back"}}

    rec = db.query(Recommendation).filter(Recommendation.id == execution.recommendation_id).first()
    restored: list[str] = []
    for item in execution.payload or []:
        operation = item.get("operation")
        previous = item.get("previous") or {}
        if operation == "update_campaign_budget" and previous.get("daily_budget") is not None:
            campaign = db.query(Campaign).filter(Campaign.id == item.get("campaign_id")).first()
            if campaign:
                campaign.daily_budget = float(previous["daily_budget"])
                restored.append(f"campaign {campaign.id} budget -> {previous['daily_budget']}")
        elif operation == "update_campaign_status" and previous.get("status"):
            campaign = db.query(Campaign).filter(Campaign.id == item.get("campaign_id")).first()
            if campaign:
                campaign.status = previous["status"]
                restored.append(f"campaign {campaign.id} status -> {previous['status']}")
        elif operation == "update_creative_status" and previous.get("status"):
            creative = db.query(Creative).filter(Creative.id == item.get("creative_id")).first()
            if creative:
                attributes = dict(creative.attributes or {})
                attributes["status"] = previous["status"]
                creative.attributes = attributes
                creative.retired_at = None
                restored.append(f"creative {creative.id} status -> {previous['status']}")

    execution.status = "rolled_back"
    execution.rolled_back_at = dt.datetime.utcnow()
    if rec:
        rec.status = "rolled_back"
    write_audit(
        db, brand_id=brand_id, actor=actor, actor_role="operator", action="rollback",
        entity_type=execution.entity_type, entity_id=execution.entity_id,
        before={"execution_id": execution.id}, after={"restored": restored},
        context={"reason": "manual rollback"},
    )
    db.flush()
    return {"status": "ok", "execution_id": execution.id, "restored": restored}


def run_autonomous_actions(db: Session, brand_id: int, actor: str = "autopilot") -> dict[str, Any]:
    """Execute every recommendation that policy allows without a human."""
    eligible = db.query(Recommendation).filter(
        Recommendation.brand_id == brand_id,
        Recommendation.status == "proposed",
        Recommendation.requires_approval.is_(False),
    ).order_by(Recommendation.priority_score.desc()).all()
    executed = []
    for rec in eligible:
        result = execute_recommendation(db, brand_id, rec.id, actor=actor, mode="live",
                                        notes="autonomous action inside policy envelope")
        executed.append({"recommendation_id": rec.id, "kind": rec.kind, "status": result.get("status"),
                         "execution_id": result.get("execution_id")})
    db.flush()
    return {"status": "ok", "auto_executed": len(executed), "actions": executed}