"""AI Copilot.

The copilot answers natural-language questions by routing them to the same
analysis tools the diagnosis agent uses — it never invents numbers. Every
answer carries the tool calls that produced it, so the UI can show sources and
evidence, and it can offer to turn the finding into a recommendation.

Routing is deterministic (keyword + entity resolution against the real
catalogue) and the prose is written by the provider-agnostic LLM wrapper with a
template fallback, so the copilot works with no API key at all.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ..core.logging import get_logger
from ..db.models import (
    Anomaly,
    Campaign,
    Creative,
    Diagnosis,
    InventorySnapshot,
    Opportunity,
    Recommendation,
    Sku,
    UnifiedFact,
)
from . import agent as agent_tools
from .llm import LLMClient, copilot_answer_template

log = get_logger("adpilot.copilot")

INTENTS = {
    "why_drop": ["why", "drop", "decline", "fell", "down", "worse", "decrease"],
    "where_to_spend": ["where", "next", "put", "allocate", "budget", "spend", "invest"],
    "inventory": ["stock", "inventory", "cover", "stockout", "out of stock"],
    "creative": ["creative", "fatigue", "ad ", "video", "asset"],
    "tracking": ["tracking", "ga4", "attribution", "discrepancy", "overlap", "measurement"],
    "price": ["price", "discount", "promo", "pricing"],
    "performance": ["roas", "mer", "cpa", "cpc", "ctr", "cvr", "aov", "performance", "efficiency"],
    "opportunity": ["opportunity", "upside", "headroom", "growth", "scale"],
    "status": ["status", "summary", "overview", "how are we", "health", "pipeline"],
}

SUGGESTED_QUESTIONS = [
    "Why did Meta ROAS drop last week?",
    "Where should I put the next $5,000?",
    "Which promoted SKUs are at risk of stocking out?",
    "Is the GA4 drop a real performance problem?",
    "Which creatives are fatiguing?",
    "How is the optimizer's plan different from today's spend?",
]


def _classify(question: str) -> str:
    text = question.lower()
    scores = {intent: sum(1 for kw in kws if kw in text) for intent, kws in INTENTS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "status"


def _resolve_entities(db: Session, brand_id: int, question: str) -> dict[str, Any]:
    """Find the campaign / SKU / creative / platform the question is about."""
    text = question.lower()
    found: dict[str, Any] = {"campaign_id": None, "sku_id": None, "creative_id": None, "platform_key": None}
    for platform in ("meta", "google", "amazon", "tiktok", "programmatic", "ga4", "shopify"):
        if platform in text:
            found["platform_key"] = platform
            break
    best_campaign, best_len = None, 0
    for campaign in db.query(Campaign).filter(Campaign.brand_id == brand_id).all():
        tokens = [t for t in re.split(r"[^a-z0-9]+", campaign.name.lower()) if len(t) > 3]
        hits = sum(1 for t in tokens if t in text)
        if hits > best_len:
            best_campaign, best_len = campaign, hits
    if best_len >= 2:
        found["campaign_id"] = best_campaign.id
        found["campaign_name"] = best_campaign.name
    for sku in db.query(Sku).filter(Sku.brand_id == brand_id).all():
        tokens = [t for t in re.split(r"[^a-z0-9]+", sku.name.lower()) if len(t) > 4]
        if any(t in text for t in tokens) or sku.sku_code.lower() in text:
            found["sku_id"] = sku.id
            found["sku_code"] = sku.sku_code
            break
    return found


def ask(db: Session, brand_id: int, question: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Answer a question using the shared analysis tools."""
    intent = _classify(question)
    entities = _resolve_entities(db, brand_id, question)
    context = context or {}
    if context.get("anomaly_id"):
        entities["anomaly_id"] = context["anomaly_id"]

    tool_calls: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}

    def call(name: str, args: dict[str, Any]) -> Any:
        result = agent_tools.call_tool(db, brand_id, name, args)
        tool_calls.append({"tool": name, "arguments": args,
                           "result_summary": _summarise(result)})
        return result

    if intent in {"why_drop", "performance"}:
        if entities.get("anomaly_id"):
            anomaly = db.query(Anomaly).filter(Anomaly.id == entities["anomaly_id"]).first()
            if anomaly:
                evidence["anomaly"] = _anomaly_payload(db, anomaly)
                evidence["decomposition"] = call("run_decomposition", {
                    "metric": anomaly.metric, "entity_type": anomaly.entity_type,
                    "entity_id": anomaly.entity_id, "direction": anomaly.direction})
                evidence["causal"] = call("run_causal_impact", {
                    "entity_type": anomaly.entity_type, "entity_id": anomaly.entity_id})
        else:
            where = {"platform_key": entities.get("platform_key"), "campaign_id": entities.get("campaign_id")}
            evidence["change"] = call("query_unified_facts", {k: v for k, v in where.items() if v})
            evidence["tracking"] = call("check_tracking_health", {})
            if entities.get("campaign_id"):
                evidence["creative"] = call("check_creative_fatigue", {"campaign_id": entities["campaign_id"]})
                evidence["inventory"] = call("check_inventory", {})
    elif intent == "inventory":
        evidence["inventory"] = call("check_inventory", {"sku_code": entities.get("sku_code")})
        evidence["spend"] = call("query_unified_facts", {"sku_id": entities.get("sku_id")}
                                 if entities.get("sku_id") else {})
    elif intent == "creative":
        evidence["creative"] = call("check_creative_fatigue", {"creative_id": entities.get("creative_id")})
        evidence["change"] = call("query_unified_facts", {"campaign_id": entities.get("campaign_id")}
                                  if entities.get("campaign_id") else {})
    elif intent == "tracking":
        evidence["tracking"] = call("check_tracking_health", {})
        evidence["change"] = call("query_unified_facts", {})
    elif intent == "price":
        evidence["price"] = call("check_price_history", {"sku_code": entities.get("sku_code")})
        evidence["change"] = call("query_unified_facts", {})
    elif intent in {"where_to_spend", "opportunity"}:
        evidence["opportunities"] = _top_opportunities(db, brand_id, entities.get("platform_key"))
        evidence["recommendations"] = _open_recommendations(db, brand_id)
    else:
        evidence["status"] = _status_snapshot(db, brand_id)
        evidence["tracking"] = call("check_tracking_health", {})

    evidence["intent"] = intent
    evidence["entities"] = entities
    answer, source = _narrate(question, intent, evidence, tool_calls)
    suggestions = _follow_ups(db, brand_id, intent, evidence)

    return {
        "question": question,
        "answer": answer,
        "narrative_source": source,
        "intent": intent,
        "entities": entities,
        "tool_calls": tool_calls,
        "evidence": evidence,
        "sources": _sources(intent, tool_calls, evidence),
        "follow_ups": suggestions,
        "offer_recommendation": _offer(db, brand_id, intent, evidence),
        "generated_at": dt.datetime.utcnow().isoformat(),
    }


def _anomaly_payload(db: Session, anomaly: Anomaly) -> dict[str, Any]:
    diag = db.query(Diagnosis).filter(Diagnosis.anomaly_id == anomaly.id).first()
    return {
        "id": anomaly.id, "metric": anomaly.metric, "entity_type": anomaly.entity_type,
        "entity_name": anomaly.entity_name, "direction": anomaly.direction,
        "magnitude_pct": anomaly.magnitude_pct, "severity": anomaly.severity,
        "window_start": str(anomaly.window_start), "window_end": str(anomaly.window_end),
        "is_data_artifact": anomaly.is_data_artifact, "artifact_reason": anomaly.artifact_reason,
        "primary_driver": diag.primary_driver if diag else None,
        "drivers": (diag.drivers or [])[:4] if diag else [],
        "narrative": diag.narrative if diag else "",
    }


def _top_opportunities(db: Session, brand_id: int, platform_key: str | None) -> list[dict[str, Any]]:
    query = db.query(Opportunity).filter(Opportunity.brand_id == brand_id, Opportunity.status == "open")
    if platform_key:
        query = query.filter(Opportunity.platform_key == platform_key)
    return [
        {"id": o.id, "kind": o.kind, "title": o.title, "entity_name": o.entity_name,
         "platform_key": o.platform_key, "expected_profit_delta": round(o.expected_profit_delta, 2),
         "confidence": o.confidence, "score": o.score}
        for o in query.order_by(Opportunity.score.desc()).limit(6).all()
    ]


def _open_recommendations(db: Session, brand_id: int) -> list[dict[str, Any]]:
    return [
        {"id": r.id, "kind": r.kind, "title": r.title, "entity_name": r.entity_name,
         "expected_profit_delta": round(r.expected_profit_delta, 2), "confidence": r.confidence,
         "risk_level": r.risk_level, "priority_score": r.priority_score,
         "requires_approval": r.requires_approval}
        for r in db.query(Recommendation).filter(
            Recommendation.brand_id == brand_id, Recommendation.status == "proposed"
        ).order_by(Recommendation.priority_score.desc()).limit(6).all()
    ]


def _status_snapshot(db: Session, brand_id: int) -> dict[str, Any]:
    from sqlalchemy import func as sqlfunc

    from .reconcile import _read

    facts = _read(db, __import__("sqlalchemy").select(
        UnifiedFact.date, UnifiedFact.spend, UnifiedFact.attributed_revenue, UnifiedFact.contribution_margin,
    ).where(UnifiedFact.brand_id == brand_id))
    if facts.empty:
        return {"facts": 0}
    facts["date"] = __import__("pandas").to_datetime(facts["date"]).dt.date
    clock = max(facts["date"])
    recent = facts[facts["date"] > clock - dt.timedelta(days=14)]
    prior = facts[(facts["date"] > clock - dt.timedelta(days=28)) & (facts["date"] <= clock - dt.timedelta(days=14))]
    return {
        "clock_date": str(clock),
        "recent_spend": round(float(recent["spend"].sum()), 2),
        "recent_revenue": round(float(recent["attributed_revenue"].sum()), 2),
        "recent_profit": round(float(recent["contribution_margin"].sum() - recent["spend"].sum()), 2),
        "blended_roas": round(float(recent["attributed_revenue"].sum() / max(recent["spend"].sum(), 1e-9)), 2),
        "prior_blended_roas": round(float(prior["attributed_revenue"].sum() / max(prior["spend"].sum(), 1e-9)), 2),
        "open_anomalies": db.query(Anomaly).filter(Anomaly.brand_id == brand_id, Anomaly.status == "open").count(),
        "open_recommendations": db.query(Recommendation).filter(
            Recommendation.brand_id == brand_id, Recommendation.status == "proposed").count(),
        "opportunities": db.query(Opportunity).filter(
            Opportunity.brand_id == brand_id, Opportunity.status == "open").count(),
        "skus_at_risk": db.query(InventorySnapshot).filter(
            InventorySnapshot.brand_id == brand_id, InventorySnapshot.days_of_cover < 14).count(),
    }


def _summarise(result: Any) -> str:
    if isinstance(result, dict):
        keys = [k for k in ("change", "evidence", "drivers", "summary", "blended_roas", "skus", "skus_at_risk")
                if k in result]
        return "keys: " + ", ".join(keys) if keys else f"{len(result)} fields"
    if isinstance(result, list):
        return f"{len(result)} rows"
    return str(result)[:80]


def _narrate(question: str, intent: str, evidence: dict[str, Any],
             tool_calls: list[dict[str, Any]]) -> tuple[str, str]:
    payload = {
        "question": question, "intent": intent,
        "evidence": evidence,
        "tool_calls": [c["tool"] for c in tool_calls],
    }
    template = _template_answer(payload)
    client = LLMClient()
    if client.available:
        system = (
            "You are AdPilot's analyst copilot. Answer the user's question using ONLY the supplied evidence. "
            "Quote the exact numbers and name the analysis that produced each one. Be concise (max 160 words), "
            "start with the direct answer, then the evidence, then one recommended next action. If the evidence "
            "shows a measurement artifact, say so explicitly. Never invent a number that is not in the evidence."
        )
        result = client.complete(system, json.dumps(payload, default=str)[:12000], max_tokens=450)
        if result.source == "llm" and result.text.strip():
            return result.text.strip(), "llm"
    return template, "template"


def _compact(evidence: dict[str, Any]) -> Any:
    import json

    try:
        text = json.dumps(evidence, default=str)
    except (TypeError, ValueError):
        text = str(evidence)
    return text[:6000]


def _template_answer(data: dict[str, Any]) -> str:
    intent = data.get("intent", "status")
    evidence = data.get("evidence", {})
    if intent in {"why_drop", "performance"} and isinstance(evidence.get("change"), dict):
        change = evidence["change"].get("change") or evidence["change"].get("metrics") or {}
        parts = []
        for metric, values in list(change.items())[:5]:
            if isinstance(values, dict) and "change_pct" in values:
                parts.append(f"{metric} {values['change_pct']:+.1f}%")
        head = "The most recent 14 days compared with the prior period show " + ", ".join(parts) + "." if parts else \
            "No material change was found in the compared windows."
        tracking = evidence.get("tracking") or {}
        if isinstance(tracking, dict) and tracking.get("status") == "degraded":
            head += (" Note that GA4 measurement is degraded, so part of any apparent drop is a tracking "
                     "artifact rather than a performance problem.")
        anomaly = evidence.get("anomaly")
        if anomaly:
            head += (f" The flagged {anomaly['metric']} anomaly on {anomaly['entity_name']} "
                     f"({anomaly['magnitude_pct']:+.1f}%, severity {anomaly['severity']:.0f}) is driven mainly by "
                     f"{anomaly.get('primary_driver', 'multiple factors')}.")
        return head + " Recommended next step: open the diagnosis view to confirm the driver ranking before moving budget."
    if intent in {"where_to_spend", "opportunity"}:
        opps = evidence.get("opportunities") or []
        if not opps:
            return "No open opportunities are currently scored; run the Decide stage first."
        top = opps[0]
        return (f"The highest-scoring opportunity is {top['title']} on {top['entity_name']}, worth about "
                f"${top['expected_profit_delta']:,.0f} over 90 days at {top['confidence']*100:.0f}% confidence. "
                f"It is ranked first of {len(opps)} open opportunities. Recommended next step: review the "
                f"allocation in the Budget Optimizer before committing spend.")
    if intent == "inventory":
        inv = evidence.get("inventory") or {}
        skus = inv.get("skus") if isinstance(inv, dict) else None
        if skus:
            worst = sorted(skus, key=lambda s: s.get("days_of_cover", 1e9))[0]
            return (f"{worst['sku_code']} has {worst['days_of_cover']:.0f} days of cover with "
                    f"${worst.get('promoted_spend', 0):,.0f} of recent promoted spend. That is the tightest "
                    f"position in the catalogue. Recommended next step: raise a purchase order and throttle "
                    f"prospecting on that SKU.")
        return "No inventory positions were returned for that query."
    if intent == "tracking":
        tracking = evidence.get("tracking") or {}
        return (f"Measurement status: {tracking.get('status', 'unknown')}. "
                f"{tracking.get('reason', '')} Treat any simultaneous revenue drop as a measurement artifact "
                f"until the tag issue is resolved.")
    if intent == "creative":
        creative = evidence.get("creative") or {}
        worst = creative.get("most_fatigued") if isinstance(creative, dict) else None
        if worst:
            return (f"The most fatigued creative is “{worst['name']}” with a fatigue score of "
                    f"{worst['fatigue_score']:.2f} and CTR {worst['ctr_change_pct']:+.1f}%. "
                    f"Recommended next step: pause it and route budget to the best performer in the same ad set.")
        return "No creative fatigue signal was found in the current window."
    if intent == "price":
        price = evidence.get("price") or {}
        events = price.get("events") if isinstance(price, dict) else None
        if events:
            latest = events[0]
            return (f"The latest price event for {latest.get('sku_code')} was {latest.get('event')} on "
                    f"{latest.get('date')} ({latest.get('change_pct'):+.1f}%). Check the matched conversion rate "
                    f"change before adjusting spend.")
        return "No price events were found for that SKU in the history."
    status = evidence.get("status") or {}
    return (f"Portfolio status as of {status.get('clock_date')}: blended ROAS "
            f"{status.get('blended_roas')} vs {status.get('prior_blended_roas')} in the prior period, "
            f"{status.get('open_anomalies')} open anomalies, {status.get('open_recommendations')} recommendations "
            f"awaiting a decision, and {status.get('skus_at_risk')} inventory snapshots below 14 days of cover.")


def _follow_ups(db: Session, brand_id: int, intent: str, evidence: dict[str, Any]) -> list[str]:
    """Offer deterministic next questions that stay within the available evidence."""
    choices = {
        "where_to_spend": ["Which opportunity has the most inventory headroom?", "Show me the risk behind the top recommendation."],
        "opportunity": ["Which SKU has the most days of cover?", "Compare the top two opportunities."],
        "why_drop": ["Was this change caused by a tracking issue?", "What action is already recommended?"],
        "performance": ["How is contribution margin trending by channel?", "Which campaigns explain the change?"],
        "inventory": ["Which promoted SKU is closest to stockout?", "What is the margin on the at-risk product?"],
        "creative": ["Which creative is fatiguing fastest?", "What refresh action is available?"],
        "tracking": ["Which sources are freshest?", "Show the reconciliation gap by platform."],
        "price": ["Did conversion change after the price event?", "What does the causal comparison say?"],
        "status": ["What are the highest-priority open recommendations?", "What changed in the last 14 days?"],
    }
    return choices.get(intent, choices["status"])


def _sources(intent: str, tool_calls: list[dict[str, Any]], evidence: dict[str, Any]) -> list[str]:
    sources = [f"tool:{c['tool']}" for c in tool_calls]
    if evidence.get("opportunities") is not None:
        sources.append("table:opportunities")
    if evidence.get("recommendations") is not None:
        sources.append("table:recommendations")
    if evidence.get("status"):
        sources.append("table:unified_facts")
    return sources


def _offer(db: Session, brand_id: int, intent: str, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Offer to turn the answer into a recommendation when one is justified."""
    if intent in {"where_to_spend", "opportunity"}:
        opps = evidence.get("opportunities") or []
        for opp in opps:
            rec = db.query(Recommendation).filter(
                Recommendation.brand_id == brand_id, Recommendation.opportunity_id == opp["id"]
            ).first()
            if rec:
                return {"recommendation_id": rec.id, "title": rec.title,
                        "reason": "The top opportunity already has a prepared recommendation."}
    if intent in {"why_drop", "performance"}:
        anomaly = evidence.get("anomaly")
        if anomaly:
            rec = db.query(Recommendation).filter(
                Recommendation.brand_id == brand_id, Recommendation.diagnosis_id == anomaly.get("diagnosis_id")
            ).first() if anomaly.get("diagnosis_id") else None
            if rec:
                return {"recommendation_id": rec.id, "title": rec.title,
                        "reason": "A recommendation already exists for this diagnosis."}
            return {"recommendation_id": None, "title": "Create a recommendation from this diagnosis",
                    "reason": f"Anomaly {anomaly['id']} has a ranked driver list ready to convert."}
    if intent == "inventory":
        rec = db.query(Recommendation).filter(
            Recommendation.brand_id == brand_id, Recommendation.kind == "inventory_reorder",
            Recommendation.status == "proposed",
        ).order_by(Recommendation.priority_score.desc()).first()
        if rec:
            return {"recommendation_id": rec.id, "title": rec.title,
                    "reason": "A reorder recommendation is already queued."}
    return None
