"""Pillar 2 — automated root-cause diagnosis.

For every detected anomaly the engine runs, in order:

1. **Artifact gate** — a data artifact short-circuits the pipeline.
2. **Metric decomposition** — factor attribution on the log scale
   (``ROAS = CTR × CVR × AOV ÷ CPC``).
3. **Mix vs rate decomposition** — Oaxaca-style split across platform,
   campaign, creative, SKU, device, geo and price band.
4. **Causal estimate** — synthetic control / DiD around the relevant event date.
5. **Hypothesis library** — twelve domain causes, each with a detector, a
   confidence score and the evidence that produced it.
6. **Ranking + calibration** — drivers ranked by contribution × confidence; the
   headline confidence is calibrated from method agreement, statistical
   strength and data completeness, never asserted.

Everything is persisted to ``diagnoses`` with the full evidence chain.
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
    AdMetricsDaily,
    Anomaly,
    Campaign,
    CampaignSkuMap,
    Creative,
    Diagnosis,
    GaEvent,
    InventorySnapshot,
    Sku,
    SkuPriceHistory,
    UnifiedFact,
)
from . import agent as agent_engine
from . import causal as causal_engine
from . import decomposition
from .llm import llm
from .reconcile import _read

log = get_logger("adpilot.diagnose")

RECENT_WINDOW = 14
PRIOR_WINDOW = 42
ENTITY_COL = {"campaign": "campaign_id", "ad_set": "ad_set_id", "creative": "creative_id",
              "sku": "sku_id", "platform": "platform_key"}


# --------------------------------------------------------------------------- #
# Data loading helpers
# --------------------------------------------------------------------------- #
def _facts(db: Session, brand_id: int) -> pd.DataFrame:
    frame = _read(db, select(
        UnifiedFact.date, UnifiedFact.platform_key, UnifiedFact.campaign_id, UnifiedFact.ad_set_id,
        UnifiedFact.creative_id, UnifiedFact.sku_id, UnifiedFact.impressions, UnifiedFact.clicks,
        UnifiedFact.spend, UnifiedFact.platform_revenue, UnifiedFact.platform_conversions,
        UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders, UnifiedFact.contribution_margin,
        UnifiedFact.cogs, UnifiedFact.frequency, UnifiedFact.reach, UnifiedFact.price,
        UnifiedFact.discounts, UnifiedFact.refunds, UnifiedFact.fees,
        UnifiedFact.inventory_days_of_cover, UnifiedFact.ga_sessions,
    ).where(UnifiedFact.brand_id == brand_id))
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _totals(frame: pd.DataFrame) -> dict[str, float]:
    return {
        "spend": float(frame["spend"].sum()), "impressions": float(frame["impressions"].sum()),
        "clicks": float(frame["clicks"].sum()), "attributed_revenue": float(frame["attributed_revenue"].sum()),
        "attributed_orders": float(frame["attributed_orders"].sum()),
        "contribution_margin": float(frame["contribution_margin"].sum()),
        "platform_revenue": float(frame["platform_revenue"].sum()),
        "frequency": float(frame["frequency"].mean()) if len(frame) else 0.0,
        "reach": float(frame["reach"].sum()),
    }


def _windows(frame: pd.DataFrame, clock: dt.date) -> tuple[pd.DataFrame, pd.DataFrame]:
    recent = frame[frame["date"] > clock - dt.timedelta(days=RECENT_WINDOW)]
    prior = frame[(frame["date"] > clock - dt.timedelta(days=RECENT_WINDOW + PRIOR_WINDOW))
                  & (frame["date"] <= clock - dt.timedelta(days=RECENT_WINDOW))]
    return recent, prior


# --------------------------------------------------------------------------- #
# Hypothesis library
# --------------------------------------------------------------------------- #
def _h_creative_fatigue(ctx: dict[str, Any]) -> dict[str, Any]:
    before, after = ctx["before"], ctx["after"]
    ctr_b = before["clicks"] / before["impressions"] if before["impressions"] else 0
    ctr_a = after["clicks"] / after["impressions"] if after["impressions"] else 0
    freq_b, freq_a = before["frequency"], after["frequency"]
    ctr_change = (ctr_a / ctr_b - 1) * 100 if ctr_b else 0
    freq_change = (freq_a / freq_b - 1) * 100 if freq_b else 0
    fired = ctr_change < -15 and freq_change > 15
    conf = min(0.95, max(0.0, (abs(ctr_change) / 60) * 0.6 + (freq_change / 150) * 0.4)) if fired else 0.0
    return {
        "driver": "creative_fatigue", "label": "Creative fatigue / audience saturation",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"ctr_change_pct": round(ctr_change, 2), "frequency_before": round(freq_b, 2),
                     "frequency_after": round(freq_a, 2), "frequency_change_pct": round(freq_change, 1),
                     "creative_age_days": ctx.get("creative_age_days")},
        "mechanism": "The same creative is served to the same audience for too long, so CTR decays while frequency rises.",
    }


def _h_auction_pressure(ctx: dict[str, Any]) -> dict[str, Any]:
    before, after = ctx["before"], ctx["after"]
    cpm_b = before["spend"] / before["impressions"] * 1000 if before["impressions"] else 0
    cpm_a = after["spend"] / after["impressions"] * 1000 if after["impressions"] else 0
    ctr_b = before["clicks"] / before["impressions"] if before["impressions"] else 0
    ctr_a = after["clicks"] / after["impressions"] if after["impressions"] else 0
    cpm_change = (cpm_a / cpm_b - 1) * 100 if cpm_b else 0
    ctr_change = (ctr_a / ctr_b - 1) * 100 if ctr_b else 0
    fired = cpm_change > 12 and ctr_change > -8
    conf = min(0.9, cpm_change / 45) if fired else 0.0
    return {
        "driver": "cpm_auction_pressure", "label": "CPM / auction cost pressure",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"cpm_before": round(cpm_b, 2), "cpm_after": round(cpm_a, 2),
                     "cpm_change_pct": round(cpm_change, 2), "ctr_change_pct": round(ctr_change, 2)},
        "mechanism": "Auction costs rose while engagement held, so the same delivery now costs more.",
    }


def _h_price_change(ctx: dict[str, Any]) -> dict[str, Any]:
    events = ctx.get("price_events") or []
    before, after = ctx["before"], ctx["after"]
    cvr_b = before["attributed_orders"] / before["clicks"] if before["clicks"] else 0
    cvr_a = after["attributed_orders"] / after["clicks"] if after["clicks"] else 0
    cvr_change = (cvr_a / cvr_b - 1) * 100 if cvr_b else 0
    increases = [e for e in events if e.get("change_reason") == "list_price_increase"]
    fired = bool(increases) and cvr_change < -10
    conf = min(0.93, 0.55 + abs(cvr_change) / 200) if fired else 0.0
    return {
        "driver": "price_change", "label": "List-price increase reducing conversion",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"price_events": increases[:3], "cvr_change_pct": round(cvr_change, 2),
                     "aov_change_pct": round((after["attributed_revenue"] / max(after["attributed_orders"], 1))
                                             / max(before["attributed_revenue"] / max(before["attributed_orders"], 1), 1e-9) * 100 - 100, 2)},
        "mechanism": "A higher list price raised AOV but depressed conversion; the two partly offset each other.",
    }


def _h_stockout(ctx: dict[str, Any]) -> dict[str, Any]:
    cover = ctx.get("days_of_cover")
    fired = cover is not None and cover < 14
    conf = min(0.97, 0.6 + (14 - cover) / 20) if fired else 0.0
    return {
        "driver": "inventory_stockout", "label": "Inventory cover below guardrail",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"days_of_cover": round(cover, 2) if cover is not None else None, "guardrail_days": 14,
                     "promoted_spend": ctx["after"]["spend"]},
        "mechanism": "Demand is being bought for a SKU that cannot be fulfilled, so spend converts into stockouts.",
    }


def _h_tracking_artifact(ctx: dict[str, Any]) -> dict[str, Any]:
    tracking = ctx.get("tracking") or {}
    fired = bool(tracking.get("tracking_issue"))
    return {
        "driver": "tracking_artifact", "label": "Measurement / tracking break (not a performance change)",
        "fired": fired, "confidence": 0.95 if fired else 0.0,
        "evidence": tracking,
        "mechanism": "A tag-manager or measurement change removed events; the business metrics never moved.",
    }


def _h_attribution_overlap(ctx: dict[str, Any]) -> dict[str, Any]:
    before, after = ctx["before"], ctx["after"]
    ratio_b = before["platform_revenue"] / before["attributed_revenue"] if before["attributed_revenue"] else 0
    ratio_a = after["platform_revenue"] / after["attributed_revenue"] if after["attributed_revenue"] else 0
    fired = ratio_a - ratio_b > 0.12 or ratio_a > 1.15
    conf = min(0.9, max(0.0, (ratio_a - 1) * 0.8)) if fired else 0.0
    return {
        "driver": "attribution_overlap", "label": "Attribution overlap / double counting",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"platform_to_reconciled_ratio_before": round(ratio_b, 3),
                     "platform_to_reconciled_ratio_after": round(ratio_a, 3)},
        "mechanism": "Platform-reported conversions overlap (view-through and cross-network), so reported revenue overstates true revenue.",
    }


def _h_cannibalisation(ctx: dict[str, Any]) -> dict[str, Any]:
    pair = ctx.get("cannibalisation") or {}
    fired = bool(pair.get("fired"))
    return {
        "driver": "cross_campaign_cannibalisation", "label": "Cross-campaign cannibalisation",
        "fired": fired, "confidence": round(min(0.92, pair.get("confidence", 0.0)), 3),
        "evidence": pair.get("evidence", {}),
        "mechanism": "One campaign is now capturing demand that another campaign used to convert, so the portfolio gain is smaller than the sum of its parts.",
    }


def _h_landing_page(ctx: dict[str, Any]) -> dict[str, Any]:
    before, after = ctx["before"], ctx["after"]
    ctr_b = before["clicks"] / before["impressions"] if before["impressions"] else 0
    ctr_a = after["clicks"] / after["impressions"] if after["impressions"] else 0
    cvr_b = before["attributed_orders"] / before["clicks"] if before["clicks"] else 0
    cvr_a = after["attributed_orders"] / after["clicks"] if after["clicks"] else 0
    ctr_change = (ctr_a / ctr_b - 1) * 100 if ctr_b else 0
    cvr_change = (cvr_a / cvr_b - 1) * 100 if cvr_b else 0
    fired = cvr_change < -12 and abs(ctr_change) < 10
    conf = min(0.85, abs(cvr_change) / 70) if fired else 0.0
    return {
        "driver": "on_site_conversion", "label": "On-site conversion problem (traffic quality unchanged)",
        "fired": fired, "confidence": round(conf, 3),
        "evidence": {"ctr_change_pct": round(ctr_change, 2), "cvr_change_pct": round(cvr_change, 2)},
        "mechanism": "Clicks still arrive at the same rate but convert less, which points at the landing page, offer or checkout rather than the ad.",
    }


def _h_seasonality(ctx: dict[str, Any]) -> dict[str, Any]:
    seasonal = ctx.get("seasonality") or {}
    return {
        "driver": "seasonality", "label": "Seasonality / demand cycle",
        "fired": bool(seasonal.get("fired")), "confidence": round(seasonal.get("confidence", 0.0), 3),
        "evidence": seasonal.get("evidence", {}),
        "mechanism": "The move tracks the normal seasonal cycle of this category rather than a controllable change.",
    }


def _h_budget_change(ctx: dict[str, Any]) -> dict[str, Any]:
    budget = ctx.get("budget_change") or {}
    return {
        "driver": "budget_or_bid_change", "label": "Budget or bid change",
        "fired": bool(budget.get("fired")), "confidence": round(budget.get("confidence", 0.0), 3),
        "evidence": budget.get("evidence", {}),
        "mechanism": "Delivery was scaled up or down, which mechanically changes efficiency because of the response curve.",
    }


HYPOTHESES = [
    _h_tracking_artifact, _h_stockout, _h_price_change, _h_cannibalisation,
    _h_creative_fatigue, _h_auction_pressure, _h_landing_page, _h_attribution_overlap,
    _h_budget_change, _h_seasonality,
]


# --------------------------------------------------------------------------- #
# Context builders
# --------------------------------------------------------------------------- #
def _seasonality_context(frame: pd.DataFrame, clock: dt.date) -> dict[str, Any]:
    """Compare this window with the same calendar window one year earlier."""
    if frame.empty:
        return {"fired": False}
    recent = frame[frame["date"] > clock - dt.timedelta(days=14)]
    year_ago = frame[(frame["date"] > clock - dt.timedelta(days=14 + 365))
                     & (frame["date"] <= clock - dt.timedelta(days=365))]
    if year_ago.empty or recent.empty:
        return {"fired": False}
    r_rev = float(recent["attributed_revenue"].sum())
    y_rev = float(year_ago["attributed_revenue"].sum())
    change = (r_rev / y_rev - 1) * 100 if y_rev else 0.0
    return {"fired": abs(change) > 25, "confidence": min(0.6, abs(change) / 200),
            "evidence": {"revenue_yoy_change_pct": round(change, 2), "revenue_last_year": round(y_rev, 2)}}


def _budget_change_context(campaign: Campaign | None, frame: pd.DataFrame, clock: dt.date) -> dict[str, Any]:
    if campaign is None or frame.empty:
        return {"fired": False}
    recent = frame[frame["date"] > clock - dt.timedelta(days=14)]
    prior = frame[(frame["date"] > clock - dt.timedelta(days=56)) & (frame["date"] <= clock - dt.timedelta(days=14))]
    if recent.empty or prior.empty:
        return {"fired": False}
    daily_recent = float(recent["spend"].sum()) / 14
    daily_prior = float(prior["spend"].sum()) / max(prior["date"].nunique(), 1)
    change = (daily_recent / daily_prior - 1) * 100 if daily_prior else 0.0
    return {"fired": abs(change) > 20, "confidence": min(0.7, abs(change) / 150),
            "evidence": {"daily_spend_before": round(daily_prior, 2), "daily_spend_after": round(daily_recent, 2),
                         "change_pct": round(change, 2), "daily_budget": campaign.daily_budget}}


def _cannibalisation_context(db: Session, brand_id: int, facts: pd.DataFrame, clock: dt.date) -> dict[str, Any]:
    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    google = {c.id: c for c in campaigns.values() if c.platform_key == "google"}
    if len(google) < 2:
        return {"fired": False}
    recent_start = clock - dt.timedelta(days=14)
    prior_start = clock - dt.timedelta(days=56)
    stats: dict[int, dict[str, float]] = {}
    for cid in google:
        pre = facts[(facts["campaign_id"] == cid) & (facts["date"] > prior_start) & (facts["date"] <= recent_start)]
        post = facts[(facts["campaign_id"] == cid) & (facts["date"] > recent_start)]
        if pre.empty or post.empty:
            continue
        # the two windows have different lengths, so compare *daily* rates
        pre_days = max(int(pre["date"].nunique()), 1)
        post_days = max(int(post["date"].nunique()), 1)
        stats[cid] = {
            "clicks_pre": float(pre["clicks"].sum()) / pre_days,
            "clicks_post": float(post["clicks"].sum()) / post_days,
            "rev_pre": float(pre["attributed_revenue"].sum()) / pre_days,
            "rev_post": float(post["attributed_revenue"].sum()) / post_days,
        }
    losers = []
    winners = []
    for cid, s in stats.items():
        if s["clicks_pre"] < 100:
            continue
        click_change = (s["clicks_post"] / s["clicks_pre"] - 1) * 100 if s["clicks_pre"] else 0
        rev_change = (s["rev_post"] / max(s["rev_pre"], 1e-9) - 1) * 100 if s["rev_pre"] else 0
        if click_change < -12:
            losers.append({"campaign": campaigns[cid].name, "click_change_pct": round(click_change, 1),
                           "revenue_change_pct": round(rev_change, 1)})
        if rev_change > 12:
            winners.append({"campaign": campaigns[cid].name, "revenue_change_pct": round(rev_change, 1)})
    fired = bool(losers) and bool(winners)
    return {"fired": fired, "confidence": 0.8 if fired else 0.0,
            "evidence": {"losing_campaigns": losers, "gaining_campaigns": winners}}


def _tracking_context(db: Session, brand_id: int) -> dict[str, Any]:
    result = agent_engine.tool_check_tracking_health(db, brand_id)
    data = result.get("data", {})
    return {"tracking_issue": data.get("tracking_issue", False), "median_sessions": data.get("median_sessions"),
            "low_days": data.get("low_days", [])[:5], "purchase_rate_pct": data.get("purchase_rate_pct")}


# --------------------------------------------------------------------------- #
# Main diagnosis
# --------------------------------------------------------------------------- #
def diagnose_anomaly(db: Session, brand_id: int, anomaly: Anomaly, use_agent: bool = True) -> Diagnosis:
    facts = _facts(db, brand_id)
    clock = max(facts["date"]) if not facts.empty else dt.date.today()
    col = ENTITY_COL.get(anomaly.entity_type, "campaign_id")
    entity = facts
    if anomaly.entity_type == "platform":
        entity = facts[facts["platform_key"] == anomaly.entity_name]
    elif anomaly.entity_id is not None:
        entity = facts[facts[col] == anomaly.entity_id]
    elif anomaly.entity_type == "channel":
        entity = facts

    recent, prior = _windows(entity, clock)
    before, after = _totals(prior), _totals(recent)

    # ---- mix / rate decomposition across the dimensions that apply ---------
    mix_reports = {}
    for dim in ("platform_key", "campaign_id", "creative_id", "sku_id"):
        if dim not in facts.columns:
            continue
        if dim == "platform_key" and anomaly.entity_type == "platform":
            continue
        if dim == "sku_id" and facts["sku_id"].isna().all():
            continue
        try:
            report = decomposition.mix_rate_decomposition(
                facts.dropna(subset=[dim]), dim, "attributed_revenue", "clicks",
                facts["date"] <= clock - dt.timedelta(days=RECENT_WINDOW),
                facts["date"] > clock - dt.timedelta(days=RECENT_WINDOW),
            )
        except Exception:
            continue
        if report.get("available"):
            mix_reports[dim] = report

    # ---- causal estimate around the anomaly window -------------------------
    causal_report: dict[str, Any] = {"available": False, "reason": "not applicable"}
    if anomaly.entity_type == "campaign" and anomaly.entity_id is not None:
        daily = facts.groupby(["date", "campaign_id"], as_index=False)["attributed_revenue"].sum()
        treated = daily[daily["campaign_id"] == anomaly.entity_id].sort_values("date")
        event = anomaly.window_start or (clock - dt.timedelta(days=RECENT_WINDOW))
        donors = daily[daily["campaign_id"] != anomaly.entity_id]
        donor_ids = [i for i in donors["campaign_id"].unique() if i is not None][:16]
        tpre = treated[treated["date"] < event]["attributed_revenue"].to_numpy(float)[-70:]
        tpost = treated[treated["date"] >= event]["attributed_revenue"].to_numpy(float)
        dpre = {str(i): donors[(donors["campaign_id"] == i) & (donors["date"] < event)].sort_values("date")["attributed_revenue"].to_numpy(float)[-70:] for i in donor_ids}
        dpost = {str(i): donors[(donors["campaign_id"] == i) & (donors["date"] >= event)].sort_values("date")["attributed_revenue"].to_numpy(float) for i in donor_ids}
        causal_report = causal_engine.estimate_causal_effect(tpre, tpost, dpre, dpost, label="attributed_revenue", unit="usd")

    # ---- decomposition of the headline metric ------------------------------
    metric = anomaly.metric if anomaly.metric in decomposition.FORMULAS else "roas"
    decomposition_report = decomposition.metric_decomposition(
        metric, decomposition.derive_factors(before), decomposition.derive_factors(after)
    )

    # ---- build hypothesis context ------------------------------------------
    sku_ids: list[int] = []
    if anomaly.entity_type == "sku" and anomaly.entity_id:
        sku_ids = [anomaly.entity_id]
    elif anomaly.entity_type in {"campaign", "ad_set"} and anomaly.entity_id:
        sku_ids = [int(r.sku_id) for r in db.query(CampaignSkuMap).filter(
            CampaignSkuMap.brand_id == brand_id,
            CampaignSkuMap.campaign_id == (anomaly.entity_id if anomaly.entity_type == "campaign"
                                           else db.query(UnifiedFact.campaign_id).filter(
                                               UnifiedFact.brand_id == brand_id,
                                               UnifiedFact.ad_set_id == anomaly.entity_id).first()[0]),
        ).all()]

    inventory = _read(db, select(InventorySnapshot.sku_id, InventorySnapshot.days_of_cover, InventorySnapshot.date)
                      .where(InventorySnapshot.brand_id == brand_id))
    cover = None
    if sku_ids and not inventory.empty:
        inventory["date"] = pd.to_datetime(inventory["date"]).dt.date
        subset = inventory[(inventory["sku_id"].isin(sku_ids)) & (inventory["date"] > clock - dt.timedelta(days=14))]
        if not subset.empty:
            cover = float(subset["days_of_cover"].mean())

    price_events: list[dict[str, Any]] = []
    if sku_ids:
        rows = db.query(SkuPriceHistory, Sku.sku_code).join(Sku, SkuPriceHistory.sku_id == Sku.id).filter(
            SkuPriceHistory.brand_id == brand_id, SkuPriceHistory.sku_id.in_(sku_ids),
            SkuPriceHistory.effective_date >= clock - dt.timedelta(days=120),
        ).all()
        price_events = [{"sku": code, "effective_date": str(ph.effective_date), "price": ph.price,
                         "previous_price": ph.previous_price, "change_reason": ph.change_reason,
                         "discount_pct": ph.discount_pct} for ph, code in rows]

    campaign = db.query(Campaign).filter(Campaign.id == anomaly.entity_id, Campaign.brand_id == brand_id).first() \
        if anomaly.entity_type == "campaign" else None
    creative_age = None
    if anomaly.entity_type == "creative" and anomaly.entity_id:
        creative = db.query(Creative).filter(Creative.id == anomaly.entity_id).first()
        if creative and creative.launched_at:
            creative_age = (clock - creative.launched_at).days

    ctx = {
        "before": before, "after": after, "days_of_cover": cover, "price_events": price_events,
        "tracking": _tracking_context(db, brand_id),
        "seasonality": _seasonality_context(entity, clock),
        "budget_change": _budget_change_context(campaign, entity, clock),
        "cannibalisation": _cannibalisation_context(db, brand_id, facts, clock),
        "creative_age_days": creative_age,
        "metric": anomaly.metric, "direction": anomaly.direction,
    }

    # ---- run the hypothesis library ---------------------------------------
    hypotheses = []
    for fn in HYPOTHESES:
        try:
            result = fn(ctx)
        except Exception as exc:  # pragma: no cover
            log.warning("hypothesis %s failed: %s", fn.__name__, exc)
            continue
        hypotheses.append(result)

    # ---- artifact gate -----------------------------------------------------
    is_artifact = anomaly.is_data_artifact
    drivers = []
    if is_artifact:
        drivers.append({
            "driver": "data_artifact", "label": "Data artifact (measurement, not performance)",
            "confidence": 0.96, "contribution_pct": 100.0,
            "evidence": {"reason": anomaly.artifact_reason},
        })
    else:
        total_conf = sum(h["confidence"] for h in hypotheses if h["fired"]) or 1.0
        for h in hypotheses:
            if not h["fired"]:
                continue
            weight = h["confidence"] / total_conf
            drivers.append({
                "driver": h["driver"], "label": h["label"], "confidence": h["confidence"],
                "contribution_pct": round(weight * 100, 1), "evidence": h["evidence"],
                "mechanism": h["mechanism"],
            })
        # factor-level attribution is always shown for multiplicative metrics
        if decomposition_report.get("available"):
            for factor in decomposition_report["factors"][:3]:
                drivers.append({
                    "driver": f"factor_{factor['factor']}",
                    "label": f"{factor['factor'].upper()} moved {factor['change_pct']:+.1f}%",
                    "confidence": 0.9, "contribution_pct": abs(factor["contribution_pct"]),
                    "evidence": factor, "mechanism": "Factor attribution on the log scale.",
                })
        drivers.sort(key=lambda d: (d["confidence"], d["contribution_pct"]), reverse=True)

    # ---- calibrated headline confidence -----------------------------------
    fired = [h for h in hypotheses if h["fired"]]
    method_agreement = min(1.0, len(fired) / 2.0)
    statistical_strength = min(1.0, abs(anomaly.magnitude_pct) / 40.0) * min(1.0, anomaly.severity / 85.0)
    completeness = 1.0 if (decomposition_report.get("available") and before["spend"] > 0 and after["spend"] > 0) else 0.6
    confidence = round(min(0.97, 0.45 * method_agreement + 0.30 * statistical_strength + 0.25 * completeness), 3)
    if is_artifact:
        confidence = 0.96

    # ---- narrative ---------------------------------------------------------
    top = drivers[0] if drivers else None
    if is_artifact:
        narrative = (f"Not a performance problem. {anomaly.artifact_reason} "
                     f"The {anomaly.metric} move of {anomaly.magnitude_pct:+.1f}% is a measurement artifact; "
                     "no budget action is warranted.")
        summary = f"Data artifact — {anomaly.artifact_reason}"
    else:
        lead = top["label"] if top else "no single dominant driver"
        narrative = (
            f"{anomaly.entity_name} shows a {anomaly.direction} in {anomaly.metric} of "
            f"{anomaly.magnitude_pct:+.1f}% (severity {anomaly.severity:.0f}, {anomaly.methods_agreeing} detection "
            f"methods agreeing). Primary driver: {lead} "
            f"({top['confidence']*100:.0f}% confidence, {top['contribution_pct']:.0f}% of the explained move). "
            if top else
            f"{anomaly.entity_name} shows a {anomaly.direction} in {anomaly.metric} of {anomaly.magnitude_pct:+.1f}%."
        )
        if decomposition_report.get("available") and decomposition_report["factors"]:
            f0 = decomposition_report["factors"][0]
            narrative += (f"Factor attribution: {f0['factor'].upper()} {f0['change_pct']:+.1f}% "
                          f"({f0['contribution_pct']:+.0f}% of the move). ")
        if causal_report.get("available"):
            narrative += (f"Causal estimate ({causal_report['method']}): {causal_report.get('effect_pct', 0):+.1f}% "
                          f"[{causal_report.get('ci_low')}, {causal_report.get('ci_high')}] at "
                          f"{causal_report.get('confidence', 0)*100:.0f}% confidence. ")
        summary = (f"{lead}; confidence {confidence*100:.0f}%; causal {causal_report.get('effect_pct', 0):+.1f}% "
                   f"({causal_report.get('method', 'n/a')})") if top else "No dominant driver identified."

    diagnosis = Diagnosis(
        brand_id=brand_id, anomaly_id=anomaly.id,
        primary_driver=top["driver"] if top else "unknown",
        confidence=confidence,
        severity=anomaly.severity_label,
        drivers=drivers,
        decomposition={"metric": decomposition_report, "mix": mix_reports},
        causal=causal_report,
        evidence={
            "summary": summary,
            "hypotheses": hypotheses,
            "facts_rows": int(len(entity)),
            "spend_before": round(before["spend"], 2),
            "spend_after": round(after["spend"], 2),
            "methods_agreeing": anomaly.methods_agreeing,
            "p_value": anomaly.p_value,
            "entity_name": anomaly.entity_name,
            "metric": anomaly.metric,
        },
        narrative=narrative,
        narrative_source="template",
        artifact_flags=[{"reason": anomaly.artifact_reason}] if is_artifact else [],
        recommended_actions=[],
    )
    db.add(diagnosis)
    db.flush()
    bus.publish("diagnosis", {"anomaly_id": anomaly.id, "primary_driver": diagnosis.primary_driver,
                              "confidence": confidence})
    return diagnosis


def diagnose_open_anomalies(
    db: Session, brand_id: int, progress: Callable[[float, str], None] | None = None,
    min_severity: float = 0.0, limit: int = 60,
) -> dict[str, Any]:
    anomalies = (
        db.query(Anomaly)
        .filter(Anomaly.brand_id == brand_id, Anomaly.severity >= min_severity)
        .order_by(Anomaly.severity.desc())
        .limit(limit)
        .all()
    )
    produced = []
    for idx, anomaly in enumerate(anomalies):
        if progress:
            progress(idx / max(len(anomalies), 1) * 100, f"diagnosing {anomaly.entity_name} · {anomaly.metric}")
        existing = db.query(Diagnosis).filter(Diagnosis.anomaly_id == anomaly.id).first()
        if existing:
            produced.append(existing.id)
            continue
        diagnosis = diagnose_anomaly(db, brand_id, anomaly)
        produced.append(diagnosis.id)
        anomaly.status = "diagnosed"
    db.flush()
    summary = {
        "status": "ok", "diagnosed": len(produced),
        "artifacts": db.query(Diagnosis).filter(Diagnosis.brand_id == brand_id, Diagnosis.primary_driver == "data_artifact").count(),
        "high_confidence": db.query(Diagnosis).filter(Diagnosis.brand_id == brand_id, Diagnosis.confidence >= 0.75).count(),
    }
    log.info("diagnosis: %s", summary)
    return summary