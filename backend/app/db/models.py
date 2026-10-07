"""Unified AdPilot schema.

Every table is org/brand scoped through ``brand_id`` where it represents
marketable objects, and carries ``created_at`` / ``updated_at`` timestamps.
The schema is intentionally dialect-agnostic (JSON columns, string-encoded
enums) so that the identical ORM model runs on PostgreSQL and on the SQLite
fallback used in the sandbox.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class SerialMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)


# --------------------------------------------------------------------------- #
# Organisation, identity, RBAC
# --------------------------------------------------------------------------- #
class Brand(Base, SerialMixin, TimestampMixin):
    __tablename__ = "brands"
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    vertical: Mapped[str] = mapped_column(String(60), default="skincare")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    fiscal_year_start_month: Mapped[int] = mapped_column(Integer, default=1)
    logo_emoji: Mapped[str] = mapped_column(String(8), default="✦")

    users: Mapped[list["User"]] = relationship(back_populates="brand")


class Role(Base, SerialMixin, TimestampMixin):
    __tablename__ = "roles"
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="")
    # permissions expressed as coarse capability flags consumed by RBAC deps
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)


class User(Base, SerialMixin, TimestampMixin):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(190), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role_name: Mapped[str] = mapped_column(String(40), ForeignKey("roles.name"), default="Viewer")
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("brands.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    preferences: Mapped[dict] = mapped_column(JSON, default=dict)

    brand: Mapped[Brand | None] = relationship(back_populates="users")


# --------------------------------------------------------------------------- #
# Platform topology
# --------------------------------------------------------------------------- #
class Platform(Base, SerialMixin, TimestampMixin):
    __tablename__ = "platforms"
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    category: Mapped[str] = mapped_column(String(40), default="paid_social")
    color: Mapped[str] = mapped_column(String(16), default="#6366f1")
    attribution_window_days: Mapped[int] = mapped_column(Integer, default=7)
    supports_hourly: Mapped[bool] = mapped_column(Boolean, default=True)
    api_version: Mapped[str] = mapped_column(String(40), default="")
    notes: Mapped[str] = mapped_column(Text, default="")


class AdAccount(Base, SerialMixin, TimestampMixin):
    __tablename__ = "ad_accounts"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform_key: Mapped[str] = mapped_column(String(40), ForeignKey("platforms.key"), index=True)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    timezone: Mapped[str] = mapped_column(String(60), default="UTC")
    status: Mapped[str] = mapped_column(String(20), default="active")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("brand_id", "platform_key", "external_id"),)


class Campaign(Base, SerialMixin, TimestampMixin):
    __tablename__ = "campaigns"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    platform_key: Mapped[str] = mapped_column(String(40), ForeignKey("platforms.key"), index=True)
    ad_account_id: Mapped[int | None] = mapped_column(ForeignKey("ad_accounts.id"), nullable=True)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(190), nullable=False)
    objective: Mapped[str] = mapped_column(String(40), default="conversions")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    daily_budget: Mapped[float] = mapped_column(Float, default=0.0)
    lifetime_budget: Mapped[float | None] = mapped_column(Float, nullable=True)
    bid_strategy: Mapped[str] = mapped_column(String(40), default="lowest_cost")
    target_roas: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_cpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    launched_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    ended_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    funnel_stage: Mapped[str] = mapped_column(String(24), default="prospecting")
    is_branded: Mapped[bool] = mapped_column(Boolean, default=False)
    learning_phase: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (Index("ix_campaigns_brand_platform", "brand_id", "platform_key"),)


class AdSet(Base, SerialMixin, TimestampMixin):
    __tablename__ = "ad_sets"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(190), default="")
    audience_name: Mapped[str] = mapped_column(String(120), default="")
    targeting: Mapped[dict] = mapped_column(JSON, default=dict)
    audience_size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    daily_budget: Mapped[float] = mapped_column(Float, default=0.0)
    bid_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    placement: Mapped[str] = mapped_column(String(60), default="automatic")
    device_targeting: Mapped[str] = mapped_column(String(40), default="all")
    geo_targeting: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("campaign_id", "external_id"),)


class Creative(Base, SerialMixin, TimestampMixin):
    __tablename__ = "creatives"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(190), default="")
    format: Mapped[str] = mapped_column(String(40), default="image")  # image/video/carousel/ugc/static
    hook_type: Mapped[str] = mapped_column(String(60), default="benefit")
    theme: Mapped[str] = mapped_column(String(60), default="")
    angle: Mapped[str] = mapped_column(String(60), default="")
    asset_url: Mapped[str] = mapped_column(String(400), default="")
    thumbnail_url: Mapped[str] = mapped_column(String(400), default="")
    launched_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    retired_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    fatigue_score: Mapped[float] = mapped_column(Float, default=0.0)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("brand_id", "external_id"),)


class Ad(Base, SerialMixin, TimestampMixin):
    __tablename__ = "ads"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    ad_set_id: Mapped[int] = mapped_column(ForeignKey("ad_sets.id"), index=True)
    creative_id: Mapped[int] = mapped_column(ForeignKey("creatives.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(80), default="")
    name: Mapped[str] = mapped_column(String(190), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    launched_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class CampaignSkuMap(Base, SerialMixin, TimestampMixin):
    """Which SKUs a campaign / ad set promotes (used for SKU-level attribution)."""

    __tablename__ = "campaign_sku_map"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    ad_set_id: Mapped[int | None] = mapped_column(ForeignKey("ad_sets.id"), nullable=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------------- #
# Measurement
# --------------------------------------------------------------------------- #
class AdMetricsDaily(Base, SerialMixin):
    __tablename__ = "ad_metrics_daily"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    platform_key: Mapped[str] = mapped_column(String(40), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    ad_set_id: Mapped[int] = mapped_column(ForeignKey("ad_sets.id"), index=True)
    creative_id: Mapped[int | None] = mapped_column(ForeignKey("creatives.id"), nullable=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    platform_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    platform_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    cpc: Mapped[float] = mapped_column(Float, default=0.0)
    cpm: Mapped[float] = mapped_column(Float, default=0.0)
    frequency: Mapped[float] = mapped_column(Float, default=0.0)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    video_views: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        UniqueConstraint("date", "ad_set_id", "creative_id", name="uq_metric_daily"),
        Index("ix_metrics_daily_lookup", "brand_id", "date", "campaign_id"),
    )


class AdMetricsHourly(Base, SerialMixin):
    __tablename__ = "ad_metrics_hourly"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    hour: Mapped[int] = mapped_column(Integer, index=True)
    platform_key: Mapped[str] = mapped_column(String(40), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    ad_set_id: Mapped[int | None] = mapped_column(ForeignKey("ad_sets.id"), nullable=True, index=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    platform_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    platform_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    ctr: Mapped[float] = mapped_column(Float, default=0.0)
    cpc: Mapped[float] = mapped_column(Float, default=0.0)
    cpm: Mapped[float] = mapped_column(Float, default=0.0)
    frequency: Mapped[float] = mapped_column(Float, default=0.0)
    __table_args__ = (Index("ix_metrics_hourly_lookup", "brand_id", "date", "hour", "campaign_id"),)


# --------------------------------------------------------------------------- #
# Commerce: products, pricing, inventory
# --------------------------------------------------------------------------- #
class Product(Base, SerialMixin, TimestampMixin):
    __tablename__ = "products"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(190), nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True)
    subcategory: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    lifecycle: Mapped[str] = mapped_column(String(24), default="core")  # launch/core/decline/discontinued
    hero_product: Mapped[bool] = mapped_column(Boolean, default=False)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


class Sku(Base, SerialMixin, TimestampMixin):
    __tablename__ = "skus"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    sku_code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(190), nullable=False)
    size: Mapped[str] = mapped_column(String(40), default="30ml")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    margin_pct: Mapped[float] = mapped_column(Float, default=0.0)
    msrp: Mapped[float] = mapped_column(Float, default=0.0)
    min_price: Mapped[float] = mapped_column(Float, default=0.0)
    lifecycle: Mapped[str] = mapped_column(String(24), default="core")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SkuPriceHistory(Base, SerialMixin, TimestampMixin):
    __tablename__ = "sku_price_history"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), index=True)
    effective_date: Mapped[dt.date] = mapped_column(Date, index=True)
    end_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    promo_name: Mapped[str] = mapped_column(String(80), default="")
    change_reason: Mapped[str] = mapped_column(String(80), default="")
    previous_price: Mapped[float | None] = mapped_column(Float, nullable=True)


class InventorySnapshot(Base, SerialMixin, TimestampMixin):
    __tablename__ = "inventory_snapshots"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    warehouse: Mapped[str] = mapped_column(String(60), default="WH-1")
    on_hand: Mapped[float] = mapped_column(Float, default=0.0)
    inbound: Mapped[float] = mapped_column(Float, default=0.0)
    committed: Mapped[float] = mapped_column(Float, default=0.0)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=21)
    reorder_point: Mapped[float] = mapped_column(Float, default=0.0)
    daily_velocity: Mapped[float] = mapped_column(Float, default=0.0)
    days_of_cover: Mapped[float] = mapped_column(Float, default=0.0)
    stockout_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("date", "sku_id", "warehouse", name="uq_inv_daily"),)


class Order(Base, SerialMixin, TimestampMixin):
    __tablename__ = "orders"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    order_number: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    ordered_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    channel: Mapped[str] = mapped_column(String(40), default="shopify")
    source: Mapped[str] = mapped_column(String(60), default="")
    medium: Mapped[str] = mapped_column(String(60), default="")
    campaign_name: Mapped[str] = mapped_column(String(190), default="")
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    device: Mapped[str] = mapped_column(String(24), default="mobile")
    geo: Mapped[str] = mapped_column(String(24), default="US-CA")
    is_new_customer: Mapped[bool] = mapped_column(Boolean, default=True)
    gross_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0)
    shipping_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    net_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    gross_profit: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(20), default="paid")


class OrderItem(Base, SerialMixin, TimestampMixin):
    __tablename__ = "order_items"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    sku_id: Mapped[int] = mapped_column(ForeignKey("skus.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0.0)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    net_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    shipping_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0)


class GaEvent(Base, SerialMixin, TimestampMixin):
    """Aggregated GA4 funnel events (sessions -> ATC -> checkout -> purchase)."""

    __tablename__ = "ga_events"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    utm_source: Mapped[str] = mapped_column(String(60), default="")
    utm_medium: Mapped[str] = mapped_column(String(60), default="")
    utm_campaign: Mapped[str] = mapped_column(String(190), default="")
    device: Mapped[str] = mapped_column(String(24), default="mobile")
    geo: Mapped[str] = mapped_column(String(24), default="US-CA")
    sessions: Mapped[int] = mapped_column(Integer, default=0)
    engaged_sessions: Mapped[int] = mapped_column(Integer, default=0)
    add_to_cart: Mapped[int] = mapped_column(Integer, default=0)
    begin_checkout: Mapped[int] = mapped_column(Integer, default=0)
    purchase: Mapped[int] = mapped_column(Integer, default=0)
    purchase_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    bounces: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (Index("ix_ga_lookup", "brand_id", "date", "utm_source"),)


# --------------------------------------------------------------------------- #
# Reconciliation outputs
# --------------------------------------------------------------------------- #
class UnifiedFact(Base, SerialMixin):
    __tablename__ = "unified_facts"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    platform_key: Mapped[str] = mapped_column(String(40), index=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"), index=True)
    ad_set_id: Mapped[int] = mapped_column(ForeignKey("ad_sets.id"), index=True)
    creative_id: Mapped[int | None] = mapped_column(ForeignKey("creatives.id"), nullable=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"), nullable=True, index=True)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    spend: Mapped[float] = mapped_column(Float, default=0.0)
    platform_conversions: Mapped[float] = mapped_column(Float, default=0.0)
    platform_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    attributed_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    attributed_orders: Mapped[float] = mapped_column(Float, default=0.0)
    cogs: Mapped[float] = mapped_column(Float, default=0.0)
    discounts: Mapped[float] = mapped_column(Float, default=0.0)
    refunds: Mapped[float] = mapped_column(Float, default=0.0)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    contribution_margin: Mapped[float] = mapped_column(Float, default=0.0)
    inventory_days_of_cover: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    discount_pct: Mapped[float] = mapped_column(Float, default=0.0)
    frequency: Mapped[float] = mapped_column(Float, default=0.0)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    ga_sessions: Mapped[int] = mapped_column(Integer, default=0)
    ga_purchases: Mapped[int] = mapped_column(Integer, default=0)
    discrepancy_pct: Mapped[float] = mapped_column(Float, default=0.0)
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint("date", "ad_set_id", "sku_id", name="uq_unified_fact"),
        Index("ix_unified_lookup", "brand_id", "date", "campaign_id"),
    )


class ReconciliationReport(Base, SerialMixin, TimestampMixin):
    __tablename__ = "reconciliation_reports"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    date: Mapped[dt.date] = mapped_column(Date, index=True)
    platform_key: Mapped[str] = mapped_column(String(40), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    platform_reported_conv: Mapped[float] = mapped_column(Float, default=0.0)
    platform_reported_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    store_orders: Mapped[float] = mapped_column(Float, default=0.0)
    store_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    ga_purchases: Mapped[float] = mapped_column(Float, default=0.0)
    ga_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    overlap_factor: Mapped[float] = mapped_column(Float, default=1.0)
    discrepancy_pct: Mapped[float] = mapped_column(Float, default=0.0)
    attributed_revenue: Mapped[float] = mapped_column(Float, default=0.0)
    resolution_method: Mapped[str] = mapped_column(String(120), default="proportional_deoverlap")
    notes: Mapped[str] = mapped_column(Text, default="")


class DataSource(Base, SerialMixin, TimestampMixin):
    __tablename__ = "data_sources"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    key: Mapped[str] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(40), default="ads")
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    connector: Mapped[str] = mapped_column(String(60), default="")
    mode: Mapped[str] = mapped_column(String(20), default="simulated")  # simulated | live
    status: Mapped[str] = mapped_column(String(20), default="connected")
    auth_config: Mapped[dict] = mapped_column(JSON, default=dict)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_cursor: Mapped[str] = mapped_column(String(120), default="")
    freshness_hours: Mapped[float] = mapped_column(Float, default=0.0)
    completeness_pct: Mapped[float] = mapped_column(Float, default=100.0)
    validity_pct: Mapped[float] = mapped_column(Float, default=100.0)
    quality_score: Mapped[float] = mapped_column(Float, default=100.0)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (UniqueConstraint("brand_id", "key"),)


class SyncRun(Base, SerialMixin, TimestampMixin):
    __tablename__ = "sync_runs"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(60), default="all")
    job_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")
    mode: Mapped[str] = mapped_column(String(20), default="simulated")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    rows_ingested: Mapped[int] = mapped_column(Integer, default=0)
    started_by: Mapped[str] = mapped_column(String(120), default="system")
    log: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")


class DataUpload(Base, SerialMixin, TimestampMixin):
    __tablename__ = "data_uploads"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(60))
    filename: Mapped[str] = mapped_column(String(255))
    rows: Mapped[int] = mapped_column(Integer, default=0)
    valid_rows: Mapped[int] = mapped_column(Integer, default=0)
    invalid_rows: Mapped[int] = mapped_column(Integer, default=0)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    preview: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="validated")
    uploaded_by: Mapped[str] = mapped_column(String(120), default="")


class SavedView(Base, SerialMixin, TimestampMixin):
    __tablename__ = "saved_views"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    page: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)


class Notification(Base, SerialMixin, TimestampMixin):
    __tablename__ = "notifications"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="info")
    title: Mapped[str] = mapped_column(String(190), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="info")
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


# --------------------------------------------------------------------------- #
# Decision layer
# --------------------------------------------------------------------------- #
class Anomaly(Base, SerialMixin, TimestampMixin):
    __tablename__ = "anomalies"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    metric: Mapped[str] = mapped_column(String(40), index=True)
    entity_type: Mapped[str] = mapped_column(String(24), index=True)  # platform/campaign/ad_set/creative/sku
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_name: Mapped[str] = mapped_column(String(190), default="")
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    granularity: Mapped[str] = mapped_column(String(12), default="daily")
    detected_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    window_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    severity: Mapped[float] = mapped_column(Float, default=0.0)
    severity_label: Mapped[str] = mapped_column(String(12), default="medium")
    direction: Mapped[str] = mapped_column(String(12), default="drop")
    magnitude_pct: Mapped[float] = mapped_column(Float, default=0.0)
    baseline: Mapped[float] = mapped_column(Float, default=0.0)
    observed: Mapped[float] = mapped_column(Float, default=0.0)
    expected: Mapped[float] = mapped_column(Float, default=0.0)
    lower_bound: Mapped[float] = mapped_column(Float, default=0.0)
    upper_bound: Mapped[float] = mapped_column(Float, default=0.0)
    p_value: Mapped[float] = mapped_column(Float, default=1.0)
    effect_size: Mapped[float] = mapped_column(Float, default=0.0)
    method_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    methods_agreeing: Mapped[int] = mapped_column(Integer, default=1)
    is_data_artifact: Mapped[bool] = mapped_column(Boolean, default=False)
    artifact_reason: Mapped[str] = mapped_column(String(190), default="")
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    scenario_tag: Mapped[str] = mapped_column(String(60), default="", index=True)


class Diagnosis(Base, SerialMixin, TimestampMixin):
    __tablename__ = "diagnoses"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    anomaly_id: Mapped[int] = mapped_column(ForeignKey("anomalies.id"), index=True)
    primary_driver: Mapped[str] = mapped_column(String(60), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(12), default="medium")
    drivers: Mapped[list] = mapped_column(JSON, default=list)
    decomposition: Mapped[dict] = mapped_column(JSON, default=dict)
    causal: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    narrative: Mapped[str] = mapped_column(Text, default="")
    narrative_source: Mapped[str] = mapped_column(String(24), default="template")
    artifact_flags: Mapped[list] = mapped_column(JSON, default=list)
    recommended_actions: Mapped[list] = mapped_column(JSON, default=list)
    agent_run_id: Mapped[int | None] = mapped_column(ForeignKey("agent_runs.id"), nullable=True)


class Opportunity(Base, SerialMixin, TimestampMixin):
    __tablename__ = "opportunities"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    entity_type: Mapped[str] = mapped_column(String(24), default="campaign")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    entity_name: Mapped[str] = mapped_column(String(190), default="")
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), default="winning")  # winning | leak
    profit_lower: Mapped[float] = mapped_column(Float, default=0.0)
    profit_upper: Mapped[float] = mapped_column(Float, default=0.0)
    win_probability: Mapped[float] = mapped_column(Float, default=0.0)
    availability_factor: Mapped[float] = mapped_column(Float, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    headroom_pct: Mapped[float] = mapped_column(Float, default=0.0)
    marginal_roas: Mapped[float] = mapped_column(Float, default=0.0)
    current_roas: Mapped[float] = mapped_column(Float, default=0.0)
    contribution_margin_pct: Mapped[float] = mapped_column(Float, default=0.0)
    days_of_cover: Mapped[float] = mapped_column(Float, default=0.0)
    shap_factors: Mapped[list] = mapped_column(JSON, default=list)
    response_curve: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="open")
    title: Mapped[str] = mapped_column(String(190), default="")
    expected_revenue_delta: Mapped[float] = mapped_column(Float, default=0.0)
    expected_spend_delta: Mapped[float] = mapped_column(Float, default=0.0)
    horizon_days: Mapped[int] = mapped_column(Integer, default=90)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str] = mapped_column(Text, default="")
    expected_profit_delta: Mapped[float] = mapped_column(Float, default=0.0)
class Recommendation(Base, SerialMixin, TimestampMixin):
    __tablename__ = "recommendations"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[str] = mapped_column(String(24), default="campaign")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_name: Mapped[str] = mapped_column(String(190), default="")
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    sku_id: Mapped[int | None] = mapped_column(ForeignKey("skus.id"), nullable=True)
    anomaly_id: Mapped[int | None] = mapped_column(ForeignKey("anomalies.id"), nullable=True)
    diagnosis_id: Mapped[int | None] = mapped_column(ForeignKey("diagnoses.id"), nullable=True)
    opportunity_id: Mapped[int | None] = mapped_column(ForeignKey("opportunities.id"), nullable=True)
    cause: Mapped[str] = mapped_column(Text, default="")
    current_value: Mapped[Any] = mapped_column(JSON, nullable=True)  # numeric or descriptive
    proposed_value: Mapped[Any] = mapped_column(JSON, nullable=True)  # numeric or descriptive
    value_unit: Mapped[str] = mapped_column(String(24), default="usd")
    expected_profit_delta: Mapped[float] = mapped_column(Float, default=0.0)
    expected_revenue_delta: Mapped[float] = mapped_column(Float, default=0.0)
    expected_roas_delta: Mapped[float] = mapped_column(Float, default=0.0)
    impact_p10: Mapped[float] = mapped_column(Float, default=0.0)
    impact_p90: Mapped[float] = mapped_column(Float, default=0.0)
    impact_distribution: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_level: Mapped[str] = mapped_column(String(12), default="low")
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    priority_formula: Mapped[str] = mapped_column(String(190), default="")
    constraints_check: Mapped[list] = mapped_column(JSON, default=list)
    constraints_passed: Mapped[bool] = mapped_column(Boolean, default=True)
    time_to_impact_days: Mapped[int] = mapped_column(Integer, default=7)
    rollback_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    execution_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    advisory_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", index=True)
    autonomy_level: Mapped[str] = mapped_column(String(24), default="human_in_the_loop")
    rejection_reason: Mapped[str] = mapped_column(Text, default="")
    edited_before_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[str] = mapped_column(String(120), default="")
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    action: Mapped[str] = mapped_column(Text, default="")
    expected_spend_delta: Mapped[float] = mapped_column(Float, default=0.0)
    urgency: Mapped[float] = mapped_column(Float, default=0.5)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    autonomy_reason: Mapped[str] = mapped_column(Text, default="")
    dedupe_key: Mapped[str] = mapped_column(String(40), default="", index=True)
    narrative: Mapped[str] = mapped_column(Text, default="")
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    kind: Mapped[str] = mapped_column(String(40), default="")
class Execution(Base, SerialMixin, TimestampMixin):
    __tablename__ = "executions"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"), index=True)
    platform_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    connector: Mapped[str] = mapped_column(String(60), default="")
    mode: Mapped[str] = mapped_column(String(20), default="simulated")
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    rollback_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    executed_by: Mapped[str] = mapped_column(String(120), default="")
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(40), default="")
    entity_type: Mapped[str] = mapped_column(String(24), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    entity_name: Mapped[str] = mapped_column(String(190), default="")
    actor: Mapped[str] = mapped_column(String(120), default="system")
    pre_state: Mapped[dict] = mapped_column(JSON, default=dict)
    push_response: Mapped[dict] = mapped_column(JSON, default=dict)
    rollback_plan: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    measurement_window_start: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    measurement_window_end: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
class Outcome(Base, SerialMixin, TimestampMixin):
    __tablename__ = "outcomes"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    execution_id: Mapped[int] = mapped_column(ForeignKey("executions.id"), index=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"), index=True)
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    window_start: Mapped[dt.date] = mapped_column(Date)
    window_end: Mapped[dt.date] = mapped_column(Date)
    predicted_profit_delta: Mapped[float] = mapped_column(Float, default=0.0)
    actual_profit_delta: Mapped[float] = mapped_column(Float, default=0.0)
    predicted_revenue_delta: Mapped[float] = mapped_column(Float, default=0.0)
    actual_revenue_delta: Mapped[float] = mapped_column(Float, default=0.0)
    absolute_error: Mapped[float] = mapped_column(Float, default=0.0)
    pct_error: Mapped[float] = mapped_column(Float, default=0.0)
    incremental_profit: Mapped[float] = mapped_column(Float, default=0.0)
    counterfactual_method: Mapped[str] = mapped_column(String(80), default="synthetic_control")
    counterfactual: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confounders: Mapped[list] = mapped_column(JSON, default=list)
    verdict: Mapped[str] = mapped_column(String(20), default="inconclusive")
    narrative: Mapped[str] = mapped_column(Text, default="")
    window_days: Mapped[int] = mapped_column(Integer, default=14)
    ci_low: Mapped[float] = mapped_column(Float, default=0.0)
    ci_high: Mapped[float] = mapped_column(Float, default=0.0)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
class ModelRegistry(Base, SerialMixin, TimestampMixin):
    __tablename__ = "model_registry"
    name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(24), default="v1")
    kind: Mapped[str] = mapped_column(String(40), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    hyperparams: Mapped[dict] = mapped_column(JSON, default=dict)
    trained_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("name", "version"),)


class ModelFeedback(Base, SerialMixin, TimestampMixin):
    __tablename__ = "model_feedback"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(80), index=True)
    source: Mapped[str] = mapped_column(String(40), default="outcome")
    ref_type: Mapped[str] = mapped_column(String(40), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adjustment: Mapped[dict] = mapped_column(JSON, default=dict)
    metric_before: Mapped[float] = mapped_column(Float, default=0.0)
    metric_after: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(String(255), default="")


class PredictionRecord(Base, SerialMixin, TimestampMixin):
    """Forecast/prediction ledger used for accuracy trending on the Learning page."""

    __tablename__ = "prediction_records"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    model_name: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(24), default="campaign")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_metric: Mapped[str] = mapped_column(String(40), default="roas")
    horizon_days: Mapped[int] = mapped_column(Integer, default=7)
    predicted: Mapped[float] = mapped_column(Float, default=0.0)
    lower: Mapped[float] = mapped_column(Float, default=0.0)
    upper: Mapped[float] = mapped_column(Float, default=0.0)
    issued_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, index=True)
    target_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    within_interval: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    abs_error: Mapped[float | None] = mapped_column(Float, nullable=True)


class AgentRun(Base, SerialMixin, TimestampMixin):
    __tablename__ = "agent_runs"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="diagnosis")
    trigger: Mapped[str] = mapped_column(String(120), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="completed")
    plan: Mapped[list] = mapped_column(JSON, default=list)
    tools_used: Mapped[list] = mapped_column(JSON, default=list)
    final_answer: Mapped[str] = mapped_column(Text, default="")
    narrative_source: Mapped[str] = mapped_column(String(24), default="template")
    tokens_used: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    anomaly_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AgentStep(Base, SerialMixin, TimestampMixin):
    __tablename__ = "agent_steps"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    idx: Mapped[int] = mapped_column(Integer, default=0)
    phase: Mapped[str] = mapped_column(String(24), default="tool")  # plan/tool/observe/synthesize
    tool: Mapped[str] = mapped_column(String(60), default="")
    thought: Mapped[str] = mapped_column(Text, default="")
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #
class BusinessObjective(Base, SerialMixin, TimestampMixin):
    __tablename__ = "business_objectives"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="Primary growth plan")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    total_monthly_budget: Mapped[float] = mapped_column(Float, default=0.0)
    total_daily_budget: Mapped[float] = mapped_column(Float, default=0.0)
    target_blended_roas: Mapped[float] = mapped_column(Float, default=3.2)
    target_mer: Mapped[float] = mapped_column(Float, default=3.6)
    min_contribution_margin_pct: Mapped[float] = mapped_column(Float, default=18.0)
    max_cpa: Mapped[float] = mapped_column(Float, default=70.0)
    max_daily_budget_shift_pct: Mapped[float] = mapped_column(Float, default=25.0)
    min_days_of_inventory_cover: Mapped[float] = mapped_column(Float, default=14.0)
    objective_weights: Mapped[dict] = mapped_column(
        JSON, default=lambda: {"profit": 0.5, "revenue": 0.2, "efficiency": 0.2, "clearance": 0.1}
    )
    per_platform_min: Mapped[dict] = mapped_column(JSON, default=dict)
    per_platform_max: Mapped[dict] = mapped_column(JSON, default=dict)
    seasonality_priorities: Mapped[list] = mapped_column(JSON, default=list)
    launch_priorities: Mapped[list] = mapped_column(JSON, default=list)
    protect_learning_phase: Mapped[bool] = mapped_column(Boolean, default=True)
    horizon_days: Mapped[int] = mapped_column(Integer, default=30)
    period_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class Constraint(Base, SerialMixin, TimestampMixin):
    __tablename__ = "constraints"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    objective_id: Mapped[int | None] = mapped_column(ForeignKey("business_objectives.id"), nullable=True)
    key: Mapped[str] = mapped_column(String(80), index=True)
    scope: Mapped[str] = mapped_column(String(40), default="global")
    scope_ref: Mapped[str] = mapped_column(String(80), default="")
    operator: Mapped[str] = mapped_column(String(8), default="<=")
    value: Mapped[float] = mapped_column(Float, default=0.0)
    hard: Mapped[bool] = mapped_column(Boolean, default=True)
    label: Mapped[str] = mapped_column(String(190), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AutonomySetting(Base, SerialMixin, TimestampMixin):
    __tablename__ = "autonomy_settings"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    scope: Mapped[str] = mapped_column(String(40), default="global")  # global/platform/action_type
    scope_ref: Mapped[str] = mapped_column(String(80), default="")
    level: Mapped[str] = mapped_column(String(24), default="human_in_the_loop")
    auto_execute_max_daily_spend_delta: Mapped[float] = mapped_column(Float, default=500.0)
    auto_execute_min_confidence: Mapped[float] = mapped_column(Float, default=0.8)
    auto_execute_max_risk: Mapped[str] = mapped_column(String(12), default="low")
    __table_args__ = (UniqueConstraint("brand_id", "scope", "scope_ref"),)


class Guardrail(Base, SerialMixin, TimestampMixin):
    __tablename__ = "guardrails"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    key: Mapped[str] = mapped_column(String(60), index=True)
    label: Mapped[str] = mapped_column(String(190), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(24), default="usd")
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base, SerialMixin, TimestampMixin):
    __tablename__ = "audit_log"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    actor: Mapped[str] = mapped_column(String(120), default="system")
    actor_role: Mapped[str] = mapped_column(String(40), default="system")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity_type: Mapped[str] = mapped_column(String(40), default="")
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    immutable_hash: Mapped[str] = mapped_column(String(64), default="")


class SimulationState(Base, SerialMixin, TimestampMixin):
    __tablename__ = "simulation_state"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True, unique=True)
    clock_date: Mapped[dt.date] = mapped_column(Date)
    real_start_date: Mapped[dt.date] = mapped_column(Date)
    days_advanced: Mapped[int] = mapped_column(Integer, default=0)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    streamlined: Mapped[bool] = mapped_column(Boolean, default=True)
    last_tick_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class ScenarioStatus(Base, SerialMixin, TimestampMixin):
    __tablename__ = "scenario_status"
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    key: Mapped[str] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(190), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    expected_driver: Mapped[str] = mapped_column(String(80), default="")
    planted_at: Mapped[str] = mapped_column(String(80), default="")
    detected: Mapped[bool] = mapped_column(Boolean, default=False)
    diagnosed: Mapped[bool] = mapped_column(Boolean, default=False)
    recommended: Mapped[bool] = mapped_column(Boolean, default=False)
    anomaly_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diagnosis_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detection_note: Mapped[str] = mapped_column(Text, default="")
    metric: Mapped[str] = mapped_column(String(60), default="")
    entity: Mapped[str] = mapped_column(String(190), default="")
    platform: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detection: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("brand_id", "key"),)
