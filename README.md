# GRI KPI Platform

Multi-tenant platform for collecting GRI KPI data, approving it, calculating emissions and rollups,
and generating a GRI sustainability report. Data is audited externally, so correctness and
traceability come first. Engineering rules live in [CLAUDE.md](CLAUDE.md).

## Repository layout

| Path        | Contents                                                                 |
|-------------|--------------------------------------------------------------------------|
| `backend/`  | FastAPI modular monolith (Python 3.12, uv)                               |
| `frontend/` | Placeholder, not started                                                 |
| `infra/`    | Local dev services: PostgreSQL, Redis, MinIO (`docker-compose.yml`)      |

### Backend structure

```
backend/
├── app/
│   ├── main.py            # create_app() factory
│   ├── core/              # config, db, auth, errors
│   └── modules/
│       └── <module>/      # tenancy, catalog, collection, workflow, audit,
│                          # calculation, reporting, assistant
│           ├── models.py      # ORM models
│           ├── schemas.py     # Pydantic schemas
│           ├── repository.py  # data access (always filtered by organization_id)
│           ├── service.py     # business logic; the only entry point for other modules
│           └── router.py      # thin HTTP layer
├── alembic/               # migrations
├── catalog/               # GRI catalog seed data (*.yaml)
└── tests/
```

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d            # from the repo root; Postgres is exposed on localhost:55432
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload   # http://localhost:8000/health, docs at /docs
```

Set `POSTGRES_PORT` before `docker compose up` to change the host port (update `DATABASE_URL` to match).

## Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Tests run against a real PostgreSQL started by testcontainers, so Docker must be running.
Run `uv run pytest -m "not db"` to skip database tests.
