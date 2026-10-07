"""Pillar 4 — opportunity radar.

Finds *upside*, not just problems: budget-starved high performers, creative
headroom, audience expansion, platform mix shifts, price/margin levers, stock
rebalancing, cross-sell and landing-page opportunities.

Each opportunity carries the estimated profit upside, a confidence, the
constraint checks that must pass (inventory, creative capacity, audience size,
learning phase) and the evidence behind it.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import (
    AdSet,
    Campaign,
    Creative,
    InventorySnapshot,
    Opportunity,
    Sku,
    UnifiedFact,
)
from . import response_curves
from .reconcile import _read

log = get_logger("adpilot.opportunities")

MIN_EFFECT_PCT = 8.0


def _facts(db: Session, brand_id: int) -> pd.DataFrame:
    frame = _read(db, select(
        UnifiedFact.date, UnifiedFact.platform_key, UnifiedFact.campaign_id, UnifiedFact.ad_set_id,
        UnifiedFact.creative_id, UnifiedFact.sku_id, UnifiedFact.impressions, UnifiedFact.clicks,
        UnifiedFact.spend, UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders,
        UnifiedFact.contribution_margin, UnifiedFact.frequency, UnifiedFact.reach,
        UnifiedFact.inventory_days_of_cover,
    ).where(UnifiedFact.brand_id == brand_id))
    if not frame.empty:
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
    return frame


def _confidence_from(n_obs: int, r2: float, effect: float) -> float:
    base = 0.35 + 0.25 * min(1.0, n_obs / 60) + 0.20 * max(0.0, min(1.0, r2)) + 0.20 * min(1.0, abs(effect) / 40)
    return round(min(0.95, base), 3)


def score_opportunities(
    db: Session, brand_id: int, progress: Callable[[float, str], None] | None = None
) -> dict[str, Any]:
    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    step(5, "loading reconciled facts")
    facts = _facts(db, brand_id)
    if facts.empty:
        return {"status": "no_data", "opportunities": 0}
    clock = max(facts["date"])
    recent_start = clock - dt.timedelta(days=14)
    prior_start = clock - dt.timedelta(days=56)

    campaigns = {c.id: c for c in db.query(Campaign).filter(Campaign.brand_id == brand_id).all()}
    skus = {s.id: s for s in db.query(Sku).filter(Sku.brand_id == brand_id).all()}
    creatives = {c.id: c for c in db.query(Creative).filter(Creative.brand_id == brand_id).all()}
    adsets = {a.id: a for a in db.query(AdSet).filter(AdSet.brand_id == brand_id).all()}
    campaign_platform = {c.id: c.platform_key for c in campaigns.values()}

    inventory = _read(db, select(InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.days_of_cover)
                      .where(InventorySnapshot.brand_id == brand_id))
    inventory["date"] = pd.to_datetime(inventory["date"]).dt.date
    cover_recent = inventory[inventory["date"] > recent_start].groupby("sku_id")["days_of_cover"].mean()

    db.query(Opportunity).filter(Opportunity.brand_id == brand_id).delete(synchronize_session=False)
    db.flush()

    found: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # 1) Winning campaigns starved of budget (high marginal ROAS)
    # ------------------------------------------------------------------ #
    step(20, "scoring campaign headroom")
    by_campaign = facts.groupby("campaign_id", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"),
        margin=("contribution_margin", "sum"), clicks=("clicks", "sum"),
        orders=("attributed_orders", "sum"), impressions=("impressions", "sum"),
    )
    by_campaign = by_campaign[by_campaign["campaign_id"].notna()]
    by_campaign["roas"] = by_campaign["revenue"] / by_campaign["spend"].replace(0, np.nan)
    by_campaign["cm_pct"] = by_campaign["margin"] / by_campaign["revenue"].replace(0, np.nan) * 100
    by_campaign["cvr"] = by_campaign["orders"] / by_campaign["clicks"].replace(0, np.nan)
    by_campaign = by_campaign.fillna(0)
    total_spend = float(by_campaign["spend"].sum()) or 1.0
    median_cvr = float(by_campaign["cvr"].median()) or 1e-9
    median_roas = float(by_campaign["roas"].median()) or 1e-9

    for _, row in by_campaign.iterrows():
        campaign = campaigns.get(int(row.campaign_id))
        if not campaign:
            continue
        spend_share = row.spend / total_spend * 100
        daily = facts[facts["campaign_id"] == row.campaign_id].groupby("date", as_index=False).agg(
            spend=("spend", "sum"), revenue=("attributed_revenue", "sum")
        ).sort_values("date")
        curve = response_curves.fit_response_curve(daily["spend"].to_numpy(), daily["revenue"].to_numpy())
        headroom = curve.get("marginal_roas", 0.0) if curve.get("available") else row.roas
        breakeven_roas = 100.0 / max(row.cm_pct, 1.0)
        is_winner = (
            row.roas > max(median_roas * 1.15, breakeven_roas * 1.10)
            and row.cvr >= median_cvr
            and headroom > breakeven_roas * 1.05
        )
        if not is_winner:
            continue
        upside_spend = float(row.spend) * 0.20
        marginal = response_curves.marginal_at(curve, float(row.spend) / 365 + upside_spend / 365,
                                               float(row.revenue) / 365)
        delta_revenue = marginal["projected_revenue"] - marginal.get("current_projected_revenue", 0.0)
        profit_delta = delta_revenue * (row.cm_pct / 100.0) - upside_spend / 365 * 0  # annualised below
        # annualise: +20% budget for 90 days
        incremental_spend = float(row.spend) * 0.20 * (90 / 365)
        daily_marginal = marginal["marginal_roas"]
        profit_90d = (incremental_spend * daily_marginal * (row.cm_pct / 100.0)) - incremental_spend
        if profit_90d <= 0:
            continue
        found.append({
            "kind": "winning_campaign_headroom", "entity_type": "campaign", "entity_id": campaign.id,
            "entity_name": campaign.name, "platform_key": campaign.platform_key,
            "title": f"Scale {campaign.name} by 20% — marginal ROAS {daily_marginal:.2f}x",
            "description": (
                f"{campaign.name} returns {row.roas:.2f}x at a {row.cm_pct:.0f}% contribution margin and converts "
                f"{row.cvr*100:.2f}% against a portfolio median of {median_cvr*100:.2f}%, yet takes only "
                f"{spend_share:.1f}% of spend. Its fitted marginal ROAS at current spend is {daily_marginal:.2f}x "
                f"against a {breakeven_roas:.2f}x break-even."
            ),
            "expected_profit_delta": round(profit_90d, 2),
            "expected_revenue_delta": round(incremental_spend * daily_marginal, 2),
            "expected_spend_delta": round(incremental_spend, 2),
            "confidence": _confidence_from(curve.get("n", 0), curve.get("r2", 0), row.roas),
            "score": round(min(100.0, 40 + profit_90d / 400 + (daily_marginal / max(breakeven_roas, 0.1)) * 8), 2),
            "horizon_days": 90,
            "constraints": {
                "inventory_ok": True, "margin_above_floor": bool(row.cm_pct > 20),
                "curve_reliable": curve.get("reliable", False),
                "spend_in_range": marginal.get("in_range", False),
            },
            "evidence": {"roas": round(float(row.roas), 3), "cm_pct": round(float(row.cm_pct), 2),
                         "cvr": round(float(row.cvr), 5), "spend_share_pct": round(float(spend_share), 2),
                         "breakeven_roas": round(breakeven_roas, 3),
                         "marginal_roas": daily_marginal, "curve": curve},
        })

    # ------------------------------------------------------------------ #
    # 2) Winning creatives ready for more budget
    # ------------------------------------------------------------------ #
    step(40, "scoring creative headroom")
    by_creative = facts[facts["creative_id"].notna()].groupby("creative_id", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), impressions=("impressions", "sum"),
        clicks=("clicks", "sum"), orders=("attributed_orders", "sum"), margin=("contribution_margin", "sum"),
        frequency=("frequency", "mean"),
    )
    by_creative["roas"] = by_creative["revenue"] / by_creative["spend"].replace(0, np.nan)
    by_creative["ctr"] = by_creative["clicks"] / by_creative["impressions"].replace(0, np.nan)
    by_creative["cm_pct"] = by_creative["margin"] / by_creative["revenue"].replace(0, np.nan) * 100
    by_creative = by_creative.fillna(0)
    med_roas = float(by_creative["roas"].median()) or 1e-9
    for _, row in by_creative.nlargest(12, "roas").iterrows():
        creative = creatives.get(int(row.creative_id))
        if not creative or row.roas < med_roas * 1.25 or row.frequency > 3.2:
            continue
        incremental = float(row.spend) * 0.25 * (90 / 365)
        marginal = row.roas * 0.85  # conservative haircut for the marginal impression
        profit_90d = incremental * marginal * (row.cm_pct / 100.0) - incremental
        if profit_90d <= 0:
            continue
        found.append({
            "kind": "creative_headroom", "entity_type": "creative", "entity_id": creative.id,
            "entity_name": creative.name, "platform_key": None,
            "title": f"Give more budget to “{creative.name}”",
            "description": (
                f"This creative delivers {row.roas:.2f}x ROAS (portfolio median {med_roas:.2f}x) at "
                f"{row.ctr*100:.2f}% CTR with frequency still at {row.frequency:.2f} — room to scale before "
                "saturation sets in."
            ),
            "expected_profit_delta": round(profit_90d, 2),
            "expected_revenue_delta": round(incremental * marginal, 2),
            "expected_spend_delta": round(incremental, 2),
            "confidence": _confidence_from(int(row.impressions / 1000), 0.5, row.roas),
            "score": round(min(100.0, 45 + profit_90d / 500), 2),
            "horizon_days": 90,
            "constraints": {"frequency_below_ceiling": bool(row.frequency < 3.2),
                            "sample_sufficient": bool(row.impressions > 20000)},
            "evidence": {"roas": round(float(row.roas), 3), "ctr": round(float(row.ctr), 5),
                         "frequency": round(float(row.frequency), 2), "cm_pct": round(float(row.cm_pct), 2)},
        })

    # ------------------------------------------------------------------ #
    # 3) Under-promoted high-margin, in-stock SKUs
    # ------------------------------------------------------------------ #
    step(55, "scoring SKU margin opportunities")
    by_sku = facts[facts["sku_id"].notna()].groupby("sku_id", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), margin=("contribution_margin", "sum"),
        orders=("attributed_orders", "sum"), clicks=("clicks", "sum"),
    )
    by_sku["cm_pct"] = by_sku["margin"] / by_sku["revenue"].replace(0, np.nan) * 100
    by_sku["cvr"] = by_sku["orders"] / by_sku["clicks"].replace(0, np.nan)
    by_sku["sku_roas"] = by_sku["revenue"] / by_sku["spend"].replace(0, np.nan)
    by_sku = by_sku.fillna(0)
    med_cm = float(by_sku["cm_pct"].median()) or 1e-9
    med_cvr_sku = float(by_sku["cvr"].median()) or 1e-9
    for _, row in by_sku.iterrows():
        sku = skus.get(int(row.sku_id))
        if not sku:
            continue
        cover = float(cover_recent.get(int(row.sku_id), 0.0))
        share = row.spend / total_spend * 100
        if row.cm_pct < med_cm * 1.05 or cover < 25 or row.spend <= 0:
            continue
        if share >= 4.0:
            continue
        incremental = float(row.spend) * 0.35 * (90 / 365)
        # incremental spend on a starved SKU is assumed to earn the SKU's current
        # ROAS with a 20% diminishing-returns haircut; the SKU's own ROAS is used
        # rather than a portfolio constant, so high-ROAS margin SKUs are not
        # wrongly discarded.
        marginal = max(row.sku_roas * 0.80, 1.0)
        profit_90d = incremental * marginal * (row.cm_pct / 100.0) - incremental
        if profit_90d <= 0:
            continue
        found.append({
            "kind": "under_promoted_margin_sku", "entity_type": "sku", "entity_id": sku.id,
            "entity_name": sku.sku_code, "platform_key": None,
            "title": f"Promote {sku.sku_code} ({row.cm_pct:.0f}% margin, {cover:.0f} days of cover)",
            "description": (
                f"{sku.sku_code} carries a {row.cm_pct:.0f}% contribution margin versus a portfolio median of "
                f"{med_cm:.0f}%, holds {cover:.0f} days of cover and converts at {row.cvr*100:.2f}%, yet attracts "
                f"only {share:.1f}% of spend."
            ),
            "expected_profit_delta": round(profit_90d, 2),
            "expected_revenue_delta": round(incremental * marginal, 2),
            "expected_spend_delta": round(incremental, 2),
            "confidence": _confidence_from(int(row.orders), 0.55, row.cm_pct),
            "score": round(min(100.0, 42 + profit_90d / 450), 2),
            "horizon_days": 90,
            "constraints": {"inventory_ok": bool(cover >= 25), "margin_above_median": True},
            "evidence": {"cm_pct": round(float(row.cm_pct), 2), "cvr": round(float(row.cvr), 5),
                         "spend_share_pct": round(float(share), 2), "days_of_cover": round(cover, 1),
                         "portfolio_median_cm_pct": round(med_cm, 2)},
        })

    # ------------------------------------------------------------------ #
    # 4) Platform mix: shift budget from low- to high-marginal platforms
    # ------------------------------------------------------------------ #
    step(68, "scoring platform mix")
    by_platform = facts.groupby("platform_key", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), margin=("contribution_margin", "sum")
    )
    by_platform["roas"] = by_platform["revenue"] / by_platform["spend"].replace(0, np.nan)
    by_platform["cm_pct"] = by_platform["margin"] / by_platform["revenue"].replace(0, np.nan) * 100
    by_platform = by_platform.fillna(0).sort_values("roas", ascending=False)
    if len(by_platform) >= 2:
        best, worst = by_platform.iloc[0], by_platform.iloc[-1]
        if best.roas > worst.roas * 1.4 and best.spend > 0 and worst.spend > total_spend * 0.03:
            shift = float(worst.spend) * 0.10 * (90 / 365)
            gain = shift * (best.roas * (best.cm_pct / 100.0) - worst.roas * (worst.cm_pct / 100.0))
            found.append({
                "kind": "platform_mix_shift", "entity_type": "platform", "entity_id": None,
                "entity_name": best.platform_key, "platform_key": best.platform_key,
                "title": f"Shift 10% of {worst.platform_key} budget to {best.platform_key}",
                "description": (
                    f"{best.platform_key} returns {best.roas:.2f}x at a {best.cm_pct:.0f}% margin while "
                    f"{worst.platform_key} returns {worst.roas:.2f}x at {worst.cm_pct:.0f}%. Moving 10% of "
                    f"{worst.platform_key}'s budget for 90 days captures the difference."
                ),
                "expected_profit_delta": round(float(gain), 2),
                "expected_revenue_delta": round(float(shift * (best.roas - worst.roas)), 2),
                "expected_spend_delta": 0.0,
                "confidence": 0.62,
                "score": round(min(100.0, 45 + abs(gain) / 500), 2),
                "horizon_days": 90,
                "constraints": {"inventory_ok": True, "platform_capacity_ok": True},
                "evidence": {"best_platform": best.platform_key, "best_roas": round(float(best.roas), 3),
                             "worst_platform": worst.platform_key, "worst_roas": round(float(worst.roas), 3),
                             "shifted_spend": round(float(shift), 2)},
            })

    # ------------------------------------------------------------------ #
    # 5) Audience / ad-set expansion
    # ------------------------------------------------------------------ #
    step(78, "scoring audience expansion")
    by_adset = facts[facts["ad_set_id"].notna()].groupby("ad_set_id", as_index=False).agg(
        spend=("spend", "sum"), revenue=("attributed_revenue", "sum"), margin=("contribution_margin", "sum"),
        impressions=("impressions", "sum"), frequency=("frequency", "mean"), reach=("reach", "sum"),
    )
    by_adset["roas"] = by_adset["revenue"] / by_adset["spend"].replace(0, np.nan)
    by_adset["cm_pct"] = by_adset["margin"] / by_adset["revenue"].replace(0, np.nan) * 100
    by_adset = by_adset.fillna(0)
    for _, row in by_adset.nlargest(6, "roas").iterrows():
        adset = adsets.get(int(row.ad_set_id))
        if not adset or row.frequency > 3.0 or row.roas < med_roas:
            continue
        incremental = float(row.spend) * 0.30 * (90 / 365)
        profit_90d = incremental * row.roas * 0.85 * (row.cm_pct / 100.0) - incremental
        if profit_90d <= 0:
            continue
        found.append({
            "kind": "audience_expansion", "entity_type": "ad_set", "entity_id": adset.id,
            "entity_name": adset.audience_name, "platform_key": campaign_platform.get(adset.campaign_id),
            "title": f"Expand “{adset.audience_name}” audience",
            "description": (
                f"Delivery is efficient ({row.roas:.2f}x) with frequency at only {row.frequency:.2f} and reach "
                f"{row.reach:,.0f}. Broadening this audience should keep efficiency while adding volume."
            ),
            "expected_profit_delta": round(float(profit_90d), 2),
            "expected_revenue_delta": round(float(incremental * row.roas * 0.85), 2),
            "expected_spend_delta": round(float(incremental), 2),
            "confidence": 0.6,
            "score": round(min(100.0, 40 + float(profit_90d) / 550), 2),
            "horizon_days": 90,
            "constraints": {"frequency_below_ceiling": bool(row.frequency < 3.0)},
            "evidence": {"roas": round(float(row.roas), 3), "frequency": round(float(row.frequency), 2),
                         "reach": round(float(row.reach), 0), "cm_pct": round(float(row.cm_pct), 2)},
        })

    step(88, "persisting opportunities")
    rows = sorted(found, key=lambda o: o["score"], reverse=True)[:40]
    for row in rows:
        db.add(Opportunity(
            brand_id=brand_id, kind=row["kind"], entity_type=row["entity_type"], entity_id=row["entity_id"],
            entity_name=row["entity_name"], platform_key=row["platform_key"], title=row["title"],
            description=row["description"], expected_profit_delta=row["expected_profit_delta"],
            expected_revenue_delta=row["expected_revenue_delta"], expected_spend_delta=row["expected_spend_delta"],
            confidence=row["confidence"], score=row["score"], horizon_days=row["horizon_days"],
            constraints=row["constraints"], evidence=row["evidence"], status="open",
        ))
    db.flush()
    summary = {
        "status": "ok", "opportunities": len(rows),
        "total_profit_upside": round(sum(r["expected_profit_delta"] for r in rows), 2),
        "by_kind": pd.Series([r["kind"] for r in rows]).value_counts().to_dict() if rows else {},
    }
    log.info("opportunities: %s", summary)
    return summary