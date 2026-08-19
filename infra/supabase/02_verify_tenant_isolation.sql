-- Run in the Supabase SQL Editor AFTER `alembic upgrade head`.
--
-- A smoke test that the RLS policies bind to the application role. This is not
-- a substitute for tests/test_tenant_isolation.py (step 7), which exercises the
-- real connection path with two real projects. It is here to catch a
-- misconfigured role before you waste time debugging the application.

BEGIN;

-- Impersonate the runtime role. RLS is evaluated against current_user, so this
-- reproduces what the API sees.
SET LOCAL ROLE llmobserve_app;

-- No tenant context set. The policy predicate resolves to NULL, so this MUST
-- return 0 - not an error, and not a row count. A non-zero result means the
-- role is bypassing RLS.
SELECT 'traces visible with no tenant set (expect 0)' AS check, count(*) AS result
FROM traces
UNION ALL
SELECT 'observations visible with no tenant set (expect 0)', count(*)
FROM observations;

-- Writing outside a tenant context must be refused by the WITH CHECK clause.
-- Uncomment to confirm this raises:
--   new row violates row-level security policy for table "traces"
--
-- INSERT INTO traces (id, project_id, started_at)
-- VALUES (gen_random_uuid(), gen_random_uuid(), now());

ROLLBACK;
