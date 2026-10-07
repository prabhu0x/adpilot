"""Agentic reasoning: planner → tool calls → synthesizer.

The agent owns a small, well-typed tool set. Every call is persisted to
``agent_steps`` so the UI can render the full reasoning trail, and the
synthesizer only ever phrases numbers the tools produced.
"""
from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import (
    AdMetricsDaily,
    AgentRun,
    AgentStep,
    Campaign,
    Creative,
    GaEvent,
    InventorySnapshot,
    Sku,
    SkuPriceHistory,
    UnifiedFact,
)
from . import causal as causal_engine
from . import decomposition
from .llm import copilot_answer_template, llm
from .reconcile import _read

log = get_logger("adpilot.agent")


# --------------------------------------------------------------------------- #
# Tool implementations
# --------------------------------------------------------------------------- #
def _window(db: Session, brand_id: int) -> tuple[dt.date, dt.date]:
    clock = db.query(UnifiedFact.date).filter(UnifiedFact.brand_id == brand_id).order_by(
        UnifiedFact.date.desc()
    ).first()
    end = clock[0] if clock else dt.date.today()
    return end - dt.timedelta(days=14), end


def tool_query_unified_facts(
    db: Session, brand_id: int, entity_type: str = "campaign", entity_id: int | None = None,
    metrics: list[str] | None = None, days: int = 90,
) -> dict[str, Any]:
    """Aggregate reconciled facts for an entity over the last N days."""
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.ad_set_id, UnifiedFact.creative_id,
        UnifiedFact.sku_id, UnifiedFact.platform_key, UnifiedFact.impressions, UnifiedFact.clicks,
        UnifiedFact.spend, UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders,
        UnifiedFact.contribution_margin, UnifiedFact.platform_revenue,
    ).where(UnifiedFact.brand_id == brand_id))
    if facts.empty:
        return {"summary": "no reconciled facts available", "data": {}}
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    col = {"campaign": "campaign_id", "ad_set": "ad_set_id", "creative": "creative_id", "sku": "sku_id", "platform": "platform_key"}.get(entity_type, "campaign_id")
    if entity_id is not None:
        facts = facts[facts[col] == entity_id]
    end = max(facts["date"])
    recent = facts[facts["date"] > end - dt.timedelta(days=14)]
    prior = facts[(facts["date"] > end - dt.timedelta(days=42)) & (facts["date"] <= end - dt.timedelta(days=14))]
    metrics = metrics or ["spend", "attributed_revenue", "attributed_orders", "contribution_margin"]
    # windows differ in length, so compare daily averages rather than raw sums
    recent_days = max(int(recent["date"].nunique()), 1)
    prior_days = max(int(prior["date"].nunique()), 1)
    out: dict[str, Any] = {}
    for m in metrics:
        a = float(recent[m].sum()) / recent_days
        b = float(prior[m].sum()) / prior_days
        out[m] = {"recent_daily": round(a, 2), "prior_daily": round(b, 2),
                  "change_pct": round((a / b - 1) * 100, 2) if b else 0.0}
    agg = {
        "spend": float(recent["spend"].sum()), "impressions": float(recent["impressions"].sum()),
        "clicks": float(recent["clicks"].sum()), "attributed_revenue": float(recent["attributed_revenue"].sum()),
        "attributed_orders": float(recent["attributed_orders"].sum()),
        "contribution_margin": float(recent["contribution_margin"].sum()),
    }
    factors = decomposition.derive_factors(agg)
    # derived factors are ratios, so window length cancels out
    summary = (
        f"Last 14 days vs prior 28: spend {out['spend']['change_pct']:+.1f}%, "
        f"attributed revenue {out['attributed_revenue']['change_pct']:+.1f}%, "
        f"orders {out['attributed_orders']['change_pct']:+.1f}%, "
        f"contribution margin {out['contribution_margin']['change_pct']:+.1f}%. "
        f"ROAS now {factors['roas']:.2f}x, CVR {factors['cvr']*100:.2f}%, AOV ${factors['aov']:.2f}."
    )
    return {"summary": summary, "data": {"windows": out, "factors": {k: round(v, 6) for k, v in factors.items()}}}


def tool_run_decomposition(
    db: Session, brand_id: int, metric: str = "roas", entity_type: str = "campaign",
    entity_id: int | None = None,
) -> dict[str, Any]:
    """Factor decomposition of a multiplicative metric between the two windows."""
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.ad_set_id, UnifiedFact.creative_id,
        UnifiedFact.sku_id, UnifiedFact.impressions, UnifiedFact.clicks, UnifiedFact.spend,
        UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders, UnifiedFact.contribution_margin,
    ).where(UnifiedFact.brand_id == brand_id))
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    col = {"campaign": "campaign_id", "ad_set": "ad_set_id", "creative": "creative_id", "sku": "sku_id"}.get(entity_type, "campaign_id")
    if entity_id is not None:
        facts = facts[facts[col] == entity_id]
    end = max(facts["date"])
    recent = facts[facts["date"] > end - dt.timedelta(days=14)]
    prior = facts[(facts["date"] > end - dt.timedelta(days=42)) & (facts["date"] <= end - dt.timedelta(days=14))]
    before = decomposition.derive_factors({c: float(prior[c].sum()) for c in ("spend", "impressions", "clicks", "attributed_revenue", "attributed_orders", "contribution_margin")})
    after = decomposition.derive_factors({c: float(recent[c].sum()) for c in ("spend", "impressions", "clicks", "attributed_revenue", "attributed_orders", "contribution_margin")})
    result = decomposition.metric_decomposition(metric, before, after)
    if not result.get("available"):
        return {"summary": result.get("reason", "decomposition unavailable"), "data": result}
    top = result["factors"][:3]
    summary = (
        f"{result['formula']}: total change {result['modelled_change_pct']:+.1f}%. "
        + "; ".join(f"{f['factor']} {f['change_pct']:+.1f}% ({f['contribution_pct']:+.0f}% of the move)" for f in top)
    )
    return {"summary": summary, "data": {"decomposition": result, "before": before, "after": after}}


def tool_run_causal_impact(
    db: Session, brand_id: int, entity_type: str = "campaign", entity_id: int | None = None,
    metric: str = "attributed_revenue", event_date: dt.date | None = None,
) -> dict[str, Any]:
    """Synthetic-control / DiD counterfactual around an event date."""
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.campaign_id, UnifiedFact.ad_set_id, UnifiedFact.sku_id,
        UnifiedFact.attributed_revenue, UnifiedFact.attributed_orders, UnifiedFact.spend,
    ).where(UnifiedFact.brand_id == brand_id))
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    col = {"campaign": "campaign_id", "ad_set": "ad_set_id", "sku": "sku_id"}.get(entity_type, "campaign_id")
    end = max(facts["date"])
    event = event_date or (end - dt.timedelta(days=21))
    daily = facts.groupby(["date", col], as_index=False)[metric].sum()
    treated = daily[daily[col] == entity_id].sort_values("date")
    donors = daily[daily[col] != entity_id]
    donor_ids = [i for i in donors[col].unique() if i is not None][:14]
    tpre = treated[treated["date"] < event][metric].to_numpy(float)[-60:]
    tpost = treated[treated["date"] >= event][metric].to_numpy(float)
    dpre = {str(i): donors[(donors[col] == i) & (donors["date"] < event)].sort_values("date")[metric].to_numpy(float)[-60:] for i in donor_ids}
    dpost = {str(i): donors[(donors[col] == i) & (donors["date"] >= event)].sort_values("date")[metric].to_numpy(float) for i in donor_ids}
    result = causal_engine.estimate_causal_effect(tpre, tpost, dpre, dpost, label=metric, unit="usd")
    if not result.get("available"):
        return {"summary": result.get("reason", "causal estimate unavailable"), "data": result}
    summary = (
        f"{result['method']} around {event}: effect {result.get('effect_pct', 0):+.1f}% "
        f"[{result.get('ci_low')}, {result.get('ci_high')}] at {result.get('confidence', 0)*100:.0f}% confidence "
        f"(p={result.get('p_value')})."
    )
    return {"summary": summary, "data": result}


def tool_check_inventory(db: Session, brand_id: int, sku_code: str | None = None, sku_id: int | None = None) -> dict[str, Any]:
    """Days-of-cover and stockout risk for one SKU or the whole catalogue."""
    inv = _read(db, select(
        InventorySnapshot.date, InventorySnapshot.sku_id, InventorySnapshot.on_hand,
        InventorySnapshot.inbound, InventorySnapshot.days_of_cover, InventorySnapshot.daily_velocity,
    ).where(InventorySnapshot.brand_id == brand_id))
    inv["date"] = pd.to_datetime(inv["date"]).dt.date
    skus = {s.id: s for s in db.query(Sku).filter(Sku.brand_id == brand_id).all()}
    if sku_code and not sku_id:
        sku_id = next((sid for sid, s in skus.items() if s.sku_code == sku_code), None)
    if sku_id is not None:
        inv = inv[inv["sku_id"] == sku_id]
    end = max(inv["date"])
    recent = inv[inv["date"] > end - dt.timedelta(days=14)]
    prior = inv[(inv["date"] > end - dt.timedelta(days=42)) & (inv["date"] <= end - dt.timedelta(days=14))]
    grouped = recent.groupby("sku_id", as_index=False).agg(cover=("days_of_cover", "mean"), on_hand=("on_hand", "mean"), velocity=("daily_velocity", "mean"))
    prior_g = prior.groupby("sku_id", as_index=False).agg(prior_cover=("days_of_cover", "mean"))
    merged = grouped.merge(prior_g, on="sku_id", how="left").fillna(0)
    at_risk = merged[merged["cover"] < 14]
    rows = [
        {"sku": skus[int(r.sku_id)].sku_code if int(r.sku_id) in skus else str(r.sku_id),
         "days_of_cover": round(r.cover, 1), "prior_cover": round(r.prior_cover, 1),
         "on_hand": round(r.on_hand, 0), "velocity_per_day": round(r.velocity, 2),
         "risk": "critical" if r.cover < 7 else ("at_risk" if r.cover < 14 else "ok")}
        for _, r in merged.sort_values("cover").head(10).iterrows()
    ]
    summary = (
        f"{len(at_risk)} SKUs below the 14-day cover guardrail. "
        + ("; ".join(f"{r['sku']} at {r['days_of_cover']}d cover" for r in rows[:3]) if rows else "")
    )
    return {"summary": summary, "data": {"at_risk_count": int(len(at_risk)), "skus": rows}}


def tool_check_price_history(db: Session, brand_id: int, sku_code: str | None = None) -> dict[str, Any]:
    """Recent list-price / discount changes."""
    rows = db.query(SkuPriceHistory, Sku.sku_code).join(Sku, SkuPriceHistory.sku_id == Sku.id).filter(
        SkuPriceHistory.brand_id == brand_id,
    ).order_by(SkuPriceHistory.effective_date.desc()).limit(400).all()
    changes = [
        {"sku": code, "effective_date": str(ph.effective_date), "price": ph.price,
         "previous_price": ph.previous_price, "discount_pct": ph.discount_pct,
         "reason": ph.change_reason, "promo": ph.promo_name}
        for ph, code in rows
        if ph.change_reason not in {"baseline"} and (sku_code is None or code == sku_code)
    ]
    material = [c for c in changes if c["reason"] == "list_price_increase"]
    summary = (
        f"{len(material)} list-price increases in the last 12 months"
        + ("; " + "; ".join(f"{c['sku']} ${c['previous_price']}→${c['price']} on {c['effective_date']}" for c in material[:3]) if material else "")
        + f". {len(changes)} total price/promo events recorded."
    )
    return {"summary": summary, "data": {"changes": changes[:20], "material_changes": material}}


def tool_check_creative_fatigue(db: Session, brand_id: int, creative_id: int | None = None) -> dict[str, Any]:
    """CTR decay and frequency growth per creative."""
    facts = _read(db, select(
        UnifiedFact.date, UnifiedFact.creative_id, UnifiedFact.impressions, UnifiedFact.clicks, UnifiedFact.frequency
    ).where(UnifiedFact.brand_id == brand_id, UnifiedFact.creative_id.is_not(None)))
    facts["date"] = pd.to_datetime(facts["date"]).dt.date
    if creative_id is not None:
        facts = facts[facts["creative_id"] == creative_id]
    end = max(facts["date"])
    recent = facts[facts["date"] > end - dt.timedelta(days=14)]
    prior = facts[(facts["date"] > end - dt.timedelta(days=42)) & (facts["date"] <= end - dt.timedelta(days=14))]
    grouped = recent.groupby("creative_id", as_index=False).agg(imp=("impressions", "sum"), clk=("clicks", "sum"), freq=("frequency", "mean"))
    grouped["ctr"] = grouped["clk"] / grouped["imp"].replace(0, np.nan)
    prior_g = prior.groupby("creative_id", as_index=False).agg(prior_imp=("impressions", "sum"), prior_clk=("clicks", "sum"), prior_freq=("frequency", "mean"))
    prior_g["prior_ctr"] = prior_g["prior_clk"] / prior_g["prior_imp"].replace(0, np.nan)
    merged = grouped.merge(prior_g[["creative_id", "prior_ctr", "prior_freq"]], on="creative_id", how="left").fillna(0)
    merged["ctr_change_pct"] = np.where(merged["prior_ctr"] > 0, (merged["ctr"] / merged["prior_ctr"] - 1) * 100, 0.0)
    merged["freq_change_pct"] = np.where(merged["prior_freq"] > 0, (merged["freq"] / merged["prior_freq"] - 1) * 100, 0.0)
    creatives = {c.id: c.name for c in db.query(Creative).filter(Creative.brand_id == brand_id).all()}
    merged = merged[merged["imp"] > 5000].sort_values("ctr_change_pct")
    rows = [
        {"creative": creatives.get(int(r.creative_id), str(r.creative_id)),
         "ctr_change_pct": round(r.ctr_change_pct, 1), "freq_change_pct": round(r.freq_change_pct, 1),
         "frequency": round(r.freq, 2), "fatigue": "severe" if r.ctr_change_pct < -25 and r.freq_change_pct > 20 else "watch"}
        for _, r in merged.head(8).iterrows()
    ]
    fatigued = [r for r in rows if r["fatigue"] == "severe"]
    summary = (
        f"{len(fatigued)} creatives show severe fatigue (CTR down >25% with frequency up >20%). "
        + ("; ".join(f"{r['creative'][:38]}: CTR {r['ctr_change_pct']:+.0f}%, frequency {r['freq_change_pct']:+.0f}%" for r in fatigued[:3]) if fatigued else "")
    )
    return {"summary": summary, "data": {"creatives": rows}}


def tool_check_tracking_health(db: Session, brand_id: int) -> dict[str, Any]:
    """Compare GA4 purchase signals against store orders to spot tracking breaks."""
    ga = _read(db, select(GaEvent.date, GaEvent.sessions, GaEvent.purchase, GaEvent.purchase_revenue).where(GaEvent.brand_id == brand_id))
    ga["date"] = pd.to_datetime(ga["date"]).dt.date
    daily = ga.groupby("date", as_index=False).agg(sessions=("sessions", "sum"), purchases=("purchase", "sum"), revenue=("purchase_revenue", "sum"))
    daily = daily.sort_values("date")
    end = max(daily["date"])
    recent = daily[daily["date"] > end - dt.timedelta(days=10)]
    prior = daily[(daily["date"] > end - dt.timedelta(days=40)) & (daily["date"] <= end - dt.timedelta(days=10))]
    med = float(prior["sessions"].median()) or 1.0
    low_days = recent[recent["sessions"] < med * 0.6]
    ratio = float(recent["purchases"].sum()) / max(float(recent["sessions"].sum()), 1)
    summary = (
        f"GA4 sessions median {med:,.0f}/day; {len(low_days)} of the last 10 days are >40% below that median "
        f"({'tracking anomaly' if len(low_days) >= 3 else 'normal'}). Purchase/session ratio {ratio*100:.2f}%."
    )
    return {
        "summary": summary,
        "data": {
            "median_sessions": round(med, 0),
            "low_days": [{"date": str(r.date), "sessions": int(r.sessions)} for _, r in low_days.iterrows()],
            "purchase_rate_pct": round(ratio * 100, 3),
            "tracking_issue": len(low_days) >= 3,
        },
    }


TOOLS: dict[str, dict[str, Any]] = {
    "query_unified_facts": {"fn": tool_query_unified_facts, "label": "Query reconciled facts",
                            "args": ["entity_type", "entity_id", "days"]},
    "run_decomposition": {"fn": tool_run_decomposition, "label": "Decompose metric into factors",
                          "args": ["metric", "entity_type", "entity_id"]},
    "run_causal_impact": {"fn": tool_run_causal_impact, "label": "Causal impact (synthetic control / DiD)",
                          "args": ["entity_type", "entity_id", "metric", "event_date"]},
    "check_inventory": {"fn": tool_check_inventory, "label": "Check inventory cover", "args": ["sku_code", "sku_id"]},
    "check_price_history": {"fn": tool_check_price_history, "label": "Check price & promo history", "args": ["sku_code"]},
    "check_creative_fatigue": {"fn": tool_check_creative_fatigue, "label": "Check creative fatigue", "args": ["creative_id"]},
    "check_tracking_health": {"fn": tool_check_tracking_health, "label": "Check tracking health", "args": []},
}


def call_tool(db: Session, brand_id: int, name: str, args: dict[str, Any]) -> dict[str, Any]:
    spec = TOOLS.get(name)
    if not spec:
        return {"summary": f"unknown tool '{name}'", "data": {}}
    clean = {k: v for k, v in (args or {}).items() if k in spec["args"] and v is not None}
    try:
        return spec["fn"](db, brand_id, **clean)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("tool %s failed: %s", name, exc)
        return {"summary": f"tool '{name}' failed: {exc}", "data": {}}


# --------------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------------- #
def build_plan(question: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Deterministic planner: chooses tools from the question + anomaly context."""
    context = context or {}
    q = question.lower()
    plan: list[dict[str, Any]] = []

    def add(tool: str, args: dict[str, Any], why: str) -> None:
        plan.append({"tool": tool, "args": args, "why": why})

    entity_type = context.get("entity_type", "campaign")
    entity_id = context.get("entity_id")
    metric = context.get("metric", "roas")

    if context.get("anomaly_id") or any(w in q for w in ("why", "drop", "spike", "changed", "diagnose")):
        add("query_unified_facts", {"entity_type": entity_type, "entity_id": entity_id}, "establish the size and direction of the change")
        add("run_decomposition", {"metric": metric, "entity_type": entity_type, "entity_id": entity_id}, "attribute the move to CTR/CVR/AOV/CPC")
    if any(w in q for w in ("creative", "fatigue", "ad", "ctr")) or metric in {"ctr", "frequency"}:
        add("check_creative_fatigue", {"creative_id": context.get("creative_id")}, "test creative fatigue / audience saturation")
    if any(w in q for w in ("inventory", "stock", "cover", "out of stock")) or entity_type == "sku":
        add("check_inventory", {"sku_code": context.get("sku_code"), "sku_id": context.get("sku_id")}, "test stock availability")
    if any(w in q for w in ("price", "promo", "discount", "margin")) or context.get("check_price"):
        add("check_price_history", {"sku_code": context.get("sku_code")}, "test pricing / promo effects")
    if any(w in q for w in ("tracking", "ga4", "attribution", "data")) or context.get("check_tracking"):
        add("check_tracking_health", {}, "rule out a data artifact before blaming performance")
    if any(w in q for w in ("impact", "causal", "effect", "lift", "cannibal")) or context.get("check_causal"):
        add("run_causal_impact", {
            "entity_type": entity_type, "entity_id": entity_id, "metric": "attributed_revenue",
            "event_date": context.get("event_date"),
        }, "quantify the effect with a counterfactual")
    if any(w in q for w in ("where should", "budget", "allocate", "reallocate", "next $")):
        add("query_unified_facts", {"entity_type": "platform", "entity_id": None}, "size the platform-level opportunity")
        add("check_inventory", {}, "screen out SKUs that cannot absorb more demand")
    if not plan:
        add("query_unified_facts", {"entity_type": entity_type, "entity_id": entity_id}, "establish current performance")
        add("check_tracking_health", {}, "rule out data artifacts")
    # de-duplicate while preserving order
    seen = set()
    unique = []
    for step in plan:
        key = (step["tool"], tuple(sorted(step["args"].items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(step)
    return unique


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run_agent(
    db: Session,
    brand_id: int,
    question: str,
    context: dict[str, Any] | None = None,
    kind: str = "diagnosis",
    anomaly_id: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    started = time.time()
    plan = build_plan(question, context)
    run = AgentRun(
        brand_id=brand_id, kind=kind, trigger=context.get("trigger", "manual") if context else "manual",
        question=question, status="running", plan=plan, tools_used=[p["tool"] for p in plan],
        anomaly_id=anomaly_id,
    )
    if persist:
        db.add(run)
        db.flush()

    steps: list[dict[str, Any]] = []
    for idx, planned in enumerate(plan):
        t0 = time.time()
        result = call_tool(db, brand_id, planned["tool"], planned["args"])
        steps.append({
            "idx": idx, "phase": "tool", "tool": planned["tool"], "thought": planned["why"],
            "arguments": planned["args"], "result_summary": result["summary"], "result": result["data"],
            "duration_ms": int((time.time() - t0) * 1000),
        })

    # ---- synthesize ---------------------------------------------------------
    evidence_lines = [f"{s['tool']}: {s['result_summary']}" for s in steps if s["result_summary"]]
    template = copilot_answer_template(question, steps)
    narrative_source = "template"
    final = template
    tokens = 0
    if llm.available and evidence_lines:
        system = (
            "You are AdPilot's diagnostic agent for a D2C brand. Using ONLY the tool outputs provided, "
            "write a short, decisive answer (max 200 words). Quote the exact numbers and name the tool "
            "that produced each. Never invent data. If the evidence is inconclusive, say so plainly."
        )
        prompt = f"QUESTION: {question}\n\nTOOL OUTPUTS:\n" + "\n".join(f"- {line}" for line in evidence_lines)
        llm_result = llm.complete(system, prompt, max_tokens=450)
        if llm_result.text:
            final = llm_result.text
            narrative_source = "llm"
            tokens = llm_result.tokens

    synth_step = {
        "idx": len(steps), "phase": "synthesize", "tool": "synthesizer",
        "thought": "phrase the computed evidence into a grounded answer",
        "arguments": {}, "result_summary": final[:400], "result": {"sources": [s["tool"] for s in steps]},
        "duration_ms": 0,
    }
    steps.append(synth_step)

    if persist:
        for s in steps:
            db.add(AgentStep(brand_id=brand_id, run_id=run.id, **s))
        run.status = "completed"
        run.final_answer = final
        run.narrative_source = narrative_source
        run.tokens_used = tokens
        run.duration_ms = int((time.time() - started) * 1000)
        run.tools_used = [s["tool"] for s in steps if s["phase"] == "tool"]
        db.flush()

    return {
        "run_id": run.id if persist else None,
        "question": question,
        "plan": plan,
        "steps": steps,
        "answer": final,
        "narrative_source": narrative_source,
        "tools_used": run.tools_used,
        "duration_ms": int((time.time() - started) * 1000),
    }