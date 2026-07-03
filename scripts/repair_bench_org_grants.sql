-- Repair org_benchmark: fix schema grants + copy indexed data from org_demos.
--
-- PROBLEM: When org_benchmark was provisioned, the provisioner (superuser)
-- pre-created empty project schemas (django, pytest, flask, requests).
-- Because the provisioner created them, org_benchmark_rw has no USAGE on
-- those schemas → "permission denied for schema X" on every index attempt.
--
-- SOLUTION (run everything on TheHive as superuser):
--
-- STEP 1 — Fix grants (run this file against org_benchmark):
--   docker exec -it acip-postgres psql -U scopenos org_benchmark \
--     -f repair_bench_org_grants.sql
--
-- STEP 2 — Copy indexed data from org_demos (schema-by-schema pg_dump):
--   for schema in django pytest flask requests; do
--     docker exec acip-postgres pg_dump -U scopenos org_demos \
--       --schema="$schema" --no-owner --no-acl -Fc \
--       | docker exec -i acip-postgres pg_restore -U scopenos org_benchmark \
--           --schema="$schema" --no-owner --no-acl \
--           -d org_benchmark --clean --if-exists
--   done
--
-- STEP 3 — Update node/edge counts in projects table:
--   docker exec -it acip-postgres psql -U scopenos org_benchmark -c "
--     UPDATE projects p SET
--       node_count = (SELECT count(*) FROM nodes n WHERE n.project_id = p.id),
--       edge_count = (SELECT count(*) FROM edges e WHERE e.project_id = p.id);
--   "
--   (run this against each schema, or use the multi-schema version below)

\echo 'Step 1: Fixing schema grants in org_benchmark...'

DO $$
DECLARE
  preseeded  text[] := ARRAY['django', 'pytest', 'flask', 'requests'];
  org_role   text   := 'org_benchmark_rw';
  prov_role  text   := 'scopenos_provisioner';
  s          text;
BEGIN
  FOREACH s IN ARRAY preseeded LOOP
    IF EXISTS (
      SELECT 1 FROM information_schema.schemata WHERE schema_name = s
    ) THEN
      EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO %I', s, org_role);
      EXECUTE format('GRANT ALL ON ALL TABLES IN SCHEMA %I TO %I', s, org_role);
      EXECUTE format('GRANT ALL ON ALL SEQUENCES IN SCHEMA %I TO %I', s, org_role);
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

  -- Prevent recurrence: future schemas created by the provisioner in this
  -- database will automatically be accessible to org_benchmark_rw.
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

  RAISE NOTICE 'Done. Run Step 2 (pg_dump copy) from the shell, then Step 3 (count update).';
END $$;
