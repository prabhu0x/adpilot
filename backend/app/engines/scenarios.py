"""The eight planted scenarios that ship with the seeded demo brand.

The seeder reads these definitions to *plant* the events; the detection layer
matches produced anomalies back to them so the Scenario Guide page can show a
live detected / diagnosed / recommended checklist.
"""
from __future__ import annotations

from typing import Any

# Absolute dates inside the seeded window (2025-10-07 .. 2026-10-06)
SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "creative_fatigue",
        "title": "Creative fatigue on a top Meta prospecting campaign",
        "description": (
            "The hero UGC creative on 'META | Prospecting | Hero Serum UGC' has been running since "
            "2026-09-08 without rotation: CTR decays ~45%, frequency climbs from ~2.1 to ~4.8 and CPA "
            "spikes, while CPM is roughly stable — the signature of audience saturation rather than "
            "auction inflation."
        ),
        "expected_driver": "creative_fatigue",
        "planted_at": "2026-09-08",
        "metric": "ctr",
        "entity": "META | Prospecting | Hero Serum UGC",
        "platform": "meta",
        "detection": "CTR/CPA anomaly on a single Meta campaign with rising frequency and stable CPM",
    },
    {
        "key": "stockout_risk",
        "title": "Stockout on a heavily promoted hero SKU",
        "description": (
            "LUM-VITC-30 (Vitamin C Brightening Serum) is promoted by three always-on campaigns while "
            "the last purchase order was placed on 2026-06-01 and never followed up. Days-of-cover "
            "decays from ~30 to under 14 by late August and reaches single digits in September while ad "
            "spend continues unabated, so the brand is buying demand it cannot fulfil."
        ),
        "expected_driver": "stockout",
        "planted_at": "2026-09-10",
        "metric": "days_of_cover",
        "entity": "LUM-VITC-30 / META | Retargeting | Vitamin C",
        "platform": "meta",
        "detection": "Inventory-led anomaly: low days-of-cover on a promoted SKU with sustained spend",
    },
    {
        "key": "price_change_cvr_drop",
        "title": "Price increase drops conversion rate on the hydration line",
        "description": (
            "On 2026-09-12 the price of LUM-HYDRA-50 moved from $46 to $54 (+17.4%) and the standing "
            "15% promo was removed. CTR stays flat but CVR falls ~32% and AOV rises, producing a net "
            "ROAS decline across the Hydration campaigns."
        ),
        "expected_driver": "price_change",
        "planted_at": "2026-09-12",
        "metric": "cvr",
        "entity": "META | Prospecting | Hydration",
        "platform": "meta",
        "detection": "CVR anomaly whose only corroborating event is a SKU price change",
    },
    {
        "key": "google_cannibalization",
        "title": "Google Shopping wins but cannibalises branded search",
        "description": (
            "Shopping 'GOOG | Shopping | Core Catalog' delivers ~6.5x ROAS from mid-September, but branded "
            "search 'GOOG | Brand | Exact' loses ~38% of its clicks and ~36% of conversions over the same "
            "weeks. Incremental profit is far below the reported ROAS suggests."
        ),
        "expected_driver": "cannibalization",
        "planted_at": "2026-09-15",
        "metric": "clicks",
        "entity": "GOOG | Brand | Exact",
        "platform": "google",
        "detection": "Paired anomaly: Shopping uplift + branded-search decline inside the same platform",
    },
    {
        "key": "tiktok_headroom",
        "title": "TikTok prospecting has unexhausted headroom",
        "description": (
            "TikTok ROAS rose from ~1.9 to ~3.4 between 2026-08-10 and 2026-10-06 while marginal ROAS "
            "stays above the blended target — diminishing returns have not yet been reached. The "
            "opportunity engine should flag it as an under-funded winner."
        ),
        "expected_driver": "scaling_opportunity",
        "planted_at": "2026-08-10",
        "metric": "roas",
        "entity": "TIKTOK | Prospecting | Glow Routine UGC",
        "platform": "tiktok",
        "detection": "Opportunity Radar: high marginal ROAS with available headroom",
    },
    {
        "key": "tracking_outage",
        "title": "GA4 tracking outage creates a false performance drop",
        "description": (
            "Between 2026-09-27 and 2026-10-01 GA4 events collapse ~62% (tag-manager release) while ad "
            "platform delivery, store orders and revenue are entirely normal. This must be labelled a "
            "data artifact, never a performance problem."
        ),
        "expected_driver": "data_artifact",
        "planted_at": "2026-09-27",
        "metric": "ga_sessions",
        "entity": "GA4 (all sources)",
        "platform": None,
        "detection": "Artifact detector: GA-only collapse with stable catalogue and store metrics",
    },
    {
        "key": "attribution_overlap",
        "title": "Platform-reported revenue overstates store revenue",
        "description": (
            "Meta and TikTok double-count view-through and cross-network conversions, so platform-reported "
            "revenue runs 18–26% above reconciled store revenue all year. The reconciliation engine must "
            "publish a de-overlapped attributed revenue and the discrepancy percentage."
        ),
        "expected_driver": "attribution_overlap",
        "planted_at": "all year",
        "metric": "revenue",
        "entity": "all platforms",
        "platform": None,
        "detection": "Reconciliation report with positive discrepancy and corrected attribution",
    },
    {
        "key": "under_promoted_margin_sku",
        "title": "High-margin, in-stock SKU is starved of spend",
        "description": (
            "LUM-RETIN-30 (Retinol Night Renewal, 74% contribution margin, ~92 days of cover) receives "
            "under 1.5% of total spend while its conversion rate beats the portfolio median and it holds "
            "60+ days of cover. A "
            "classic winning opportunity worth reallocating budget to."
        ),
        "expected_driver": "under_funded_margin_sku",
        "planted_at": "steady state",
        "metric": "spend_share",
        "entity": "LUM-RETIN-30",
        "platform": None,
        "detection": "Opportunity Radar: high margin x high cover x above-median CVR, low spend share",
    },
]

SCENARIO_BY_KEY = {s["key"]: s for s in SCENARIOS}


def scenario_keys() -> list[str]:
    return [s["key"] for s in SCENARIOS]
