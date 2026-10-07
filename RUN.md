# Run AdPilot locally

The repository contains the **Next.js dashboard** and integrated **FastAPI API**.

## Recommended: one command

Requirements: Python 3.11+, Node.js 20+, npm and bash.

From the repository root:

```bash
bash scripts/dev.sh
```

Open <http://localhost:3000>. API docs: <http://localhost:8000/docs>; health: <http://localhost:8000/health>. Stop both services with **Ctrl+C**.

The first startup creates `backend/.venv`, installs requirements, seeds a 12-month simulated dataset, and runs the analysis pipeline. **Allow about 2–3 minutes on first launch**; later launches reuse `backend/var/adpilot.db`. The script polls for up to five minutes and prints progress.

Port/API overrides:

```bash
API_PORT=8010 WEB_PORT=3010 bash scripts/dev.sh
NEXT_PUBLIC_API_URL=https://api.example.com/api/v1 bash scripts/dev.sh
```

`NEXT_PUBLIC_API_URL` is the URL the browser can reach, not a private container hostname. The backend currently allows all origins for local development; restrict CORS before deployment.

## Demo accounts

| Role | Email | Password |
|---|---|---|
| Admin | `admin@adpilot.io` | `AdPilot!2026` |
| Brand manager | `brand@adpilot.io` | `AdPilot!2026` |
| Analyst | `analyst@adpilot.io` | `AdPilot!2026` |
| Viewer | `viewer@adpilot.io` | `AdPilot!2026` |

These seeded identities and the default signing secret are local-demo-only.

## Start services separately

Backend terminal:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
ADPILOT_FORCE_SQLITE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Frontend terminal:

```bash
cd frontend
npm ci
# Optional: cp .env.example .env.local and set NEXT_PUBLIC_API_URL
npm run dev
```

Default browser API address: `http://localhost:8000/api/v1`.

## Checks

```bash
make test            # pytest; uses a disposable /tmp/adpilot-pytest.db
make backend-check
make frontend-check  # lint + TypeScript + optimized build
make check
```

The backend test suite includes health, JWT login/current user, access control, overview, optimizer and What-If. The clean-database test seeds and analyzes the full dataset, so it can take about 2–3 minutes.

## Production-build starting point

```bash
# Run the API with production database/secrets and a restricted CORS origin.
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000

# Build and serve the dashboard.
cd ../frontend
npm ci
NEXT_PUBLIC_API_URL=https://your-api.example.com/api/v1 npm run build
NEXT_PUBLIC_API_URL=https://your-api.example.com/api/v1 npm run start
```

Set a high-entropy `SECRET_KEY`, proper database/backups, TLS, restricted CORS, non-demo users and approved connector authorization before public deployment. Live ad-platform pushes, production OAuth, Alembic migrations, Docker and a production security review are not part of this repository. Do not commit `.env` files, credentials, or `backend/var/` data.
