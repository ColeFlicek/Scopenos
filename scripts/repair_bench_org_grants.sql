-- Repair org_benchmark: fix grants on pre-seeded project schemas.
--
-- PROBLEM: When org_benchmark was provisioned, the provisioner (superuser)
-- pre-created empty project schemas (django, pytest, flask, requests).
-- Because the provisioner created them, org_benchmark_rw has no USAGE on
-- those schemas and the indexer gets "permission denied for schema django".
--
-- RUN THIS ON THEIVE:
--   docker exec -it acip-postgres psql -U scopenos org_benchmark \
--     -f /path/to/repair_bench_org_grants.sql
--
-- Or paste the DO block into an interactive psql session against org_benchmark.
--
-- AFTER running this, trigger re-indexing via MCP:
--   mcp__scopenos_bench__index_project("/tmp/scopenos-demos/django__django", "django")
--   mcp__scopenos_bench__index_project("/tmp/scopenos-demos/pytest-dev__pytest", "pytest")
--   mcp__scopenos_bench__index_project("/tmp/scopenos-demos/pallets__flask", "flask")
--   mcp__scopenos_bench__index_project("/tmp/scopenos-demos/psf__requests", "requests")

\echo 'Repairing org_benchmark schema grants...'

DO $$
DECLARE
  preseeded  text[] := ARRAY['django', 'pytest', 'flask', 'requests'];
  org_role   text   := 'org_benchmark_rw';
  prov_role  text   := 'scopenos_provisioner';
  s          text;
BEGIN
  FOREACH s IN ARRAY preseeded LOOP
    -- Check schema exists before granting (skip silently if not)
    IF EXISTS (
      SELECT 1 FROM information_schema.schemata WHERE schema_name = s
    ) THEN
      EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', s, org_role);
      EXECUTE format('GRANT ALL ON ALL TABLES IN SCHEMA %I TO %I', s, org_role);
      EXECUTE format('GRANT ALL ON ALL SEQUENCES IN SCHEMA %I TO %I', s, org_role);
      -- Future objects in this schema created by any role
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL ON TABLES TO %I',
        s, org_role
      );
      EXECUTE format(
        'ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT ALL ON SEQUENCES TO %I',
        s, org_role
      );
      RAISE NOTICE 'Granted % access to schema %', org_role, s;
    ELSE
      RAISE NOTICE 'Schema % does not exist — skipping', s;
    END IF;
  END LOOP;

  -- Future schemas created by the provisioner will auto-grant to org_benchmark_rw.
  -- This prevents the same problem when new demo projects are added.
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT ALL ON TABLES TO %I',
    prov_role, org_role
  );
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT ALL ON SEQUENCES TO %I',
    prov_role, org_role
  );
  EXECUTE format(
    'ALTER DEFAULT PRIVILEGES FOR ROLE %I GRANT USAGE, CREATE ON SCHEMAS TO %I',
    prov_role, org_role
  );

  RAISE NOTICE 'Default privileges set for future provisioner-created schemas.';
  RAISE NOTICE 'Done. Now re-index via mcp__scopenos_bench__index_project.';
END $$;
