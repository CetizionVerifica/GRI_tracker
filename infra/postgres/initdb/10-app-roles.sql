-- Local development only; runs once, when the postgres-data volume is first created.
-- The app logs in as gri_api, a member of gri_app, and is subject to row-level security.
-- Migrations run as the owner (POSTGRES_USER). Migration 0001 also creates gri_app if missing.
CREATE ROLE gri_app NOLOGIN;
CREATE ROLE gri_api LOGIN PASSWORD 'gri_api' IN ROLE gri_app;
