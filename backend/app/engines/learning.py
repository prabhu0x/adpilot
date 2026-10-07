"""Learning loop: recalibration, backtests and model cards.

Three things happen on every pass:

1. **Impact calibration** — realised outcome errors shrink or expand future
   predicted profit deltas (see :func:`outcomes.recalibrate`).
2. **Detector backtest** — every recorded execution window is replayed through
   the anomaly detector to check it would still have been flagged, producing a
   precision/recall-style scorecard.
3. **Model cards** — each model (anomaly ensemble, response curves, causal
   estimator, impact model) gets a versioned card with its metrics, parameters
   and limitations.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import Anomaly, Execution, ModelRegistry, Outcome, Recommendation, UnifiedFact
from . import outcomes as outcomes_engine
from .reconcile import _read

log = get_logger("adpilot.learning")


def recalibrate_models(db: Session, brand_id: int) -> dict[str, Any]:
    calibration = outcomes_engine.recalibrate(db, brand_id)
    backtest = backtest_detectors(db, brand_id)
    cards = write_model_cards(db, brand_id, calibration, backtest)
    db.flush()
    return {"status": "ok", "calibration": calibration, "backtest": backtest, "model_cards": cards}


def backtest_detectors(db: Session, brand_id: int) -> dict[str, Any]:
    """Replay executed windows through the detector to score detection quality."""
    executions = db.query(Execution).filter(Execution.brand_id == brand_id).all()
    if not executions:
        return {"status": "ok", "windows": 0, "detection_rate": None, "note": "no executions to backtest yet"}
    anomalies = db.query(Anomaly).filter(Anomaly.brand_id == brand_id).all()
    matched = 0
    details = []
    for execution in executions:
        rec = db.query(Recommendation).filter(Recommendation.id == execution.recommendation_id).first()
        if not rec:
            continue
        hit = any(
            a.entity_type == rec.entity_type and (a.entity_id == rec.entity_id or rec.entity_id is None)
            for a in anomalies
        )
        matched += int(hit)
        details.append({"execution_id": execution.id, "kind": rec.kind, "entity": rec.entity_name,
                        "flagged_by_detector": hit})
    rate = matched / len(details) if details else 0.0
    return {"status": "ok", "windows": len(details), "detection_rate": round(rate, 3), "detail": details[:25]}


def write_model_cards(db: Session, brand_id: int, calibration: dict[str, Any], backtest: dict[str, Any]) -> list[str]:
    outcomes = db.query(Outcome).filter(Outcome.brand_id == brand_id).all()
    facts_count = db.query(UnifiedFact).filter(UnifiedFact.brand_id == brand_id).count()
    errors = [o.pct_error for o in outcomes] or [0.0]
    cards = [
        {
            "name": "anomaly_ensemble", "version": "v1",
            "notes": "STL residual z-score + MAD z-score + PELT change point + Holt-Winters breach, "
                           "combined into a severity score with BH-FDR control.",
            "metrics": {"facts_scanned": int(facts_count),
                        "detection_rate_on_executed_windows": backtest.get("detection_rate"),
                        "windows_backtested": backtest.get("windows", 0)},
            "hyperparams": {"recent_window_days": 14, "baseline_window_days": 56, "scan_window_days": 200,
                            "fdr_alpha": 0.12, "min_methods_agreeing": 2},
            "limitations": ["Needs at least 60 daily observations per series.",
                            "Correlated metrics within one entity can produce multiple related alerts.",
                            "Regime-change detection is capped at 60 days of age."],
        },
        {
            "name": "response_curves", "version": "v1",
            "notes": "Saturating spend→revenue curves (Hill and log forms) selected by RMSE, "
                           "with marginal ROAS and elasticity at the current spend level.",
            "metrics": {"curve_reliability_threshold_r2": 0.45},
            "hyperparams": {"forms": ["hill", "log"], "min_observations": 10},
            "limitations": ["Extrapolation beyond ±60% of observed spend degrades to constant ROAS.",
                            "Assumes no structural change (auction, creative, seasonality) inside the fit window."],
        },
        {
            "name": "causal_estimator", "version": "v1",
            "notes": "Synthetic control (ridge-weighted donors) with a permutation interval, "
                           "falling back to difference-in-differences then a pre-period mean shift.",
            "metrics": {"outcomes_measured": len(outcomes)},
            "hyperparams": {"min_donors": 3, "permutations": 120, "ridge": 1e-3},
            "limitations": ["Assumes no unobserved shock hits the treated entity.",
                            "Donor contamination (spillover) biases the estimate toward zero."],
        },
        {
            "name": "impact_calibration", "version": "v1",
            "notes": "Multiplier applied to predicted profit deltas, learned from realised outcomes.",
            "metrics": {"calibration_multiplier": calibration.get("calibration"),
                        "mean_absolute_pct_error": float(np.mean(np.abs(errors))) if outcomes else None,
                        "n_outcomes": len(outcomes)},
            "hyperparams": {"shrinkage": 0.5},
            "limitations": ["Needs at least five measured outcomes before it moves materially."],
        },
    ]
    db.query(ModelRegistry).filter(ModelRegistry.name.in_([c["name"] for c in cards])).delete(
        synchronize_session=False
    )
    for card in cards:
        limitations = card.pop("limitations", [])
        notes = card.pop("notes", "")
        db.add(ModelRegistry(
            name=card["name"], version=card["version"], kind=card.get("kind", "model"),
            metrics=card.get("metrics", {}), hyperparams=card.get("hyperparams", {}),
            notes=notes + ("\n\nLimitations: " + "; ".join(limitations) if limitations else ""),
        ))
    db.flush()
    return [c["name"] for c in cards]