"""Uniform API error envelope + audit-log writer."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session

from ..db.models import AuditLog
from .security import audit_hash


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_body(code: str, message: str, details: Any = None, request_id: str | None = None) -> dict:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    if request_id:
        body["error"]["request_id"] = request_id
    return body


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message, exc.details))


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(detail.get("code", "error"), detail.get("message", ""), detail.get("details")),
        )
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(f"http_{exc.status_code}", str(detail), request_id=request.headers.get("x-request-id")),
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=error_body("validation_error", "Request payload failed validation", exc.errors()),
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    from .logging import get_logger

    get_logger("adpilot.error").exception("unhandled error", extra={"path": request.url.path})
    return JSONResponse(status_code=500, content=error_body("internal_error", str(exc)))


# --------------------------------------------------------------------------- #
def write_audit(
    db: Session,
    *,
    brand_id: int,
    actor: str,
    actor_role: str,
    action: str,
    entity_type: str = "",
    entity_id: int | None = None,
    before: dict | None = None,
    after: dict | None = None,
    context: dict | None = None,
) -> AuditLog:
    """Append an immutable, hash-chained audit record."""
    prev = (
        db.query(AuditLog)
        .filter(AuditLog.brand_id == brand_id)
        .order_by(AuditLog.id.desc())
        .first()
    )
    prev_hash = prev.immutable_hash if prev else "genesis"
    payload = json.dumps(
        {
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "before": before or {},
            "after": after or {},
        },
        sort_keys=True,
        default=str,
    )
    entry = AuditLog(
        brand_id=brand_id,
        actor=actor,
        actor_role=actor_role,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before or {},
        after=after or {},
        context=context or {},
        immutable_hash=audit_hash(prev_hash, payload),
    )
    db.add(entry)
    db.flush()
    return entry