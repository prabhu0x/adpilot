"""JWT auth, password hashing and server-side RBAC dependencies."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.session import get_db
from .config import settings

pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.api_prefix}/auth/login", auto_error=False)

ROLE_PERMISSIONS: dict[str, dict[str, bool]] = {
    "Admin": {
        "view": True, "approve": True, "execute": True, "configure": True,
        "ingest": True, "manage_users": True, "rollback": True, "simulate": True,
    },
    "Brand Manager": {
        "view": True, "approve": True, "execute": True, "configure": True,
        "ingest": True, "manage_users": False, "rollback": True, "simulate": True,
    },
    "Analyst": {
        "view": True, "approve": False, "execute": False, "configure": False,
        "ingest": True, "manage_users": False, "rollback": False, "simulate": True,
    },
    "Viewer": {
        "view": True, "approve": False, "execute": False, "configure": False,
        "ingest": False, "manage_users": False, "rollback": False, "simulate": False,
    },
}


def hash_password(raw: str) -> str:
    return pwd_context.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(raw, hashed)
    except Exception:  # pragma: no cover - malformed hash
        return False


def _token(payload: dict, expires_delta: dt.timedelta) -> str:
    data = payload.copy()
    data["exp"] = dt.datetime.now(dt.timezone.utc) + expires_delta
    data["iat"] = dt.datetime.now(dt.timezone.utc)
    return jwt.encode(data, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str, role: str, brand_id: int | None) -> str:
    return _token(
        {"sub": subject, "role": role, "brand_id": brand_id, "type": "access"},
        dt.timedelta(minutes=settings.access_token_minutes),
    )


def create_refresh_token(subject: str) -> str:
    return _token({"sub": subject, "type": "refresh"}, dt.timedelta(days=settings.refresh_token_days))


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:  # pragma: no cover - exercised in API tests
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "Could not validate credentials"},
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "not_authenticated", "message": "Authentication required"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail={"code": "wrong_token_type", "message": "Access token required"})
    user = db.query(User).filter(User.email == payload.get("sub")).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail={"code": "user_inactive", "message": "User not found or inactive"})
    return user


def require_permission(permission: str):
    def _dep(user: User = Depends(get_current_user)) -> User:
        perms = ROLE_PERMISSIONS.get(user.role_name, {})
        if not perms.get(permission, False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "forbidden",
                    "message": f"Role '{user.role_name}' lacks permission '{permission}'",
                },
            )
        return user

    return _dep


def audit_hash(prev_hash: str, payload: str) -> str:
    """Append-only chain hash so the audit log is tamper-evident."""
    return hashlib.sha256(f"{prev_hash}:{payload}".encode()).hexdigest()


def idempotency_key(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def new_salt() -> str:
    return secrets.token_hex(8)


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)