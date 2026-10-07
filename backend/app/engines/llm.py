"""Provider-agnostic LLM layer with a deterministic offline fallback.

Design contract
---------------
* ``LLMClient.available`` is False whenever no key is configured, or the call
  fails. Every caller must therefore be able to produce the *same structured*
  output from the template engine: narratives, driver summaries, copilot
  answers all have a deterministic implementation that is grounded strictly in
  computed evidence (numbers are injected, never invented).
* The LLM is only ever used to *phrase* evidence that the statistical engines
  already produced. It cannot change driver order, numbers or recommendations.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.config import settings
from ..core.logging import get_logger

log = get_logger("adpilot.llm")


@dataclass
class LLMResult:
    text: str
    source: str  # "llm" | "template"
    model: str = ""
    tokens: int = 0
    warnings: list[str] = field(default_factory=list)


class LLMClient:
    """Thin wrapper over OpenAI-compatible and Anthropic chat APIs."""

    def __init__(self) -> None:
        self.provider = "none"
        self.model = ""
        self._client = None
        self._detect()

    def _detect(self) -> None:
        requested = (settings.llm_provider or "auto").lower()
        key = settings.llm_api_key
        base = settings.llm_base_url

        # 1) explicit provider
        if requested in {"openai", "anthropic"} and key:
            self.provider = requested
            self.model = settings.llm_model or (
                "gpt-4o-mini" if requested == "openai" else "claude-3-5-sonnet-latest"
            )
        # 2) auto-detection from key shape
        elif requested == "auto" and key:
            self.provider = "anthropic" if key.startswith("sk-ant") else "openai"
            self.model = settings.llm_model or (
                "gpt-4o-mini" if self.provider == "openai" else "claude-3-5-sonnet-latest"
            )
        # 3) sandbox OpenAI-compatible gateway
        elif requested == "auto" and settings.openai_api_key:
            self.provider = "openai"
            base = base or settings.openai_api_base
            self.model = settings.llm_model or "gpt-4.1-mini"

        if self.provider == "none":
            return
        try:
            if self.provider == "openai":
                from openai import OpenAI

                kwargs: dict[str, Any] = {"api_key": key or settings.openai_api_key}
                if base:
                    kwargs["base_url"] = base
                self._client = OpenAI(**kwargs)
            elif self.provider == "anthropic":
                import anthropic

                self._client = anthropic.Anthropic(api_key=key)
        except Exception as exc:  # pragma: no cover - optional dependency
            log.warning("llm client init failed; falling back to templates: %s", exc)
            self.provider = "none"
            self._client = None

    @property
    def available(self) -> bool:
        return self.provider != "none" and self._client is not None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "provider": self.provider,
            "model": self.model if self.available else "",
            "fallback": "deterministic template engine",
        }

    def complete(self, system: str, prompt: str, max_tokens: int = 700, temperature: float = 0.2) -> LLMResult:
        if not self.available:
            return LLMResult(text="", source="template", warnings=["llm_not_configured"])
        try:
            if self.provider == "openai":
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                text = (resp.choices[0].message.content or "").strip()
                tokens = getattr(resp.usage, "total_tokens", 0) or 0
                return LLMResult(text=text, source="llm", model=self.model, tokens=tokens)
            resp = self._client.messages.create(
                model=self.model,
                system=system,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text").strip()
            tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0) if resp.usage else 0
            return LLMResult(text=text, source="llm", model=self.model, tokens=tokens)
        except Exception as exc:  # pragma: no cover - network dependent
            log.warning("llm call failed, using templates: %s", exc)
            return LLMResult(text="", source="template", warnings=[f"llm_error: {exc}"])


llm = LLMClient()


# --------------------------------------------------------------------------- #
# Deterministic narrative builders (grounded, offline-safe)
# --------------------------------------------------------------------------- #
def _fmt_money(value: float) -> str:
    return f"${value:,.0f}"


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def narrate_diagnosis(evidence: dict[str, Any], drivers: list[dict[str, Any]], decomposition: dict[str, Any]) -> LLMResult:
    """Template narrative first; optionally rephrased by the LLM."""
    entity = evidence.get("entity_name", "the entity")
    metric = evidence.get("metric", "the metric")
    direction = evidence.get("direction", "drop")
    magnitude = float(evidence.get("magnitude_pct", 0.0))
    window = evidence.get("window", "")
    primary = drivers[0] if drivers else {"label": "unclear", "contribution_pct": 0.0, "confidence": 0.0}

    lines: list[str] = []
    if evidence.get("is_data_artifact"):
        lines.append(
            f"**Data artifact detected — not a performance change.** {entity} shows a {direction} of "
            f"{abs(magnitude):.1f}% in {metric} over {window}, but catalogue metrics (impressions, spend, "
            f"store orders) moved normally. {evidence.get('artifact_reason', '')}"
        )
    else:
        lines.append(
            f"{entity} saw a {direction} of **{abs(magnitude):.1f}%** in {metric} over {window} "
            f"(baseline {_fmt_metric(metric, evidence.get('baseline', 0))}, observed "
            f"{_fmt_metric(metric, evidence.get('observed', 0))}; severity "
            f"{float(evidence.get('severity', 0)):.0f}/100, p={float(evidence.get('p_value', 1)):.3f})."
        )
        lines.append(
            f"The primary driver is **{primary.get('label', 'unknown')}**, explaining "
            f"{float(primary.get('contribution_pct', 0)):.0f}% of the move with "
            f"{float(primary.get('confidence', 0)) * 100:.0f}% confidence."
        )

    for driver in drivers[1:4]:
        lines.append(
            f"Secondary driver: {driver.get('label')} ({float(driver.get('contribution_pct', 0)):.0f}% contribution, "
            f"{float(driver.get('confidence', 0)) * 100:.0f}% confidence)."
        )

    factors = decomposition.get("factors") or []
    if factors:
        ranked = sorted(factors, key=lambda f: abs(float(f.get("contribution_pct", 0))), reverse=True)[:3]
        parts = ", ".join(
            f"{f.get('factor')} {_fmt_pct(float(f.get('change_pct', 0)))} "
            f"(contributes {float(f.get('contribution_pct', 0)):+.1f} pp)"
            for f in ranked
        )
        lines.append(f"Metric decomposition of {decomposition.get('formula', '')}: {parts}.")

    causal = evidence.get("causal") or {}
    if causal.get("available"):
        lines.append(
            f"Causal check ({causal.get('method', 'counterfactual')}): effect "
            f"{causal.get('effect_estimate', 0):+.2f} {causal.get('effect_unit', '')} "
            f"[{causal.get('ci_low', 0):.2f}, {causal.get('ci_high', 0):.2f}] at "
            f"{float(causal.get('confidence', 0)) * 100:.0f}% confidence. "
            f"{causal.get('caveat', '')}"
        )

    lines.append(f"Evidence used: {', '.join(sorted({evidence.get('source', 'unified_facts')} | set(evidence.get('evidence_sources', []))))}.")
    template_text = "\n\n".join(lines)

    if not llm.available:
        return LLMResult(text=template_text, source="template")

    system = (
        "You are AdPilot's diagnosis narrator for a direct-to-consumer brand. Rewrite the provided "
        "evidence-grounded summary as 2-4 short paragraphs of fluent analyst prose. Hard rules: never "
        "invent numbers, never reorder or re-word driver rankings, never claim causality beyond the "
        "stated counterfactual bounds, always keep every number exactly as given. If a data artifact "
        "is flagged, lead with that."
    )
    prompt = (
        f"EVIDENCE (authoritative, do not change):\n{json.dumps(evidence, default=str)[:6000]}\n\n"
        f"DRIVERS (ranked):\n{json.dumps(drivers, default=str)[:3000]}\n\n"
        f"DECOMPOSITION:\n{json.dumps(decomposition, default=str)[:2000]}\n\n"
        f"TEMPLATE VERSION:\n{template_text}"
    )
    result = llm.complete(system, prompt, max_tokens=700)
    if result.text:
        return result
    return LLMResult(text=template_text, source="template", warnings=result.warnings)


def _fmt_metric(metric: str, value: float) -> str:
    money_metrics = {"spend", "revenue", "platform_revenue", "attributed_revenue", "cpa", "cpc", "cpm", "aov", "contribution_margin", "profit"}
    if metric in money_metrics:
        return _fmt_money(value)
    if metric in {"ctr", "cvr", "frequency"} and abs(value) < 10:
        return f"{value * 100:.2f}%" if metric != "frequency" else f"{value:.2f}"
    if metric in {"roas", "mer"}:
        return f"{value:.2f}x"
    return f"{value:,.2f}"


def narrate_recommendation(rec: dict[str, Any]) -> str:
    lines = [
        f"{rec.get('title', 'Recommendation')}.",
        f"Cause: {rec.get('cause', 'n/a')}",
        f"Change: {rec.get('current_value')} → {rec.get('proposed_value')} ({rec.get('value_unit', '')}). "
        f"Expected profit impact {_fmt_money(float(rec.get('expected_profit_delta', 0)))} "
        f"(80% interval {_fmt_money(float(rec.get('impact_p10', 0)))} … {_fmt_money(float(rec.get('impact_p90', 0)))}), "
        f"confidence {float(rec.get('confidence', 0)) * 100:.0f}%, risk {rec.get('risk_level', 'low')}.",
    ]
    if rec.get("constraints_check"):
        failed = [c for c in rec["constraints_check"] if not c.get("passed")]
        lines.append(
            "Constraints: all passed." if not failed else "Constraints violated: "
            + ", ".join(c.get("label", c.get("key", "")) for c in failed)
        )
    return " ".join(lines)


def narrate_outcome(outcome: dict[str, Any]) -> str:
    verdict = outcome.get("verdict", "inconclusive")
    return (
        f"Measured over {outcome.get('window_start')} → {outcome.get('window_end')} using "
        f"{outcome.get('counterfactual_method', 'synthetic control')}. Predicted incremental profit "
        f"{_fmt_money(float(outcome.get('predicted_profit_delta', 0)))}, actual "
        f"{_fmt_money(float(outcome.get('actual_profit_delta', 0)))}, error "
        f"{float(outcome.get('pct_error', 0)):+.1f}%. Verdict: {verdict}. "
        f"Confidence {float(outcome.get('confidence', 0)) * 100:.0f}%."
    )


def copilot_answer_template(question: str, tool_results: list[dict[str, Any]]) -> str:
    """Deterministic copilot answer built purely from executed tool results."""
    if not tool_results:
        return (
            "I could not ground an answer in the current data: no analysis tool returned results for "
            f"“{question}”. Try asking about a specific campaign, SKU, platform or date range."
        )
    chunks: list[str] = []
    for tr in tool_results:
        tool = tr.get("tool", "tool")
        summary = tr.get("summary") or tr.get("result_summary") or ""
        if summary:
            chunks.append(f"**{tool}** — {summary}")
    detail = "\n\n".join(chunks)
    return (
        f"Grounded answer for “{question}”:\n\n{detail}\n\n"
        "Every figure above comes from the executed analyses listed; open the Anomaly Studio or "
        "Decision Center to act on it."
    )
