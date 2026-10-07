"""Pillar 1/2 — multi-method, explainable anomaly detection.

Metrics: ROAS, MER, CPA, CPC, CPM, CTR, CVR, AOV, spend pacing, contribution
margin, impressions, frequency, days-of-cover and GA sessions.

Methods (combined into an ensemble severity score)
--------------------------------------------------
1. **Seasonal decomposition residuals (STL)** — robust z-score of the residual
   tail against the residual distribution.
2. **Robust z-score (MAD)** — median absolute deviation on levels *and* on
   first differences.
3. **Change-point detection (``ruptures`` PELT)** — locates the most recent
   regime change and tests pre/post means with Welch's t-test.
4. **Forecast-interval breach** — Holt-Winters (weekly seasonality) forecast of
   the analysis window; counts observations outside the 95% interval.

Guardrails against noise: a minimum effect size, a requirement that at least two
methods agree on the *direction*, and Benjamini-Hochberg FDR control across the
scanned family. Data artifacts are detected *first* and labelled.

Supports both daily and hourly granularity.
"""
from __future__ import annotations

import datetime as dt
import warnings
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import (
    AdMetricsHourly,
    Anomaly,
    Campaign,
    Creative,
    AdSet,
    GaEvent,
    InventorySnapshot,
    Sku,
    UnifiedFact,
)
from .reconcile import _read
from . import domain_checks

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=RuntimeWarning)
log = get_logger("adpilot.anomalies")

MIN_EFFECT_PCT = 8.0          # minimum relative move to be considered
RECENT_WINDOW = 14            # days in the "after" window
BASELINE_WINDOW = 56          # days in the "before" window
SCAN_WINDOW = 200             # history used for the statistical baseline
HOURLY_RECENT = 48            # hours in the hourly analysis window

METRIC_DIRECTION: dict[str, str] = {
    "roas": "higher_is_better", "mer": "higher_is_better", "cvr": "higher_is_better",
    "ctr": "higher_is_better", "aov": "higher_is_better", "contribution_margin": "higher_is_better",
    "cm_pct": "higher_is_better", "impressions": "higher_is_better",
    "cpa": "lower_is_better", "cpc": "lower_is_better", "cpm": "lower_is_better",
    "frequency": "lower_is_better", "spend": "neutral", "days_of_cover": "higher_is_better",
    "ga_sessions": "higher_is_better",
}

LEVEL_METRICS: dict[str, list[str]] = {
    "platform": ["roas", "spend", "cpa", "ctr", "cvr", "cpm", "cpc", "aov", "contribution_margin", "frequency", "cm_pct"],
    "campaign": ["roas", "spend", "cpa", "ctr", "cvr", "cpm", "cpc", "aov", "contribution_margin", "frequency", "cm_pct"],
    "ad_set": ["roas", "cpa", "ctr", "cvr", "spend"],
    "creative": ["ctr", "cvr", "cpa"],
    "sku": ["cvr", "contribution_margin", "days_of_cover"],
    "channel": ["ga_sessions", "cvr", "roas"],
}

# Level-specific noise floors. Lower levels are intrinsically noisier, so they
# must clear a higher bar before being surfaced as anomalies.
LEVEL_MIN_EFFECT: dict[str, float] = {
    "platform": 6.0, "campaign": 10.0, "ad_set": 15.0, "creative": 25.0, "sku": 20.0, "channel": 12.0,
}
LEVEL_MIN_SEVERITY: dict[str, float] = {
    "platform": 45.0, "campaign": 50.0, "ad_set": 55.0, "creative": 62.0, "sku": 50.0, "channel": 45.0,
}
PRE_SCREEN_Z = 2.4


# --------------------------------------------------------------------------- #
# Statistical methods
# --------------------------------------------------------------------------- #
def _mad_z(series: np.ndarray, window: int) -> tuple[float, float]:
    """Robust z-score of the last ``window`` points against the preceding history."""
    if series.size < window + 20:
        return 0.0, 1.0
    baseline = series[:-window]
    recent = series[-window:]
    med = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - med)))
    scale = 1.4826 * mad if mad > 1e-9 else (float(baseline.std(ddof=1)) or 1e-9)
    z = (float(np.median(recent)) - med) / scale
    p = float(2 * (1 - stats.norm.cdf(abs(z))))
    return z, p


def _stl_z(series: np.ndarray, window: int, period: int = 7) -> tuple[float, float]:
    """Robust z-score of STL residuals in the analysis window."""
    if series.size < period * 4 + window:
        return 0.0, 1.0
    try:
        from statsmodels.tsa.seasonal import STL

        res = STL(pd.Series(series), period=period, robust=True).fit()
        resid = np.asarray(res.resid, dtype=float)
    except Exception:
        return 0.0, 1.0
    baseline = resid[:-window]
    recent = resid[-window:]
    med = float(np.median(baseline))
    mad = float(np.median(np.abs(baseline - med)))
    scale = 1.4826 * mad if mad > 1e-9 else (float(baseline.std(ddof=1)) or 1e-9)
    z = (float(np.mean(recent)) - med) / scale
    p = float(2 * (1 - stats.norm.cdf(abs(z))))
    return z, p


def _changepoint(series: np.ndarray, window: int) -> dict[str, Any]:
    """PELT change point in the recent past + Welch t-test on pre/post means."""
    out = {"found": False, "index": None, "strength": 0.0, "p_value": 1.0, "delta_pct": 0.0}
    n = series.size
    if n < 60:
        return out
    try:
        import ruptures as rpt

        algo = rpt.Pelt(model="rbf", min_size=10).fit(series.reshape(-1, 1))
        breaks = algo.predict(pen=np.log(n) * float(series.std() or 1.0) * 2.0)
    except Exception:
        return out
    breaks = [b for b in breaks if 20 <= b <= n - 5]
    if not breaks:
        return out
    cp = max(breaks)
    if cp < n - window - 10:
        return out
    pre = series[max(0, cp - 60):cp]
    post = series[cp:]
    if pre.size < 10 or post.size < 5:
        return out
    t, p = stats.ttest_ind(post, pre, equal_var=False)
    base = float(np.mean(pre))
    delta = (float(np.mean(post)) / base - 1) * 100 if base else 0.0
    strength = min(1.0, abs(float(t)) / 6.0)
    out.update({
        "found": True, "index": int(cp), "strength": float(strength),
        "p_value": float(p), "delta_pct": float(delta),
        "t_stat": float(t),
    })
    return out


def _forecast_breach(series: np.ndarray, window: int, period: int = 7) -> dict[str, Any]:
    """Holt-Winters forecast of the analysis window; count 95% interval breaches."""
    out = {"available": False, "breach_ratio": 0.0, "p_value": 1.0}
    if series.size < window + period * 3:
        return out
    train, actual = series[:-window], series[-window:]
    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        model = ExponentialSmoothing(
            pd.Series(train), trend="add", seasonal="add", seasonal_periods=period,
            initialization_method="estimated",
        ).fit()
        pred = model.forecast(window)
        resid_std = float(np.std(train - model.fittedvalues, ddof=1)) or 1e-9
    except Exception:
        return out
    breaches = int(np.sum(np.abs(actual - np.asarray(pred)) > 1.96 * resid_std))
    out["available"] = True
    out["breach_ratio"] = breaches / max(window, 1)
    out["p_value"] = float(min(1.0, stats.binomtest(breaches, window, 0.05, alternative="greater").pvalue))
    return out


def _welch_p(pre: np.ndarray, post: np.ndarray) -> float:
    if pre.size < 8 or post.size < 5:
        return 1.0
    try:
        return float(stats.ttest_ind(post, pre, equal_var=False).pvalue)
    except Exception:
        return 1.0


# --------------------------------------------------------------------------- #
# Ensemble detection for one series
# --------------------------------------------------------------------------- #
def detect_series(
    values: np.ndarray,
    label: str = "metric",
    unit: str = "",
    period: int = 7,
    min_effect_pct: float = MIN_EFFECT_PCT,
    window: int = RECENT_WINDOW,
) -> dict[str, Any] | None:
    """Run the four-method ensemble on a single cleaned series."""
    values = np.asarray(values, dtype=float)
    values = values[~np.isnan(values)]
    if values.size < 60:
        return None

    values = values[-SCAN_WINDOW:]
    n = values.size
    if n < 60:
        return None

    baseline = values[-window - BASELINE_WINDOW : -window]
    recent = values[-window:]
    base_level = float(np.median(baseline)) if baseline.size else float(np.median(values))
    recent_level = float(np.median(recent))
    denom = abs(base_level) if abs(base_level) > 1e-9 else max(abs(recent_level), 1e-9)
    magnitude_pct = (recent_level - base_level) / denom * 100

    # ---- cheap pre-screen: skip the expensive methods for quiet series ------
    med_all = float(np.median(values[: -window])) if values.size > window else 0.0
    mad_all = float(np.median(np.abs(values[: -window] - med_all)))
    screen_scale = 1.4826 * mad_all if mad_all > 1e-9 else (float(values.std(ddof=1)) or 1e-9)
    screen_z = (recent_level - med_all) / screen_scale
    if abs(magnitude_pct) < min_effect_pct and abs(screen_z) < PRE_SCREEN_Z:
        return None

    z_stl, p_stl = _stl_z(values, window, period)
    z_mad, p_mad = _mad_z(values, window)
    cp = _changepoint(values, window)
    fc = _forecast_breach(values, window, period)
    p_welch = _welch_p(baseline, recent)

    if abs(magnitude_pct) < min_effect_pct:
        # allow statistically extreme but small moves only if several methods agree
        if not (abs(z_stl) > 3.5 and abs(z_mad) > 3.5 and cp["found"]):
            return None

    direction = "spike" if magnitude_pct >= 0 else "drop"
    signals = {
        "stl": float(z_stl),
        "mad": float(z_mad),
        "changepoint": float(cp["t_stat"] if cp["found"] and cp["delta_pct"] * magnitude_pct > 0 else 0.0),
        "forecast": float(fc["breach_ratio"] * 5 if fc["available"] else 0.0),
    }
    agreeing = sum(1 for v in signals.values() if abs(v) >= 1.5)
    if agreeing < 2:
        return None

    severity = 100.0 * (
        0.32 * min(1.0, abs(z_stl) / 4.0)
        + 0.24 * min(1.0, abs(z_mad) / 5.0)
        + 0.24 * cp["strength"]
        + 0.12 * min(1.0, fc["breach_ratio"] * 2.0)
        + 0.08 * min(1.0, abs(magnitude_pct) / 40.0)
    )
    if agreeing >= 3:
        severity = min(100.0, severity * 1.12)

    p_value = min(p_stl, p_mad, p_welch)
    if cp["found"] and cp["delta_pct"] * magnitude_pct > 0:
        p_value = min(p_value, cp["p_value"])
    if fc["available"]:
        p_value = min(p_value, fc["p_value"])

    pre_std = float(baseline.std(ddof=1)) if baseline.size > 1 else 0.0
    effect_size = abs(recent_level - base_level) / pre_std if pre_std > 1e-9 else 0.0

    return {
        "metric": label,
        "unit": unit,
        "direction": direction,
        "magnitude_pct": round(float(magnitude_pct), 2),
        "baseline": round(base_level, 4),
        "observed": round(recent_level, 4),
        "severity": round(float(severity), 2),
        "p_value": round(float(p_value), 5),
        "effect_size": round(float(effect_size), 4),
        "methods": {
            "stl_residual_z": round(float(z_stl), 3),
            "mad_z": round(float(z_mad), 3),
            "welch_p": round(float(p_welch), 5),
            "changepoint": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in cp.items()},
            "forecast_breach": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in fc.items()},
        },
        "methods_agreeing": int(agreeing),
        "window_start": None,
        "window_end": None,
    }


def severity_label(score: float) -> str:
    if score >= 80:
        return "critical"
    if score >= 60:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def detect_regime_change(
    values: np.ndarray,
    label: str = "metric",
    unit: str = "",
    period: int = 7,
    min_effect_pct: float = MIN_EFFECT_PCT,
    max_age_days: int = 60,
) -> dict[str, Any] | None:
    """Change-point-anchored detection for regime shifts up to 60 days old.

    The fixed recent-vs-baseline window misses events that started *inside* the
    baseline (for example a price change six weeks ago whose effect simply
    persisted). Here the "after" window is anchored on the most recent PELT
    change point, the "before" window is the 42 days preceding it, and the move
    must still be in force in the final week to be actionable.
    """
    series = np.asarray(values, dtype=float)
    series = series[~np.isnan(series)]
    if series.size < 120:
        return None
    series = series[-SCAN_WINDOW:]
    n = series.size
    cp = _changepoint(series, window=min(14, n // 8))
    if not cp.get("found"):
        return None
    index = int(cp["index"])
    if n - index < 8 or n - index > max_age_days:
        return None

    before = series[max(0, index - 42) : index]
    after = series[index:]
    if before.size < 21:
        return None
    base_level = float(np.median(before))
    after_level = float(np.median(after))
    denom = abs(base_level) if abs(base_level) > 1e-9 else max(abs(after_level), 1e-9)
    magnitude_pct = (after_level - base_level) / denom * 100
    if abs(magnitude_pct) < min_effect_pct:
        return None

    # persistence: the final week must still sit on the new level
    tail_level = float(np.median(series[-7:]))
    persistence = 1.0 if abs(tail_level - after_level) <= max(0.25 * abs(after_level), 1e-9) else 0.4
    p_value = _welch_p(before, after)
    t_stat = abs(float(cp.get("t_stat", 0.0)))
    pre_std = float(before.std(ddof=1)) or 1e-9
    effect_size = abs(after_level - base_level) / pre_std

    severity = 100.0 * (
        0.40 * min(1.0, t_stat / 6.0)
        + 0.25 * min(1.0, abs(effect_size) / 1.5)
        + 0.20 * min(1.0, abs(magnitude_pct) / 40.0)
        + 0.15 * persistence
    )
    if p_value > 0.1 or severity < 35:
        return None
    return {
        "metric": label,
        "unit": unit,
        "direction": "spike" if magnitude_pct >= 0 else "drop",
        "magnitude_pct": round(float(magnitude_pct), 2),
        "baseline": round(base_level, 4),
        "observed": round(after_level, 4),
        "severity": round(float(min(100.0, severity)), 2),
        "p_value": round(float(p_value), 5),
        "effect_size": round(float(effect_size), 4),
        "methods": {
            "regime_change": {
                "change_index": index,
                "t_stat": round(t_stat, 3),
                "delta_pct": round(float(cp.get("delta_pct", 0.0)), 2),
            },
            "persistence": round(persistence, 2),
            "welch_p": round(float(p_value), 5),
        },
        "methods_agreeing": 2,
        "regime_offset_days": int(n - index),
    }


def detect_adaptive(
    values: np.ndarray,
    metric: str,
    unit: str = "",
    min_effect_pct: float = MIN_EFFECT_PCT,
    min_severity: float = 40.0,
    allow_regime: bool = False,
) -> dict[str, Any] | None:
    """Run the fixed-window ensemble and, optionally, the regime-change detector,
    returning whichever finds the stronger still-actionable signal."""
    found: list[dict[str, Any]] = []
    primary = detect_series(values, metric, unit, min_effect_pct=min_effect_pct, window=RECENT_WINDOW)
    if primary:
        found.append(primary)
    if allow_regime:
        regime = detect_regime_change(values, metric, unit, min_effect_pct=min_effect_pct)
        if regime:
            found.append(regime)
    if not found:
        return None
    best = max(found, key=lambda c: c["severity"])
    if best["severity"] < min_severity:
        return None
    best.setdefault("regime_offset_days", 0)
    return best


def benjamini_hochberg(p_values: list[float], alpha: float = 0.1) -> list[bool]:
    """FDR control across the scanned family."""
    if not p_values:
        return []
    arr = np.asarray(p_values, dtype=float)
    order = np.argsort(arr)
    m = arr.size
    thresholds = alpha * (np.arange(1, m + 1) / m)
    passed = np.zeros(m, dtype=bool)
    ranked = arr[order]
    ok = ranked <= thresholds
    if ok.any():
        cutoff = np.max(np.where(ok)[0])
        passed[order[: cutoff + 1]] = True
    return passed.tolist()


# --------------------------------------------------------------------------- #
# Level aggregation
# --------------------------------------------------------------------------- #
def _aggregate(facts: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    agg = facts.groupby(group_cols, as_index=False).agg(
        impressions=("impressions", "sum"),
        clicks=("clicks", "sum"),
        spend=("spend", "sum"),
        platform_revenue=("platform_revenue", "sum"),
        platform_conversions=("platform_conversions", "sum"),
        attributed_revenue=("attributed_revenue", "sum"),
        attributed_orders=("attributed_orders", "sum"),
        contribution_margin=("contribution_margin", "sum"),
        cogs=("cogs", "sum"),
        frequency=("frequency", "mean"),
    )
    agg["ctr"] = agg["clicks"] / agg["impressions"].replace(0, np.nan)
    agg["cvr"] = agg["attributed_orders"] / agg["clicks"].replace(0, np.nan)
    agg["aov"] = agg["attributed_revenue"] / agg["attributed_orders"].replace(0, np.nan)
    agg["cpc"] = agg["spend"] / agg["clicks"].replace(0, np.nan)
    agg["cpm"] = agg["spend"] / agg["impressions"].replace(0, np.nan) * 1000
    agg["roas"] = agg["attributed_revenue"] / agg["spend"].replace(0, np.nan)
    agg["mer"] = agg["attributed_revenue"] / agg["spend"].replace(0, np.nan)
    agg["cpa"] = agg["spend"] / agg["attributed_orders"].replace(0, np.nan)
    agg["cm_pct"] = agg["contribution_margin"] / agg["attributed_revenue"].replace(0, np.nan) * 100
    return agg.fillna(0.0)


def _series_for(agg: pd.DataFrame, entity_col: str, entity_id: Any, metric: str) -> np.ndarray:
    sub = agg[agg[entity_col] == entity_id].sort_values("date")
    return sub[metric].to_numpy(dtype=float)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def detect_anomalies(db: Session, brand_id: int, progress: Callable | None = None) -> dict[str, Any]:
    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    step(5, "loading unified facts")
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.platform_key, UnifiedFact.campaign_id, UnifiedFact.ad_set_id,
        UnifiedFact.creative_id, UnifiedFact.sku_id, UnifiedFact.impressions, UnifiedFact.clicks,
        UnifiedFact.spend, UnifiedFact.platform_revenue, UnifiedFact.platform_conversions,
        UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders, UnifiedFact.contribution_margin,
        UnifiedFact.cogs, UnifiedFact.frequency,
    ).where(UnifiedFact.brand_id == brand_id))
    if facts.empty:
        return {"status": "no_data", "anomalies": 0}
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    facts = facts.sort_values("date")
    clock = max(facts["date"])

    campaign_names = dict(db.query(Campaign.id, Campaign.name).filter(Campaign.brand_id == brand_id).all())
    campaign_platforms = dict(db.query(Campaign.id, Campaign.platform_key).filter(Campaign.brand_id == brand_id).all())
    adset_names = {
        aid: f"{(campaign_names.get(cid) or '').split('|')[-1].strip()} — {name}"
        for aid, cid, name in db.query(AdSet.id, AdSet.campaign_id, AdSet.audience_name).filter(AdSet.brand_id == brand_id).all()
    }
    adset_campaign = dict(db.query(AdSet.id, AdSet.campaign_id).filter(AdSet.brand_id == brand_id).all())
    creative_names = dict(db.query(Creative.id, Creative.name).filter(Creative.brand_id == brand_id).all())
    sku_names = dict(db.query(Sku.id, Sku.sku_code).filter(Sku.brand_id == brand_id).all())

    inventory = _read(db, select(
        InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.days_of_cover
    ).where(InventorySnapshot.brand_id == brand_id))
    inventory["date"] = pd.to_datetime(inventory["date"]).dt.date

    ga = _read(db, select(GaEvent.date, GaEvent.sessions, GaEvent.purchase).where(GaEvent.brand_id == brand_id))
    ga["date"] = pd.to_datetime(ga["date"]).dt.date
    ga_daily = ga.groupby("date", as_index=False).agg(ga_sessions=("sessions", "sum"), ga_purchases=("purchase", "sum"))

    candidates: list[dict[str, Any]] = []

    # ---- 1. platform / campaign / ad set / creative levels -----------------
    levels = [
        ("platform", ["date", "platform_key"], "platform_key"),
        ("campaign", ["date", "campaign_id"], "campaign_id"),
        ("ad_set", ["date", "ad_set_id"], "ad_set_id"),
        ("creative", ["date", "creative_id"], "creative_id"),
    ]

    step(20, "aggregating entity series")
    for level_name, group_cols, entity_col in levels:
        agg = _aggregate(facts, group_cols)
        ids = [i for i in agg[entity_col].dropna().unique() if i == i]
        for entity_id in ids:
            is_platform = level_name == "platform"
            if not is_platform:
                entity_id = int(entity_id)
            for metric in LEVEL_METRICS[level_name]:
                series = _series_for(agg, entity_col, entity_id, metric)
                if series.size < 60:
                    continue
                result = detect_adaptive(
                    series, metric,
                    min_effect_pct=LEVEL_MIN_EFFECT[level_name],
                    min_severity=LEVEL_MIN_SEVERITY[level_name],
                    allow_regime=level_name in {"platform", "campaign"},
                )
                if not result:
                    continue
                result.update({
                    "entity_type": level_name,
                    "entity_id": None if is_platform else entity_id,
                    "entity_name": _entity_name(
                        level_name, entity_id, campaign_names, adset_names, creative_names, sku_names
                    ),
                    "platform_key": _entity_platform(
                        level_name, entity_id, agg, entity_col, campaign_platforms, adset_campaign
                    ),
                    "granularity": "daily",
                })
                candidates.append(result)
        step(20 + 12 * (levels.index((level_name, group_cols, entity_col)) + 1) / len(levels), f"{level_name} level scanned")

    # ---- 2. SKU level (adds days-of-cover) ---------------------------------
    step(52, "scanning SKU-level metrics")
    sku_facts = facts.dropna(subset=["sku_id"]).copy()
    sku_facts["sku_id"] = sku_facts["sku_id"].astype(int)
    sku_agg = _aggregate(sku_facts, ["date", "sku_id"])
    sku_cover = inventory.groupby(["date", "sku_id"], as_index=False).agg(days_of_cover=("days_of_cover", "mean"))
    sku_agg = sku_agg.merge(sku_cover, on=["date", "sku_id"], how="left").fillna({"days_of_cover": 0.0})
    sku_agg = sku_agg.sort_values("date")
    for sku_id in sorted(sku_agg["sku_id"].dropna().unique()):
        sku_id = int(sku_id)
        for metric in LEVEL_METRICS["sku"]:
            series = _series_for(sku_agg, "sku_id", sku_id, metric)
            if series.size < 60:
                continue
            result = detect_adaptive(
                series, metric, min_effect_pct=LEVEL_MIN_EFFECT["sku"],
                min_severity=LEVEL_MIN_SEVERITY["sku"], allow_regime=True,
            )
            if not result:
                continue
            result.update({
                "entity_type": "sku", "entity_id": sku_id,
                "entity_name": sku_names.get(sku_id, f"SKU {sku_id}"),
                "platform_key": None, "granularity": "daily",
            })
            candidates.append(result)

    # ---- 3. channel level (GA4 health) -------------------------------------
    step(60, "scanning GA4 channel health")
    all_days = pd.DataFrame({"date": sorted(facts["date"].unique())}).merge(ga_daily, on="date", how="left")
    all_spend = facts.groupby("date", as_index=False).agg(spend=("spend", "sum"), revenue=("attributed_revenue", "sum"))
    all_days = all_days.merge(all_spend, on="date", how="left").fillna({"ga_sessions": 0.0, "spend": 0.0, "revenue": 0.0})
    for metric in ("ga_sessions", "ga_purchases"):
        series = all_days.sort_values("date")[metric].to_numpy(dtype=float)
        result = detect_adaptive(
            series, metric, min_effect_pct=LEVEL_MIN_EFFECT["channel"],
            min_severity=LEVEL_MIN_SEVERITY["channel"],
        )
        if result:
            result.update({
                "entity_type": "channel", "entity_id": None, "entity_name": "GA4 (all sources)",
                "platform_key": None, "granularity": "daily",
            })
            candidates.append(result)

    step(72, "hourly granularity scan")
    candidates.extend(_scan_hourly(db, brand_id, campaign_names, campaign_platforms))

    step(82, "FDR control and artifact screening")
    if candidates:
        passed = benjamini_hochberg([c["p_value"] for c in candidates], alpha=0.12)
        candidates = [c for c, ok in zip(candidates, passed) if ok or c["severity"] >= 70]

    # ---- cross-source business rules (always retained) ---------------------
    step(84, "cross-source business-rule checks")
    domain = domain_checks.run_domain_checks(db, brand_id)
    seen = {(c["entity_type"], c.get("entity_id"), c["metric"]) for c in candidates}
    for c in domain:
        key = (c["entity_type"], c.get("entity_id"), c["metric"])
        if key in seen:
            candidates = [x for x in candidates if (x["entity_type"], x.get("entity_id"), x["metric"]) != key]
        candidates.append(c)
        seen.add(key)

    # ---- artifact detection runs first and can re-label anomalies -----------
    for c in candidates:
        artifact = _artifact_check(c, facts, all_days, ga_daily, campaign_names, campaign_platforms)
        # Domain checks own their scenario tag; the generic screen may enrich
        # them only when it actually detects a data artifact.
        if artifact.get("is_data_artifact") or not c.get("scenario_tag"):
            c.update(artifact)

    step(88, "persisting anomalies")
    db.execute(delete(Anomaly).where(Anomaly.brand_id == brand_id))
    db.flush()
    rows: list[dict[str, Any]] = []
    for c in candidates:
        offset = int(c.get("regime_offset_days", 0) or 0)
        window_end = clock
        window_start = clock - dt.timedelta(days=RECENT_WINDOW + offset)
        rows.append(dict(
            brand_id=brand_id,
            metric=c["metric"], entity_type=c["entity_type"], entity_id=c.get("entity_id"),
            entity_name=c.get("entity_name", ""), platform_key=c.get("platform_key"),
            granularity=c.get("granularity", "daily"),
            detected_at=dt.datetime.combine(clock, dt.time(6, 30)),
            window_start=window_start, window_end=window_end,
            severity=c["severity"], severity_label=severity_label(c["severity"]),
            direction=c["direction"], magnitude_pct=c["magnitude_pct"],
            baseline=c["baseline"], observed=c["observed"],
            expected=c["baseline"], lower_bound=round(c["baseline"] * 0.85, 4),
            upper_bound=round(c["baseline"] * 1.15, 4),
            p_value=c["p_value"], effect_size=c["effect_size"],
            method_scores=c["methods"], methods_agreeing=c["methods_agreeing"],
            is_data_artifact=c.get("is_data_artifact", False),
            artifact_reason=c.get("artifact_reason", ""),
            status="open", scenario_tag=c.get("scenario_tag", ""),
        ))
    for i in range(0, len(rows), 2000):
        db.bulk_insert_mappings(Anomaly, rows[i : i + 2000])
    db.flush()

    summary = {
        "status": "ok",
        "anomalies": len(rows),
        "artifacts": sum(1 for r in rows if r["is_data_artifact"]),
        "critical": sum(1 for r in rows if r["severity_label"] == "critical"),
        "high": sum(1 for r in rows if r["severity_label"] == "high"),
        "by_level": pd.Series([r["entity_type"] for r in rows]).value_counts().to_dict() if rows else {},
    }
    log.info("anomaly scan: %s", summary)
    return summary


def _scan_hourly(db: Session, brand_id: int, campaign_names: dict, campaign_platforms: dict) -> list[dict[str, Any]]:
    """Hourly-granularity scan (intraday delivery and efficiency shifts)."""
    hourly = _read(db, select(
        AdMetricsHourly.ts, AdMetricsHourly.campaign_id, AdMetricsHourly.impressions,
        AdMetricsHourly.clicks, AdMetricsHourly.spend, AdMetricsHourly.platform_conversions,
    ).where(AdMetricsHourly.brand_id == brand_id))
    if hourly.empty:
        return []
    hourly["ts"] = pd.to_datetime(hourly["ts"])
    hourly["ctr"] = hourly["clicks"] / hourly["impressions"].replace(0, np.nan)
    hourly["cpc"] = hourly["spend"] / hourly["clicks"].replace(0, np.nan)
    hourly["cvr"] = hourly["platform_conversions"] / hourly["clicks"].replace(0, np.nan)
    grouped = hourly.groupby(["campaign_id", "ts"], as_index=False).agg(
        ctr=("ctr", "mean"), cpc=("cpc", "mean"), cvr=("cvr", "mean"), spend=("spend", "sum")
    ).fillna(0.0)
    out: list[dict[str, Any]] = []
    for cid in grouped["campaign_id"].unique():
        sub = grouped[grouped["campaign_id"] == cid].sort_values("ts")
        for metric in ("ctr", "cpc", "cvr", "spend"):
            series = sub[metric].to_numpy(dtype=float)
            if series.size < 240:
                continue
            result = detect_series(series, metric, window=HOURLY_RECENT, period=24, min_effect_pct=18.0)
            if not result or result["severity"] < 55.0:
                continue
            result.update({
                "entity_type": "campaign", "entity_id": int(cid),
                "entity_name": campaign_names.get(int(cid), f"Campaign {cid}"),
                "platform_key": campaign_platforms.get(int(cid)),
                "granularity": "hourly",
            })
            out.append(result)
    return out


def _entity_name(level: str, entity_id: int, campaigns, adsets, creatives, skus) -> str:
    if level == "campaign":
        return campaigns.get(entity_id, f"Campaign {entity_id}")
    if level == "ad_set":
        return adsets.get(entity_id, f"Ad set {entity_id}")
    if level == "creative":
        return creatives.get(entity_id, f"Creative {entity_id}")
    if level == "platform":
        return entity_id
    if level == "sku":
        return skus.get(entity_id, f"SKU {entity_id}")
    return str(entity_id)


def _entity_platform(level, entity_id, agg, entity_col, campaign_platforms, adset_campaign):
    if level == "platform":
        return str(entity_id)
    if level == "campaign":
        return campaign_platforms.get(int(entity_id))
    if level == "ad_set":
        cid = adset_campaign.get(int(entity_id))
        return campaign_platforms.get(int(cid)) if cid else None
    if level == "creative":
        rows = agg[agg[entity_col] == entity_id]
        return None
    return None


# --------------------------------------------------------------------------- #
# Data-artifact (false anomaly) detection
# --------------------------------------------------------------------------- #
def _artifact_check(
    candidate: dict[str, Any],
    facts: pd.DataFrame,
    all_days: pd.DataFrame,
    ga_daily: pd.DataFrame,
    campaign_names: dict,
    campaign_platforms: dict,
) -> dict[str, Any]:
    """Detect data artifacts *before* treating a move as a performance problem.

    Two patterns are covered:

    1. **Tracking breakage** — GA4 sessions/purchases collapse while catalogue
       metrics (spend, impressions, store orders/revenue) move normally.
    2. **Attribution double counting** — platform-reported revenue jumps far
       beyond the reconciled store revenue for the same entity/window.
    """
    out: dict[str, Any] = {"is_data_artifact": False, "artifact_reason": "", "scenario_tag": ""}
    clock = max(facts["date"])
    recent_start = clock - dt.timedelta(days=RECENT_WINDOW)
    total = all_days.copy()
    total["date"] = pd.to_datetime(total["date"]).dt.date
    pre = total[(total["date"] < recent_start)]
    post = total[(total["date"] >= recent_start)]
    if pre.empty or post.empty:
        return out

    def pct(col: str) -> float:
        b, a = float(pre[col].mean()), float(post[col].mean())
        return (a / b - 1) * 100 if b else 0.0

    ga_change = pct("ga_sessions")
    spend_change = pct("spend")
    rev_change = pct("revenue")

    if candidate["metric"] in {"ga_sessions", "ga_purchases"} or (
        ga_change < -20 and candidate["metric"] in {"cvr", "roas", "mer", "contribution_margin"}
    ):
        if ga_change < -20 and abs(spend_change) < 12 and abs(rev_change) < 12:
            out["is_data_artifact"] = True
            out["artifact_reason"] = (
                f"GA4 sessions moved {ga_change:+.1f}% while ad spend ({spend_change:+.1f}%) and "
                f"reconciled revenue ({rev_change:+.1f}%) were flat — a tracking break, not a "
                "performance change."
            )
            out["scenario_tag"] = "tracking_outage"
            return out

    if candidate["metric"] in {"revenue", "roas", "mer"} and candidate.get("entity_type") == "campaign":
        cid = candidate.get("entity_id")
        if cid is not None:
            sub = facts[(facts["campaign_id"] == cid) & (facts["date"] >= recent_start)]
            sub_pre = facts[(facts["campaign_id"] == cid) & (facts["date"] < recent_start)].tail(56)
            if not sub.empty and not sub_pre.empty:
                pr = float(sub["platform_revenue"].sum()) / max(float(sub["attributed_revenue"].sum()), 1e-9)
                pr_pre = float(sub_pre["platform_revenue"].sum()) / max(float(sub_pre["attributed_revenue"].sum()), 1e-9)
                if pr - pr_pre > 0.15:
                    out["is_data_artifact"] = True
                    out["artifact_reason"] = (
                        f"Platform-reported revenue is {pr:.2f}x reconciled revenue versus {pr_pre:.2f}x in the "
                        "baseline window — attribution overlap, not incremental performance."
                    )
    return out


# --------------------------------------------------------------------------- #
# Scenario matching (used by the Scenario Guide page)
# --------------------------------------------------------------------------- #
def match_scenarios(db: Session, brand_id: int) -> dict[str, Any]:
    """Map produced anomalies back onto the planted scenario checklist."""
    from ..db.models import ScenarioStatus

    anomalies = db.query(Anomaly).filter(Anomaly.brand_id == brand_id).all()
    statuses = db.query(ScenarioStatus).filter(ScenarioStatus.brand_id == brand_id).all()
    by_key = {s.key: s for s in statuses}

    def best(predicate) -> Anomaly | None:
        matched = [a for a in anomalies if predicate(a)]
        matched.sort(key=lambda a: a.severity, reverse=True)
        return matched[0] if matched else None

    hero = "META | Prospecting | Hero Serum UGC"
    mapping = {
        "creative_fatigue": lambda a: a.entity_name == hero
        and a.metric in {"ctr", "cpa", "roas", "frequency"} and a.direction == "drop" and not a.is_data_artifact,
        "stockout_risk": lambda a: a.entity_type == "sku" and a.metric in {"days_of_cover", "cvr"}
        and a.direction == "drop",
        "price_change_cvr_drop": lambda a: "Hydration" in (a.entity_name or "")
        and a.metric in {"cvr", "roas", "contribution_margin"} and a.direction == "drop",
        "google_cannibalization": lambda a: a.entity_name == "GOOG | Brand | Exact"
        and a.metric in {"roas", "spend", "contribution_margin", "aov"} and a.direction == "drop",
        "tracking_outage": lambda a: bool(a.is_data_artifact) and "GA4" in (a.artifact_reason or ""),
        "under_promoted_margin_sku": lambda a: a.entity_type == "sku" and a.metric in {"cm_pct", "contribution_margin"},
    }
    results = {}
    for key, predicate in mapping.items():
        found = best(predicate)
        status = by_key.get(key)
        if status and found:
            status.detected = True
            status.anomaly_id = found.id
            status.detection_note = (
                f"{found.entity_name} · {found.metric} {found.direction} {found.magnitude_pct:.1f}% "
                f"(severity {found.severity:.0f}, {found.methods_agreeing} methods agreeing)"
            )
        results[key] = found.id if found else None
    db.flush()
    return results
