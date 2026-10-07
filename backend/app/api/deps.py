"""Shared FastAPI dependencies: auth, brand scoping, pagination, serialisation."""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.errors import ApiError
from ..core.logging import get_logger
from ..core.security import decode_token
from ..db.models import Brand, Role, User
from ..db.session import get_db

log = get_logger("adpilot.api")
bearer = HTTPBearer(auto_error=False)

ROLE_RANK = {"viewer": 0, "analyst": 1, "brand_manager": 2, "admin": 3}


class Principal:
    """The authenticated caller."""

    def __init__(self, user: User, role: str, brand_ids: list[int]) -> None:
        self.user = user
        self.id = user.id
        self.email = user.email
        self.name = user.full_name
        self.role = role
        self.brand_ids = brand_ids

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role, 0)

    def can(self, minimum: str) -> bool:
        return self.rank >= ROLE_RANK.get(minimum, 99)

    def require(self, minimum: str) -> None:
        if not self.can(minimum):
            raise ApiError(403, "forbidden", f"role '{self.role}' cannot perform this action (needs '{minimum}')")


def get_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Principal:
    token = credentials.credentials if credentials else (x_api_key or "")
    if not token:
        raise ApiError(401, "unauthorized", "missing bearer token")
    payload = decode_token(token)
    if not payload or payload.get("type") != "access":
        raise ApiError(401, "unauthorized", "invalid or expired access token")
    user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
    if not user or not user.is_active:
        raise ApiError(401, "unauthorized", "user not found or inactive")
    role = (user.role_name or "Viewer").lower().replace(" ", "_")
    brand_ids = [b.id for b in db.query(Brand).all()]
    request.state.principal = user.email
    return Principal(user, role, brand_ids)


def optional_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal | None:
    """Used by public endpoints (health, docs) that still want audit context."""
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
    except Exception:
        return None
    if not payload:
        return None
    user = db.query(User).filter(User.id == int(payload.get("sub", 0))).first()
    if not user:
        return None
    role = (user.role_name or "Viewer").lower().replace(" ", "_")
    return Principal(user, role, [b.id for b in db.query(Brand).all()])


def resolve_brand(db: Session, principal: Principal, brand_id: int | None) -> int:
    brands = db.query(Brand).order_by(Brand.id).all()
    if not brands:
        raise ApiError(404, "no_brand", "no brand has been seeded yet")
    if brand_id is None:
        return brands[0].id
    if brand_id not in {b.id for b in brands}:
        raise ApiError(404, "brand_not_found", f"brand {brand_id} does not exist")
    return brand_id


class Page:
    """Offset pagination with a hard cap so a dashboard cannot be DOSed."""

    def __init__(self, limit: int = 50, offset: int = 0) -> None:
        self.limit = max(1, min(limit, 500))
        self.offset = max(0, offset)


def pagination(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page:
    return Page(limit, offset)


def paginated(items: Iterable[Any], total: int, page: Page, serializer) -> dict[str, Any]:
    return {
        "items": [serializer(item) for item in items],
        "total": total,
        "limit": page.limit,
        "offset": page.offset,
        "has_more": page.offset + page.limit < total,
    }


def iso(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


def as_dict(obj: Any, fields: Iterable[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {f: iso(getattr(obj, f, None)) for f in fields}
    if extra:
        out.update(extra)
    return out