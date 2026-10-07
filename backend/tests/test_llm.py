from __future__ import annotations

import httpx

from app.core.config import settings
from app.engines.llm import LLMClient, build_prompt_context


def test_build_prompt_context_is_compact_and_grounded():
    evidence = {
        "status": {
            "clock_date": "2026-10-06",
            "recent_spend": 5000,
            "recent_revenue": 12000,
            "blended_roas": 2.4,
            "open_anomalies": 3,
            "open_recommendations": 2,
            "skus_at_risk": 4,
        },
        "anomaly": {
            "metric": "revenue",
            "entity_name": "Meta Prospecting",
            "magnitude_pct": -12.5,
            "severity": 81,
            "primary_driver": "creative_fatigue",
        },
        "opportunities": [{"title": "Scale Meta", "expected_profit_delta": 6500, "confidence": 0.82}],
        "tracking": {"status": "degraded", "reason": "GA4 discrepancy"},
    }

    payload = build_prompt_context("Why did ROAS decline?", evidence)

    assert payload["question"] == "Why did ROAS decline?"
    assert payload["summary"]["roas"] == 2.4
    assert payload["summary"]["open_anomalies"] == 3
    assert payload["summary"]["inventory_risk_skus"] == 4
    assert payload["anomaly"]["entity_name"] == "Meta Prospecting"
    assert "tracking" in payload
    assert "opportunities" in payload
    assert "raw_evidence" not in payload


def test_openai_compatible_completion(monkeypatch):
    client = LLMClient()
    client.provider = "openai"
    client.model = "test-model"
    client.api_key = "test-key"
    client.base_url = "https://llm.example/v1"
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    response = httpx.Response(
        200,
        request=request,
        json={
            "choices": [{"message": {"content": "A grounded recommendation."}}],
            "usage": {"total_tokens": 18},
        },
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return response

    monkeypatch.setattr("app.engines.llm.httpx.post", fake_post)

    result = client.complete("system instructions", "computed evidence", max_tokens=120)

    assert result.text == "A grounded recommendation."
    assert result.source == "llm"
    assert result.tokens == 18
    assert calls[0][0] == "https://llm.example/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer test-key"
    assert calls[0][1]["json"]["messages"][1]["content"] == "computed evidence"


def test_demo_key_enables_provider_path(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "demo-key")
    monkeypatch.setattr(settings, "openai_api_key", "")

    client = LLMClient()

    assert client.available
    assert client.provider == "openai"
    assert client.api_key == "demo-key"