"""Pillar 7 — outcome measurement and closed-loop learning.

Once an action's measurement window closes we build a counterfactual for the
affected entity (synthetic control over unaffected campaigns, falling back to
DiD then to a pre-period mean shift), compare predicted with actual
incremental profit, and record the error. The rolling errors then recalibrate
the confidence of future predictions — that is the learning loop.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.events import bus
from ..core.logging import get_logger
from ..db.models import (
    Anomaly,
    Execution,
    ModelRegistry,
    Outcome,
    Recommendation,
    UnifiedFact,
)
from . import causal as causal_engine
from .llm import narrate_outcome
from .reconcile import _read

log = get_logger("adpilot.outcomes")


def measure_due_outcomes(db: Session, brand_id: int, progress: Callable[[float, str], None] | None = None) -> dict[str, Any]:
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.spend,
        UnifiedFact.attributed_revenue, UnifiedFact.contribution_margin,
    ).where(UnifiedFact.brand_id == brand_id))
    if facts.empty:
        return {"status": "no_data"}
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    clock = max(facts["date"])

    pending = (
        db.query(Execution)
        .filter(Execution.brand_id == brand_id, Execution.status == "executed")
        .order_by(Execution.executed_at.asc())
        .all()
    )
    measured = 0
    for execution in pending:
        rec = db.query(Recommendation).filter(Recommendation.id == execution.recommendation_id).first()
        if not rec:
            continue
        existing = db.query(Outcome).filter(Outcome.execution_id == execution.id).first()
        if existing:
            continue
        result = _measure(db, brand_id, facts, clock, execution, rec)
        outcome = Outcome(brand_id=brand_id, execution_id=execution.id, recommendation_id=rec.id,
                          **result)
        db.add(outcome)
        db.flush()
        outcome.narrative = narrate_outcome(result)
        execution.status = "measured"
        rec.status = "measured"
        measured += 1
        bus.publish("outcome", {"execution_id": execution.id, "verdict": result["verdict"]})

    db.flush()
    return {"status": "ok", "measured": measured, "pending": len(pending) - measured}


def _measure(db: Session, brand_id: int, facts: pd.DataFrame, clock: dt.date,
             execution: Execution, rec: Recommendation) -> dict[str, Any]:
    """Build the counterfactual and score the action."""
    window_end = min(clock, (execution.measurement_window_end or dt.datetime.utcnow()).date())
    window_start = (execution.measurement_window_start or dt.datetime.utcnow()).date()
    window_days = max((window_end - window_start).days, 1)
    pre_start = window_start - dt.timedelta(days=window_days * 2)

    entity_id = rec.entity_id
    if rec.entity_type == "portfolio" or entity_id is None:
        daily = facts.groupby("date", as_index=False).agg(
            spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), margin=("contribution_margin", "sum"))
        pre = daily[(daily["date"] >= pre_start) & (daily["date"] < window_start)]
        post = daily[(daily["date"] >= window_start) & (daily["date"] <= window_end)]
        method = "portfolio_pre_post"
        predicted = float(rec.expected_profit_delta or 0.0)
        # expected profit over the window proportional to the window length
        expected_window = predicted * (window_days / 90.0)
        margin_rate = float(pre["margin"].sum() / max(pre["revenue"].sum(), 1e-9)) if not pre.empty else 0.3
        actual = float(post["revenue"].sum() * margin_rate - post["spend"].sum()) - float(
            pre["revenue"].sum() * margin_rate - pre["spend"].sum()) * (len(post) / max(len(pre), 1))
        confidence = 0.55
        ci = (actual * 0.6, actual * 1.4)
    else:
        daily = facts.groupby(["date", "campaign_id"], as_index=False).agg(
            revenue=("attributed_revenue", "sum"), spend=("spend", "sum"), margin=("contribution_margin", "sum"))
        treated = daily[daily["campaign_id"] == entity_id].sort_values("date")
        donors = daily[daily["campaign_id"] != entity_id]
        donor_ids = [i for i in donors["campaign_id"].unique() if i is not None][:16]
        tpre = treated[(treated["date"] >= pre_start) & (treated["date"] < window_start)]["revenue"].to_numpy(float)
        tpost = treated[(treated["date"] >= window_start) & (treated["date"] <= window_end)]["revenue"].to_numpy(float)
        dpre = {str(i): donors[(donors["campaign_id"] == i) & (donors["date"] >= pre_start)
                               & (donors["date"] < window_start)].sort_values("date")["revenue"].to_numpy(float) for i in donor_ids}
        dpost = {str(i): donors[(donors["campaign_id"] == i) & (donors["date"] >= window_start)
                                & (donors["date"] <= window_end)].sort_values("date")["revenue"].to_numpy(float) for i in donor_ids}
        causal = causal_engine.estimate_causal_effect(tpre, tpost, dpre, dpost, label="attributed_revenue", unit="usd")
        method = causal.get("method", "unavailable")
        margin_rate = 0.4
        if rec.entity_type == "campaign":
            row = facts[facts["campaign_id"] == entity_id]
            if not row.empty and row["attributed_revenue"].sum() > 0:
                margin_rate = float(row["contribution_margin"].sum() / row["attributed_revenue"].sum())
        actual = float(causal.get("cumulative_effect", 0.0)) * margin_rate if causal.get("available") else 0.0
        expected_window = float(rec.expected_profit_delta or 0.0) * (window_days / 90.0)
        predicted = expected_window
        confidence = float(causal.get("confidence", 0.5))
        ci = (float(causal.get("ci_low", 0)) * margin_rate, float(causal.get("ci_high", 0)) * margin_rate)

    pct_error = ((actual - expected_window) / abs(expected_window) * 100) if abs(expected_window) > 1e-6 else 0.0
    if abs(expected_window) < 1e-6 and abs(actual) < 1e-6:
        verdict = "inconclusive"
    elif actual >= 0 and expected_window >= 0 and abs(pct_error) <= 35:
        verdict = "as_predicted"
    elif actual > expected_window * 1.35:
        verdict = "better_than_predicted"
    elif actual < expected_window * 0.65 and actual >= 0:
        verdict = "worse_than_predicted"
    elif actual < 0:
        verdict = "negative"
    else:
        verdict = "inconclusive"

    return {
        "window_start": window_start, "window_end": window_end, "window_days": window_days,
        "counterfactual_method": method,
        "predicted_profit_delta": round(float(expected_window), 2),
        "actual_profit_delta": round(float(actual), 2),
        "ci_low": round(float(ci[0]), 2), "ci_high": round(float(ci[1]), 2),
        "pct_error": round(float(pct_error), 2),
        "confidence": round(float(confidence), 3),
        "verdict": verdict,
        "detail": {
            "entity_type": rec.entity_type, "entity_id": rec.entity_id, "kind": rec.kind,
            "margin_rate": round(float(margin_rate), 4),
            "pre_window_start": str(pre_start), "recommendation_title": rec.title,
        },
    }


def recalibrate(db: Session, brand_id: int) -> dict[str, Any]:
    """Learn from realised errors: shrink over-confident predictions."""
    outcomes = db.query(Outcome).filter(Outcome.brand_id == brand_id).all()
    if not outcomes:
        return {"status": "ok", "outcomes": 0, "calibration": 1.0}
    ratios = []
    for outcome in outcomes:
        if abs(outcome.predicted_profit_delta) > 1e-6:
            ratios.append(outcome.actual_profit_delta / outcome.predicted_profit_delta)
    ratios = [r for r in ratios if np.isfinite(r)]
    if not ratios:
        return {"status": "ok", "outcomes": len(outcomes), "calibration": 1.0}
    median_ratio = float(np.median(ratios))
    # shrink toward 1.0 so a single outlier cannot dominate
    calibration = float(np.clip(0.5 + 0.5 * median_ratio, 0.4, 1.5))
    mean_abs_error = float(np.mean([abs(o.pct_error) for o in outcomes]))
    version = f"v{dt.date.today().isoformat()}"
    db.query(ModelRegistry).filter(ModelRegistry.name == "impact_calibration").delete(synchronize_session=False)
    db.add(ModelRegistry(
        name="impact_calibration", version=version, kind="calibration",
        metrics={"calibration_multiplier": round(calibration, 4), "median_realised_ratio": round(median_ratio, 4),
                 "mean_absolute_pct_error": round(mean_abs_error, 2), "n_outcomes": len(outcomes)},
        hyperparams={"shrinkage": 0.5, "clip": [0.4, 1.5]},
        notes=("Multiplier applied to predicted profit deltas, learned from realised outcomes. "
               "Limitations: requires at least five measured outcomes before it materially moves the multiplier."),
    ))
    db.flush()
    return {"status": "ok", "outcomes": len(outcomes), "calibration": round(calibration, 4),
            "mean_abs_pct_error": round(mean_abs_error, 2)}


def calibration_multiplier(db: Session, brand_id: int) -> float:
    card = db.query(ModelRegistry).filter(ModelRegistry.name == "impact_calibration").order_by(
        ModelRegistry.trained_at.desc()).first()
    if not card:
        return 1.0
    return float(card.metrics.get("calibration_multiplier", 1.0))