"""Unified Data Hub: sources, sync, uploads, lineage, reconciliation, quality."""
from __future__ import annotations

import datetime as dt
import io
import json
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import ApiError, write_audit
from ...core.events import bus, jobs
from ...db.models import (
    AdAccount,
    Campaign,
    CampaignSkuMap,
    DataSource,
    DataUpload,
    GaEvent,
    InventorySnapshot,
    Order,
    Platform,
    Product,
    ReconciliationReport,
    Sku,
    SkuPriceHistory,
    SyncRun,
    UnifiedFact,
)
from ...db.session import get_db
from ...engines.ingestion import connector_catalog, run_incremental_ingest, suggest_mapping, validate_upload
from ...engines.reconcile import _read
from ..deps import Principal, get_principal, pagination, resolve_brand

router = APIRouter(tags=["data"])


@router.get("/sources")
def sources(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    rows = db.query(DataSource).filter(DataSource.brand_id == brand).order_by(DataSource.id).all()
    platforms = {p.key: p for p in db.query(Platform).all()}
    accounts = db.query(AdAccount).filter(AdAccount.brand_id == brand).all()
    return {
        "items": [{
            "id": s.id, "key": s.key, "name": s.name, "kind": s.kind, "platform_key": s.platform_key,
            "platform_label": platforms[s.platform_key].name if s.platform_key in platforms else None,
            "connector": s.connector, "mode": s.mode, "status": s.status,
            "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None,
            "last_cursor": s.last_cursor, "freshness_hours": s.freshness_hours,
            "completeness_pct": s.completeness_pct, "validity_pct": s.validity_pct,
            "quality_score": s.quality_score, "row_count": s.row_count, "issues": s.issues or [],
            "auth_configured": bool(s.auth_config),
        } for s in rows],
        "accounts": [{
            "id": a.id, "platform_key": a.platform_key, "external_id": a.external_id, "name": a.name,
            "currency": a.currency, "timezone": a.timezone, "status": a.status,
        } for a in accounts],
        "catalog": connector_catalog(),
    }


@router.post("/sources/sync")
def sync(
    source_key: str = Query("all"),
    brand_id: int | None = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    principal.require("analyst")
    brand = resolve_brand(db, principal, brand_id)
    result = run_incremental_ingest(db, brand, source_key=source_key, actor=principal.email)
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role,
                action="sync", entity_type="data_source", context={"source_key": source_key,
                                                                   "rows": result.get("rows_ingested")})
    db.commit()
    return result


@router.get("/sync-runs")
def sync_runs(limit: int = Query(20, ge=1, le=200), brand_id: int | None = Query(None),
              principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    brand = resolve_brand(db, principal, brand_id)
    rows = db.query(SyncRun).filter(SyncRun.brand_id == brand).order_by(SyncRun.id.desc()).limit(limit).all()
    return {"items": [{
        "id": r.id, "source_key": r.source_key, "job_name": r.job_name, "status": r.status, "mode": r.mode,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "progress": r.progress, "rows_ingested": r.rows_ingested, "started_by": r.started_by,
        "log": r.log or [], "error": r.error,
    } for r in rows]}


@router.get("/uploads")
def uploads(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)) -> dict:
    brand = resolve_brand(db, principal, brand_id)
    rows = db.query(DataUpload).filter(DataUpload.brand_id == brand).order_by(DataUpload.id.desc()).limit(50).all()
    return {"items": [{
        "id": r.id, "source_key": r.source_key, "filename": r.filename, "rows": r.rows,
        "valid_rows": r.valid_rows, "invalid_rows": r.invalid_rows, "mapping": r.mapping or {},
        "errors": r.errors or [], "preview": r.preview or [], "status": r.status,
        "uploaded_by": r.uploaded_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


@router.get("/uploads/schema")
def upload_schema(source_key: str = Query("store_orders")) -> dict[str, Any]:
    from ...engines.ingestion import UPLOAD_SCHEMAS

    if source_key not in UPLOAD_SCHEMAS:
        raise ApiError(400, "unknown_schema", f"no upload schema for '{source_key}'")
    return {"source_key": source_key, "fields": UPLOAD_SCHEMAS[source_key]}


@router.post("/uploads/preview")
async def upload_preview(file: UploadFile = File(...), source_key: str = Form("store_orders")) -> dict[str, Any]:
    """Parse an uploaded CSV/JSON and return a preview plus a suggested mapping."""
    raw = await file.read()
    if len(raw) > 8_000_000:
        raise ApiError(413, "file_too_large", "uploads are limited to 8 MB")
    try:
        if file.filename and file.filename.lower().endswith(".json"):
            records = json.loads(raw.decode("utf-8"))
            frame = pd.DataFrame(records if isinstance(records, list) else [records])
        else:
            frame = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise ApiError(400, "unparseable_file", f"could not parse the file: {exc}")
    frame = frame.head(500)
    columns = [str(c) for c in frame.columns]
    mapping = suggest_mapping(columns, source_key)
    validation = validate_upload(frame, source_key)
    return {
        "filename": file.filename, "source_key": source_key, "rows": int(len(frame)),
        "columns": columns, "suggested_mapping": mapping,
        "preview": json.loads(frame.head(25).to_json(orient="records", date_format="iso")),
        "validation": validation,
    }


@router.post("/uploads")
async def upload_commit(
    file: UploadFile = File(...),
    source_key: str = Form("store_orders"),
    mapping: str = Form("{}"),
    brand_id: int | None = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    principal.require("analyst")
    brand = resolve_brand(db, principal, brand_id)
    raw = await file.read()
    try:
        if file.filename and file.filename.lower().endswith(".json"):
            records = json.loads(raw.decode("utf-8"))
            frame = pd.DataFrame(records if isinstance(records, list) else [records])
        else:
            frame = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        raise ApiError(400, "unparseable_file", f"could not parse the file: {exc}")
    try:
        parsed_mapping = json.loads(mapping) if mapping else {}
    except json.JSONDecodeError:
        raise ApiError(400, "bad_mapping", "mapping must be a JSON object")
    if parsed_mapping:
        frame = frame.rename(columns={v: k for k, v in parsed_mapping.items() if v})

    result = ingest_uploaded_frame(db, brand, frame, source_key, file.filename or "upload.csv", principal.email)
    write_audit(db, brand_id=brand, actor=principal.email, actor_role=principal.role,
                action="upload", entity_type="data_upload", after={"source_key": source_key,
                                                                   "filename": file.filename,
                                                                   "valid_rows": result["valid_rows"]})
    db.commit()
    bus.publish("upload", {"source_key": source_key, "valid_rows": result["valid_rows"], "brand_id": brand})
    return result


def ingest_uploaded_frame(db: Session, brand_id: int, frame: pd.DataFrame, source_key: str,
                          filename: str, actor: str) -> dict[str, Any]:
    """Validate, persist and record an uploaded dataset."""
    from ...engines.ingestion import UPLOAD_SCHEMAS

    validation = validate_upload(frame, source_key)
    errors = validation["errors"]
    valid = validation["valid_frame"]
    status = "processed" if not errors else ("partial" if len(valid) else "rejected")

    inserted = 0
    if source_key == "sku_price_history" and len(valid):
        inserted = _insert_price_history(db, brand_id, valid)
    elif source_key == "inventory_snapshots" and len(valid):
        inserted = _insert_inventory(db, brand_id, valid)
    elif source_key == "unified_facts" and len(valid):
        inserted = _insert_unified(db, brand_id, valid)
    elif source_key == "store_orders" and len(valid):
        inserted = _insert_orders(db, brand_id, valid)
    elif source_key == "ga_events" and len(valid):
        inserted = _insert_ga(db, brand_id, valid)

    upload = DataUpload(
        brand_id=brand_id, source_key=source_key, filename=filename, rows=int(len(frame)),
        valid_rows=int(len(valid)), invalid_rows=int(len(frame) - len(valid)),
        mapping=validation["mapping"], errors=errors[:200],
        preview=json.loads(frame.head(15).to_json(orient="records", date_format="iso")),
        status=status, uploaded_by=actor,
    )
    db.add(upload)
    db.flush()
    return {"status": "ok", "upload_id": upload.id, "rows": int(len(frame)), "valid_rows": int(len(valid)),
            "invalid_rows": int(len(frame) - len(valid)), "inserted": inserted, "errors": errors[:50],
            "schema_fields": UPLOAD_SCHEMAS[source_key], "preview": upload.preview}


def _insert_price_history(db: Session, brand_id: int, frame: pd.DataFrame) -> int:
    count = 0
    for row in frame.to_dict("records"):
        sku = db.query(Sku).filter(Sku.brand_id == brand_id, Sku.sku_code == str(row.get("sku_code"))).first()
        if not sku:
            continue
        db.add(SkuPriceHistory(
            brand_id=brand_id, sku_id=sku.id, effective_date=_as_date(row.get("effective_date")) or dt.date.today(),
            price=float(row.get("price") or 0), list_price=float(row.get("list_price") or row.get("price") or 0),
            discount_pct=float(row.get("discount_pct") or 0), promo_name=str(row.get("promo_name") or ""),
            change_reason=str(row.get("change_reason") or "upload"),
        ))
        count += 1
    db.flush()
    return count


def _insert_inventory(db: Session, brand_id: int, frame: pd.DataFrame) -> int:
    count = 0
    for row in frame.to_dict("records"):
        sku = db.query(Sku).filter(Sku.brand_id == brand_id, Sku.sku_code == str(row.get("sku_code"))).first()
        if not sku:
            continue
        date = _as_date(row.get("date")) or dt.date.today()
        on_hand = float(row.get("on_hand") or 0)
        velocity = float(row.get("daily_velocity") or 1.0)
        db.add(InventorySnapshot(
            brand_id=brand_id, sku_id=sku.id, date=date, warehouse=str(row.get("warehouse") or "MAIN"),
            on_hand=int(on_hand), inbound=int(float(row.get("inbound") or 0)),
            committed=0, reorder_point=int(float(row.get("reorder_point") or 0)),
            lead_time_days=int(float(row.get("lead_time_days") or 21)), daily_velocity=velocity,
            days_of_cover=round(on_hand / max(velocity, 0.01), 2), stockout_flag=on_hand <= 0,
        ))
        count += 1
    db.flush()
    return count


def _insert_orders(db: Session, brand_id: int, frame: pd.DataFrame) -> int:
    count = 0
    for row in frame.to_dict("records"):
        date = _as_date(row.get("date") or row.get("ordered_at")) or dt.date.today()
        db.add(Order(
            brand_id=brand_id, order_number=str(row.get("order_number") or f"UP-{date}-{count}"),
            ordered_at=dt.datetime.combine(date, dt.time(12)), date=date,
            channel=str(row.get("channel") or "shopify"), source=str(row.get("source") or ""),
            medium=str(row.get("medium") or ""), campaign_name=str(row.get("campaign") or ""),
            device=str(row.get("device") or "mobile"), geo=str(row.get("geo") or "US-CA"),
            is_new_customer=bool(row.get("is_new_customer", False)),
            gross_revenue=float(row.get("gross_revenue") or 0),
            discount_amount=float(row.get("discount") or 0),
            shipping_revenue=float(row.get("shipping") or 0), tax_amount=float(row.get("tax") or 0),
            refund_amount=float(row.get("refund") or 0), fees=float(row.get("fees") or 0),
            net_revenue=float(row.get("net_revenue") or 0), cogs=float(row.get("cogs") or 0),
            gross_profit=float(row.get("net_revenue") or 0) - float(row.get("cogs") or 0),
            status="completed",
        ))
        count += 1
    db.flush()
    return count


def _insert_ga(db: Session, brand_id: int, frame: pd.DataFrame) -> int:
    count = 0
    for row in frame.to_dict("records"):
        db.add(GaEvent(
            brand_id=brand_id, date=_as_date(row.get("date")) or dt.date.today(),
            platform_key=str(row.get("platform_key") or row.get("utm_source") or "unknown"),
            utm_source=str(row.get("utm_source") or ""), utm_medium=str(row.get("utm_medium") or ""),
            utm_campaign=str(row.get("utm_campaign") or ""), device=str(row.get("device") or "mobile"),
            geo=str(row.get("geo") or "US-CA"), sessions=int(float(row.get("sessions") or 0)),
            engaged_sessions=int(float(row.get("engaged_sessions") or 0)),
            add_to_cart=int(float(row.get("add_to_cart") or 0)),
            begin_checkout=int(float(row.get("begin_checkout") or 0)),
            purchase=int(float(row.get("purchase") or 0)),
            purchase_revenue=float(row.get("purchase_revenue") or 0),
            bounces=int(float(row.get("bounces") or 0)),
        ))
        count += 1
    db.flush()
    return count


def _insert_unified(db: Session, brand_id: int, frame: pd.DataFrame) -> int:
    count = 0
    for row in frame.to_dict("records"):
        campaign = db.query(Campaign).filter(Campaign.brand_id == brand_id,
                                             Campaign.name == str(row.get("campaign_name") or "")).first()
        if not campaign:
            continue
        from ...db.models import AdSet

        ad_set = db.query(AdSet).filter(AdSet.campaign_id == campaign.id).first()
        if not ad_set:
            continue
        db.add(UnifiedFact(
            brand_id=brand_id, date=_as_date(row.get("date")) or dt.date.today(),
            platform_key=campaign.platform_key, campaign_id=campaign.id, ad_set_id=ad_set.id,
            impressions=int(float(row.get("impressions") or 0)), clicks=int(float(row.get("clicks") or 0)),
            spend=float(row.get("spend") or 0), attributed_revenue=float(row.get("attributed_revenue") or 0),
            attribution_method="uploaded",
        ))
        count += 1
    db.flush()
    return count


def _as_date(value: Any) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return pd.to_datetime(value).date()
    except Exception:
        return None


@router.get("/lineage")
def lineage(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    """React Flow-ready graph: sources -> raw tables -> unified facts -> models -> decisions."""
    brand = resolve_brand(db, principal, brand_id)
    sources = db.query(DataSource).filter(DataSource.brand_id == brand).all()
    counts = {
        "unified_facts": db.query(UnifiedFact).filter(UnifiedFact.brand_id == brand).count(),
        "orders": db.query(Order).filter(Order.brand_id == brand).count(),
        "ga_events": db.query(GaEvent).filter(GaEvent.brand_id == brand).count(),
        "inventory_snapshots": db.query(InventorySnapshot).filter(InventorySnapshot.brand_id == brand).count(),
        "sku_price_history": db.query(SkuPriceHistory).filter(SkuPriceHistory.brand_id == brand).count(),
    }
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    raw_tables = {
        "meta_ads": "ad_metrics_daily", "google_ads": "ad_metrics_daily", "amazon_ads": "ad_metrics_daily",
        "tiktok_ads": "ad_metrics_daily", "programmatic_dsp": "ad_metrics_daily",
        "store_orders": "orders", "ga4_events": "ga_events", "erp_inventory": "inventory_snapshots",
        "creative_library": "creatives", "sku_margins": "sku_price_history",
    }
    for index, source in enumerate(sources):
        nodes.append({
            "id": f"source:{source.key}", "type": "source",
            "position": {"x": 0, "y": index * 110},
            "data": {"label": source.name, "kind": source.kind, "mode": source.mode,
                     "quality": source.quality_score, "status": source.status,
                     "freshness_hours": source.freshness_hours, "rows": source.row_count},
        })
        table = raw_tables.get(source.key, "ad_metrics_daily")
        edges.append({"id": f"e-{source.key}-{table}", "source": f"source:{source.key}",
                      "target": f"table:{table}", "animated": False})
    table_labels = {"ad_metrics_daily": "Ad metrics (daily + hourly)", "orders": "Store orders",
                    "ga_events": "GA4 events", "inventory_snapshots": "ERP inventory",
                    "creatives": "Creative library", "sku_price_history": "SKU price & margin"}
    for index, (table, label) in enumerate(table_labels.items()):
        nodes.append({
            "id": f"table:{table}", "type": "table",
            "position": {"x": 300, "y": index * 110},
            "data": {"label": label, "rows": counts.get(table, 0)},
        })
        edges.append({"id": f"e-{table}-facts", "source": f"table:{table}", "target": "table:unified_facts"})
    nodes.append({
        "id": "table:unified_facts", "type": "table",
        "position": {"x": 620, "y": 240},
        "data": {"label": "Unified facts (reconciled)", "rows": counts["unified_facts"]},
    })
    stages = [("reconcile", "Reconciliation", 0), ("anomalies", "Anomaly ensemble", 1),
              ("diagnose", "Root cause & causal", 2), ("optimizer", "Decision engine", 3),
              ("execution", "Execution & learning", 4)]
    for key, label, index in stages:
        nodes.append({
            "id": f"stage:{key}", "type": "model",
            "position": {"x": 920, "y": index * 110},
            "data": {"label": label},
        })
    edges.append({"id": "e-facts-reconcile", "source": "table:unified_facts", "target": "stage:reconcile"})
    for previous, current in zip([s[0] for s in stages], [s[0] for s in stages][1:]):
        edges.append({"id": f"e-{previous}-{current}", "source": f"stage:{previous}", "target": f"stage:{current}"})
    return {"nodes": nodes, "edges": edges, "tables": counts}


@router.get("/reconciliation")
def reconciliation(
    platform_key: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    brand_id: int | None = Query(None),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    clock = db.query(func.max(ReconciliationReport.date)).filter(
        ReconciliationReport.brand_id == brand).scalar()
    if not clock:
        return {"items": [], "totals": {}, "window": {}}
    start = clock - dt.timedelta(days=days)
    query = db.query(ReconciliationReport).filter(
        ReconciliationReport.brand_id == brand, ReconciliationReport.date > start)
    if platform_key:
        query = query.filter(ReconciliationReport.platform_key == platform_key)
    rows = query.order_by(ReconciliationReport.date.desc()).limit(2000).all()

    platforms = sorted({r.platform_key for r in rows})
    by_platform = []
    for key in platforms:
        subset = [r for r in rows if r.platform_key == key]
        reported = sum(r.platform_reported_revenue for r in subset)
        attributed = sum(r.attributed_revenue for r in subset)
        store = sum(r.store_revenue for r in subset)
        ga = sum(r.ga_revenue for r in subset)
        by_platform.append({
            "platform_key": key,
            "platform_reported_revenue": round(reported, 2),
            "store_revenue": round(store, 2),
            "ga_revenue": round(ga, 2),
            "attributed_revenue": round(attributed, 2),
            "overstatement_pct": round((reported / attributed - 1) * 100, 2) if attributed else 0.0,
            "overlap_factor": round(sum(r.overlap_factor for r in subset) / len(subset), 4) if subset else 1.0,
            "resolution_method": subset[0].resolution_method if subset else "",
        })
    daily = {}
    for row in rows:
        bucket = daily.setdefault(str(row.date), {"date": str(row.date), "reported": 0.0, "attributed": 0.0, "store": 0.0})
        bucket["reported"] += row.platform_reported_revenue
        bucket["attributed"] += row.attributed_revenue
        bucket["store"] += row.store_revenue
    series = sorted(daily.values(), key=lambda d: d["date"])
    for item in series:
        item["reported"] = round(item["reported"], 2)
        item["attributed"] = round(item["attributed"], 2)
        item["store"] = round(item["store"], 2)

    totals_reported = sum(r.platform_reported_revenue for r in rows)
    totals_attributed = sum(r.attributed_revenue for r in rows)
    return {
        "window": {"start": str(start), "end": str(clock), "days": days},
        "by_platform": by_platform,
        "series": series,
        "items": [{
            "id": r.id, "date": str(r.date), "platform_key": r.platform_key, "campaign_id": r.campaign_id,
            "platform_reported_revenue": round(r.platform_reported_revenue, 2),
            "store_revenue": round(r.store_revenue, 2), "ga_revenue": round(r.ga_revenue, 2),
            "attributed_revenue": round(r.attributed_revenue, 2),
            "discrepancy_pct": round(r.discrepancy_pct, 2), "overlap_factor": r.overlap_factor,
            "resolution_method": r.resolution_method, "notes": r.notes,
        } for r in rows[:400]],
        "totals": {
            "platform_reported_revenue": round(totals_reported, 2),
            "attributed_revenue": round(totals_attributed, 2),
            "overstatement_pct": round((totals_reported / totals_attributed - 1) * 100, 2) if totals_attributed else 0.0,
            "rows": len(rows),
        },
    }


@router.get("/quality")
def quality(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
            db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    rows = db.query(DataSource).filter(DataSource.brand_id == brand).all()
    issues = []
    for source in rows:
        for issue in source.issues or []:
            issues.append({"source_key": source.key, "source_name": source.name, **issue})
    return {
        "sources": [{
            "key": s.key, "name": s.name, "kind": s.kind, "mode": s.mode,
            "freshness_hours": s.freshness_hours, "completeness_pct": s.completeness_pct,
            "validity_pct": s.validity_pct, "quality_score": s.quality_score,
            "status": s.status, "row_count": s.row_count,
        } for s in rows],
        "issues": issues,
        "summary": {
            "mean_quality": round(sum(s.quality_score or 0 for s in rows) / len(rows), 1) if rows else 0,
            "stale": sum(1 for s in rows if (s.freshness_hours or 0) > 48),
            "degraded": sum(1 for s in rows if s.status != "healthy"),
            "issues": len(issues),
        },
    }


@router.get("/campaigns")
def campaigns(
    platform_key: str | None = Query(None),
    status: str | None = Query(None),
    search: str | None = Query(None),
    brand_id: int | None = Query(None),
    page=Depends(pagination),
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    query = db.query(Campaign).filter(Campaign.brand_id == brand)
    if platform_key:
        query = query.filter(Campaign.platform_key == platform_key)
    if status:
        query = query.filter(Campaign.status == status)
    if search:
        query = query.filter(Campaign.name.ilike(f"%{search}%"))
    total = query.count()
    rows = query.order_by(Campaign.id).offset(page.offset).limit(page.limit).all()
    return {"items": [{
        "id": c.id, "name": c.name, "platform_key": c.platform_key, "external_id": c.external_id,
        "objective": c.objective, "status": c.status, "daily_budget": c.daily_budget,
        "bid_strategy": c.bid_strategy, "target_roas": c.target_roas, "target_cpa": c.target_cpa,
        "funnel_stage": c.funnel_stage, "is_branded": c.is_branded, "learning_phase": c.learning_phase,
        "tags": c.tags or [], "launched_at": str(c.launched_at) if c.launched_at else None,
    } for c in rows], "total": total, "limit": page.limit, "offset": page.offset,
        "has_more": page.offset + page.limit < total}


@router.get("/sku-catalogue")
def sku_catalogue(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    brand = resolve_brand(db, principal, brand_id)
    products = {p.id: p for p in db.query(Product).filter(Product.brand_id == brand).all()}
    oos = db.query(InventorySnapshot).join(Sku, Sku.id == InventorySnapshot.sku_id).filter(
        InventorySnapshot.brand_id == brand).order_by(InventorySnapshot.date.desc()).limit(4000).all()
    latest: dict[int, InventorySnapshot] = {}
    for snap in oos:
        latest.setdefault(snap.sku_id, snap)
    return {"items": [{
        "id": s.id, "sku_code": s.sku_code, "name": s.name, "size": s.size, "price": s.price, "cost": s.cost,
        "margin_pct": s.margin_pct, "lifecycle": s.lifecycle, "is_active": s.is_active,
        "product": products[s.product_id].name if s.product_id in products else None,
        "category": products[s.product_id].category if s.product_id in products else None,
        "days_of_cover": latest[s.id].days_of_cover if s.id in latest else None,
        "on_hand": latest[s.id].on_hand if s.id in latest else None,
        "daily_velocity": latest[s.id].daily_velocity if s.id in latest else None,
    } for s in db.query(Sku).filter(Sku.brand_id == brand).order_by(Sku.id).all()]}


@router.get("/campaign-sku-map")
def campaign_sku_map(brand_id: int | None = Query(None), principal: Principal = Depends(get_principal),
                     db: Session = Depends(get_db)) -> dict:
    brand = resolve_brand(db, principal, brand_id)
    rows = db.query(CampaignSkuMap).filter(CampaignSkuMap.brand_id == brand).all()
    skus = {s.id: s.sku_code for s in db.query(Sku).filter(Sku.brand_id == brand).all()}
    campaigns = {c.id: c.name for c in db.query(Campaign).filter(Campaign.brand_id == brand).all()}
    return {"items": [{
        "campaign_id": m.campaign_id, "campaign": campaigns.get(m.campaign_id),
        "ad_set_id": m.ad_set_id, "sku_id": m.sku_id, "sku_code": skus.get(m.sku_id),
        "weight": m.weight, "is_primary": m.is_primary,
    } for m in rows]}