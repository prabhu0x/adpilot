"""AdPilot FastAPI entry point for the integrated local full-stack demo."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.v1.auth import router as auth_router
from .api.v1.data import router as data_router
from .api.v1.overview import router as overview_router
from .api.v1.workspace import router as workspace_router
from .core.config import settings
from .core.errors import (ApiError, error_body, http_error_handler,
                          unhandled_error_handler, validation_error_handler)
from .core.events import bus
from .core.logging import get_logger
from .db.models import Base, Brand, UnifiedFact
from .db.session import SessionLocal, engine
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

log = get_logger("adpilot.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create schema, seed a clean first run, and materialize its analytics."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        brand = db.query(Brand).order_by(Brand.id).first()
        if settings.seed_on_start and brand is None:
            log.info("empty database detected; starting deterministic demo seed")
            from .engines.seed import seed_database

            seeded = seed_database(db, force=False)
            brand = db.query(Brand).filter(Brand.id == seeded.get("brand_id")).first()
        else:
            log.info("existing database detected; leaving it unchanged")
        if brand is not None:
            has_unified_facts = (
                db.query(UnifiedFact.id)
                .filter(UnifiedFact.brand_id == brand.id)
                .first()
                is not None
            )
            if not has_unified_facts:
                log.info("unified facts are missing; materializing the analysis pipeline")
                from .engines.pipeline import run_full_pipeline

                result = run_full_pipeline(db, brand.id, reset=False)
                db.commit()
                log.info("initial analysis pipeline completed in %sms", result.get("duration_ms"))
    except Exception:
        db.rollback()
        log.exception("startup initialization failed")
        raise
    finally:
        db.close()
    try:
        bus.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description=(
        "AdPilot D2C advertising intelligence and decision API for the integrated Next.js dashboard. "
        "All seeded connectors and execution actions are simulated in this development build."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details, getattr(request.state, "request_id", None)),
    )


# Consistent JSON error envelope for framework validation and HTTP errors.
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

app.include_router(auth_router, prefix=settings.api_prefix)
app.include_router(overview_router, prefix=settings.api_prefix)
app.include_router(data_router, prefix=settings.api_prefix)
app.include_router(workspace_router, prefix=settings.api_prefix)


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.version,
            "database": "sqlite" if settings.is_sqlite else "postgresql",
            "dashboard": "Next.js frontend in ../frontend"}


@app.get("/metrics", tags=["system"])
def get_metrics() -> dict:
    from .core.logging import metrics

    return {"prometheus": metrics.render()}


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"app": settings.app_name, "status": "running", "docs": "/docs",
            "api_prefix": settings.api_prefix, "dashboard": "run the frontend on port 3000"}
