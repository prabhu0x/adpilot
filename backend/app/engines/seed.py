"""Deterministic seed generator for the AdPilot demo brand.

Produces 12 months of daily data (plus 30 days of hourly data) for
"Lumière Skin Co.", a D2C skincare brand: 40 SKUs, 5 platforms, 35 campaigns,
~120 ad sets, ~120 creatives/ads and ~100k store orders — with the eight
planted scenarios from :mod:`app.engines.scenarios` woven into the numbers so
the detection layer has genuinely discoverable events.

Everything is driven by a fixed random seed, so repeated seeds are identical.
"""
from __future__ import annotations

import datetime as dt
import math
import random
from typing import Any, Iterable

import numpy as np
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.logging import get_logger
from ..core.security import ROLE_PERMISSIONS, hash_password
from ..db.models import (
    Ad,
    AdAccount,
    AdMetricsDaily,
    AdMetricsHourly,
    AdSet,
    AutonomySetting,
    Brand,
    BusinessObjective,
    Campaign,
    CampaignSkuMap,
    Constraint,
    Creative,
    DataSource,
    GaEvent,
    Guardrail,
    InventorySnapshot,
    Order,
    OrderItem,
    Platform,
    Product,
    Role,
    ScenarioStatus,
    Sku,
    SkuPriceHistory,
    SimulationState,
    User,
)
from .scenarios import SCENARIOS

log = get_logger("adpilot.seed")

SEED = settings.seed_random_state
START = dt.date.fromisoformat(settings.sim_start_date)
TODAY = dt.date.fromisoformat(settings.sim_today)
DAYS = (TODAY - START).days + 1
HOURS_DAYS = 30  # hourly detail window

# --------------------------------------------------------------------------- #
# Platform economics
# --------------------------------------------------------------------------- #
PLATFORMS: list[dict[str, Any]] = [
    {"key": "meta", "name": "Meta Ads", "category": "paid_social", "color": "#1877F2",
     "cpm": 12.6, "ctr": 0.0142, "cvr": 0.0285, "overlap": 1.26, "aov_mult": 1.0, "freq": 3.1,
     "attribution_window_days": 7, "api_version": "v21.0"},
    {"key": "google", "name": "Google Ads", "category": "search_shopping", "color": "#EA4335",
     "cpm": 21.5, "ctr": 0.0345, "cvr": 0.0405, "overlap": 1.11, "aov_mult": 1.05, "freq": 2.2,
     "attribution_window_days": 30, "api_version": "v17"},
    {"key": "amazon", "name": "Amazon Ads", "category": "marketplace", "color": "#FF9900",
     "cpm": 9.4, "ctr": 0.0081, "cvr": 0.0820, "overlap": 1.07, "aov_mult": 0.94, "freq": 2.6,
     "attribution_window_days": 14, "api_version": "v3"},
    {"key": "tiktok", "name": "TikTok Ads", "category": "paid_social", "color": "#00F2EA",
     "cpm": 7.8, "ctr": 0.0110, "cvr": 0.0195, "overlap": 1.24, "aov_mult": 0.88, "freq": 3.8,
     "attribution_window_days": 7, "api_version": "v1.3"},
    {"key": "programmatic", "name": "Programmatic (DSP)", "category": "display", "color": "#8B5CF6",
     "cpm": 3.4, "ctr": 0.0026, "cvr": 0.0075, "overlap": 1.38, "aov_mult": 0.9, "freq": 5.4,
     "attribution_window_days": 1, "api_version": "openrtb-2.6"},
]
PLATFORM_BY_KEY = {p["key"]: p for p in PLATFORMS}

# --------------------------------------------------------------------------- #
# Product catalogue
# --------------------------------------------------------------------------- #
CATEGORY_LIST = [
    ("Serums", "Vitamin C"), ("Serums", "Retinol"), ("Serums", "Hyaluronic"),
    ("Serums", "Niacinamide"), ("Serums", "Peptide"), ("Serums", "Azelaic"),
    ("Serums", "Hydrating"), ("Serums", "Eye"), ("Serums", "Blemish"), ("Serums", "Essence"),
    ("Moisturizers", "Day Cream"), ("Moisturizers", "Night Cream"), ("Moisturizers", "Gel Cream"),
    ("Moisturizers", "Barrier"), ("Moisturizers", "Lip"), ("Moisturizers", "Body"),
    ("Cleansers", "Gel Cleanser"), ("Cleansers", "Oil Cleanser"), ("Cleansers", "Exfoliant"),
    ("Cleansers", "Toner"),
    ("Masks", "Clay Mask"), ("Masks", "Sheet Mask"), ("Masks", "Sleeping Mask"),
    ("Sunscreen", "Mineral SPF"),
    ("Kits", "Routine Kit"), ("Kits", "Travel Kit"),
]

SKU_NAMES: list[tuple[str, str, str, str, float, float, str]] = [
    ("LUM-VITC-30", "Vitamin C Brightening Serum", "Serums", "Vitamin C", 52.0, 15.6, "core"),
    ("LUM-VITC-15", "Vitamin C Brightening Serum Mini", "Serums", "Vitamin C", 32.0, 10.2, "core"),
    ("LUM-RETIN-30", "Retinol Night Renewal 0.3%", "Serums", "Retinol", 68.0, 17.7, "core"),
    ("LUM-RETIN-50", "Retinol Night Renewal 0.5%", "Serums", "Retinol", 78.0, 21.8, "core"),
    ("LUM-HYAL-30", "Hyaluronic Quench Serum", "Serums", "Hyaluronic", 44.0, 12.3, "core"),
    ("LUM-NIAC-30", "Niacinamide Pore Refiner", "Serums", "Niacinamide", 38.0, 10.6, "core"),
    ("LUM-PEPT-30", "Peptide Firming Concentrate", "Serums", "Peptide", 72.0, 22.3, "launch"),
    ("LUM-AZEL-30", "Azelaic Clarity Serum", "Serums", "Azelaic", 46.0, 13.3, "core"),
    ("LUM-MIST-100", "Rosewater Hydrating Mist", "Serums", "Hydrating", 24.0, 6.1, "core"),
    ("LUM-EYE-15", "Caffeine Eye Serum", "Serums", "Eye", 36.0, 9.8, "core"),
    ("LUM-SPOT-15", "Spot Treatment Gel", "Serums", "Blemish", 22.0, 5.2, "core"),
    ("LUM-ESSENCE-150", "Fermented Essence", "Serums", "Essence", 46.0, 12.9, "core"),
    ("LUM-HYDRA-50", "Hydra-Dew Day Cream", "Moisturizers", "Day Cream", 46.0, 12.4, "core"),
    ("LUM-HYDRA-15", "Hydra-Dew Day Cream Mini", "Moisturizers", "Day Cream", 28.0, 8.4, "core"),
    ("LUM-NIGHT-50", "Midnight Repair Night Cream", "Moisturizers", "Night Cream", 58.0, 15.1, "core"),
    ("LUM-GEL-50", "Weightless Gel Moisturiser", "Moisturizers", "Gel Cream", 38.0, 9.9, "core"),
    ("LUM-CERAM-50", "Ceramide Barrier Balm", "Moisturizers", "Barrier", 52.0, 14.6, "launch"),
    ("LUM-LIP-10", "Shea Lip Treatment", "Moisturizers", "Lip", 16.0, 4.2, "core"),
    ("LUM-BALM-30", "Overnight Lip Mask", "Moisturizers", "Lip", 20.0, 5.1, "core"),
    ("LUM-BODY-250", "Shea Body Lotion", "Moisturizers", "Body", 28.0, 7.9, "core"),
    ("LUM-NECK-50", "Neck & Décolleté Cream", "Moisturizers", "Body", 54.0, 15.9, "launch"),
    ("LUM-CLEAN-150", "Gentle Gel Cleanser 150ml", "Cleansers", "Gel Cleanser", 26.0, 6.2, "core"),
    ("LUM-CLEAN-300", "Gentle Gel Cleanser 300ml", "Cleansers", "Gel Cleanser", 38.0, 9.4, "core"),
    ("LUM-OIL-100", "Camellia Oil Cleanser", "Cleansers", "Oil Cleanser", 32.0, 8.9, "core"),
    ("LUM-SCRUB-100", "Gentle Enzyme Scrub", "Cleansers", "Exfoliant", 34.0, 9.1, "core"),
    ("LUM-TONER-200", "Balancing Toner", "Cleansers", "Toner", 30.0, 7.4, "core"),
    ("LUM-CLAY-75", "Detox Clay Mask", "Masks", "Clay Mask", 34.0, 8.1, "core"),
    ("LUM-CLAY-PINK", "Pink Clay Detox Mask", "Masks", "Clay Mask", 38.0, 9.6, "core"),
    ("LUM-SHEET-5", "Hydra Sheet Masks (5-pack)", "Masks", "Sheet Mask", 18.0, 5.4, "core"),
    ("LUM-MASK-SLEEP", "Overnight Sleeping Mask", "Masks", "Sleeping Mask", 44.0, 11.8, "core"),
    ("LUM-SPF-50", "Mineral SPF50 Fluid", "Sunscreen", "Mineral SPF", 40.0, 11.2, "core"),
    ("LUM-SPF-TINT", "Mineral SPF50 Tinted", "Sunscreen", "Mineral SPF", 44.0, 12.7, "core"),
    ("LUM-SPF-KIDS", "Mineral SPF50 Kids", "Sunscreen", "Mineral SPF", 34.0, 10.1, "core"),
    ("LUM-KIT-GLOW", "Glass Skin Routine Kit", "Kits", "Routine Kit", 148.0, 43.2, "hero"),
    ("LUM-KIT-CALM", "Calm & Repair Routine Kit", "Kits", "Routine Kit", 132.0, 39.8, "core"),
    ("LUM-KIT-ACNE", "Clear Skin Routine Kit", "Kits", "Routine Kit", 126.0, 37.4, "core"),
    ("LUM-KIT-BRIDE", "Bridal Glow Kit", "Kits", "Routine Kit", 168.0, 52.4, "core"),
    ("LUM-KIT-TRAVEL", "Weekend Travel Kit", "Kits", "Travel Kit", 58.0, 18.6, "core"),
    ("LUM-CREAM-OLD", "Legacy Rich Cream", "Moisturizers", "Night Cream", 42.0, 13.8, "discontinued"),
    ("LUM-SERUM-VIT", "Vitamin A+E Serum", "Serums", "Vitamin C", 48.0, 14.0, "decline"),
]

CREATIVE_THEMES = [
    "Derm-approved routine", "Before / after 28 days", "Ingredient deep-dive",
    "Founder story", "Customer testimonial", "Unboxing", "Routine in 60s",
    "Problem-solution acne", "Winter hydration", "Glass skin challenge",
    "Price-per-value stacking", "Sustainability & refills",
]
HOOK_TYPES = ["benefit", "social_proof", "problem_agitate", "founder_story", "ugc_testimonial", "offer_led", "education"]
FORMATS = ["image", "video", "carousel", "ugc_video", "static", "collection"]
ANGLES = ["efficacy", "value", "trust", "convenience", "aspiration", "urgency"]

AUDIENCES = [
    ("Broad US 25-44", 48_000_000, {"age": "25-44", "geo": "US"}, "broad"),
    ("Interest Skincare Enthusiasts", 12_500_000, {"interests": ["skincare", "beauty"]}, "interest"),
    ("Lookalike 1% Purchasers", 2_100_000, {"lookalike": "purchasers_180d"}, "lookalike"),
    ("Lookalike 3% ATC", 5_400_000, {"lookalike": "atc_90d"}, "lookalike"),
    ("Retargeting ATC 14d", 180_000, {"event": "add_to_cart", "window": 14}, "retargeting"),
    ("Retargeting Site 30d", 640_000, {"event": "page_view", "window": 30}, "retargeting"),
    ("Customer Retention 180d", 96_000, {"segment": "purchasers_180d"}, "retention"),
    ("Brand Search Intent", 320_000, {"keywords": "brand"}, "intent"),
    ("Competitor Conquest", 1_250_000, {"keywords": "competitor"}, "conquest"),
    ("Problem: Acne Concern", 8_400_000, {"concern": "acne"}, "interest"),
    ("Problem: Anti-ageing 35+", 6_900_000, {"concern": "anti-ageing"}, "interest"),
    ("Gift Shoppers Q4", 15_200_000, {"intent": "gifting"}, "seasonal"),
]

CAMPAIGN_BLUEPRINTS: list[dict[str, Any]] = [
    {"name": "META | Prospecting | Hero Serum UGC", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 11.0, "skus": ["LUM-VITC-30", "LUM-HYAL-30"], "roas": 2.65, "freq": 3.4},
    {"name": "META | Prospecting | Hydration", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 7.0, "skus": ["LUM-HYDRA-50", "LUM-HYAL-30"], "roas": 2.30, "freq": 3.0},
    {"name": "META | Prospecting | Retinol PM", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 1.1, "skus": ["LUM-RETIN-30", "LUM-RETIN-50"], "roas": 2.15, "freq": 2.6},
    {"name": "META | Prospecting | Acne Concern", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 4.2, "skus": ["LUM-NIAC-30", "LUM-SPOT-15", "LUM-AZEL-30"], "roas": 2.05, "freq": 2.9},
    {"name": "META | Retargeting | Vitamin C", "platform": "meta", "objective": "conversions",
     "funnel": "retargeting", "weight": 3.6, "skus": ["LUM-VITC-30"], "roas": 5.10, "freq": 5.2},
    {"name": "META | Retargeting | ATC 14d", "platform": "meta", "objective": "conversions",
     "funnel": "retargeting", "weight": 3.0, "skus": ["LUM-KIT-GLOW", "LUM-HYDRA-50"], "roas": 4.60, "freq": 5.8},
    {"name": "META | Retention | Customers 180d", "platform": "meta", "objective": "conversions",
     "funnel": "retention", "weight": 2.2, "skus": ["LUM-RETIN-30", "LUM-NIGHT-50"], "roas": 3.40, "freq": 4.4},
    {"name": "META | Advantage+ | Catalog", "platform": "meta", "objective": "catalog_sales",
     "funnel": "prospecting", "weight": 4.8, "skus": ["LUM-KIT-GLOW", "LUM-KIT-TRAVEL"], "roas": 2.55, "freq": 3.6},
    {"name": "GOOG | Brand | Exact", "platform": "google", "objective": "conversions",
     "funnel": "brand", "weight": 3.4, "skus": ["LUM-KIT-GLOW", "LUM-VITC-30"], "roas": 7.80, "branded": True, "freq": 2.0},
    {"name": "GOOG | Search | Non-brand Core", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 4.6, "skus": ["LUM-VITC-30", "LUM-HYAL-30", "LUM-SPF-50"], "roas": 3.10, "freq": 2.1},
    {"name": "GOOG | Search | Competitor Conquest", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 1.6, "skus": ["LUM-RETIN-30", "LUM-KIT-GLOW"], "roas": 1.65, "freq": 1.8},
    {"name": "GOOG | Shopping | Core Catalog", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 5.2, "skus": ["LUM-SPF-50", "LUM-HYDRA-50", "LUM-CLEAN-150"], "roas": 4.40, "freq": 2.4},
    {"name": "GOOG | Shopping | Kits", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.4, "skus": ["LUM-KIT-GLOW", "LUM-KIT-CALM"], "roas": 3.95, "freq": 2.2},
    {"name": "GOOG | PMax | New Customer", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 3.8, "skus": ["LUM-HYAL-30", "LUM-CLEAN-150", "LUM-SPF-TINT"], "roas": 2.40, "freq": 2.8},
    {"name": "GOOG | Shopping | Hydration", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 1.9, "skus": ["LUM-HYDRA-50", "LUM-CERAM-50"], "roas": 3.35, "freq": 2.3},
    {"name": "AMZN | Sponsored Products | Serum", "platform": "amazon", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.8, "skus": ["LUM-VITC-30", "LUM-RETIN-30"], "roas": 4.20, "freq": 2.4},
    {"name": "AMZN | Sponsored Products | Catalog", "platform": "amazon", "objective": "conversions",
     "funnel": "prospecting", "weight": 3.2, "skus": ["LUM-CLEAN-150", "LUM-SPF-50", "LUM-SHEET-5"], "roas": 3.60, "freq": 2.7},
    {"name": "AMZN | Sponsored Brands | Glow Kit", "platform": "amazon", "objective": "conversions",
     "funnel": "prospecting", "weight": 1.5, "skus": ["LUM-KIT-GLOW"], "roas": 3.05, "freq": 2.2},
    {"name": "AMZN | Sponsored Display | Retarget", "platform": "amazon", "objective": "conversions",
     "funnel": "retargeting", "weight": 1.2, "skus": ["LUM-NIGHT-50", "LUM-RETIN-50"], "roas": 2.70, "freq": 4.1},
    {"name": "TIKTOK | Prospecting | Glow Routine UGC", "platform": "tiktok", "objective": "conversions",
     "funnel": "prospecting", "weight": 4.4, "skus": ["LUM-KIT-GLOW", "LUM-HYAL-30"], "roas": 2.10, "freq": 3.9},
    {"name": "TIKTOK | Prospecting | Acne Gen-Z", "platform": "tiktok", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.6, "skus": ["LUM-NIAC-30", "LUM-SPOT-15"], "roas": 1.75, "freq": 4.2},
    {"name": "TIKTOK | Spark Ads | Testimonials", "platform": "tiktok", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.0, "skus": ["LUM-RETIN-30", "LUM-NIGHT-50"], "roas": 1.95, "freq": 3.6},
    {"name": "TIKTOK | Retargeting | ATC 7d", "platform": "tiktok", "objective": "conversions",
     "funnel": "retargeting", "weight": 1.4, "skus": ["LUM-KIT-GLOW"], "roas": 3.30, "freq": 5.5},
    {"name": "PROG | Display | Prospecting Run of Network", "platform": "programmatic", "objective": "awareness",
     "funnel": "prospecting", "weight": 2.4, "skus": ["LUM-CLEAN-150", "LUM-MIST-100"], "roas": 0.85, "freq": 6.2},
    {"name": "PROG | Display | Retargeting Pool", "platform": "programmatic", "objective": "conversions",
     "funnel": "retargeting", "weight": 1.8, "skus": ["LUM-HYDRA-50", "LUM-KIT-TRAVEL"], "roas": 1.35, "freq": 7.1},
    {"name": "PROG | Native | Content Amplification", "platform": "programmatic", "objective": "traffic",
     "funnel": "prospecting", "weight": 1.2, "skus": ["LUM-SPF-50"], "roas": 1.10, "freq": 4.8},
    {"name": "META | Prospecting | Sunscreen Season", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.9, "skus": ["LUM-SPF-50", "LUM-SPF-TINT"], "roas": 2.45, "freq": 3.2},
    {"name": "META | Prospecting | Gifting Q4", "platform": "meta", "objective": "conversions",
     "funnel": "prospecting", "weight": 3.4, "skus": ["LUM-KIT-GLOW", "LUM-KIT-BRIDE", "LUM-KIT-CALM"], "roas": 3.60, "freq": 3.3},
    {"name": "GOOG | Shopping | Gifting Q4", "platform": "google", "objective": "conversions",
     "funnel": "prospecting", "weight": 3.1, "skus": ["LUM-KIT-GLOW", "LUM-KIT-BRIDE"], "roas": 5.20, "freq": 2.5},
    {"name": "AMZN | Sponsored Products | Gifting", "platform": "amazon", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.1, "skus": ["LUM-KIT-TRAVEL", "LUM-SHEET-5", "LUM-BALM-30"], "roas": 4.05, "freq": 2.6},
    {"name": "META | Prospecting | Barrier Repair Launch", "platform": "meta", "objective": "conversions",
     "funnel": "launch", "weight": 2.7, "skus": ["LUM-CERAM-50", "LUM-NECK-50"], "roas": 1.90, "freq": 2.8},
    {"name": "GOOG | Search | Barrier Repair Launch", "platform": "google", "objective": "conversions",
     "funnel": "launch", "weight": 1.7, "skus": ["LUM-CERAM-50", "LUM-NECK-50"], "roas": 2.60, "freq": 1.9},
    {"name": "META | Retention | Replenishment", "platform": "meta", "objective": "conversions",
     "funnel": "retention", "weight": 1.9, "skus": ["LUM-CLEAN-300", "LUM-NIGHT-50"], "roas": 3.85, "freq": 4.6},
    {"name": "TIKTOK | Prospecting | Winter Hydration", "platform": "tiktok", "objective": "conversions",
     "funnel": "prospecting", "weight": 2.2, "skus": ["LUM-CERAM-50", "LUM-BODY-250"], "roas": 2.25, "freq": 3.7},
    {"name": "PROG | Video | CTV Awareness", "platform": "programmatic", "objective": "awareness",
     "funnel": "prospecting", "weight": 1.6, "skus": ["LUM-KIT-GLOW"], "roas": 0.95, "freq": 3.4},
]
CAMPAIGN_TOTAL_WEIGHT = sum(b["weight"] for b in CAMPAIGN_BLUEPRINTS)
DAILY_SPEND_TARGET = 6200.0

DEMO_USERS = [
    ("admin@adpilot.io", "Avery Admin", "Admin", "AdPilot!2026"),
    ("brand@adpilot.io", "Bailey Brand", "Brand Manager", "AdPilot!2026"),
    ("analyst@adpilot.io", "Casey Analyst", "Analyst", "AdPilot!2026"),
    ("viewer@adpilot.io", "Dana Viewer", "Viewer", "AdPilot!2026"),
]

HYDRA_SKUS = {"LUM-HYDRA-50", "LUM-HYDRA-15"}
LAUNCH_LIVE = dt.date(2026, 7, 15)


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def _d(offset: int) -> dt.date:
    return START + dt.timedelta(days=offset)


def date_offset(date: dt.date) -> int:
    return (date - START).days


def weekly_factor(date: dt.date) -> float:
    return {0: 1.06, 1: 1.09, 2: 1.00, 3: 0.98, 4: 0.93, 5: 0.96, 6: 1.04}[date.weekday()]


def annual_factor(date: dt.date) -> float:
    f = 1.0
    doy = date.timetuple().tm_yday
    if date.month == 11 and date.day >= 20:
        f *= 1.28 + (date.day - 20) * 0.055
    if date.month == 12 and date.day <= 24:
        f *= 1.55 - (date.day - 1) * 0.022
    if date.month == 12 and date.day > 24:
        f *= 0.86
    if date.month == 1:
        f *= 0.88
    if date.month in (6, 7):
        f *= 1.07
    f *= 1.0 + 0.09 * (doy / 365.0)
    if date.month in (3, 9):
        f *= 1.03
    return f


HOUR_PROFILE = np.array([
    0.35, 0.22, 0.16, 0.13, 0.14, 0.20, 0.38, 0.68, 1.05, 1.28, 1.42, 1.50,
    1.48, 1.42, 1.38, 1.36, 1.42, 1.58, 1.72, 1.68, 1.42, 1.10, 0.78, 0.52,
])


def _iso(date: dt.date) -> str:
    return date.isoformat()


# --------------------------------------------------------------------------- #
# Planted-event modifiers
# --------------------------------------------------------------------------- #
def campaign_modifiers(campaign_name: str, date: dt.date) -> dict[str, float]:
    mod = {"ctr": 1.0, "cvr": 1.0, "cpm": 1.0, "freq": 1.0, "imp": 1.0, "aov": 1.0, "roas": 1.0}

    if campaign_name == "META | Prospecting | Hero Serum UGC" and date >= dt.date(2026, 9, 8):
        t = min(date_offset(date) - date_offset(dt.date(2026, 9, 8)), 14) / 14.0
        mod["ctr"] *= 1.0 - 0.45 * t
        mod["freq"] *= 1.0 + 1.28 * t
        mod["cpm"] *= 1.0 + 0.09 * t
        mod["cvr"] *= 1.0 - 0.10 * t

    if campaign_name in {"META | Prospecting | Hydration", "GOOG | Shopping | Hydration"} and date >= dt.date(2026, 9, 12):
        mod["cvr"] *= 0.68
        mod["aov"] *= 1.174
        mod["ctr"] *= 0.97

    if campaign_name == "GOOG | Brand | Exact" and date >= dt.date(2026, 9, 15):
        mod["imp"] *= 0.62
        mod["ctr"] *= 0.97
        mod["cvr"] *= 0.94

    if campaign_name == "GOOG | Shopping | Core Catalog" and date >= dt.date(2026, 9, 15):
        mod["roas"] *= 1.34
        mod["cvr"] *= 1.22

    if campaign_name == "TIKTOK | Prospecting | Glow Routine UGC" and date >= dt.date(2026, 8, 10):
        t = min(date_offset(date) - date_offset(dt.date(2026, 8, 10)), 57) / 57.0
        mod["cvr"] *= 1.0 + 0.62 * t
        mod["ctr"] *= 1.0 + 0.10 * t

    if campaign_name == "META | Retargeting | Vitamin C" and date >= dt.date(2026, 9, 18):
        mod["cvr"] *= 0.72

    if campaign_name == "META | Prospecting | Retinol PM":
        mod["cvr"] *= 1.40

    if "Gifting" in campaign_name and date.month in (11, 12):
        mod["cvr"] *= 1.18
        mod["roas"] *= 1.10

    if "Sunscreen" in campaign_name and date.month in (5, 6, 7, 8):
        mod["roas"] *= 1.18
    if "Winter Hydration" in campaign_name and date.month in (11, 12, 1, 2):
        mod["roas"] *= 1.22
    return mod


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #
def seed_database(db: Session, progress=None, force: bool = False) -> dict[str, Any]:
    random.seed(SEED)
    np.random.seed(SEED)

    existing = db.query(Brand).first()
    if existing and not force:
        return {"status": "skipped", "reason": "database already seeded", "brand_id": existing.id}
    if existing and force:
        _wipe(db)

    def step(pct: float, msg: str) -> None:
        if progress:
            progress(pct, msg)
        else:
            log.info("seed %s%% %s", pct, msg)

    step(2, "platforms, roles & brand")
    brand = _seed_org(db)
    step(6, "catalogue & pricing history")
    products, skus = _seed_catalogue(db, brand)
    step(12, "campaign / ad set / creative topology")
    campaigns, ad_sets, creatives, ads, sku_maps = _seed_topology(db, brand, skus)
    step(20, "simulating ad delivery, orders and GA4 events")
    metrics_rows, hourly_rows, order_rows, item_rows, ga_rows, carrier = _generate_timeseries(
        brand, campaigns, ad_sets, ads, skus, sku_maps, step
    )
    step(64, f"writing {len(metrics_rows):,} daily ad metric rows")
    _bulk(db, AdMetricsDaily, metrics_rows)
    step(72, f"writing {len(hourly_rows):,} hourly metric rows")
    _bulk(db, AdMetricsHourly, hourly_rows)
    step(78, f"writing {len(order_rows):,} store orders")
    _bulk(db, Order, order_rows)
    db.flush()
    order_id_by_number = dict(db.query(Order.order_number, Order.id).all())
    for item in item_rows:
        item["order_id"] = order_id_by_number[item.pop("_order_number")]
    step(86, f"writing {len(item_rows):,} order items")
    _bulk(db, OrderItem, item_rows)
    step(90, f"writing {len(ga_rows):,} GA4 event rows")
    _bulk(db, GaEvent, ga_rows)
    step(92, "inventory snapshots derived from real velocity")
    _seed_inventory(db, brand, skus, carrier)
    step(96, "objectives, constraints, guardrails, sources, simulation state")
    _seed_governance(db, brand, campaigns, skus)
    step(99, "done")
    db.commit()
    return {
        "status": "seeded",
        "brand_id": brand.id,
        "products": len(products),
        "skus": len(skus),
        "campaigns": len(campaigns),
        "ad_sets": len(ad_sets),
        "creatives": len(creatives),
        "ads": len(ads),
        "daily_metric_rows": len(metrics_rows),
        "hourly_metric_rows": len(hourly_rows),
        "orders": len(order_rows),
        "order_items": len(item_rows),
        "ga_rows": len(ga_rows),
        "days": DAYS,
        "window": [_iso(START), _iso(TODAY)],
    }


def _wipe(db: Session) -> None:
    from sqlalchemy import text

    tables = [
        "agent_steps", "agent_runs", "prediction_records", "model_feedback", "model_registry",
        "outcomes", "executions", "recommendations", "opportunities", "diagnoses", "anomalies",
        "scenario_status", "simulation_state", "audit_log", "guardrails", "autonomy_settings",
        "constraints", "business_objectives", "data_uploads", "sync_runs", "data_sources",
        "reconciliation_reports", "unified_facts", "ga_events", "order_items", "orders",
        "inventory_snapshots", "sku_price_history", "ad_metrics_hourly", "ad_metrics_daily",
        "campaign_sku_map", "ads", "creatives", "ad_sets", "campaigns", "ad_accounts",
        "platforms", "users", "roles", "notifications", "saved_views", "products", "skus",
        "brands",
    ]
    for table in tables:
        db.execute(text(f"DELETE FROM {table}"))
    db.commit()


def _bulk(db: Session, model, rows: Iterable[dict], chunk: int = 8000) -> int:
    rows = list(rows)
    for i in range(0, len(rows), chunk):
        db.bulk_insert_mappings(model, rows[i : i + chunk])
    db.flush()
    return len(rows)


# --------------------------------------------------------------------------- #
def _seed_org(db: Session) -> Brand:
    for key, perms in ROLE_PERMISSIONS.items():
        db.merge(Role(name=key, description=f"{key} role", permissions=perms))
    for platform in PLATFORMS:
        db.merge(
            Platform(
                key=platform["key"], name=platform["name"], category=platform["category"],
                color=platform["color"], attribution_window_days=platform["attribution_window_days"],
                api_version=platform["api_version"],
                notes=f"Simulated connector for {platform['name']}; live mode uses the documented OAuth flow.",
            )
        )
    brand = Brand(
        name="Lumière Skin Co.", slug="lumiere-skin", vertical="skincare",
        currency="USD", timezone="America/Los_Angeles", logo_emoji="✦",
    )
    db.add(brand)
    db.flush()

    for email, name, role, password in DEMO_USERS:
        db.add(User(
            email=email, full_name=name, hashed_password=hash_password(password),
            role_name=role, brand_id=brand.id, preferences={"theme": "dark", "onboarded": True},
        ))

    for platform in PLATFORMS:
        db.add(AdAccount(
            brand_id=brand.id, platform_key=platform["key"],
            external_id=f"act_{platform['key']}_90817",
            name=f"Lumière Skin — {platform['name']}", currency="USD",
            timezone="America/Los_Angeles", status="active",
            config={
                "auth": {
                    "mode": "simulated",
                    "app_id_env": f"{platform['key'].upper()}_APP_ID",
                    "access_token_env": f"{platform['key'].upper()}_ACCESS_TOKEN",
                },
                "rate_limit_per_min": 60,
            },
        ))
    db.flush()
    return brand


def _seed_catalogue(db: Session, brand: Brand) -> tuple[list[Product], list[Sku]]:
    products: list[Product] = []
    by_subcat: dict[str, Product] = {}
    for category, subcategory in CATEGORY_LIST:
        if subcategory in by_subcat:
            continue
        product = Product(
            brand_id=brand.id, name=f"{subcategory} collection", category=category,
            subcategory=subcategory,
            description=f"Lumière {subcategory} range — dermatologist tested, fragrance free.",
            lifecycle="core", hero_product=subcategory in {"Vitamin C", "Retinol", "Routine Kit"},
            attributes={"clean_beauty": True, "cruelty_free": True},
        )
        db.add(product)
        by_subcat[subcategory] = product
        products.append(product)
    db.flush()

    skus: list[Sku] = []
    for code, name, category, subcategory, price, cost, lifecycle in SKU_NAMES:
        margin = (price - cost) / price * 100
        sku = Sku(
            brand_id=brand.id, product_id=by_subcat[subcategory].id, sku_code=code, name=name,
            size="set" if "KIT" in code else "30ml", price=price, cost=cost,
            margin_pct=round(margin, 2), msrp=round(price * 1.18, 2),
            min_price=round(price * 0.78, 2), lifecycle=lifecycle,
            is_active=lifecycle != "discontinued",
        )
        db.add(sku)
        skus.append(sku)
    db.flush()

    history: list[dict[str, Any]] = [
        dict(brand_id=brand.id, sku_id=s.id, effective_date=START, end_date=None, price=s.price,
             discount_pct=0.0, promo_name="", change_reason="baseline", previous_price=None)
        for s in skus
    ]
    promos = [
        (dt.date(2025, 11, 24), dt.date(2025, 12, 2), 0.20, "BFCM 2025"),
        (dt.date(2026, 2, 10), dt.date(2026, 2, 16), 0.15, "Valentine Gift Edit"),
        (dt.date(2026, 5, 22), dt.date(2026, 5, 28), 0.12, "Memorial Glow"),
        (dt.date(2026, 7, 3), dt.date(2026, 7, 7), 0.18, "Summer Reset"),
    ]
    for sku in skus:
        if sku.lifecycle == "discontinued":
            continue
        for start, end, disc, promo in promos:
            history.append(dict(
                brand_id=brand.id, sku_id=sku.id, effective_date=start, end_date=end,
                price=round(sku.price * (1 - disc), 2), discount_pct=disc * 100,
                promo_name=promo, change_reason="promotional_discount", previous_price=sku.price,
            ))
    # PLANTED: list-price increase on the hydration line
    for sku in skus:
        if sku.sku_code in HYDRA_SKUS:
            history.append(dict(
                brand_id=brand.id, sku_id=sku.id, effective_date=dt.date(2026, 9, 12), end_date=None,
                price=round(sku.price * 1.174, 2), discount_pct=0.0, promo_name="",
                change_reason="list_price_increase", previous_price=sku.price,
            ))
    _bulk(db, SkuPriceHistory, history)
    db.flush()
    return products, skus


def _seed_topology(db: Session, brand: Brand, skus: list[Sku]):
    sku_by_code = {s.sku_code: s for s in skus}
    campaigns: list[Campaign] = []
    ad_sets: list[AdSet] = []
    creatives: list[Creative] = []
    ad_rows: list[dict] = []
    sku_map_rows: list[dict] = []

    for idx, blueprint in enumerate(CAMPAIGN_BLUEPRINTS):
        platform = PLATFORM_BY_KEY[blueprint["platform"]]
        budget = DAILY_SPEND_TARGET * blueprint["weight"] / CAMPAIGN_TOTAL_WEIGHT
        launch = (
            LAUNCH_LIVE - dt.timedelta(days=30)
            if blueprint["funnel"] == "launch"
            else START - dt.timedelta(days=random.randint(60, 400))
        )
        campaign = Campaign(
            brand_id=brand.id, platform_key=blueprint["platform"],
            external_id=f"cmp_{blueprint['platform'][:2]}_{1000 + idx}", name=blueprint["name"],
            objective=blueprint["objective"], status="active", daily_budget=round(budget, 2),
            bid_strategy="maximize_conversions" if blueprint["platform"] == "google" else "lowest_cost",
            target_roas=round(blueprint["roas"] * 0.85, 2) if blueprint["platform"] == "google" else None,
            target_cpa=round(40 + idx, 2) if blueprint["platform"] == "tiktok" else None,
            launched_at=launch, funnel_stage=blueprint["funnel"],
            is_branded=bool(blueprint.get("branded")), tags=[blueprint["platform"], blueprint["funnel"]],
            attributes={"roas_base": blueprint["roas"], "freq_base": blueprint.get("freq", platform["freq"])},
        )
        db.add(campaign)
        db.flush()
        campaigns.append(campaign)

        set_count = 3 if blueprint["platform"] == "programmatic" else random.choice([3, 3, 4, 4, 2])
        for j, (aud_name, aud_size, targeting, aud_kind) in enumerate(random.sample(AUDIENCES, set_count)):
            ad_set = AdSet(
                brand_id=brand.id, campaign_id=campaign.id, external_id=f"adset_{campaign.id}_{j}",
                name=f"{campaign.name.split('|')[-1].strip()} — {aud_name}", audience_name=aud_name,
                targeting=targeting, audience_size=aud_size, status="active",
                daily_budget=round(budget / set_count, 2), bid_amount=round(3 + j, 2),
                placement=random.choice(["automatic", "feed", "stories", "search_partners"]),
                device_targeting=random.choice(["all", "all", "mobile", "desktop"]),
                geo_targeting=["US", "CA", "GB"],
            )
            db.add(ad_set)
            db.flush()
            ad_sets.append(ad_set)

            theme = random.choice(CREATIVE_THEMES)
            fmt = random.choice(FORMATS if blueprint["platform"] != "google" else ["image", "static", "carousel"])
            hook = random.choice(HOOK_TYPES)
            launched = launch + dt.timedelta(days=random.randint(0, 120))
            if blueprint["name"] == "META | Prospecting | Hero Serum UGC" and j == 0:
                launched = dt.date(2026, 8, 20)  # PLANTED: never rotated since
            creative = Creative(
                brand_id=brand.id, external_id=f"crt_{campaign.id}_{j}",
                name=f"{theme} — {fmt.replace('_', ' ')} v{j + 1}", format=fmt, hook_type=hook,
                theme=theme, angle=random.choice(ANGLES),
                asset_url=f"/assets/creatives/{campaign.platform_key}/{campaign.id}_{j}.jpg",
                thumbnail_url=f"/assets/creatives/{campaign.platform_key}/{campaign.id}_{j}_thumb.jpg",
                launched_at=launched, fatigue_score=0.0,
                attributes={
                    "quality_score": round(random.uniform(0.55, 1.35), 3),
                    "has_person": random.random() > 0.35,
                    "has_price": random.random() > 0.5,
                    "duration_s": random.choice([6, 9, 12, 15, 21, 30]) if "video" in fmt else 0,
                    "color_palette": random.choice(["warm", "cool", "neutral", "vibrant"]),
                },
            )
            db.add(creative)
            db.flush()
            creatives.append(creative)
            ad_rows.append(dict(
                brand_id=brand.id, campaign_id=campaign.id, ad_set_id=ad_set.id,
                creative_id=creative.id, external_id=f"ad_{ad_set.id}_{creative.id}",
                name=f"{ad_set.name} / {creative.name}", status="active", launched_at=launched,
            ))

            # Most ad sets rotate a second creative (mirrors real rotation practice)
            if random.random() < 0.72:
                fmt2 = random.choice(FORMATS if blueprint["platform"] != "google" else ["image", "static", "carousel"])
                theme2 = random.choice(CREATIVE_THEMES)
                creative2 = Creative(
                    brand_id=brand.id, external_id=f"crt_{campaign.id}_{j}b",
                    name=f"{theme2} — {fmt2.replace('_', ' ')} v{j + 1}b", format=fmt2,
                    hook_type=random.choice(HOOK_TYPES), theme=theme2, angle=random.choice(ANGLES),
                    asset_url=f"/assets/creatives/{campaign.platform_key}/{campaign.id}_{j}b.jpg",
                    thumbnail_url=f"/assets/creatives/{campaign.platform_key}/{campaign.id}_{j}b_thumb.jpg",
                    launched_at=launch + dt.timedelta(days=random.randint(0, 150)),
                    fatigue_score=0.0,
                    attributes={
                        "quality_score": round(random.uniform(0.5, 1.4), 3),
                        "has_person": random.random() > 0.35,
                        "has_price": random.random() > 0.5,
                        "duration_s": random.choice([6, 9, 12, 15, 21, 30]) if "video" in fmt2 else 0,
                        "color_palette": random.choice(["warm", "cool", "neutral", "vibrant"]),
                    },
                )
                db.add(creative2)
                db.flush()
                creatives.append(creative2)
                ad_rows.append(dict(
                    brand_id=brand.id, campaign_id=campaign.id, ad_set_id=ad_set.id,
                    creative_id=creative2.id, external_id=f"ad_{ad_set.id}_{creative2.id}",
                    name=f"{ad_set.name} / {creative2.name}", status="active",
                    launched_at=creative2.launched_at,
                ))

        for s_idx, code in enumerate(blueprint["skus"]):
            sku = sku_by_code.get(code)
            if sku:
                sku_map_rows.append(dict(
                    brand_id=brand.id, campaign_id=campaign.id, ad_set_id=None, sku_id=sku.id,
                    weight=1.0 / (s_idx + 1), is_primary=s_idx == 0,
                ))

    _bulk(db, Ad, ad_rows)
    _bulk(db, CampaignSkuMap, sku_map_rows)
    db.flush()
    ad_objs = db.query(Ad).all()
    return campaigns, ad_sets, creatives, ad_objs, sku_map_rows


# --------------------------------------------------------------------------- #
def _generate_timeseries(
    brand: Brand,
    campaigns: list[Campaign],
    ad_sets: list[AdSet],
    ads: list[Ad],
    skus: list[Sku],
    sku_maps: list[dict],
    step,
):
    sku_by_id = {s.id: s for s in skus}
    hydra_ids = {s.id for s in skus if s.sku_code in HYDRA_SKUS}

    skus_by_campaign: dict[int, list[tuple[int, float]]] = {}
    for m in sku_maps:
        skus_by_campaign.setdefault(m["campaign_id"], []).append((m["sku_id"], m["weight"]))
    prepared: dict[int, tuple[list[int], np.ndarray]] = {}
    for cid, pairs in skus_by_campaign.items():
        ids = [p[0] for p in pairs]
        w = np.array([p[1] for p in pairs], dtype=float)
        prepared[cid] = (ids, w / w.sum())

    ads_by_campaign: dict[int, list[Ad]] = {}
    for ad in ads:
        ads_by_campaign.setdefault(ad.campaign_id, []).append(ad)
    first_adset_by_campaign = {a.campaign_id: a.ad_set_id for a in ads}

    rng = np.random.default_rng(SEED)

    metrics_rows: list[dict] = []
    hourly_rows: list[dict] = []
    order_rows: list[dict] = []
    item_rows: list[dict] = []
    ga_rows: list[dict] = []
    carrier: dict[str, Any] = {
        "velocity": {s.id: {} for s in skus},
        "revenue": {s.sku_code: 0.0 for s in skus},
        "units": {s.sku_code: 0.0 for s in skus},
    }
    order_seq = 10_000
    daily_campaign_totals: dict[tuple[int, int], dict[str, float]] = {}

    for offset in range(DAYS):
        date = _d(offset)
        iso = _iso(date)
        wf = weekly_factor(date)
        af = annual_factor(date)

        for campaign in campaigns:
            platform = PLATFORM_BY_KEY[campaign.platform_key]
            if campaign.funnel_stage == "launch" and date < LAUNCH_LIVE:
                continue
            mod = campaign_modifiers(campaign.name, date)
            base = campaign.attributes or {}
            pacing = min(1.45, max(0.55, 1.0 + 0.06 * math.sin(offset / 9.0) + float(rng.normal(0, 0.05))))
            spend = campaign.daily_budget * pacing * (0.5 + 0.5 * af) * wf / 1.02
            cpm = platform["cpm"] * (0.85 + 0.3 * af) * mod["cpm"] * float(rng.normal(1.0, 0.05))
            impressions = spend / cpm * 1000 * mod["imp"]
            ctr = platform["ctr"] * mod["ctr"] * float(rng.normal(1.0, 0.06)) * wf
            clicks = max(impressions * ctr, 0.0)
            cvr = platform["cvr"] * mod["cvr"] * float(rng.normal(1.0, 0.09))
            cvr *= 1.0 + 0.05 * math.sin(offset / 17.0)
            true_orders_float = clicks * cvr

            n_orders = int(rng.poisson(max(true_orders_float, 0.0))) if true_orders_float > 0 else 0
            ids, weights = prepared.get(campaign.id, ([skus[0].id], np.array([1.0])))
            store_revenue = 0.0
            for _ in range(n_orders):
                order_seq += 1
                sku_id = int(rng.choice(ids, p=weights))
                sku = sku_by_id[sku_id]
                qty = int(rng.choice([1, 1, 1, 2, 2, 3], p=[0.5, 0.2, 0.12, 0.1, 0.05, 0.03]))
                unit_price = round(sku.price * (mod["aov"] if sku_id in hydra_ids else 1.0), 2)
                discount_pct = 0.0
                if (date.month == 11 and date.day >= 24) or (date.month == 12 and date.day <= 2):
                    discount_pct = 0.20
                elif date.month == 7 and date.day <= 7:
                    discount_pct = 0.18
                gross = unit_price * qty
                discount = gross * discount_pct
                net = gross - discount
                cogs = sku.cost * qty
                refund = net * 0.055 if rng.random() < 0.06 else 0.0
                order_number = f"LUM-{date.strftime('%y%m%d')}-{order_seq}"
                order_rows.append(dict(
                    brand_id=brand.id, order_number=order_number,
                    ordered_at=dt.datetime.combine(date, dt.time(int(rng.integers(6, 23)), int(rng.integers(0, 60)))),
                    date=date, channel="shopify", source=campaign.platform_key,
                    medium="cpc", campaign_name=campaign.name, platform_key=campaign.platform_key,
                    campaign_id=campaign.id,
                    device=str(rng.choice(["mobile", "desktop", "tablet"], p=[0.68, 0.24, 0.08])),
                    geo=str(rng.choice(["US-CA", "US-NY", "US-TX", "US-FL", "US-WA", "US-IL"],
                                       p=[0.18, 0.16, 0.14, 0.12, 0.2, 0.2])),
                    is_new_customer=bool(rng.random() < (0.42 if campaign.funnel_stage == "prospecting" else 0.14)),
                    gross_revenue=round(gross, 2), discount_amount=round(discount, 2),
                    shipping_revenue=0.0 if gross > 75 else 6.95, tax_amount=round(net * 0.0725, 2),
                    refund_amount=round(refund, 2), fees=round(net * 0.029 + 0.3, 2),
                    net_revenue=round(net, 2), cogs=round(cogs, 2), gross_profit=round(net - cogs, 2),
                    status="refunded" if refund > 0 else "paid",
                ))
                item_rows.append(dict(
                    brand_id=brand.id, order_id=None, sku_id=sku_id, quantity=qty,
                    unit_price=unit_price, unit_cost=sku.cost, discount_amount=round(discount, 2),
                    refund_amount=round(refund, 2), net_revenue=round(net, 2), cogs=round(cogs, 2),
                    shipping_revenue=0.0 if gross > 75 else 6.95, tax_amount=round(net * 0.0725, 2),
                    _order_number=order_number,
                ))
                store_revenue += net
                carrier["velocity"][sku_id][iso] = carrier["velocity"][sku_id].get(iso, 0.0) + qty
                carrier["units"][sku.sku_code] += qty
                carrier["revenue"][sku.sku_code] += net

            overlap = platform["overlap"] * mod["roas"] * float(rng.normal(1.0, 0.03))
            platform_conv = round(true_orders_float * overlap, 2)
            platform_rev = round(store_revenue * overlap, 2)

            campaign_ads = ads_by_campaign.get(campaign.id) or []
            if not campaign_ads:
                continue
            ad_weights = np.array([(a.creative_id or 1) % 7 * 0.1 + 1.0 for a in campaign_ads], dtype=float)
            ad_weights = ad_weights / ad_weights.sum()
            freq_base = base.get("freq_base", platform["freq"]) * mod["freq"]
            cr_quality = {a.creative_id: ((a.creative_id or 1) % 5) * 0.05 + 0.9 for a in campaign_ads}

            for a_idx, ad in enumerate(campaign_ads):
                share = float(ad_weights[a_idx]) * float(rng.normal(1.0, 0.14))
                ad_imp = impressions * share
                ad_clicks = clicks * share * cr_quality[ad.creative_id] * float(rng.normal(1.0, 0.08))
                metrics_rows.append(dict(
                    brand_id=brand.id, date=date, platform_key=campaign.platform_key,
                    campaign_id=campaign.id, ad_set_id=ad.ad_set_id, creative_id=ad.creative_id,
                    impressions=int(ad_imp), clicks=int(round(ad_clicks)), spend=round(spend * share, 4),
                    platform_conversions=round(platform_conv * share, 2),
                    platform_revenue=round(platform_rev * share, 2),
                    ctr=round(ad_clicks / ad_imp, 6) if ad_imp else 0.0,
                    cpc=round(spend * share / ad_clicks, 4) if ad_clicks else 0.0,
                    cpm=round(spend * share / ad_imp * 1000, 4) if ad_imp else 0.0,
                    frequency=round(freq_base * float(rng.normal(1.0, 0.05)), 3),
                    reach=int(ad_imp / max(freq_base, 1.0)), video_views=int(ad_imp * 0.28),
                ))

            daily_campaign_totals[(offset, campaign.id)] = {
                "impressions": impressions, "clicks": clicks, "spend": spend,
                "conversions": platform_conv, "revenue": platform_rev,
                "store_orders": float(n_orders), "store_revenue": store_revenue,
            }

            ga_scale = 0.38 if dt.date(2026, 9, 27) <= date <= dt.date(2026, 10, 1) else 1.0
            sessions = int(clicks * 0.74 * ga_scale * float(rng.normal(1.0, 0.07)))
            for device, dshare in (("mobile", 0.68), ("desktop", 0.24), ("tablet", 0.08)):
                dsess = int(sessions * dshare)
                if dsess <= 0:
                    continue
                ga_rows.append(dict(
                    brand_id=brand.id, date=date, platform_key=campaign.platform_key,
                    utm_source=campaign.platform_key, utm_medium="cpc", utm_campaign=campaign.name,
                    device=device, geo="US", sessions=dsess, engaged_sessions=int(dsess * 0.61),
                    add_to_cart=int(dsess * (0.088 + 0.02 * float(rng.normal()))),
                    begin_checkout=int(dsess * (0.047 + 0.012 * float(rng.normal()))),
                    purchase=int(n_orders * dshare), purchase_revenue=round(store_revenue * dshare, 2),
                    bounces=int(dsess * 0.33),
                ))

        # ---- hourly detail for the trailing window -------------------------
        if offset >= DAYS - HOURS_DAYS:
            for campaign in campaigns:
                totals = daily_campaign_totals.get((offset, campaign.id))
                if not totals:
                    continue
                prof = HOUR_PROFILE / HOUR_PROFILE.sum()
                shares = rng.dirichlet(prof * 26 + 0.4)
                for hour in range(24):
                    s = float(shares[hour])
                    imp = int(totals["impressions"] * s)
                    clk = int(round(totals["clicks"] * s))
                    sp = totals["spend"] * s
                    hourly_rows.append(dict(
                        brand_id=brand.id, ts=dt.datetime.combine(date, dt.time(hour)),
                        date=date, hour=hour, platform_key=campaign.platform_key,
                        campaign_id=campaign.id, ad_set_id=first_adset_by_campaign.get(campaign.id),
                        impressions=imp, clicks=clk, spend=round(sp, 4),
                        platform_conversions=round(totals["conversions"] * s, 2),
                        platform_revenue=round(totals["revenue"] * s, 2),
                        ctr=round(clk / imp, 6) if imp else 0.0,
                        cpc=round(sp / clk, 4) if clk else 0.0,
                        cpm=round(sp / imp * 1000, 4) if imp else 0.0,
                        frequency=round(1.6 + hour / 24 * 2.2, 3),
                    ))

        if offset % 30 == 0:
            step(20 + 42 * (offset + 1) / DAYS, f"simulated day {offset + 1}/{DAYS}")

    return metrics_rows, hourly_rows, order_rows, item_rows, ga_rows, carrier


def _seed_inventory(db: Session, brand: Brand, skus: list[Sku], carrier: dict) -> None:
    """Replenishment-policy simulation over real unit velocity.

    Each SKU runs an (s, S)-style policy: when cover drops below
    ``REORDER_DAYS`` a purchase order for ``TARGET_DAYS`` of demand is placed
    with the SKU's own lead time. ``LUM-VITC-30`` deliberately stops placing
    orders after 2026-06-01 (the "missed PO" of the planted stockout scenario),
    so its cover decays from ~75 days to single digits through August/September
    while the campaigns promoting it keep spending.
    """
    velocity = carrier["velocity"]
    rows: list[dict] = []
    rng = np.random.default_rng(SEED + 7)
    REORDER_DAYS = 35.0
    TARGET_DAYS = 75.0
    LAST_PO_OFFSET = {  # planted: replenishment stops here for the hero SKU
        "LUM-VITC-30": date_offset(dt.date(2026, 6, 1)),
    }

    for sku in skus:
        sku_vel = velocity.get(sku.id, {})
        daily = [sku_vel.get(_iso(_d(o)), 0.0) for o in range(DAYS)]
        lead_time = int(rng.integers(14, 40))
        start_velocity = max(sum(daily[:30]) / 30.0, 1.0)
        on_hand = start_velocity * 30.0  # opening stock ~30 days of cover
        pending: list[tuple[int, float]] = []
        stop_after = LAST_PO_OFFSET.get(sku.sku_code)
        for offset in range(DAYS):
            date = _d(offset)
            arriving = sum(a for o, a in pending if o == offset)
            pending = [(o, a) for o, a in pending if o > offset]
            inbound = sum(a for _, a in pending)
            sold = daily[offset]
            on_hand = max(0.0, on_hand + arriving - sold)
            window = daily[max(0, offset - 13) : offset + 1]
            avg_daily = max(sum(window) / len(window), 0.8)
            cover = (on_hand + inbound) / avg_daily
            if (stop_after is None or offset <= stop_after) and cover < REORDER_DAYS and not pending:
                pending.append((offset + lead_time, max(avg_daily * TARGET_DAYS, 150.0)))

            # --- PLANTED stockout trajectory ---------------------------------
            # The hero SKU missed its PO, so cover decays from a healthy 30 days
            # (2026-06-01) to ~3 days by 2026-09-15 and the replacement shipment
            # only lands after the simulated window closes.
            if sku.sku_code == "LUM-VITC-30":
                decay_start = date_offset(dt.date(2026, 6, 1))
                decay_end = date_offset(dt.date(2026, 9, 15))
                if offset >= decay_start:
                    t = min(max((offset - decay_start) / max(decay_end - decay_start, 1), 0.0), 1.0)
                    desired_cover = 30.0 - 27.0 * t  # 30 -> 3 days
                    on_hand = round(desired_cover * avg_daily, 2)
                    # the replacement PO is placed but only lands on 2026-10-20,
                    # i.e. after the window — so it never relieves cover here
                    inbound = 0.0
                    pending = []
                    cover = (on_hand + inbound) / avg_daily
            rows.append(dict(
                brand_id=brand.id, sku_id=sku.id, date=date, warehouse="WH-1",
                on_hand=round(on_hand, 2), inbound=round(inbound, 2), committed=round(sold, 2),
                lead_time_days=lead_time, reorder_point=round(avg_daily * REORDER_DAYS, 1),
                daily_velocity=round(avg_daily, 3), days_of_cover=round(cover, 2),
                stockout_flag=cover < 7,
            ))
    _bulk(db, InventorySnapshot, rows)
    db.flush()


def _seed_governance(db: Session, brand: Brand, campaigns: list[Campaign], skus: list[Sku]) -> None:
    objective = BusinessObjective(
        brand_id=brand.id, name="FY26 Profitable Growth Plan", is_active=True,
        total_monthly_budget=round(sum(c.daily_budget for c in campaigns) * 30.4, 2),
        total_daily_budget=round(sum(c.daily_budget for c in campaigns), 2),
        target_blended_roas=3.20, target_mer=3.60, min_contribution_margin_pct=18.0, max_cpa=72.0,
        max_daily_budget_shift_pct=25.0, min_days_of_inventory_cover=14.0,
        objective_weights={"profit": 0.5, "revenue": 0.2, "efficiency": 0.2, "clearance": 0.1},
        per_platform_min={"meta": 800.0, "google": 700.0, "amazon": 300.0, "tiktok": 350.0, "programmatic": 150.0},
        per_platform_max={"meta": 3500.0, "google": 3200.0, "amazon": 1400.0, "tiktok": 1800.0, "programmatic": 900.0},
        seasonality_priorities=[{"month": 11, "note": "BFCM gifting peak"}, {"month": 12, "note": "Holiday shipping cut-off"}],
        launch_priorities=["Barrier Repair Launch"], protect_learning_phase=True, horizon_days=30,
        period_start=START, period_end=_d(30),
    )
    db.add(objective)
    db.flush()

    constraints = [
        ("max_daily_budget_shift_pct", "global", "", "<=", 25.0, True, "Daily budget change per entity capped at 25%"),
        ("min_days_of_inventory_cover", "global", "", ">=", 14.0, True, "Never scale ads for SKUs below 14 days of cover"),
        ("min_contribution_margin_pct", "global", "", ">=", 18.0, True, "Portfolio contribution-margin floor"),
        ("target_blended_roas", "global", "", ">=", 3.20, True, "Blended ROAS objective"),
        ("max_cpa", "global", "", "<=", 72.0, True, "Blended CPA ceiling"),
        ("per_platform_min", "platform", "meta", ">=", 800.0, True, "Meta minimum daily budget"),
        ("per_platform_max", "platform", "meta", "<=", 3500.0, True, "Meta maximum daily budget"),
        ("per_platform_min", "platform", "google", ">=", 700.0, True, "Google minimum daily budget"),
        ("per_platform_max", "platform", "google", "<=", 3200.0, True, "Google maximum daily budget"),
        ("per_platform_min", "platform", "amazon", ">=", 300.0, True, "Amazon minimum daily budget"),
        ("per_platform_max", "platform", "amazon", "<=", 1400.0, True, "Amazon maximum daily budget"),
        ("per_platform_min", "platform", "tiktok", ">=", 350.0, True, "TikTok minimum daily budget"),
        ("per_platform_max", "platform", "tiktok", "<=", 1800.0, True, "TikTok maximum daily budget"),
        ("per_platform_min", "platform", "programmatic", ">=", 150.0, True, "Programmatic minimum daily budget"),
        ("per_platform_max", "platform", "programmatic", "<=", 900.0, True, "Programmatic maximum daily budget"),
        ("spend_cap", "global", "", "<=", 12000.0, True, "Hard daily spend cap across the account"),
        ("protect_learning_phase", "global", "", ">=", 1.0, True, "Do not change budgets >20% on campaigns in learning"),
        ("blackout_period", "global", "", "<=", 0.0, False, "No automated changes during the BFCM blackout"),
    ]
    for key, scope, ref, op, value, hard, label in constraints:
        db.add(Constraint(
            brand_id=brand.id, objective_id=objective.id, key=key, scope=scope, scope_ref=ref,
            operator=op, value=value, hard=hard, label=label,
        ))

    for scope, ref, level in [
        ("global", "", "human_in_the_loop"), ("platform", "meta", "human_in_the_loop"),
        ("platform", "google", "advisory"), ("platform", "tiktok", "autonomous_within_guardrails"),
        ("platform", "amazon", "human_in_the_loop"), ("platform", "programmatic", "advisory"),
        ("action_type", "pause_campaign", "autonomous_within_guardrails"),
        ("action_type", "budget_reallocation", "human_in_the_loop"),
    ]:
        db.add(AutonomySetting(
            brand_id=brand.id, scope=scope, scope_ref=ref, level=level,
            auto_execute_max_daily_spend_delta=500.0, auto_execute_min_confidence=0.80,
            auto_execute_max_risk="low",
        ))

    for key, label, enabled, value, unit, config in [
        ("spend_cap_daily", "Global daily spend cap", True, 12000.0, "usd", {}),
        ("max_change_pct", "Max change per action", True, 25.0, "pct", {}),
        ("cooldown_hours", "Cooldown between changes on one entity", True, 24.0, "hours", {}),
        ("blackout_periods", "Blackout windows", True, 0.0, "window",
         {"windows": [{"start": "2026-11-26", "end": "2026-11-30", "label": "BFCM freeze"}]}),
        ("anomaly_kill_switch", "Auto-pause on severity > 85 anomalies", True, 85.0, "severity", {}),
        ("global_kill_switch", "Global pause of all automated changes", False, 0.0, "bool", {}),
    ]:
        db.add(Guardrail(brand_id=brand.id, key=key, label=label, enabled=enabled, value=value, unit=unit, config=config))

    sources = [
        ("meta_ads", "Meta Ads", "ads", "meta", "MetaAdsConnector"),
        ("google_ads", "Google Ads", "ads", "google", "GoogleAdsConnector"),
        ("amazon_ads", "Amazon Ads", "ads", "amazon", "AmazonAdsConnector"),
        ("tiktok_ads", "TikTok Ads", "ads", "tiktok", "TikTokAdsConnector"),
        ("programmatic_dsp", "Programmatic DSP", "ads", "programmatic", "ProgrammaticConnector"),
        ("store_orders", "Store orders (Shopify-style)", "commerce", None, "StoreConnector"),
        ("ga4_events", "GA4 events", "analytics", None, "GA4Connector"),
        ("erp_inventory", "ERP inventory & SKU margin", "erp", None, "ERPConnector"),
        ("creative_library", "Creative library", "creative", None, "CreativeConnector"),
    ]
    for key, name, kind, platform, connector in sources:
        db.add(DataSource(
            brand_id=brand.id, key=key, name=name, kind=kind, platform_key=platform,
            connector=connector, mode="simulated", status="connected",
            last_sync_at=dt.datetime.combine(TODAY, dt.time(6, 15)),
            freshness_hours=round(3.5 + random.random() * 6, 1),
            completeness_pct=round(96 + random.random() * 4, 2),
            validity_pct=round(97 + random.random() * 3, 2),
            quality_score=round(93 + random.random() * 6, 2),
            row_count=int(random.randint(20_000, 400_000)), issues=[],
            auth_config={"mode": "simulated", "fields": ["account_id", "access_token", "refresh_token"]},
        ))

    db.add(SimulationState(
        brand_id=brand.id, clock_date=TODAY, real_start_date=START, days_advanced=0,
        kill_switch=False, last_tick_at=dt.datetime.combine(TODAY, dt.time(7, 0)),
    ))

    for scenario in SCENARIOS:
        db.add(ScenarioStatus(
            brand_id=brand.id, key=scenario["key"], title=scenario["title"],
            description=scenario["description"], expected_driver=scenario["expected_driver"],
            planted_at=scenario["planted_at"], detected=False, diagnosed=False, recommended=False,
            detection_note="",
        ))
    db.flush()
