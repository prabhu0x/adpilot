# AdPilot — verified build status

## Delivered

- Next.js 16.4 / React 19 App Router dashboard integrated with the FastAPI API.
- Fourteen workspace screens, responsive shell, JWT demo login, dark/light themes, command palette, notifications, filters and SSE-driven refresh.
- Workspace APIs for Command Center, Data Hub, performance, diagnosis, opportunities, recommendation workflow, constrained optimizer/What-If, inventory, creatives, execution/audit, learning, simulation, copilot and settings.
- First-run database schema creation and deterministic seed; the full analysis pipeline materializes unified facts when the database has none.
- One-command local launcher (`bash scripts/dev.sh` / `make dev`); backend and frontend examples; root Makefile.
- Four backend pytest API smoke tests with a disposable SQLite database.

## Verification performed

- Backend pytest in the repository's installed runtime: **4 passed** on a clean seeded database; seed + analysis took 129 seconds.
- `python -m compileall -q app tests`: passed.
- Frontend `npm run lint`: passed without warnings.
- Frontend `npm run typecheck`: passed.
- Frontend `npm run build`: passed; Next.js static output includes all 14 screens.
- One-command local launcher: API health and dashboard login route returned HTTP 200.
- Authenticated login, Command Center overview and optimizer allocation returned HTTP 200; optimizer returned 35 campaigns.
- Interactive browser smoke test: demo login reached the authenticated Command Center and rendered API-backed KPI, anomaly and recommendation data.
- `npm audit --omit=dev --audit-level=moderate`: **0 vulnerabilities**. The full audit has development-only Tailwind/ESLint findings; see [`DEPENDENCY_AUDIT.md`](DEPENDENCY_AUDIT.md).

## Scope and limitations

- SQLite plus deterministic simulator data is the default. Existing databases are preserved; a fresh database generates demo inputs and analysis before serving.
- No real ad account is connected, no live platform budget change is sent, and no third-party OAuth secrets are supplied.
- This is not a production-ready service: no Alembic migration history, Docker deployment, complete browser E2E suite, permanent public hosting, or completed security review is included.
- Demo users and the default signing key are development-only. Set secure production credentials, database/backup, TLS and restricted CORS before any public deployment.

## Run

See [`../README.md`](../README.md) and [`../RUN.md`](../RUN.md).
