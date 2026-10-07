"""Central application configuration.

All values can be overridden through environment variables (see `.env.example`).
The application is designed to boot with **zero** external configuration: if no
LLM key is present the deterministic template explanation engine is used, and if
PostgreSQL is not reachable we fall back to SQLite behind the very same ORM.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_DIR = BACKEND_DIR.parent
DATA_DIR = Path(os.getenv("ADPILOT_DATA_DIR", BACKEND_DIR / "var"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _default_database_url() -> str:
    """Prefer PostgreSQL, fall back to SQLite (documented sandbox fallback)."""
    explicit = os.getenv("DATABASE_URL")
    if explicit:
        return explicit
    pg_host = os.getenv("POSTGRES_HOST", "localhost")
    pg_port = os.getenv("POSTGRES_PORT", "5432")
    pg_user = os.getenv("POSTGRES_USER", "adpilot")
    pg_pass = os.getenv("POSTGRES_PASSWORD", "adpilot")
    pg_db = os.getenv("POSTGRES_DB", "adpilot")
    if os.getenv("ADPILOT_FORCE_SQLITE", "").lower() in {"1", "true", "yes"}:
        return f"sqlite+pysqlite:///{DATA_DIR / 'adpilot.db'}"
    if os.getenv("ADPILOT_TRY_POSTGRES", "").lower() in {"1", "true", "yes"}:
        import socket

        try:  # pragma: no cover - environment dependent
            with socket.create_connection((pg_host, int(pg_port)), timeout=0.35):
                pass
            return f"postgresql+psycopg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}"
        except OSError:
            pass
    return f"sqlite+pysqlite:///{DATA_DIR / 'adpilot.db'}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(REPO_DIR / ".env"), extra="ignore")

    app_name: str = "AdPilot"
    api_prefix: str = "/api/v1"
    version: str = "1.0.0"
    environment: str = os.getenv("ENVIRONMENT", "development")

    # --- database -----------------------------------------------------------
    database_url: str = _default_database_url()
    db_echo: bool = False

    # --- auth ---------------------------------------------------------------
    secret_key: str = os.getenv("SECRET_KEY", "adpilot-dev-secret-change-me")
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 60 * 12
    refresh_token_days: int = 14

    # --- LLM ----------------------------------------------------------------
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto")  # auto | openai | anthropic | none
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_base_url: str = os.getenv("LLM_BASE_URL", "")
    llm_model: str = os.getenv("LLM_MODEL", "")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))
    # When the sandbox only exposes an OpenAI-compatible gateway, reuse it.
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_api_base: str = os.getenv("OPENAI_API_BASE", "")

    # --- ingestion / jobs ---------------------------------------------------
    ingest_interval_minutes: int = int(os.getenv("ADPILOT_INGEST_INTERVAL_MINUTES", "15"))
    scheduler_enabled: bool = os.getenv("ADPILOT_SCHEDULER", "1").lower() not in {"0", "false", "no"}
    seed_on_start: bool = os.getenv("ADPILOT_SEED_ON_START", "1").lower() not in {"0", "false", "no"}

    # --- simulation ---------------------------------------------------------
    # The demo clock. "Today" for the seeded dataset; advancing the clock moves
    # simulated time forward so the full closed loop can be observed in minutes.
    sim_start_date: str = os.getenv("ADPILOT_SIM_START", "2025-10-07")
    sim_today: str = os.getenv("ADPILOT_SIM_TODAY", "2026-10-06")
    seed_random_state: int = 42

    # --- guardrails defaults ------------------------------------------------
    default_max_daily_shift_pct: float = 25.0
    default_min_days_of_cover: float = 14.0
    default_min_contribution_margin_pct: float = 18.0
    kill_switch: bool = os.getenv("ADPILOT_KILL_SWITCH", "0").lower() in {"1", "true", "yes"}

    # --- misc ---------------------------------------------------------------
    cors_origins: str = os.getenv("CORS_ORIGINS", "*")
    rate_limit_per_minute: int = int(os.getenv("ADPILOT_RATE_LIMIT", "600"))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()