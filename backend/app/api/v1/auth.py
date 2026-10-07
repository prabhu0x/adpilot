"""Authentication, session and user administration."""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from ...core.errors import ApiError, write_audit
from ...core.security import create_access_token, create_refresh_token, decode_token, hash_password, verify_password
from ...db.models import Brand, Role, User
from ...db.session import get_db
from ..deps import Principal, get_principal

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict


class RefreshRequest(BaseModel):
    refresh_token: str


def _user_payload(db: Session, user: User) -> dict:
    role = db.query(Role).filter(Role.name == user.role_name).first()
    brand = db.query(Brand).filter(Brand.id == user.brand_id).first() if user.brand_id else None
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "role": (user.role_name or "Viewer").lower().replace(" ", "_"),
        "role_label": user.role_name or "Viewer",
        "permissions": (role.permissions if role else {}) or {},
        "brand_id": user.brand_id,
        "brand_name": brand.name if brand else None,
        "avatar_initials": "".join(p[0] for p in (user.full_name or user.email).split()[:2]).upper(),
        "is_active": user.is_active,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise ApiError(401, "invalid_credentials", "email or password is incorrect")
    if not user.is_active:
        raise ApiError(403, "account_disabled", "this account has been disabled")
    role = db.query(Role).filter(Role.name == user.role_name).first()
    role_key = (user.role_name or "Viewer").lower().replace(" ", "_")
    user.last_login_at = dt.datetime.utcnow()
    db.commit()
    if user.brand_id:
        write_audit(db, brand_id=user.brand_id, actor=user.email, actor_role=role_key,
                    action="login", entity_type="user", entity_id=user.id,
                    context={"ip": request.client.host if request.client else "unknown"})
        db.commit()
    from ...core.config import settings

    return TokenResponse(
        access_token=create_access_token(str(user.id), role_key, user.brand_id),
        refresh_token=create_refresh_token(str(user.id)),
        expires_in=settings.access_token_minutes * 60,
        user=_user_payload(db, user),
    )


@router.post("/auth/refresh")
def refresh(payload: RefreshRequest, db: Session = Depends(get_db)) -> dict:
    claims = decode_token(payload.refresh_token)
    if not claims or claims.get("type") != "refresh":
        raise ApiError(401, "invalid_refresh_token", "refresh token is invalid or expired")
    user = db.query(User).filter(User.id == int(claims["sub"])).first()
    if not user or not user.is_active:
        raise ApiError(401, "unauthorized", "user not found or inactive")
    role = db.query(Role).filter(Role.name == user.role_name).first()
    from ...core.config import settings

    return {
        "access_token": create_access_token(str(user.id), (user.role_name or "Viewer").lower().replace(" ", "_"), user.brand_id),
        "expires_in": settings.access_token_minutes * 60,
        "user": _user_payload(db, user),
    }


@router.get("/auth/me")
def me(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    return _user_payload(db, principal.user)


@router.get("/auth/demo-users")
def demo_users(db: Session = Depends(get_db)) -> dict:
    """Seed credentials shown on the login page (demo only)."""
    users = db.query(User).filter(User.is_active.is_(True)).all()
    return {
        "items": [
            {**_user_payload(db, u), "demo_password": "AdPilot!2026"}
            for u in users
        ],
        "note": "Every seeded demo account uses the password 'AdPilot!2026'.",
    }


@router.get("/users")
def list_users(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    principal.require("viewer")
    return {"items": [_user_payload(db, u) for u in db.query(User).order_by(User.id).all()]}


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str
    role: str = "viewer"
    password: str = Field(default="adpilot", min_length=6)
    brand_id: int | None = None


@router.post("/users", status_code=201)
def create_user(payload: UserCreate, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict:
    principal.require("admin")
    if db.query(User).filter(User.email == payload.email.lower()).first():
        raise ApiError(409, "email_taken", "a user with that email already exists")
    role_name = payload.role.replace("_", " ").title()
    role = db.query(Role).filter(Role.name == role_name).first()
    if not role:
        raise ApiError(400, "unknown_role", f"role '{payload.role}' does not exist")
    brands = db.query(Brand).order_by(Brand.id).all()
    user = User(
        email=payload.email.lower(), full_name=payload.full_name, role_name=role.name,
        hashed_password=hash_password(payload.password), is_active=True,
        brand_id=payload.brand_id or (brands[0].id if brands else None),
    )
    db.add(user)
    db.flush()
    write_audit(db, brand_id=user.brand_id or 1, actor=principal.email, actor_role=principal.role,
                action="create_user", entity_type="user", entity_id=user.id,
                after={"email": user.email, "role": role.name})
    db.commit()
    return _user_payload(db, user)


class UserPatch(BaseModel):
    full_name: str | None = None
    role: str | None = None
    is_active: bool | None = None
    password: str | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserPatch, principal: Principal = Depends(get_principal),
                db: Session = Depends(get_db)) -> dict:
    principal.require("admin")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise ApiError(404, "not_found", "user not found")
    before = _user_payload(db, user)
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password:
        user.hashed_password = hash_password(payload.password)
    if payload.role:
        role_name = payload.role.replace("_", " ").title()
        role = db.query(Role).filter(Role.name == role_name).first()
        if not role:
            raise ApiError(400, "unknown_role", f"role '{payload.role}' does not exist")
        user.role_name = role.name
    db.flush()
    write_audit(db, brand_id=user.brand_id or 1, actor=principal.email, actor_role=principal.role,
                action="update_user", entity_type="user", entity_id=user.id,
                before=before, after=_user_payload(db, user))
    db.commit()
    return _user_payload(db, user)


@router.get("/roles")
def list_roles(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    roles = db.query(Role).order_by(Role.id).all()
    return {"items": [{"key": r.name.lower().replace(" ", "_"), "label": r.name,
                       "description": r.description, "permissions": r.permissions or {}} for r in roles]}