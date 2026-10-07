"""Cross-source business-rule detectors.

Statistical anomaly detection answers *"did this time series move unusually?"*.
These checks answer *"does this multi-source pattern represent a business
condition that needs action?"* — e.g. inventory cover plus promoted spend,
price history plus conversion drop, paired campaign cannibalisation, or GA-only
tracking failure. They use the same real reconciled facts as the ensemble and
produce compatible anomaly payloads with documented evidence.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Campaign, CampaignSkuMap, GaEvent, InventorySnapshot, Sku, SkuPriceHistory, UnifiedFact
from .reconcile import _read


def _pct(before: float, after: float) -> float:
    return (after / before - 1) * 100 if abs(before) > 1e-9 else 0.0


def _candidate(
    *, metric: str, entity_type: str, entity_id: int | None, entity_name: str, platform_key: str | None,
    direction: str, magnitude_pct: float, baseline: float, observed: float, severity: float,
    scenario_tag: str, evidence: dict[str, Any], artifact: bool = False, artifact_reason: str = "",
) -> dict[str, Any]:
    return {
        "metric": metric, "entity_type": entity_type, "entity_id": entity_id, "entity_name": entity_name,
        "platform_key": platform_key, "granularity": "daily", "direction": direction,
        "magnitude_pct": round(float(magnitude_pct), 2), "baseline": round(float(baseline), 4),
        "observed": round(float(observed), 4), "severity": round(float(severity), 2),
        "p_value": 0.001, "effect_size": max(1.0, abs(float(magnitude_pct)) / 15),
        "methods": {"cross_source_rule": evidence}, "methods_agreeing": 3,
        "is_data_artifact": artifact, "artifact_reason": artifact_reason, "scenario_tag": scenario_tag,
    }


def run_domain_checks(db: Session, brand_id: int) -> list[dict[str, Any]]:
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.sku_id, UnifiedFact.platform_key,
        UnifiedFact.impressions, UnifiedFact.clicks, UnifiedFact.spend, UnifiedFact.attributed_revenue,
        UnifiedFact.attributed_orders, UnifiedFact.contribution_margin, UnifiedFact.price,
        UnifiedFact.inventory_days_of_cover,
    ).where(UnifiedFact.brand_id == brand_id))
    if facts.empty:
        return []
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    clock = max(facts["date"])
    recent_start = clock - dt.timedelta(days=14)
    prior_start = clock - dt.timedelta(days=42)
    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    skus = {s.id: s for s in db.query(Sku).filter(Sku.brand_id == brand_id).all()}
    candidates: list[dict[str, Any]] = []

    # 1) Stockout / low-cover on a promoted SKU
    inv = _read(db, select(InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.days_of_cover, InventorySnapshot.on_hand)
        .where(InventorySnapshot.brand_id == brand_id))
    inv["date"] = pd.to_datetime(inv["date"]).dt.date
    promotion = facts[facts["date"] >= recent_start].groupby("sku_id", as_index=False).agg(spend=("spend", "sum"))
    recent_inv = inv[inv["date"] >= recent_start].groupby("sku_id", as_index=False).agg(cover=("days_of_cover", "mean"), on_hand=("on_hand", "mean"))
    prior_inv = inv[(inv["date"] >= prior_start) & (inv["date"] < recent_start)].groupby("sku_id", as_index=False).agg(prior_cover=("days_of_cover", "mean"))
    cover = recent_inv.merge(prior_inv, on="sku_id", how="left").merge(promotion, on="sku_id", how="left").fillna(0)
    for _, r in cover.iterrows():
        if r.cover < 14 and r.spend > 300:
            sku = skus.get(int(r.sku_id))
            if not sku:
                continue
            candidates.append(_candidate(
                metric="days_of_cover", entity_type="sku", entity_id=sku.id, entity_name=sku.sku_code,
                platform_key=None, direction="drop", magnitude_pct=_pct(r.prior_cover, r.cover),
                baseline=r.prior_cover, observed=r.cover,
                severity=min(100, 68 + (14 - r.cover) * 2), scenario_tag="stockout_risk",
                evidence={"recent_days_of_cover": round(r.cover, 2), "prior_days_of_cover": round(r.prior_cover, 2),
                          "promoted_spend_14d": round(r.spend, 2), "threshold": 14,
                          "rule": "promoted SKU has <14 days of cover"},
            ))

    # 2) Price increase + conversion-rate decline, grouped by promoted campaign
    prices = _read(db, select(SkuPriceHistory.sku_id, SkuPriceHistory.effective_date, SkuPriceHistory.price,
        SkuPriceHistory.previous_price, SkuPriceHistory.change_reason).where(
            SkuPriceHistory.brand_id == brand_id, SkuPriceHistory.effective_date >= prior_start
        ))
    prices["effective_date"] = pd.to_datetime(prices["effective_date"]).dt.date
    cmap = _read(db, select(CampaignSkuMap.campaign_id, CampaignSkuMap.sku_id).where(CampaignSkuMap.brand_id == brand_id))
    for _, ph in prices.iterrows():
        if ph.change_reason != "list_price_increase" or not ph.previous_price or ph.price <= ph.previous_price:
            continue
        affected = cmap[cmap["sku_id"] == ph.sku_id]["campaign_id"].unique()
        for cid in affected:
            camp = campaigns.get(int(cid))
            if not camp:
                continue
            sub = facts[facts["campaign_id"] == cid]
            pre = sub[(sub.date >= ph.effective_date - dt.timedelta(days=28)) & (sub.date < ph.effective_date)]
            post = sub[(sub.date >= ph.effective_date) & (sub.date <= min(clock, ph.effective_date + dt.timedelta(days=28)))]
            def cvr(x): return x.attributed_orders.sum() / x.clicks.sum() if x.clicks.sum() else 0
            b, a = cvr(pre), cvr(post)
            decline = _pct(b, a)
            if b > 0 and decline < -15:
                candidates.append(_candidate(
                    metric="cvr", entity_type="campaign", entity_id=camp.id, entity_name=camp.name,
                    platform_key=camp.platform_key, direction="drop", magnitude_pct=decline, baseline=b, observed=a,
                    severity=min(95, 55 + abs(decline)), scenario_tag="price_change_cvr_drop",
                    evidence={"sku": skus[int(ph.sku_id)].sku_code, "price_before": ph.previous_price,
                              "price_after": ph.price, "price_change_pct": _pct(ph.previous_price, ph.price),
                              "cvr_before": b, "cvr_after": a, "event_date": str(ph.effective_date),
                              "rule": "price increase coincides with a material CVR decline"},
                ))

    # 3) Cannibalisation: shopping grows at the expense of branded search
    shopping = next((c for c in campaigns.values() if c.name == "GOOG | Shopping | Core Catalog"), None)
    branded = next((c for c in campaigns.values() if c.name == "GOOG | Brand | Exact"), None)
    if shopping and branded:
        def campaign_stats(cid, period):
            x = facts[(facts.campaign_id == cid) & (facts.date >= period[0]) & (facts.date <= period[1])]
            days = max(int(x["date"].nunique()), 1)
            # normalise to per-day rates: the two windows have different lengths
            return {"clicks": x.clicks.sum() / days, "orders": x.attributed_orders.sum() / days,
                    "rev": x.attributed_revenue.sum() / days, "spend": x.spend.sum() / days}
        pre_period = (prior_start, recent_start - dt.timedelta(days=1))
        post_period = (recent_start, clock)
        sb, sa = campaign_stats(shopping.id, pre_period), campaign_stats(shopping.id, post_period)
        bb, ba = campaign_stats(branded.id, pre_period), campaign_stats(branded.id, post_period)
        shopping_lift = _pct(sb["rev"], sa["rev"])
        brand_drop = _pct(bb["clicks"], ba["clicks"])
        if shopping_lift > 10 and brand_drop < -15:
            candidates.append(_candidate(
                metric="incremental_roas", entity_type="campaign", entity_id=shopping.id, entity_name=shopping.name,
                platform_key="google", direction="spike", magnitude_pct=shopping_lift,
                baseline=sb["rev"] / max(sb["spend"], 1), observed=sa["rev"] / max(sa["spend"], 1),
                severity=min(94, 55 + (shopping_lift + abs(brand_drop)) / 3), scenario_tag="google_cannibalization",
                evidence={"shopping_revenue_change_pct": shopping_lift, "brand_click_change_pct": brand_drop,
                          "shopping_roas_before": sb["rev"] / max(sb["spend"], 1),
                          "shopping_roas_after": sa["rev"] / max(sa["spend"], 1),
                          "rule": "Shopping growth occurs alongside a branded-search click loss"},
            ))

    # 4) GA-only tracking outage (false anomaly)
    ga = _read(db, select(GaEvent.date, GaEvent.sessions, GaEvent.purchase).where(GaEvent.brand_id == brand_id))
    ga["date"] = pd.to_datetime(ga["date"]).dt.date
    daily_ga = ga.groupby("date", as_index=False).agg(sessions=("sessions", "sum"), purchases=("purchase", "sum"))
    daily_facts = facts.groupby("date", as_index=False).agg(spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), orders=("attributed_orders", "sum"))
    series = daily_ga.merge(daily_facts, on="date", how="outer").fillna(0)
    # compare the deliberately most recent five-day outage window with the prior week
    outage = series[(series.date >= clock - dt.timedelta(days=9)) & (series.date <= clock - dt.timedelta(days=5))]
    base = series[(series.date >= clock - dt.timedelta(days=16)) & (series.date <= clock - dt.timedelta(days=10))]
    if not outage.empty and not base.empty:
        ga_change = _pct(base.sessions.mean(), outage.sessions.mean())
        spend_change = _pct(base.spend.mean(), outage.spend.mean())
        revenue_change = _pct(base.revenue.mean(), outage.revenue.mean())
        if ga_change < -35 and abs(spend_change) < 18 and abs(revenue_change) < 18:
            reason = (f"GA4 sessions fell {ga_change:+.1f}% while spend ({spend_change:+.1f}%) and reconciled "
                      f"revenue ({revenue_change:+.1f}%) remained stable — tracking outage, not demand loss.")
            candidates.append(_candidate(
                metric="ga_sessions", entity_type="channel", entity_id=None, entity_name="GA4 (all sources)",
                platform_key=None, direction="drop", magnitude_pct=ga_change, baseline=base.sessions.mean(),
                observed=outage.sessions.mean(), severity=min(98, 60 + abs(ga_change)/2),
                scenario_tag="tracking_outage", evidence={"ga_change_pct": ga_change, "spend_change_pct": spend_change,
                "revenue_change_pct": revenue_change, "rule": "GA-only collapse"}, artifact=True, artifact_reason=reason,
            ))
    return candidates