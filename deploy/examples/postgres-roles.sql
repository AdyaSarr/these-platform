-- Example roles of the platform's database (docs/DEPLOYMENT.md, PostgreSQL).
-- Two roles: theses_owner owns the tables and runs the migrations;
-- theses_app is the role of the application (DATABASE_URL of gunicorn and
-- Celery): neither superuser, nor owner, nor member of the owner (vault.E003).
-- Passwords: \password <role> in psql, never in this file nor in shell history.

-- 1. Once, as a superuser (psql -U postgres).
CREATE ROLE theses_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE ROLE theses_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE DATABASE theses OWNER theses_owner;
\connect theses
-- PostgreSQL 15+: PUBLIC no longer creates in public; theses_owner owns it.
ALTER SCHEMA public OWNER TO theses_owner;
GRANT USAGE ON SCHEMA public TO theses_app;

-- 2. After EVERY migrate (as theses_owner; idempotent). Same grants as the
-- application role of apps/vault/tests/test_mfa_vault.py.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO theses_app;
REVOKE UPDATE, DELETE, TRUNCATE ON vault_auditevent, vault_auditcheckpoint FROM theses_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO theses_app;

-- 3. Once, as a superuser: the role of the nightly pg_dump (read only, every
-- table, present and future; PostgreSQL 14+). Its password in ~/.pgpass (0600)
-- of the backup user.
CREATE ROLE theses_backup LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT pg_read_all_data TO theses_backup;
