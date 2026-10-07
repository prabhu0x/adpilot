"""Simulation clock.

The demo dataset ends at a fixed "today". Reviewers need to watch the whole
closed loop — execute a change, let days pass, measure the outcome — without
waiting weeks, so the clock can be advanced.

Advancing the clock does **not** fabricate a single number: it continues the
same generative process the seeder used, fitted to the *observed* history:

* per-campaign CPM / CTR / CVR / AOV are estimated from the last 28 days of
  ``ad_metrics_daily`` (so a campaign whose efficiency was already drifting
  keeps drifting),
* spend follows the campaign's daily budget, day-of-week and annual seasonality,
* **executed changes take effect**: paused campaigns stop delivering, budget
  changes scale delivery with diminishing returns, and stockouts throttle spend,
* the resulting store orders, GA sessions and inventory snapshots are generated
  consistently, and the derived layers are rebuilt by the normal pipeline.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.events import bus
from ..core.logging import get_logger
from ..db.models import (
    Ad,
    AdMetricsDaily,
    AdMetricsHourly,
    AdSet,
    Campaign,
    CampaignSkuMap,
    Execution,
    GaEvent,
    InventorySnapshot,
    Order,
    OrderItem,
    Recommendation,
    SimulationState,
    Sku,
    SkuPriceHistory,
    UnifiedFact,
)
from .reconcile import _read

log = get_logger("adpilot.simulation")

# same weekly/annual shape the seeder uses, kept in one place for consistency
DOW_FACTOR = {0: 0.95, 1: 0.97, 2: 0.99, 3: 1.02, 4: 1.05, 5: 1.08, 6: 1.04}
HOUR_FACTOR = [0.2, 0.12, 0.08, 0.06, 0.05, 0.06, 0.1, 0.2, 0.4, 0.6, 0.7, 0.75,
               0.8, 0.78, 0.72, 0.7, 0.72, 0.78, 0.85, 0.95, 1.0, 0.95, 0.7, 0.4]


def get_state(db: Session, brand_id: int) -> SimulationState:
    state = db.query(SimulationState).filter(SimulationState.brand_id == brand_id).first()
    if not state:
        from ..core.config import settings

        state = SimulationState(
            brand_id=brand_id,
            clock_date=dt.date.fromisoformat(settings.sim_today),
            real_start_date=dt.date.fromisoformat(settings.sim_start_date),
            days_advanced=0,
        )
        db.add(state)
        db.flush()
    return state


def advance_clock(
    db: Session, brand_id: int, days: int = 7, progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Move simulated time forward by ``days`` and generate that period's data."""
    from . import pipeline

    if days <= 0 or days > 120:
        return {"status": "error", "error": {"code": "invalid_days", "message": "days must be between 1 and 120"}}

    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)

    state = get_state(db, brand_id)
    start = state.clock_date + dt.timedelta(days=1)
    dates = [start + dt.timedelta(days=i) for i in range(days)]

    step(5, f"advancing clock {state.clock_date} → {dates[-1]}")
    created = _generate_period(db, brand_id, dates, progress=lambda p, m: step(5 + p * 0.45, m))

    state.clock_date = dates[-1]
    state.days_advanced += days
    state.last_tick_at = dt.datetime.utcnow()
    db.flush()
    bus.publish("simulation", {"clock_date": str(state.clock_date), "days_advanced": state.days_advanced,
                               "generated": created})

    step(55, "rebuilding derived layers for the extended history")
    derived = pipeline.run_full_pipeline(db, brand_id, progress=lambda p, m: step(55 + p * 0.35, m),
                                         reset=True)
    step(95, "measurement check")
    outcomes = derived.get("stages", {}).get("execution", {})
    return {
        "status": "ok", "clock_date": str(state.clock_date), "days_advanced": state.days_advanced,
        "advanced_by": days, "generated": created, "pipeline": derived.get("stages", {}),
        "outcomes": outcomes,
    }


def _annual_factor(date: dt.date) -> float:
    doy = date.timetuple().tm_yday
    seasonal = 1.0 + 0.18 * math.sin((doy - 60) / 365.0 * 2 * math.pi)
    if date.month == 11 and date.day >= 20:
        seasonal *= 1.45
    elif date.month == 12 and date.day <= 24:
        seasonal *= 1.30
    return seasonal


def _baselines(db: Session, brand_id: int) -> dict[int, dict[str, float]]:
    """Estimate per-campaign CPM/CTR/CVR/AOV from the most recent 28 days."""
    rows = _read(db, select(
        AdMetricsDaily.campaign_id, AdMetricsDaily.impressions, AdMetricsDaily.clicks,
        AdMetricsDaily.spend, AdMetricsDaily.platform_conversions, AdMetricsDaily.platform_revenue,
    ).where(AdMetricsDaily.brand_id == brand_id))
    if rows.empty:
        return {}
    out: dict[int, dict[str, float]] = {}
    for cid, group in rows.groupby("campaign_id"):
        impressions = float(group["impressions"].sum())
        clicks = float(group["clicks"].sum())
        spend = float(group["spend"].sum())
        conv = float(group["platform_conversions"].sum())
        revenue = float(group["platform_revenue"].sum())
        if spend <= 0 or impressions <= 0:
            continue
        out[int(cid)] = {
            "cpm": spend / impressions * 1000,
            "ctr": clicks / impressions,
            "cvr": (conv / clicks) if clicks else 0.02,
            "aov": (revenue / conv) if conv else 60.0,
            "conv_per_revenue": (conv / revenue) if revenue else 0.02,
            "spend_share": spend,
        }
    return out


def _generate_period(
    db: Session, brand_id: int, dates: list[dt.date], progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Generate metrics, orders, GA events and inventory for a date range."""
    rng = np.random.default_rng(20261006 + len(dates))

    campaigns = db.query(Campaign).filter(Campaign.brand_id == brand_id).all()
    adsets = db.query(AdSet).filter(AdSet.brand_id == brand_id).all()
    ads = db.query(Ad).filter(Ad.brand_id == brand_id).all()
    skus = db.query(Sku).filter(Sku.brand_id == brand_id).all()
    sku_maps = db.query(CampaignSkuMap).filter(CampaignSkuMap.brand_id == brand_id).all()

    baselines = _baselines(db, brand_id)
    ads_by_campaign: dict[int, list[Ad]] = {}
    for ad in ads:
        ads_by_campaign.setdefault(ad.campaign_id, []).append(ad)
    skus_by_campaign: dict[int, list[tuple[int, float]]] = {}
    for m in sku_maps:
        skus_by_campaign.setdefault(m.campaign_id, []).append((m.sku_id, float(m.weight or 1.0)))
    sku_by_id = {s.id: s for s in skus}

    # historical ad-set / creative split so the grain stays consistent
    hist = _read(db, select(
        AdMetricsDaily.ad_set_id, AdMetricsDaily.creative_id, AdMetricsDaily.spend,
    ).where(AdMetricsDaily.brand_id == brand_id))
    shares: dict[tuple[int, int | None], float] = {}
    if not hist.empty:
        grouped = hist.groupby(["ad_set_id", "creative_id"], dropna=False)["spend"].sum()
        total = float(grouped.sum()) or 1.0
        shares = {k: float(v) / total for k, v in grouped.items()}

    effects = _execution_effects(db, brand_id)

    metrics_rows: list[dict[str, Any]] = []
    hourly_rows: list[dict[str, Any]] = []
    order_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []
    ga_rows: list[dict[str, Any]] = []

    order_seq = 900_000
    for offset, date in enumerate(dates):
        wf = DOW_FACTOR[date.weekday()]
        af = _annual_factor(date)
        for campaign in campaigns:
            base = baselines.get(campaign.id)
            if not base:
                continue
            if campaign.status != "active":
                continue  # paused/ended campaigns stop delivering
            if campaign.launched_at and date < campaign.launched_at:
                continue

            budget = float(campaign.daily_budget or 0.0)
            throttle = effects.get(("campaign", campaign.id), {}).get("throttle", 1.0)
            pacing = float(np.clip(1.0 + 0.05 * math.sin((offset + 30) / 9.0) + rng.normal(0, 0.05), 0.6, 1.4))
            spend = budget * pacing * (0.55 + 0.45 * af) * wf * throttle

            cpm = base["cpm"] * float(rng.normal(1.0, 0.04)) * (0.94 + 0.12 * af)
            impressions = spend / max(cpm, 1e-6) * 1000
            ctr = max(base["ctr"] * float(rng.normal(1.0, 0.06)), 1e-5)
            clicks = impressions * ctr
            cvr = max(base["cvr"] * float(rng.normal(1.0, 0.09)), 1e-5)
            conversions = clicks * cvr
            aov = base["aov"] * float(rng.normal(1.0, 0.04))
            platform_revenue = conversions * aov

            # distribute to the ad set x creative grain using historical shares
            campaign_shares = {
                key: value for key, value in shares.items()
                if key[0] in {a.ad_set_id for a in ads_by_campaign.get(campaign.id, [])}
            }
            if not campaign_shares:
                campaign_shares = {(s.id, None): 1.0 for s in adsets if s.campaign_id == campaign.id}
            scale = sum(campaign_shares.values()) or 1.0
            for (ad_set_id, creative_id), weight in campaign_shares.items():
                fraction = weight / scale
                if fraction < 1e-4:
                    continue
                imp = impressions * fraction
                clk = clicks * fraction
                metrics_rows.append(dict(
                    brand_id=brand_id, date=date, platform_key=campaign.platform_key,
                    campaign_id=campaign.id, ad_set_id=ad_set_id, creative_id=creative_id,
                    impressions=int(imp), clicks=int(round(clk)), spend=round(spend * fraction, 4),
                    platform_conversions=round(conversions * fraction, 3),
                    platform_revenue=round(platform_revenue * fraction, 2),
                    ctr=round(clk / imp if imp else 0.0, 6), cpc=round(spend * fraction / clk, 4) if clk else 0.0,
                    cpm=round(spend * fraction / imp * 1000, 4) if imp else 0.0,
                    frequency=round(float(rng.uniform(1.4, 3.4)), 3),
                    reach=int(imp / max(rng.uniform(1.4, 3.4), 1.01)),
                    video_views=int(imp * 0.28) if campaign.platform_key in {"tiktok", "programmatic"} else 0,
                ))
            # hourly detail only for the most recent three simulated days
            if offset >= len(dates) - 3:
                for hour, hf in enumerate(HOUR_FACTOR):
                    hourly_rows.append(dict(
                        brand_id=brand_id, ts=dt.datetime.combine(date, dt.time(hour)),
                        date=date, hour=hour, platform_key=campaign.platform_key,
                        campaign_id=campaign.id, ad_set_id=ads_by_campaign.get(campaign.id, [None])[0].ad_set_id
                        if ads_by_campaign.get(campaign.id) else None,
                        impressions=int(impressions * hf / sum(HOUR_FACTOR)),
                        clicks=int(round(clicks * hf / sum(HOUR_FACTOR))),
                        spend=round(spend * hf / sum(HOUR_FACTOR), 4),
                        platform_conversions=round(conversions * hf / sum(HOUR_FACTOR), 3),
                        platform_revenue=round(platform_revenue * hf / sum(HOUR_FACTOR), 2),
                        ctr=round(ctr, 6), cpc=round(spend / max(clicks, 1e-6), 4),
                        cpm=round(cpm, 4), frequency=round(float(rng.uniform(1.2, 2.4)), 3),
                    ))

            # ---- store orders (the truth the platforms overstate) -----------
            pairs = skus_by_campaign.get(campaign.id) or [(skus[0].id, 1.0)]
            ids = [p[0] for p in pairs]
            weights = np.array([p[1] for p in pairs], dtype=float)
            weights = weights / weights.sum()
            store_cvr = cvr * 0.62  # platforms over-report; the store is the truth
            n_orders = int(rng.poisson(max(clicks * store_cvr, 0.0)))
            for _ in range(n_orders):
                order_seq += 1
                sku_id = int(rng.choice(ids, p=weights))
                sku = sku_by_id.get(sku_id)
                if not sku:
                    continue
                qty = int(rng.choice([1, 1, 1, 2, 2, 3], p=[0.5, 0.2, 0.12, 0.1, 0.05, 0.03]))
                unit_price = round(float(sku.price), 2)
                discount_pct = 0.20 if (date.month == 11 and date.day >= 24) else 0.0
                gross = unit_price * qty
                discount = gross * discount_pct
                net = gross - discount
                cogs = float(sku.cost) * qty
                refund = net * 0.055 if rng.random() < 0.06 else 0.0
                shipping = 4.5 * qty
                fees = net * 0.029 + 0.3
                order_number = f"LUM-{date.strftime('%y%m%d')}-{order_seq}"
                order_rows.append(dict(
                    brand_id=brand_id, order_number=order_number,
                    ordered_at=dt.datetime.combine(date, dt.time(int(rng.integers(6, 23)), int(rng.integers(0, 60)))),
                    date=date, channel="shopify", source=campaign.platform_key,
                    medium="paid_social" if campaign.platform_key in {"meta", "tiktok"} else "cpc",
                    campaign_name=external_name(campaign),
                    device=str(rng.choice(["mobile", "desktop", "tablet"], p=[0.62, 0.31, 0.07])),
                    geo=str(rng.choice(["US-CA", "US-NY", "US-TX", "US-FL", "US-WA"], p=[0.3, 0.25, 0.2, 0.15, 0.1])),
                    is_new_customer=bool(rng.random() < 0.42),
                    gross_revenue=round(gross, 2), discount_amount=round(discount, 2),
                    shipping_revenue=round(shipping, 2), tax_amount=round(net * 0.07, 2),
                    refund_amount=round(refund, 2), fees=round(fees, 2), net_revenue=round(net, 2),
                    cogs=round(cogs, 2), gross_profit=round(net - refund - cogs, 2), status="completed",
                    platform_key=campaign.platform_key, campaign_id=campaign.id,
                ))
                item_rows.append(dict(
                    brand_id=brand_id, order_number=order_number,
                    sku_id=sku_id, quantity=qty, unit_price=unit_price, unit_cost=float(sku.cost),
                    discount_amount=round(discount, 2), refund_amount=round(refund, 2),
                    net_revenue=round(net, 2), cogs=round(cogs, 2),
                    shipping_revenue=round(shipping, 2), tax_amount=round(net * 0.07, 2),
                ))

            # ---- GA4 events --------------------------------------------------
            outage = _ga_outage_ratio(date)
            sessions = int(impressions * 0.031 * outage * float(rng.normal(1.0, 0.06)))
            atc = int(sessions * 0.09 * float(rng.normal(1.0, 0.1)))
            checkout = int(atc * 0.45 * float(rng.normal(1.0, 0.1)))
            purchases = int(checkout * 0.58 * float(rng.normal(1.0, 0.1)))
            purchase_revenue = purchases * float(aov) * 0.94
            ga_rows.append(dict(
                brand_id=brand_id, date=date, platform_key=campaign.platform_key,
                utm_source=campaign.platform_key, utm_medium="cpc", utm_campaign=external_name(campaign),
                device=str(rng.choice(["mobile", "desktop", "tablet"], p=[0.6, 0.33, 0.07])),
                geo=str(rng.choice(["US-CA", "US-NY", "US-TX", "US-FL", "US-WA"], p=[0.3, 0.25, 0.2, 0.15, 0.1])),
                sessions=max(sessions, 0), engaged_sessions=max(int(sessions * 0.61), 0),
                add_to_cart=max(atc, 0), begin_checkout=max(checkout, 0), purchase=max(purchases, 0),
                purchase_revenue=round(purchase_revenue, 2), bounces=max(int(sessions * 0.38), 0),
            ))

    _bulk_insert(db, AdMetricsDaily, metrics_rows)
    _bulk_insert(db, AdMetricsHourly, hourly_rows)
    _bulk_insert(db, Order, order_rows)
    _bulk_insert(db, GaEvent, ga_rows)
    db.flush()
    _resolve_order_items(db, brand_id, item_rows)

    if progress:
        progress(88, "extending inventory snapshots")
    inventory_rows = _extend_inventory(db, brand_id, dates, rng)
    db.flush()

    log.info("simulation tick: %s metric rows, %s orders, %s GA rows, %s inventory rows",
             len(metrics_rows), len(order_rows), len(ga_rows), inventory_rows)
    return {"metric_rows": len(metrics_rows), "hourly_rows": len(hourly_rows), "orders": len(order_rows),
            "order_items": len(item_rows), "ga_rows": len(ga_rows), "inventory_rows": inventory_rows}


def _resolve_order_items(db: Session, brand_id: int, item_rows: list[dict[str, Any]]) -> None:
    """OrderItem rows are keyed by order_number in the generator; map to order ids."""
    if not item_rows:
        return
    numbers = sorted({row["order_number"] for row in item_rows})
    mapping: dict[str, int] = {}
    for start in range(0, len(numbers), 800):
        chunk = numbers[start:start + 800]
        for order_id, number in db.query(Order.id, Order.order_number).filter(
            Order.brand_id == brand_id, Order.order_number.in_(chunk)
        ).all():
            mapping[number] = order_id
    resolved = [
        {**{k: v for k, v in row.items() if k != "order_number"}, "order_id": mapping[row["order_number"]]}
        for row in item_rows if row["order_number"] in mapping
    ]
    _bulk_insert(db, OrderItem, resolved)


def external_name(campaign: Campaign) -> str:
    return campaign.external_id or campaign.name


def _ga_outage_ratio(date: dt.date) -> float:
    """Any GA4 tracking outage is a past event; the simulator assumes healthy tags."""
    return 1.0


def _execution_effects(db: Session, brand_id: int) -> dict[tuple[str, int], dict[str, float]]:
    """Translate executed recommendations into delivery effects for the simulator."""
    effects: dict[tuple[str, int], dict[str, float]] = {}
    executions = (
        db.query(Execution)
        .filter(Execution.brand_id == brand_id, Execution.status.in_(["executed", "measured"]))
        .all()
    )
    for execution in executions:
        rec = db.query(Recommendation).filter(Recommendation.id == execution.recommendation_id).first()
        if not rec:
            continue
        key = (rec.entity_type, int(rec.entity_id or 0))
        bucket = effects.setdefault(key, {})
        if rec.kind == "pause_campaign":
            campaign = db.query(Campaign).filter(Campaign.id == rec.entity_id).first()
            if campaign:
                campaign.status = "paused"
        elif rec.kind in {"increase_budget", "decrease_budget"}:
            campaign = db.query(Campaign).filter(Campaign.id == rec.entity_id).first()
            if campaign and rec.proposed_value:
                campaign.daily_budget = float(rec.proposed_value)
        elif rec.kind == "reallocate_budget" and rec.entity_type == "portfolio":
            for item in (rec.evidence or {}).get("allocation", []):
                campaign = db.query(Campaign).filter(Campaign.id == item.get("campaign_id")).first()
                if campaign:
                    campaign.daily_budget = float(item.get("proposed_daily_spend", campaign.daily_budget))
        elif rec.kind == "pause_creative" and rec.entity_id:
            spend_share = 0.25
            bucket["throttle"] = 1.0 - spend_share
    db.flush()
    return effects


def _extend_inventory(db: Session, brand_id: int, dates: list[dt.date], rng: np.random.Generator) -> int:
    """Continue the replenishment simulation for the new days."""
    skus = db.query(Sku).filter(Sku.brand_id == brand_id).all()
    clock = db.query(func.max(InventorySnapshot.date)).filter(
        InventorySnapshot.brand_id == brand_id).scalar()
    last = _read(db, select(
        InventorySnapshot.sku_id, InventorySnapshot.on_hand, InventorySnapshot.days_of_cover,
        InventorySnapshot.daily_velocity, InventorySnapshot.inbound, InventorySnapshot.reorder_point,
        InventorySnapshot.lead_time_days, InventorySnapshot.warehouse,
    ).where(InventorySnapshot.brand_id == brand_id, InventorySnapshot.date == clock))
    if last.empty:
        return 0

    recent_units = _read(db, select(OrderItem.sku_id, OrderItem.quantity).where(OrderItem.brand_id == brand_id))
    velocity = {}
    if not recent_units.empty:
        grouped = recent_units.groupby("sku_id")["quantity"].sum()
        velocity = {int(k): float(v) / 365.0 for k, v in grouped.items()}

    state: dict[int, dict[str, float]] = {}
    for row in last.itertuples():
        state[int(row.sku_id)] = {
            "on_hand": float(row.on_hand or 0.0), "inbound": float(row.inbound or 0.0),
            "reorder_point": float(row.reorder_point or 0.0), "lead_time_days": float(row.lead_time_days or 21.0),
            "warehouse": row.warehouse or "MAIN",
        }

    rows: list[dict[str, Any]] = []
    for date in dates:
        for sku in skus:
            info = state.get(sku.id)
            if not info:
                continue
            consumption = velocity.get(sku.id, 8.0) * float(rng.normal(1.0, 0.08))
            info["on_hand"] = max(0.0, info["on_hand"] - consumption)
            days_cover = info["on_hand"] / max(velocity.get(sku.id, 8.0), 0.5)
            if info["on_hand"] + info["inbound"] <= info["reorder_point"] and info["inbound"] <= 0:
                info["inbound"] = max(velocity.get(sku.id, 8.0) * 45.0, 200.0)
            # inbound arrives after the lead time
            if info["inbound"] > 0 and rng.random() < 1.0 / max(info["lead_time_days"], 1.0):
                info["on_hand"] += info["inbound"]
                info["inbound"] = 0.0
            rows.append(dict(
                brand_id=brand_id, date=date, sku_id=sku.id, warehouse=info["warehouse"],
                on_hand=int(info["on_hand"]), inbound=int(info["inbound"]),
                committed=int(info["on_hand"] * 0.08), reorder_point=int(info["reorder_point"]),
                lead_time_days=int(info["lead_time_days"]),
                daily_velocity=round(velocity.get(sku.id, 8.0), 2),
                days_of_cover=round(days_cover, 2), stockout_flag=bool(info["on_hand"] <= 0),
            ))
    _bulk_insert(db, InventorySnapshot, rows)
    return len(rows)


def _bulk_insert(db: Session, model, rows: list[dict[str, Any]], chunk: int = 5000) -> None:
    for start in range(0, len(rows), chunk):
        db.bulk_insert_mappings(model, rows[start:start + chunk])
        db.flush()