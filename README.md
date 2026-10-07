# AdPilot — D2C decision intelligence

AdPilot is a locally runnable full-stack prototype for unifying advertising and commerce data, detecting changes, investigating likely causes, scoring opportunities, reviewing guarded recommendations, and learning from simulated outcomes.

**This repository includes the Next.js dashboard and the FastAPI/SQLAlchemy backend.** The demo uses a seeded SQLite database and simulated connectors/actions. No live ad account is connected and no real budget change is sent.

## Quick start

Requirements: **Python 3.11+**, **Node.js 20+**, npm, and `bash`.

```bash
git clone <your-repository-url> adpilot
cd adpilot
bash scripts/dev.sh
```

The first launch creates `backend/.venv`, installs dependencies, seeds the demo data and materializes the analysis pipeline. Because the demo includes 12 months of data, the **first startup can take about 2–3 minutes**; subsequent starts use the local database and are much faster. The launcher reports progress and allows up to five minutes for first-run initialization.

Open:

- Dashboard: <http://localhost:3000>
- API health: <http://localhost:8000/health>
- Interactive API docs: <http://localhost:8000/docs>

Sign in with the demo account below. Stop both servers with **Ctrl+C**.

```bash
# Optional port overrides
API_PORT=8010 WEB_PORT=3010 bash scripts/dev.sh
```

`NEXT_PUBLIC_API_URL` defaults to `http://localhost:${API_PORT}/api/v1`. Set it to an API prefix reachable by the browser when the frontend and backend use different hosts.

## Demo sign-in

All seeded users use the development-only password `AdPilot!2026`:

| Role | Email |
|---|---|
| Admin | `admin@adpilot.io` |
| Brand manager | `brand@adpilot.io` |
| Analyst | `analyst@adpilot.io` |
| Viewer | `viewer@adpilot.io` |

The login screen lists the demo accounts. **Do not expose these credentials or the default development JWT key on a public deployment.**

## Dashboard

The responsive workspace has 14 screens:

1. **Command Center** — API-backed KPI trends, anomalies, actions and lifecycle status.
2. **Unified Data Hub** — source freshness/quality, CSV/JSON preview and import, reconciliation and React Flow lineage.
3. **Performance Explorer** — performance by period/channel/campaign/SKU with charts and CSV export.
4. **Anomaly & Diagnosis** — anomaly queue, evidence, driver ranking, decomposition, persisted agent trace and grounded follow-up questions.
5. **Opportunity Radar** — scored opportunities with profit, confidence, stock-cover and model evidence.
6. **Decision Center** — recommendation review, approve/reject, simulated execute, and guardrails/rollback details.
7. **Budget Optimizer** — constrained campaign allocation, response curves and editable What-If estimates; apply creates reviewable recommendations.
8. **Inventory & Margin Guard** and **Creative Intelligence**.
9. **Execution & Audit**, **Learning Loop**, **AI Copilot**, **Settings**, and the planted-event **Scenario Guide**.

The shell includes dark/light themes, reporting-period and channel controls, notifications, manual sync, simulator status, responsive navigation and a keyboard command palette. UI data and mutations use the backend APIs; loading, empty and error states are surfaced rather than replaced with client-side sample metrics.

## Useful commands

```bash
make dev             # install if needed, then launch the API and dashboard
make test            # isolated backend API smoke tests (fresh test DB)
make backend-check   # Python bytecode compilation
make frontend-check  # lint, TypeScript check, production build
make check           # backend and frontend checks
```

Tests use a disposable SQLite database at `/tmp/adpilot-pytest.db`; they do not replace the development database. The API tests cover health, JWT login/current user, authorization, overview, optimizer and What-If.

## Run the services separately

Backend terminal:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate       # PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
ADPILOT_FORCE_SQLITE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Frontend terminal:

```bash
cd frontend
npm ci
# Optional: cp .env.example .env.local, then set NEXT_PUBLIC_API_URL
npm run dev
```

By default, the browser calls `http://localhost:8000/api/v1`.

## Build and deploy starting point

```bash
# API: use an appropriate production database and secure server configuration.
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Dashboard: the public API URL is compiled into the browser bundle.
cd ../frontend
npm ci
NEXT_PUBLIC_API_URL=https://your-api.example.com/api/v1 npm run build
NEXT_PUBLIC_API_URL=https://your-api.example.com/api/v1 npm run start
```

Before public exposure, set a unique high-entropy `SECRET_KEY`, restrict CORS, use TLS, configure database backups, replace demo users and arrange real connector authorization. Do not commit `.env` files, browser-visible secrets, access tokens or database files. **This is a local simulator prototype, not a production-ready deployment.** It does not ship a Docker deployment, Alembic migration history, production OAuth, live ad-platform pushes, or a completed security review.

## Data and configuration

- Local SQLite database: `backend/var/adpilot.db` (generated and Git-ignored).
- On an empty database, the app seeds demo data and runs analysis so dashboard routes have materialized facts. Existing databases are preserved; if unified facts are absent, the pipeline is materialized once.
- Backend settings: `backend/.env.example`; browser API base: `frontend/.env.example`.
- `ADPILOT_FORCE_SQLITE=1` forces SQLite; `ADPILOT_SEED_ON_START=0` disables automatic seeding.
- The deterministic copilot fallback works without an LLM key. Optional LLM configuration is server-side only.
- Seeded connectors and all execution actions are simulated; the repository contains adapter scaffolding, not live account authorization.

## Verification snapshot

Verified in this environment:

- Backend: **4 pytest smoke tests passed** against a clean seeded test database.
- Frontend: `npm run lint`, `npm run typecheck`, and `npm run build` passed (Next.js 16.4 / React 19).
- Production dependency audit: `npm audit --omit=dev --audit-level=moderate` reported **0 vulnerabilities**. Some development-only Tailwind/ESLint transitive advisories remain; see [`docs/DEPENDENCY_AUDIT.md`](docs/DEPENDENCY_AUDIT.md).

## Repository layout

```text
adpilot/
├── backend/         FastAPI, SQLAlchemy models, engines, tests and API
├── frontend/        Next.js App Router dashboard
├── scripts/dev.sh   one-command local launcher
├── Makefile         dev/test/check commands
├── docs/            build status, handoff and dependency audit
├── RUN.md           detailed local run guide
└── TODO.md          acceptance checklist and remaining scope
```
