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
docker compose up -d --wait     # from the repo root; Postgres on :55432, Redis :6379, MinIO :9000
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload   # docs at http://localhost:8000/docs
```

The app connects as `gri_api`, a member of the `gri_app` role, which is subject to row-level
security. Migrations run as the owner (`MIGRATION_DATABASE_URL`). `gri_api` is created by
`infra/postgres/initdb/` only when the Postgres volume is first created. For a volume that
already exists, run `docker compose down -v` (this deletes local data) or create the role by hand.

Set `POSTGRES_PORT`, `REDIS_PORT`, `MINIO_PORT` or `MINIO_CONSOLE_PORT` before `docker compose up` to
change host ports (update the matching URL in `.env`). The `minio-init` service creates the bucket.

## Health and observability

- `GET /health/live`: liveness; 200 whenever the process is serving. Never checks dependencies.
- `GET /health/ready`: readiness; checks PostgreSQL, Redis and the S3 bucket, 503 if any fail.
- Logs are JSON lines on stdout (`LOG_JSON=false` for console output). Every request gets an
  `X-Request-ID` (an incoming safe value is reused), included in logs and error responses.
- Errors are RFC 9457 `application/problem+json`.

## Checks

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Integration tests run against real PostgreSQL, Redis and MinIO started by testcontainers, so Docker
must be running. Run `uv run pytest -m "not integration"` to skip them.
