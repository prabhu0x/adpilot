"""The visible AdPilot lifecycle pipeline.

``INGEST & RECONCILE → DIAGNOSE & REASON → DECIDE → EXECUTE → LEARN``

Every stage is a plain function so it can be triggered from the API, from the
scheduler, from the CLI or from the simulation clock.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from sqlalchemy import delete
from sqlalchemy.orm import Session

from ..core.events import bus, jobs
from ..core.logging import get_logger, metrics
from . import anomalies as anomaly_engine
from . import diagnose, execution, ingestion, learning, opportunities, optimizer, outcomes, recommender
from . import reconcile as reconcile_engine

log = get_logger("adpilot.pipeline")

STAGES = ["ingest", "diagnose", "decide", "execute", "learn"]


def reset_derived(db: Session, brand_id: int) -> None:
    """Clear derived artefacts so the pipeline is idempotent.

    Executions, outcomes and audit records are *never* deleted; only the
    regenerable analysis layer and still-unactioned recommendations are.
    """
    from ..db.models import (
        AgentRun,
        AgentStep,
        Anomaly,
        Diagnosis,
        Opportunity,
        PredictionRecord,
        Recommendation,
        ReconciliationReport,
        ScenarioStatus,
        UnifiedFact,
    )

    executed_rec_ids = {
        rid for (rid,) in db.query(Recommendation.id).filter(
            Recommendation.brand_id == brand_id,
            Recommendation.status.in_(["executed", "measured"]),
        ).all()
    }
    db.execute(delete(AgentStep).where(AgentStep.brand_id == brand_id))
    db.execute(delete(AgentRun).where(AgentRun.brand_id == brand_id))
    db.execute(delete(Diagnosis).where(Diagnosis.brand_id == brand_id))
    db.execute(delete(Anomaly).where(Anomaly.brand_id == brand_id))
    db.execute(delete(Opportunity).where(Opportunity.brand_id == brand_id))
    db.execute(delete(PredictionRecord).where(PredictionRecord.brand_id == brand_id))
    db.execute(delete(ReconciliationReport).where(ReconciliationReport.brand_id == brand_id))
    db.execute(delete(UnifiedFact).where(UnifiedFact.brand_id == brand_id))
    q = db.query(Recommendation).filter(Recommendation.brand_id == brand_id)
    if executed_rec_ids:
        q = q.filter(Recommendation.id.notin_(executed_rec_ids))
    q.delete(synchronize_session=False)
    # ScenarioStatus is deliberately *not* reset: once a planted event has been
    # detected, diagnosed and actioned it stays recorded even after the demo
    # clock advances past it (the checklist is a historical record).
    db.flush()


def run_full_pipeline(
    db: Session,
    brand_id: int,
    progress: Callable[[float, str], None] | None = None,
    job_id: str | None = None,
    reset: bool = True,
) -> dict[str, Any]:
    """Run the whole closed loop once and return a stage-by-stage summary."""

    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)
        if job_id:
            jobs.update(job_id, pct, msg)
        bus.publish("pipeline", {"stage_pct": pct, "message": msg})

    started = dt.datetime.utcnow()
    result: dict[str, Any] = {"brand_id": brand_id, "started_at": started.isoformat(), "stages": {}}

    if reset:
        step(1, "resetting derived artefacts")
        reset_derived(db, brand_id)
        db.commit()

    step(6, "INGEST — unified schema vectors")
    ingest = ingestion.run_incremental_ingest(db, brand_id)
    result["stages"]["ingest"] = ingest
    db.commit()

    step(16, "RECONCILE — de-overlap, margin, unified facts")
    recon = reconcile_engine.run_reconciliation(db, brand_id, progress=lambda p, m: step(16 + p * 0.16, m))
    result["stages"]["reconcile"] = recon
    db.commit()

    step(34, "DIAGNOSE — anomaly ensemble")
    anomalies = anomaly_engine.detect_anomalies(db, brand_id, progress=lambda p, m: step(34 + p * 0.14, m))
    result["stages"]["anomalies"] = anomalies
    db.commit()

    step(48, "DIAGNOSE — root cause & causal reasoning")
    diag = diagnose.diagnose_open_anomalies(db, brand_id, progress=lambda p, m: step(48 + p * 0.14, m))
    result["stages"]["diagnosis"] = diag
    db.commit()

    step(63, "DECIDE — opportunity scoring")
    opps = opportunities.score_opportunities(db, brand_id, progress=lambda p, m: step(63 + p * 0.09, m))
    result["stages"]["opportunities"] = opps
    db.commit()

    step(72, "DECIDE — constrained budget optimization")
    opt = optimizer.run_optimizer(db, brand_id, progress=lambda p, m: step(72 + p * 0.08, m))
    result["stages"]["optimizer"] = opt
    db.commit()

    step(80, "DECIDE — recommendation generation")
    recs = recommender.generate_recommendations(db, brand_id, progress=lambda p, m: step(80 + p * 0.07, m))
    result["stages"]["recommendations"] = recs
    db.commit()

    step(88, "EXECUTE — measuring open outcomes & autonomous actions")
    outcome_summary = outcomes.measure_due_outcomes(db, brand_id)
    auto = execution.run_autonomous_actions(db, brand_id)
    result["stages"]["execution"] = {"outcomes_measured": outcome_summary, "autonomous": auto}
    db.commit()

    step(94, "LEARN — recalibration & feedback")
    learn = learning.recalibrate_models(db, brand_id)
    result["stages"]["learning"] = learn
    db.commit()

    step(99, "scenario verification")
    result["stages"]["scenarios"] = scenario_status(db, brand_id)
    result["duration_ms"] = int((dt.datetime.utcnow() - started).total_seconds() * 1000)
    metrics.inc("pipeline_runs")
    log.info("pipeline complete in %sms", result["duration_ms"])
    return result


def scenario_status(db: Session, brand_id: int) -> dict[str, Any]:
    """Refresh the planted-scenario detection/diagnosis/recommendation checklist.

    Most scenarios surface as anomalies; two of them (platform attribution
    overlap and the under-promoted margin SKU) surface in the reconciliation
    report and the opportunity radar respectively, so those are verified from
    their own evidence.
    """
    from ..db.models import Diagnosis, Opportunity, ReconciliationReport, Recommendation, ScenarioStatus

    anomaly_map = anomaly_engine.match_scenarios(db, brand_id)

    # --- attribution overlap is a reconciliation finding, not an anomaly -----
    reconciliation = (
        db.query(ReconciliationReport)
        .filter(ReconciliationReport.brand_id == brand_id)
        .order_by(ReconciliationReport.date.desc())
        .first()
    )
    statuses = db.query(ScenarioStatus).filter(ScenarioStatus.brand_id == brand_id).all()
    for status in statuses:
        if status.key == "attribution_overlap" and reconciliation and reconciliation.discrepancy_pct > 10:
            status.detected = True
            status.detection_note = (
                f"Reconciliation: platform-reported revenue ${reconciliation.platform_reported_revenue:,.0f} vs "
                f"attributed ${reconciliation.attributed_revenue:,.0f} on {reconciliation.date} "
                f"({reconciliation.discrepancy_pct:.1f}% overstatement, resolution "
                f"'{reconciliation.resolution_method}')"
            )
        if status.key in {"under_promoted_margin_sku", "tiktok_headroom"}:
            kinds = ("under_promoted_margin_sku",) if status.key == "under_promoted_margin_sku" \
                else ("winning_campaign_headroom", "creative_headroom", "audience_expansion")
            query = db.query(Opportunity).filter(
                Opportunity.brand_id == brand_id, Opportunity.kind.in_(kinds)
            )
            if status.key == "tiktok_headroom":
                query = query.filter(Opportunity.platform_key == "tiktok")
            opportunity = query.order_by(Opportunity.score.desc()).first()
            if opportunity:
                status.detected = True
                status.detection_note = (
                    f"{opportunity.kind}: {opportunity.title} — "
                    f"${opportunity.expected_profit_delta:,.0f} expected 90-day contribution, "
                    f"confidence {opportunity.confidence*100:.0f}%"
                )
                rec = db.query(Recommendation).filter(
                    Recommendation.brand_id == brand_id, Recommendation.opportunity_id == opportunity.id
                ).order_by(Recommendation.priority_score.desc()).first()
                if rec:
                    status.recommended = True
                    status.recommendation_id = rec.id
        anomaly_id = anomaly_map.get(status.key)
        if not anomaly_id:
            continue
        status.anomaly_id = anomaly_id
        status.detected = True
        if not status.detection_note:
            status.detection_note = "Detected by the anomaly ensemble (statistical or domain rule)."
        diag = (
            db.query(Diagnosis)
            .filter(Diagnosis.brand_id == brand_id, Diagnosis.anomaly_id == anomaly_id)
            .first()
        )
        if diag:
            status.diagnosed = True
            status.diagnosis_id = diag.id
            rec = (
                db.query(Recommendation)
                .filter(Recommendation.brand_id == brand_id, Recommendation.diagnosis_id == diag.id)
                .order_by(Recommendation.priority_score.desc())
                .first()
            )
            if rec:
                status.recommended = True
                status.recommendation_id = rec.id
    db.flush()
    return {
        s.key: {"detected": s.detected, "diagnosed": s.diagnosed, "recommended": s.recommended}
        for s in statuses
    }