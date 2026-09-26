# CLAUDE.md — GRI KPI Platform (Backend)

## Project
Multi-tenant backend for collecting GRI KPI data, approving it, calculating emissions and rollups,
and generating a GRI sustainability report. Data is audited externally: correctness and traceability
come first.

## Core rule
GRI Standards, disclosures, metrics and units are **data** (seeded from `backend/catalog/*.yaml`),
never hard-coded in logic.

## Stack
Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, PostgreSQL, Redis + Celery, S3/MinIO,
pint, Decimal. Tooling: uv, ruff, mypy --strict, pytest + testcontainers.

## Structure (modular monolith)
`app/core` (config, db, auth, errors) · `app/modules/{tenancy, catalog, collection, workflow,
audit, calculation, reporting, assistant}`, each with `models, schemas, repository, service, router`.
- Modules talk only via `service.py`. Routers stay thin: no logic, no queries.

## Non-negotiables
- Every tenant table has `organization_id`; every query filters by it (plus Postgres RLS).
- Data points are versioned, never overwritten. Audit log is append-only.
- Locked periods are read-only.
- Use `Decimal` for all quantities, never `float`. Every value carries a unit. Round only in reports.
- Calculated values store their inputs, emission factor versions and formula ID.
- Workflow: draft → submitted → approved (or returned). Users can't approve their own data.
- AI output uses approved data only, invents no numbers, and needs human approval.

## Testing
Real Postgres in tests. Each endpoint needs tests for: happy path, bad input, wrong role, and cross-tenant access (expect 404).
Golden tests for every calculation; never edit expected values to pass.

## Commands
```bash
docker compose up -d && uv sync && uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run ruff check . && uv run mypy app && uv run pytest
```

## How to work
1. Plan before multi-file changes; wait for approval.
2. One module per task, with tests in the same change.
3. Run lint, types and tests before saying "done"; report failures honestly.
4. Ask before assuming anything about GRI rules, formulas, emission factors, permissions or personal data.
5. No new dependencies without asking.

## Build order
core → tenancy/auth → catalog → audit → collection + workflow → calculation → reporting → assistant
