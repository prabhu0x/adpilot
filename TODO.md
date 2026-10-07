# AdPilot local full-stack acceptance checklist

This checklist tracks the requested **runnable local repository** deliverable. It does not claim that the aspirational production platform is complete.

## Delivered

- [x] Next.js App Router + TypeScript dashboard using Tailwind, Framer Motion, Recharts, TanStack Query/Table, React Flow, Zustand and Lucide; dark default and working light toggle.
- [x] FastAPI workspace APIs invoke the real SQLAlchemy models and engines for overview/data, diagnosis, opportunity, recommendation lifecycle, optimizer/What-If, execution/audit, learning, simulation, copilot and settings.
- [x] JWT demo login with Admin, Brand Manager, Analyst and Viewer accounts; responsive navigation shell, brand/date/channel UI, notifications, sync, kill-switch state, user menu, command palette and loading/error/empty states.
- [x] Command Center with backend-derived metrics, trends, anomalies, recommendations and lifecycle summary.
- [x] Data Hub with source freshness/quality, upload preview/import, React Flow lineage and reconciliation.
- [x] Explorer with performance tables/charts and export.
- [x] Diagnosis Studio with evidence, ranked drivers, decomposition, stored agent trace, and grounded follow-up questions.
- [x] Opportunity Radar with profit, confidence, headroom, inventory context and model evidence.
- [x] Decision Center with review, approve/reject reason, simulated execute, rollback details, impact and guardrails.
- [x] Budget Optimizer with response curves, allocations, constraints and editable backend-calculated What-If results.
- [x] Inventory & Margin Guard and Creative Intelligence.
- [x] Execution & Audit, Learning Loop/clock, Copilot, Settings and seeded Scenario Guide.
- [x] First-run DB creation/seeding plus analytics materialization when unified facts are absent.
- [x] One-command local launcher (`bash scripts/dev.sh` / `make dev`), `.env.example` files, setup and run guide.
- [x] Backend smoke tests on an isolated SQLite DB; TypeScript check, ESLint and Next production build.
- [x] Clean source archive excludes `.git`, virtual environments, `node_modules`, Next output and generated database data.

## Remaining before any production use

- [ ] Implement/authorize live OAuth connectors and real platform `push_changes` flows; current adapters/actions are simulated.
- [ ] Add production DB migrations (Alembic), deployment manifests/Docker if desired, and backup/restore operations.
- [ ] Replace demo users and default JWT secret; restrict CORS; add rate limiting, security review, monitoring and operational runbooks.
- [ ] Expand test coverage beyond smoke tests, including mutation permission matrices, rollback and full browser E2E.
- [ ] Review analytics/model outputs against real data and validate statistical assumptions/calibration before using them for spend decisions.
