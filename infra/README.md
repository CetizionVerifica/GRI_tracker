# infra

Local development services (`docker-compose.yml`): PostgreSQL 16, Redis 7, MinIO (S3-compatible,
community build `pgsty/minio` since upstream no longer publishes images), plus a one-off
`minio-init` that creates the bucket. All data lives in named volumes.

Start from the repo root with `docker compose up -d` (the root `compose.yaml` includes this file).

Default credentials are for local development only.
