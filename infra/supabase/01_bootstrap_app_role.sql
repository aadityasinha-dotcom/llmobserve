-- Run ONCE per Supabase project, in the SQL Editor, as the `postgres` role.
-- Run this BEFORE `alembic upgrade head`: the migration issues GRANTs to this
-- role and will fail if it does not exist yet.
--
-- Why this is not an Alembic migration: roles are cluster-level objects, not
-- database objects. They are not part of the schema an Alembic downgrade should
-- ever revert, and the password does not belong in a committed file.
--
-- Why this is not infra/postgres/init/01-app-role.sh: that script is a
-- docker-entrypoint-initdb.d hook and only ever runs for a local container.
-- The two files do the same thing for the two deploy targets.
--
-- Plain SQL only - no psql backslash meta-commands. The Supabase SQL Editor is
-- a web UI with no psql client, so `\set` is not available there; and even
-- under psql, `:'var'` is NOT interpolated inside a dollar-quoted block (psql
-- leaves quoted strings alone), which fails with `syntax error at or near ":"`.
-- Hence the literal placeholder below, repeated.

-- ---------------------------------------------------------------------------
-- 1. Replace BOTH occurrences of REPLACE_ME_WITH_A_GENERATED_SECRET below.
--
--    Generate an alphanumeric secret:  openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | head -c 32
--
--    Alphanumeric matters. The password goes into a URL in .env, where `@` `:`
--    `/` `?` `#` are reserved and must be percent-encoded. A URL parser splits
--    userinfo at the FIRST `@`, so an unencoded one does not raise - it
--    silently truncates the password and folds the remainder into the hostname.
-- ---------------------------------------------------------------------------

BEGIN;

-- The role the API connects as. Deliberately: LOGIN, and nothing else.
-- No SUPERUSER, no BYPASSRLS, no CREATEDB, no CREATEROLE, and it owns no
-- tables. Those absences are what make the row-level security policies in
-- migration 0001 actually bind. Postgres exempts superusers from RLS
-- unconditionally and table owners unless the table is FORCE ROW LEVEL
-- SECURITY, so an app connecting as `postgres` would silently enforce nothing.
--
-- Idempotent: re-running resets the password rather than erroring.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'llmobserve_app') THEN
        CREATE ROLE llmobserve_app LOGIN PASSWORD 'REPLACE_ME_WITH_A_GENERATED_SECRET';
    ELSE
        ALTER ROLE llmobserve_app LOGIN PASSWORD 'REPLACE_ME_WITH_A_GENERATED_SECRET';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE postgres TO llmobserve_app;
GRANT USAGE ON SCHEMA public TO llmobserve_app;

-- Table-level grants are issued by migration 0001, which is where the tables
-- are created. Nothing further is granted here.

COMMIT;


-- ---------------------------------------------------------------------------
-- 2. Verify. This is the check that matters, and it is worth actually reading
--    the output rather than assuming.
--
--    llmobserve_app MUST come back:  rolsuper = f, rolbypassrls = f, rolcanlogin = t
--
--    Note that on Supabase the `postgres` role itself reports rolbypassrls = t.
--    That is expected and is exactly why the API must not connect as it: every
--    tenant-isolation policy would be inert, with no error anywhere.
-- ---------------------------------------------------------------------------
SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
FROM pg_roles
WHERE rolname IN ('postgres', 'llmobserve_app')
ORDER BY rolname;
