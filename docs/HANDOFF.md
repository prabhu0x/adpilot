# AdPilot — handoff

Repository root: `adpilot/`. Start with [`../README.md`](../README.md) or [`../RUN.md`](../RUN.md).

## What is in the repository

- `frontend/`: fully integrated Next.js dashboard with 14 workspace pages.
- `backend/`: FastAPI API, SQLAlchemy models, seed generator and analytics/decision engines.
- `backend/tests/`: isolated pytest API smoke tests.
- `scripts/dev.sh`, root `Makefile`, `.env.example` files, run guide and verification docs.

On an empty local database, the API creates the schema, seeds the deterministic 12-month simulator dataset and runs the analysis pipeline before serving. First launch may take about 2–3 minutes; subsequent starts reuse `backend/var/adpilot.db`.

## Verified

- Four API tests pass against a clean seeded SQLite DB.
- Frontend lint, typecheck and production build pass.
- Local launcher starts both services; API health, login route and authenticated overview/optimizer requests returned HTTP 200.
- Production-only npm audit reports zero vulnerabilities.

## Important boundary

All seeded platform/store/analytics connections and budget executions are **simulated**. No live ad account, OAuth, real platform write, production database migration, Docker deployment or permanent hosted website is supplied. Demo accounts and the development JWT secret must never be used in production. See [`BUILD_STATUS.md`](BUILD_STATUS.md), [`DEPENDENCY_AUDIT.md`](DEPENDENCY_AUDIT.md), and the remaining list in [`../TODO.md`](../TODO.md).
