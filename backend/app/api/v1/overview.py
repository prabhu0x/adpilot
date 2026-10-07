"""Command Center: KPI cards, the what-changed feed and the pipeline strip."""
from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import (
    Anomaly,
    Brand,
    Campaign,
    Diagnosis,
    Execution,
    InventorySnapshot,
    Notification,
    Opportunity,
    Outcome,
    Recommendation,
    ScenarioStatus,
    Sku,
    UnifiedFact,
)
from ...db.session import get_db
from ...engines.reconcile import _read
from ...engines.simulation import get_state
from ..deps import Principal, get_principal, resolve_brand

router = APIRouter(tags=["overview"])

SPARK_DAYS = 30


def _facts(db: Session, brand_id: int) -> pd.DataFrame:
    frame = _read(db, select(
        UnifiedFact.date, UnifiedFact.platform_key, UnifiedFact.campaign_id, UnifiedFact.sku_id,
        UnifiedFact.impressions, UnifiedFact.clicks, UnifiedFact.spend, UnifiedFact.platform_revenue,
        UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders, UnifiedFact.contribution_margin,
        UnifiedFact.inventory_days_of_cover,
    ).where(UnifiedFact.brand_id == brand_id))
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _delta(current: float, prior: float) -> dict[str, float]:
    if not prior:
        return {"value": round(current, 2), "change_pct": 0.0, "direction": "flat"}
    change = (current / prior - 1) * 100
    return {
        "value": round(current, 2),
        "change_pct": round(change, 2),
        "direction": "up" if change > 0.5 else ("down" if change < -0.5 else "flat"),
    }


def _window(frame: pd.DataFrame, clock: dt.date, days: int, offset: int = 0) -> pd.DataFrame:
    end = clock - dt.timedelta(days=offset)
    return frame[(frame["date"] > end - dt.timedelta(days=days)) & (frame["date"] <= end)]


@router.get("/overview")
def overview(
    brand_id: int | None = Query(None),
    days: int = Query(14, ge=1, le=120),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Everything the Command Center needs in one round trip."""
    brand = resolve_brand(db, principal, brand_id)
    state = get_state(db, brand)
    frame = _facts(db, brand)
    if frame.empty:
        return {"brand_id": brand, "clock_date": str(state.clock_date), "cards": [], "feed": [],
                "recommendations": [], "pipeline": [], "uplift": {}}

    clock = max(frame["date"])
    recent = _window(frame, clock, days)
    prior = _window(frame, clock, days, offset=days)
    prior28 = _window(frame, clock, 28, offset=0)
    prior28_prev = _window(frame, clock, 28, offset=28)

    spend, prior_spend = float(recent["spend"].sum()), float(prior["spend"].sum())
    revenue, prior_revenue = float(recent["attributed_revenue"].sum()), float(prior["attributed_revenue"].sum())
    profit = revenue - float(recent["cogs"].sum()) if "cogs" in recent else revenue - float(recent["contribution_margin"].sum())
    # contribution profit = contribution margin - ad spend
    cm = float(recent["contribution_margin"].sum())
    profit = cm - spend
    prior_cm = float(prior["contribution_margin"].sum())
    prior_profit = prior_cm - prior_spend
    orders = float(recent["attributed_orders"].sum())
    prior_orders = float(prior["attributed_orders"].sum())
    roas = revenue / spend if spend else 0.0
    prior_roas = prior_revenue / prior_spend if prior_spend else 0.0
    cpa = spend / orders if orders else 0.0
    prior_cpa = prior_spend / prior_orders if prior_orders else 0.0
    aov = revenue / orders if orders else 0.0
    mer = revenue / spend if spend else 0.0

    at_risk = (
        db.query(func.count(func.distinct(InventorySnapshot.sku_id)))
        .filter(InventorySnapshot.brand_id == brand, InventorySnapshot.days_of_cover < 14)
        .scalar() or 0
    )
    promoted_at_risk = _promoted_sku_risk(db, brand)

    series = _sparkline(frame, clock, SPARK_DAYS)

    cards = [
        {"key": "spend", "label": "Ad spend", "unit": "usd", **_delta(spend, prior_spend),
         "series": series["spend"], "sparkline_total": round(spend, 2)},
        {"key": "revenue", "label": "Attributed revenue", "unit": "usd", **_delta(revenue, prior_revenue),
         "series": series["revenue"], "sparkline_total": round(revenue, 2)},
        {"key": "profit", "label": "Contribution profit", "unit": "usd", **_delta(profit, prior_profit),
         "series": series["profit"], "sparkline_total": round(profit, 2)},
        {"key": "blended_roas", "label": "Blended ROAS", "unit": "x", **_delta(roas, prior_roas),
         "series": series["roas"], "sparkline_total": round(roas, 3)},
        {"key": "mer", "label": "MER (revenue / spend)", "unit": "x", **_delta(mer, prior_roas),
         "series": series["roas"], "sparkline_total": round(mer, 3)},
        {"key": "cpa", "label": "CPA", "unit": "usd", **_delta(cpa, prior_cpa),
         "series": series["cpa"], "sparkline_total": round(cpa, 2),
         "invert": True},
        {"key": "aov", "label": "AOV", "unit": "usd", **_delta(aov, prior_revenue / prior_orders if prior_orders else 0),
         "series": series["aov"], "sparkline_total": round(aov, 2)},
        {"key": "inventory_at_risk", "label": "SKUs below 14d cover", "unit": "count",
         **_delta(promoted_at_risk, at_risk), "series": [], "sparkline_total": promoted_at_risk,
         "invert": True,
         "hint": f"{promoted_at_risk} promoted SKU(s) at risk of stocking out"},
    ]

    feed = _what_changed(db, brand, frame, clock)
    top_recs = _top_recommendations(db, brand)
    pipeline = _pipeline_strip(db, brand, clock)
    uplift = _ai_uplift(db, brand)

    return {
        "brand_id": brand,
        "clock_date": str(clock),
        "period": {"days": days, "start": str(clock - dt.timedelta(days=days)), "end": str(clock)},
        "comparison": {"start": str(clock - dt.timedelta(days=2 * days)), "end": str(clock - dt.timedelta(days=days))},
        "cards": cards,
        "feed": feed,
        "recommendations": top_recs,
        "pipeline": pipeline,
        "uplift": uplift,
        "totals": {
            "spend": round(spend, 2), "revenue": round(revenue, 2), "profit": round(profit, 2),
            "roas": round(roas, 3), "orders": int(orders), "campaigns": db.query(Campaign).filter(
                Campaign.brand_id == brand, Campaign.status == "active").count(),
            "skus": db.query(Sku).filter(Sku.brand_id == brand, Sku.is_active.is_(True)).count(),
        },
    }


def _sparkline(frame: pd.DataFrame, clock: dt.date, days: int) -> dict[str, list[dict[str, Any]]]:
    window = frame[frame["date"] > clock - dt.timedelta(days=days)]
    daily = window.groupby("date", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"),
        margin=("contribution_margin", "sum"), orders=("attributed_orders", "sum"),
    )
    daily["profit"] = daily["margin"] - daily["spend"]
    daily["roas"] = daily["revenue"] / daily["spend"].replace(0, pd.NA)
    daily["cpa"] = daily["spend"] / daily["orders"].replace(0, pd.NA)
    daily["aov"] = daily["revenue"] / daily["orders"].replace(0, pd.NA)
    daily = daily.fillna(0)
    return {
        key: [{"date": str(r.date), "value": round(float(getattr(r, key)), 3)} for r in daily.itertuples()]
        for key in ("spend", "revenue", "profit", "roas", "cpa", "aov")
    }


def _promoted_sku_risk(db: Session, brand_id: int) -> int:
    """SKUs with low cover *and* meaningful promoted spend in the last 14 days."""
    from ...db.models import CampaignSkuMap

    latest = db.query(func.max(InventorySnapshot.date)).filter(InventorySnapshot.brand_id == brand_id).scalar()
    if not latest:
        return 0
    risky = {
        int(sid) for (sid,) in db.query(InventorySnapshot.sku_id).filter(
            InventorySnapshot.brand_id == brand_id, InventorySnapshot.date == latest,
            InventorySnapshot.days_of_cover < 14,
        ).all()
    }
    if not risky:
        return 0
    mapped = _read(db, select(UnifiedFact.sku_id, UnifiedFact.spend).where(
        UnifiedFact.brand_id == brand_id, UnifiedFact.sku_id.in_(list(risky))))
    if mapped.empty:
        return 0
    spend = mapped.groupby("sku_id")["spend"].sum()
    return int((spend > 250).sum())


def _what_changed(db: Session, brand_id: int, frame: pd.DataFrame, clock: dt.date) -> list[dict[str, Any]]:
    anomalies = (
        db.query(Anomaly)
        .filter(Anomaly.brand_id == brand_id, Anomaly.severity >= 55)
        .order_by(Anomaly.severity.desc())
        .limit(10)
        .all()
    )
    out = []
    for anomaly in anomalies:
        diag = db.query(Diagnosis).filter(Diagnosis.anomaly_id == anomaly.id).first()
        rec = None
        if diag:
            rec = db.query(Recommendation).filter(
                Recommendation.brand_id == brand_id, Recommendation.diagnosis_id == diag.id
            ).order_by(Recommendation.priority_score.desc()).first()
        out.append({
            "anomaly_id": anomaly.id,
            "diagnosis_id": diag.id if diag else None,
            "recommendation_id": rec.id if rec else None,
            "metric": anomaly.metric,
            "entity_type": anomaly.entity_type,
            "entity_name": anomaly.entity_name,
            "severity": round(anomaly.severity, 1),
            "severity_label": anomaly.severity_label,
            "direction": anomaly.direction,
            "magnitude_pct": round(anomaly.magnitude_pct, 2),
            "detected_at": anomaly.detected_at.isoformat() if anomaly.detected_at else None,
            "window_start": str(anomaly.window_start),
            "window_end": str(anomaly.window_end),
            "is_data_artifact": anomaly.is_data_artifact,
            "artifact_reason": anomaly.artifact_reason,
            "primary_driver": diag.primary_driver if diag else None,
            "driver_label": _driver_label(diag.primary_driver) if diag else None,
            "confidence": diag.confidence if diag else None,
            "one_liner": (diag.narrative.split(". ")[0] + ".") if diag and diag.narrative else
                         f"{anomaly.entity_name}: {anomaly.metric} {anomaly.direction} {abs(anomaly.magnitude_pct):.0f}%",
            "scenario_tag": anomaly.scenario_tag,
        })
    return out


DRIVER_LABELS = {
    "creative_fatigue": "Creative fatigue",
    "inventory_stockout": "Inventory stockout",
    "stockout": "Inventory stockout",
    "price_change": "Price / discount change",
    "cross_campaign_cannibalisation": "Campaign cannibalisation",
    "attribution_overlap": "Attribution overlap",
    "data_artifact": "Measurement artifact",
    "tracking_outage": "Tracking outage",
    "audience_saturation": "Audience saturation",
    "budget_change": "Budget change",
    "seasonality": "Seasonality",
    "landing_page_conversion": "Landing-page conversion",
    "on_site_conversion": "On-site conversion",
    "auction_pressure": "Auction pressure",
    "creative_mix": "Creative mix",
    "unknown": "Unclear",
}


def _driver_label(key: str | None) -> str:
    return DRIVER_LABELS.get(key or "", (key or "Unclear").replace("_", " ").title())


def _top_recommendations(db: Session, brand_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(Recommendation)
        .filter(Recommendation.brand_id == brand_id, Recommendation.status == "proposed")
        .order_by(Recommendation.priority_score.desc())
        .limit(5)
        .all()
    )
    return [_rec_card(r) for r in rows]


def _rec_card(rec: Recommendation) -> dict[str, Any]:
    checks = rec.constraints_check or []
    return {
        "id": rec.id, "kind": rec.kind, "title": rec.title, "action": rec.action,
        "cause": rec.cause, "entity_type": rec.entity_type, "entity_id": rec.entity_id,
        "entity_name": rec.entity_name, "current_value": rec.current_value,
        "proposed_value": rec.proposed_value, "value_unit": rec.value_unit,
        "expected_profit_delta": round(rec.expected_profit_delta, 2),
        "impact_p10": round(rec.impact_p10, 2), "impact_p90": round(rec.impact_p90, 2),
        "confidence": rec.confidence, "risk_level": rec.risk_level,
        "priority_score": rec.priority_score, "urgency": rec.urgency,
        "status": rec.status, "requires_approval": rec.requires_approval,
        "autonomy_reason": rec.autonomy_reason,
        "constraints_passed": all(c.get("passed") for c in checks) if checks else True,
        "constraints_check": checks,
        "reversible": rec.reversible, "rollback_plan": rec.rollback_plan,
        "narrative": rec.narrative,
    }


def _pipeline_strip(db: Session, brand_id: int, clock: dt.date) -> list[dict[str, Any]]:
    from ...db.models import AgentRun, DataSource, ModelRegistry, Recommendation as Rec

    sources = db.query(DataSource).filter(DataSource.brand_id == brand_id).all()
    avg_quality = sum(s.quality_score or 0 for s in sources) / len(sources) if sources else 0
    stages = [
        {"key": "ingest", "label": "Ingest & Reconcile", "metric": f"{len(sources)} sources",
         "detail": f"mean quality {avg_quality:.0f}/100", "health": "healthy" if avg_quality >= 80 else "attention"},
        {"key": "diagnose", "label": "Diagnose & Reason", "metric":
            f"{db.query(Anomaly).filter(Anomaly.brand_id == brand_id, Anomaly.status == 'open').count()} open anomalies",
         "detail": f"{db.query(AgentRun).filter(AgentRun.brand_id == brand_id).count()} agent runs",
         "health": "healthy"},
        {"key": "decide", "label": "Decide",
         "metric": f"{db.query(Opportunity).filter(Opportunity.brand_id == brand_id, Opportunity.status == 'open').count()} opportunities",
         "detail": f"{db.query(Rec).filter(Rec.brand_id == brand_id, Rec.status == 'proposed').count()} recommendations queued",
         "health": "healthy"},
        {"key": "execute", "label": "Execute",
         "metric": f"{db.query(Execution).filter(Execution.brand_id == brand_id).count()} executions",
         "detail": f"{db.query(Execution).filter(Execution.brand_id == brand_id, Execution.status == 'rolled_back').count()} rolled back",
         "health": "healthy"},
        {"key": "learn", "label": "Learn",
         "metric": f"{db.query(Outcome).filter(Outcome.brand_id == brand_id).count()} measured outcomes",
         "detail": f"{db.query(ModelRegistry).count()} model versions", "health": "healthy"},
    ]
    return stages


def _ai_uplift(db: Session, brand_id: int) -> dict[str, Any]:
    """Realised uplift from executed actions, plus the modelled pipeline upside."""
    outcomes = db.query(Outcome).filter(Outcome.brand_id == brand_id).all()
    realised = sum(o.actual_profit_delta for o in outcomes)
    predicted = sum(o.predicted_profit_delta for o in outcomes)
    pipeline_upside = sum(
        r.expected_profit_delta for r in db.query(Recommendation).filter(
            Recommendation.brand_id == brand_id, Recommendation.status == "proposed").all()
    )
    return {
        "realised_profit_uplift": round(realised, 2),
        "predicted_profit_uplift": round(predicted, 2),
        "measured_outcomes": len(outcomes),
        "open_pipeline_upside": round(pipeline_upside, 2),
        "win_rate": round(
            (sum(1 for o in outcomes if o.verdict in {"as_predicted", "better_than_predicted"}) / len(outcomes)) * 100, 1
        ) if outcomes else None,
        "executions": db.query(Execution).filter(Execution.brand_id == brand_id).count(),
    }


@router.get("/notifications")
def notifications(
    unread_only: bool = Query(False),
    limit: int = Query(30, ge=1, le=200),
    brand_id: int | None = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    query = db.query(Notification).filter(Notification.brand_id == brand)
    if unread_only:
        query = query.filter(Notification.read.is_(False))
    rows = query.order_by(Notification.created_at.desc()).limit(limit).all()
    unread = db.query(Notification).filter(Notification.brand_id == brand, Notification.read.is_(False)).count()
    return {
        "items": [{
            "id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "severity": n.severity,
            "entity_type": n.entity_type, "entity_id": n.entity_id, "read": n.read,
            "payload": n.payload or {},
            "created_at": n.created_at.isoformat() if n.created_at else None,
        } for n in rows],
        "unread": unread,
    }


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: int, brand_id: int | None = Query(None),
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    brand = resolve_brand(db, principal, brand_id)
    item = db.query(Notification).filter(Notification.id == notification_id, Notification.brand_id == brand).first()
    if not item:
        from ...core.errors import ApiError

        raise ApiError(404, "not_found", "notification not found")
    item.read = True
    db.commit()
    return {"status": "ok", "id": item.id, "read": True}


@router.post("/notifications/read-all")
def mark_all_read(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict:
    brand = resolve_brand(db, principal, brand_id)
    updated = db.query(Notification).filter(
        Notification.brand_id == brand, Notification.read.is_(False)
    ).update({"read": True}, synchronize_session=False)
    db.commit()
    return {"status": "ok", "updated": updated}


@router.get("/scenarios")
def scenarios(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
              db: Session = Depends(get_db)) -> dict[str, Any]:
    """Scenario Guide: the planted events and their detection status."""
    from ...engines.scenarios import SCENARIOS

    brand = resolve_brand(db, principal, brand_id)
    statuses = {s.key: s for s in db.query(ScenarioStatus).filter(ScenarioStatus.brand_id == brand).all()}
    items = []
    for scenario in SCENARIOS:
        status = statuses.get(scenario["key"])
        items.append({
            **{k: v for k, v in scenario.items()},
            "detected": bool(status.detected) if status else False,
            "diagnosed": bool(status.diagnosed) if status else False,
            "recommended": bool(status.recommended) if status else False,
            "anomaly_id": status.anomaly_id if status else None,
            "diagnosis_id": status.diagnosis_id if status else None,
            "recommendation_id": status.recommendation_id if status else None,
            "detection_note": status.detection_note if status else "",
        })
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "detected": sum(1 for i in items if i["detected"]),
            "diagnosed": sum(1 for i in items if i["diagnosed"]),
            "recommended": sum(1 for i in items if i["recommended"]),
        },
    }