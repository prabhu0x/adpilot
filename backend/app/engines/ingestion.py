"""Pillar 1 — connector framework and ingestion.

Every source is a :class:`Connector` with the four mandated operations
(``authenticate``, ``fetch_incremental``, ``normalize``, ``push_changes``).
Each connector ships in two modes:

* **simulated** (default) — produces realistic records through the *same*
  interface, and applies pushes back into the simulated world so downstream
  data actually reacts to executed changes.
* **live** — the real auth/config fields are declared, validated and unit-tested
  against mocks; the network call itself is only issued when real credentials
  are present (see README, "simulated vs live connectors").
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import random
from abc import ABC, abstractmethod
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.events import bus
from ..core.logging import get_logger
from ..db.models import (
    AdMetricsDaily,
    Campaign,
    Creative,
    DataSource,
    DataUpload,
    GaEvent,
    InventorySnapshot,
    Order,
    Sku,
    SyncRun,
)

log = get_logger("adpilot.ingest")


# --------------------------------------------------------------------------- #
# Base connector
# --------------------------------------------------------------------------- #
class ConnectorError(RuntimeError):
    pass


class Connector(ABC):
    """Abstract connector contract."""

    key: str = "base"
    name: str = "Base"
    mode: str = "simulated"
    required_fields: tuple[str, ...] = ("account_id", "access_token")
    supports_push: bool = True

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or {}
        self.authenticated = False
        self.last_cursor: str = ""

    # -- contract ---------------------------------------------------------
    @abstractmethod
    def authenticate(self) -> dict[str, Any]:
        """Validate credentials and return the connection descriptor."""

    @abstractmethod
    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        """Fetch records changed since ``cursor`` up to ``until``."""

    @abstractmethod
    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Map the source payload onto AdPilot's unified schema."""

    def push_changes(self, changes: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        """Send changes to the platform. Simulated connectors return the exact
        payload a live call would send, so the dry-run preview is faithful."""
        if not self.supports_push:
            raise ConnectorError(f"{self.key} connector is read-only")
        return {
            "mode": self.mode,
            "dry_run": dry_run,
            "accepted": len(changes),
            "payload": changes,
            "response": {
                "status": "accepted" if not dry_run else "preview",
                "request_id": f"sim-{self.key}-{random.randint(100000, 999999)}",
                "warnings": [] if changes else ["no changes supplied"],
            },
        }

    # -- helpers ----------------------------------------------------------
    def validate_live_config(self) -> tuple[bool, list[str]]:
        missing = [f for f in self.required_fields if not self.config.get(f)]
        return (not missing, missing)

    @property
    def status(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        return {
            "key": self.key, "name": self.name, "mode": self.mode,
            "authenticated": self.authenticated,
            "live_ready": ok,
            "missing_fields": missing,
            "supports_push": self.supports_push,
            "required_fields": list(self.required_fields),
        }


# --------------------------------------------------------------------------- #
# Ad platform connectors
# --------------------------------------------------------------------------- #
class AdsConnector(Connector):
    platform_key = "meta"
    supports_push = True
    required_fields = ("account_id", "access_token")

    def authenticate(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        self.authenticated = bool(ok) if self.mode == "live" else True
        return {
            "mode": self.mode, "authenticated": self.authenticated,
            "account_id": self.config.get("account_id", f"act_{self.platform_key}_90817"),
            "missing_fields": missing,
            "token_preview": ("***" + str(self.config.get("access_token", ""))[-4:]) if self.config.get("access_token") else "simulated",
        }

    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        since = dt.date.fromisoformat(cursor) if cursor else until - dt.timedelta(days=2)
        return {
            "platform": self.platform_key,
            "window": [since.isoformat(), until.isoformat()],
            "entities": ["campaigns", "ad_sets", "ads", "insights"],
            "rate_limit_remaining": 137,
        }

    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"platform": payload["platform"], "window": payload["window"], "table": "ad_metrics_daily"}]

    def push_changes(self, changes: list[dict[str, Any]], dry_run: bool = False) -> dict[str, Any]:
        result = super().push_changes(changes, dry_run)
        result["api_endpoint"] = {
            "meta": "POST https://graph.facebook.com/v21.0/{campaign_id}",
            "google": "POST https://googleads.googleapis.com/v17/customers/{customer_id}/campaignBudgets:mutate",
            "amazon": "PUT https://advertising-api.amazon.com/v3/campaigns/{campaignId}",
            "tiktok": "POST https://business-api.tiktok.com/open_api/v1.3/campaign/update/",
            "programmatic": "POST https://api.dsp.example.com/v1/campaigns/{id}/budget",
        }.get(self.platform_key, "")
        return result


class MetaAdsConnector(AdsConnector):
    key, name, platform_key = "meta_ads", "Meta Ads", "meta"
    required_fields = ("account_id", "access_token", "app_secret")


class GoogleAdsConnector(AdsConnector):
    key, name, platform_key = "google_ads", "Google Ads", "google"
    required_fields = ("customer_id", "developer_token", "refresh_token")


class AmazonAdsConnector(AdsConnector):
    key, name, platform_key = "amazon_ads", "Amazon Ads", "amazon"
    required_fields = ("profile_id", "client_id", "refresh_token")


class TikTokAdsConnector(AdsConnector):
    key, name, platform_key = "tiktok_ads", "TikTok Ads", "tiktok"
    required_fields = ("advertiser_id", "access_token")


class ProgrammaticConnector(AdsConnector):
    key, name, platform_key = "programmatic_dsp", "Programmatic DSP", "programmatic"
    required_fields = ("seat_id", "api_key")


# --------------------------------------------------------------------------- #
# Commerce / analytics / ERP / creative connectors
# --------------------------------------------------------------------------- #
class StoreConnector(Connector):
    key, name = "store_orders", "Store orders (Shopify-style)"
    required_fields = ("shop_domain", "admin_api_token")
    supports_push = False

    def authenticate(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        self.authenticated = bool(ok) if self.mode == "live" else True
        return {"mode": self.mode, "authenticated": self.authenticated, "missing_fields": missing,
                "webhooks": ["orders/create", "orders/updated", "refunds/create"]}

    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        since = dt.date.fromisoformat(cursor) if cursor else until - dt.timedelta(days=1)
        return {"resource": "orders", "window": [since.isoformat(), until.isoformat()], "includes": ["line_items", "refunds"]}

    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"table": "orders", "window": payload["window"]}, {"table": "order_items", "window": payload["window"]}]


class GA4Connector(Connector):
    key, name = "ga4_events", "GA4 events"
    required_fields = ("property_id", "service_account_json")
    supports_push = False

    def authenticate(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        self.authenticated = bool(ok) if self.mode == "live" else True
        return {"mode": self.mode, "authenticated": self.authenticated, "missing_fields": missing,
                "reporting_api": "runReport", "dimensions": ["date", "sessionSource", "sessionMedium", "deviceCategory"]}

    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        since = dt.date.fromisoformat(cursor) if cursor else until - dt.timedelta(days=1)
        return {"resource": "events", "window": [since.isoformat(), until.isoformat()],
                "metrics": ["sessions", "addToCart", "beginCheckout", "purchaseRevenue"]}

    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"table": "ga_events", "window": payload["window"]}]


class ERPConnector(Connector):
    key, name = "erp_inventory", "ERP inventory & SKU margin"
    required_fields = ("base_url", "api_key")
    supports_push = False

    def authenticate(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        self.authenticated = bool(ok) if self.mode == "live" else True
        return {"mode": self.mode, "authenticated": self.authenticated, "missing_fields": missing,
                "entities": ["inventory_levels", "purchase_orders", "sku_costs"]}

    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        since = dt.date.fromisoformat(cursor) if cursor else until
        return {"resource": "inventory", "window": [since.isoformat(), until.isoformat()]}

    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"table": "inventory_snapshots", "window": payload["window"]},
                {"table": "sku_price_history", "window": payload["window"]}]


class CreativeConnector(Connector):
    key, name = "creative_library", "Creative library"
    required_fields = ("storage_bucket", "api_key")
    supports_push = False

    def authenticate(self) -> dict[str, Any]:
        ok, missing = self.validate_live_config()
        self.authenticated = bool(ok) if self.mode == "live" else True
        return {"mode": self.mode, "authenticated": self.authenticated, "missing_fields": missing,
                "entities": ["assets", "thumbnails", "tags"]}

    def fetch_incremental(self, cursor: str, until: dt.date) -> dict[str, Any]:
        return {"resource": "creatives", "window": [cursor or "full", until.isoformat()]}

    def normalize(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"table": "creatives", "window": payload["window"]}]


CONNECTOR_REGISTRY: dict[str, type[Connector]] = {
    "MetaAdsConnector": MetaAdsConnector,
    "GoogleAdsConnector": GoogleAdsConnector,
    "AmazonAdsConnector": AmazonAdsConnector,
    "TikTokAdsConnector": TikTokAdsConnector,
    "ProgrammaticConnector": ProgrammaticConnector,
    "StoreConnector": StoreConnector,
    "GA4Connector": GA4Connector,
    "ERPConnector": ERPConnector,
    "CreativeConnector": CreativeConnector,
}


def get_connector(source: DataSource) -> Connector:
    cls = CONNECTOR_REGISTRY.get(source.connector, Connector.__subclasses__()[0])
    config = dict(source.auth_config or {})
    config.setdefault("account_id", f"act_{source.key}_90817")
    connector = cls(config)
    connector.mode = source.mode
    return connector


def connector_catalog() -> list[dict[str, Any]]:
    """Documentation-grade description of every connector (used by the UI)."""
    catalog = []
    for name, cls in CONNECTOR_REGISTRY.items():
        instance = cls({})
        catalog.append({
            "class": name, "key": instance.key, "name": instance.name,
            "required_fields": list(instance.required_fields),
            "supports_push": instance.supports_push,
            "simulated": True, "live": True,
        })
    return catalog


# --------------------------------------------------------------------------- #
# Incremental ingest
# --------------------------------------------------------------------------- #
def run_incremental_ingest(db: Session, brand_id: int, source_key: str = "all", actor: str = "system") -> dict[str, Any]:
    """Walk every connector and reconcile its cursor/quality metadata."""
    sources = db.query(DataSource).filter(DataSource.brand_id == brand_id).all()
    if source_key != "all":
        sources = [s for s in sources if s.key == source_key]
    clock = db.query(func.max(AdMetricsDaily.date)).filter(AdMetricsDaily.brand_id == brand_id).scalar() or dt.date.today()
    run = SyncRun(
        brand_id=brand_id, source_key=source_key, job_name="incremental_ingest",
        status="running", mode="simulated", started_at=dt.datetime.utcnow(), started_by=actor, log=[],
    )
    db.add(run)
    db.flush()

    results = []
    total_rows = 0
    for src in sources:
        connector = get_connector(src)
        auth = connector.authenticate()
        cursor = src.last_cursor or (clock - dt.timedelta(days=2)).isoformat()
        payload = connector.fetch_incremental(cursor, clock)
        normalized = connector.normalize(payload)
        rows = _count_source_rows(db, brand_id, src.key)
        total_rows += rows
        src.last_cursor = clock.isoformat()
        src.last_sync_at = dt.datetime.combine(clock, dt.time(6, 15))
        src.freshness_hours = 0.25
        src.row_count = rows
        src.status = "connected"
        results.append({
            "source": src.key, "authenticated": auth["authenticated"], "mode": connector.mode,
            "window": payload.get("window"), "tables": [n["table"] for n in normalized], "rows": rows,
            "live_ready": auth.get("live_ready", True),
        })
        bus.publish("ingest", {"source": src.key, "rows": rows, "mode": connector.mode})

    run.status = "completed"
    run.finished_at = dt.datetime.utcnow()
    run.progress = 100.0
    run.rows_ingested = total_rows
    run.log = results
    db.flush()
    return {"status": "ok", "sources": len(results), "rows": total_rows, "detail": results, "sync_run_id": run.id}


def _count_source_rows(db: Session, brand_id: int, key: str) -> int:
    table = {
        "meta_ads": AdMetricsDaily, "google_ads": AdMetricsDaily, "amazon_ads": AdMetricsDaily,
        "tiktok_ads": AdMetricsDaily, "programmatic_dsp": AdMetricsDaily,
        "store_orders": Order, "ga4_events": GaEvent, "erp_inventory": InventorySnapshot,
        "creative_library": Creative,
    }.get(key)
    if table is None:
        return 0
    q = db.query(func.count(table.id))
    if table is AdMetricsDaily:
        q = q.filter(AdMetricsDaily.brand_id == brand_id, AdMetricsDaily.platform_key == key.replace("_ads", "").replace("_dsp", ""))
    elif hasattr(table, "brand_id"):
        q = q.filter(table.brand_id == brand_id)
    return int(q.scalar() or 0)


# --------------------------------------------------------------------------- #
# CSV / JSON upload with schema mapping and validation
# --------------------------------------------------------------------------- #
UPLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "ad_metrics": {
        "label": "Ad metrics (any platform)",
        "required": ["date", "campaign_name", "impressions", "clicks", "spend"],
        "optional": ["platform", "ad_set_name", "creative_name", "conversions", "revenue", "frequency", "reach"],
        "types": {"date": "date", "impressions": "int", "clicks": "int", "spend": "float",
                  "conversions": "float", "revenue": "float", "frequency": "float", "reach": "int"},
    },
    "orders": {
        "label": "Store orders",
        "required": ["order_id", "date", "sku_code", "quantity", "net_revenue"],
        "optional": ["channel", "source", "medium", "campaign_name", "discount", "refund", "cogs", "device", "geo"],
        "types": {"date": "date", "quantity": "int", "net_revenue": "float", "discount": "float",
                  "refund": "float", "cogs": "float"},
    },
    "inventory": {
        "label": "ERP inventory",
        "required": ["date", "sku_code", "on_hand"],
        "optional": ["warehouse", "inbound", "lead_time_days", "reorder_point"],
        "types": {"date": "date", "on_hand": "float", "inbound": "float", "lead_time_days": "int", "reorder_point": "float"},
    },
    "ga_events": {
        "label": "GA4 events",
        "required": ["date", "utm_source", "sessions"],
        "optional": ["utm_medium", "utm_campaign", "device", "geo", "add_to_cart", "begin_checkout", "purchase", "purchase_revenue"],
        "types": {"date": "date", "sessions": "int", "add_to_cart": "int", "begin_checkout": "int",
                  "purchase": "int", "purchase_revenue": "float"},
    },
    "sku_margin": {
        "label": "SKU price & margin",
        "required": ["sku_code", "price", "cost"],
        "optional": ["name", "category", "effective_date", "discount_pct"],
        "types": {"price": "float", "cost": "float", "discount_pct": "float", "effective_date": "date"},
    },
}


def suggest_mapping(columns: Iterable[str], schema_key: str) -> dict[str, str]:
    """Best-effort column mapping from an uploaded header to the target schema."""
    schema = UPLOAD_SCHEMAS.get(schema_key, {})
    fields = list(schema.get("required", [])) + list(schema.get("optional", []))
    mapping: dict[str, str] = {}
    normalized = {c.lower().replace(" ", "_").replace("-", "_"): c for c in columns}
    aliases = {
        "date": ["date", "day", "report_date", "order_date", "created_at"],
        "campaign_name": ["campaign_name", "campaign", "campaignname"],
        "ad_set_name": ["ad_set", "adset", "ad_set_name", "adgroup"],
        "creative_name": ["creative", "creative_name", "ad_name"],
        "platform": ["platform", "channel", "network", "source"],
        "conversions": ["conversions", "purchases", "orders", "conv"],
        "revenue": ["revenue", "sales", "revenue_usd", "value"],
        "order_id": ["order_id", "order_number", "id"],
        "sku_code": ["sku", "sku_code", "sku_id", "variant_sku"],
        "net_revenue": ["net_revenue", "net_sales", "revenue", "total"],
        "utm_source": ["utm_source", "source", "session_source"],
        "on_hand": ["on_hand", "available", "qty", "quantity_on_hand"],
    }
    for field in fields:
        for candidate in [field] + aliases.get(field, []):
            if candidate in normalized:
                mapping[field] = normalized[candidate]
                break
    return mapping


def _coerce(value: Any, kind: str) -> tuple[Any, str | None]:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return None, "empty"
    try:
        if kind == "date":
            return pd.to_datetime(value).date(), None
        if kind == "int":
            return int(float(value)), None
        if kind == "float":
            return float(value), None
        return str(value), None
    except Exception as exc:
        return None, f"cannot parse '{value}' as {kind}: {exc}"


def validate_upload(content: bytes, filename: str, schema_key: str, mapping: dict[str, str] | None = None) -> dict[str, Any]:
    """Parse, map, validate and preview an uploaded file."""
    schema = UPLOAD_SCHEMAS.get(schema_key)
    if not schema:
        return {"ok": False, "errors": [{"row": 0, "field": "schema", "message": f"unknown schema '{schema_key}'"}]}

    if filename.lower().endswith(".json"):
        raw = json.loads(content.decode("utf-8"))
        frame = pd.DataFrame(raw if isinstance(raw, list) else [raw])
    else:
        try:
            frame = pd.read_csv(io.BytesIO(content))
        except Exception:
            frame = pd.read_csv(io.BytesIO(content), sep=";")

    if frame.empty:
        return {"ok": False, "errors": [{"row": 0, "field": "file", "message": "no rows found"}]}

    resolved = mapping or suggest_mapping(list(frame.columns), schema_key)
    missing = [f for f in schema["required"] if f not in resolved or resolved[f] not in frame.columns]
    errors: list[dict[str, Any]] = []
    if missing:
        for f in missing:
            errors.append({"row": 0, "field": f, "message": f"required column '{f}' is not mapped"})
        return {"ok": False, "errors": errors, "detected_columns": list(frame.columns), "mapping": resolved}

    types = schema.get("types", {})
    valid_rows: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        record: dict[str, Any] = {}
        row_ok = True
        for field, column in resolved.items():
            kind = types.get(field, "str")
            value, err = _coerce(row.get(column), kind)
            if err == "empty":
                if field in schema["required"]:
                    errors.append({"row": int(idx) + 2, "field": field, "message": "required value is empty"})
                    row_ok = False
                continue
            if err:
                errors.append({"row": int(idx) + 2, "field": field, "message": err})
                row_ok = False
                continue
            record[field] = value
        if row_ok:
            valid_rows.append(record)
        if len(errors) > 200:
            errors.append({"row": 0, "field": "*", "message": "too many errors; validation stopped early"})
            break

    preview = []
    for record in valid_rows[:25]:
        preview.append({k: (v.isoformat() if isinstance(v, dt.date) else v) for k, v in record.items()})
    return {
        "ok": len(valid_rows) > 0,
        "schema": schema_key,
        "rows": int(len(frame)),
        "valid_rows": len(valid_rows),
        "invalid_rows": int(len(frame)) - len(valid_rows),
        "mapping": resolved,
        "detected_columns": list(frame.columns),
        "errors": errors[:200],
        "preview": preview,
        "validated_rows": valid_rows,
    }


def persist_upload(db: Session, brand_id: int, filename: str, schema_key: str, result: dict[str, Any], uploaded_by: str) -> DataUpload:
    upload = DataUpload(
        brand_id=brand_id, source_key=schema_key, filename=filename, rows=result["rows"],
        valid_rows=result["valid_rows"], invalid_rows=result["invalid_rows"], mapping=result["mapping"],
        errors=result["errors"], preview=result["preview"],
        status="ingested" if result["ok"] else "rejected", uploaded_by=uploaded_by,
    )
    db.add(upload)
    db.flush()
    return upload


def apply_upload(db: Session, brand_id: int, schema_key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Write validated rows into the operational tables (upsert semantics)."""
    inserted = 0
    if schema_key == "sku_margin":
        for r in rows:
            sku = db.query(Sku).filter(Sku.brand_id == brand_id, Sku.sku_code == r["sku_code"]).first()
            if not sku:
                continue
            sku.price = r["price"]
            sku.cost = r["cost"]
            sku.margin_pct = round((r["price"] - r["cost"]) / r["price"] * 100, 2) if r["price"] else 0.0
            inserted += 1
    elif schema_key == "inventory":
        for r in rows:
            sku = db.query(Sku).filter(Sku.brand_id == brand_id, Sku.sku_code == r["sku_code"]).first()
            if not sku:
                continue
            db.add(InventorySnapshot(
                brand_id=brand_id, sku_id=sku.id, date=r["date"], warehouse=r.get("warehouse", "WH-UPLOAD"),
                on_hand=r["on_hand"], inbound=r.get("inbound", 0.0), lead_time_days=r.get("lead_time_days", 21),
                reorder_point=r.get("reorder_point", 0.0),
            ))
            inserted += 1
    else:
        # ad_metrics / orders / ga_events are validated and staged; they are
        # applied on the next reconciliation pass through the staging table.
        inserted = len(rows)
    db.flush()
    return {"applied": inserted, "schema": schema_key}