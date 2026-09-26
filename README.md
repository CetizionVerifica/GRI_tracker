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

## Authentication and first admin

Log in with `POST /api/v1/auth/login` (email and password). It returns an opaque bearer token;
only its SHA-256 digest is stored, and `POST /api/v1/auth/logout` revokes it. Create the first
platform admin from the command line (it prompts for the password):

```bash
cd backend
uv run python -m app.modules.tenancy.bootstrap --email you@example.com --name "Your Name"
```

Organization data lives under `/api/v1/organizations/{organization_id}/...`. Non-members get
404, so other organizations' existence is never revealed. Platform admins can reach every
organization; only they create organizations and users, and only they grant or revoke `auditor`.

## GRI catalog

Standards, disclosures, metric definitions and dimensions are data, seeded from
`backend/catalog/*.yaml` (see `backend/tests/catalog/fixtures/` for the format). Seeding runs as
the schema owner, validates every file first (units via pint, decimals as strings, no floats) and
applies everything in one transaction:

```bash
cd backend
uv run python -m app.modules.catalog.seed
```

Re-running is safe. Names, titles and requirements can be updated. A metric's type, unit,
validation rules, dimensions or disclosure never change once seeded: retire it (`retired: true`)
and add a new code. Organizations can add their own metrics, dimensions and dimension values
(codes start with `custom.`); they never see each other's. Extra pint unit definitions go in
`backend/catalog/units.txt`.

## Audit log

Every change made through the API is recorded in `audit_log`, in the same transaction as the
change. Each organization has its own hash chain, and platform events (new organizations, users,
logins, catalog seeds) form one more. The database assigns each entry's sequence number,
timestamp and SHA-256 hash, and rejects UPDATE, DELETE and TRUNCATE, even for the owner.
Personal data (email, phone, display name) is recorded by field name only, never by value.

- `GET /api/v1/organizations/{id}/audit-log` and `.../audit-log/verify`: org admins, auditors and
  platform admins.
- `GET /api/v1/audit-log` and `/api/v1/audit-log/verify`: the platform chain, platform admins only.

Verification recomputes the chain and reports the first entry that doesn't match.

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
