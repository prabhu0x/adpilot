# AdPilot — implementation plan and delivered architecture

## Goal and boundary

Deliver a locally runnable full-stack monorepo with a responsive Next.js App Router dashboard integrated with the existing FastAPI/SQLAlchemy analysis backend. Business metrics and actions are API/database-backed. Missing real ad-platform integrations remain simulated; this is a local development prototype, not a hosted permanent site or production service.

## Architecture

- `frontend/`: Next.js 16.4, React 19, TypeScript, Tailwind CSS, Framer Motion, Recharts, TanStack Query/Table, React Flow, Zustand and Lucide.
- `backend/`: Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2, SQLite by default, JWT authentication and SSE events.
- `backend/app/api/v1/`: auth, overview/data and integrated workspace API routes for diagnosis, opportunities, recommendations, optimizer/What-If, inventory, creatives, execution/audit, learning, simulation, copilot and settings.
- `backend/app/engines/`: deterministic seed, reconciliation, anomaly/diagnosis, causal/decomposition, opportunity/response-curve, optimizer, recommendation, execution, outcome/learning, agent/copilot and simulation engines.
- First-run lifespan: create schema, seed deterministic demo inputs, then run the analysis pipeline if unified facts are absent.
- `backend/var/`: runtime SQLite database, intentionally Git-ignored.
- `backend/tests/`: isolated API smoke tests use `/tmp/adpilot-pytest.db`.

## Visual and interaction direction

- **Movement:** editorial instrument panel—compact analytical controls with premium D2C restraint.
- **Principles:** evidence first; clear action state; progressive disclosure; honest uncertainty and simulator boundaries.
- **Palette:** midnight charcoal/navy surfaces; acid-lime `#C8F169` for favorable movement and primary actions; amber/coral for caution and risk. Light theme is also available.
- **Layout:** fixed collapsible navigation rail + contextual main column; responsive mobile drawer; charts and dense tables reflow on small screens.
- **Signature:** CSS orbital AdPilot mark, small uppercase section labels, fine chart grids and quiet state chips.
- **Type:** Manrope/DM Sans for UI and DM Mono for technical labels.
- **Interactions:** visible focus, Cmd/Ctrl+K command palette, notifications, guarded API mutations, toasts, loading/error/empty states, SSE invalidation and restrained transitions.

## Delivered screen map

Command Center; Unified Data Hub; Performance Explorer; Anomaly & Diagnosis Studio; Opportunity Radar; Decision Center; Budget Optimizer/What-If; Inventory & Margin Guard; Creative Intelligence; Execution & Audit; Learning Loop; AI Copilot; Settings; Scenario Guide.

## Implementation sequence

1. Reconcile API contracts and add missing workspace routes over the existing models/engines.
2. Build the authenticated app shell, login, reusable components, filters, theme, and client API/query layer.
3. Implement each workspace page using API-backed records, charts, tables and mutations.
4. Run first-run bootstrap and add isolated backend tests; validate TypeScript, ESLint, production build and local startup.
5. Document demo and production boundaries; create a clean source archive without generated data, build outputs or dependency folders.

## Explicit boundaries

There is no live ad-account OAuth, real third-party push execution, production-grade database migration history, Docker deployment, hosted permanent website, or full production security review. The default SQLite data, demo credentials and signing secret are for local development only.
