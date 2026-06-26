# Scopenos Database Health Check

Run the database health check script and surface the report.

## Prerequisites

`DATABASE_URL` must be set in the environment before running this skill.
The DB lives on TheHive (K8s cluster) — set the URL to point at it:

```bash
export DATABASE_URL=postgresql://scopenos:PASSWORD@HOST/scopenos
```

If `DATABASE_URL` is not set the script will print a clear error and exit.

## Steps

1. Check that `DATABASE_URL` is set. If not, print the prerequisite instructions
   above and stop — do not attempt to run the script.

2. Run the script using the project venv (asyncpg is installed there):
   ```bash
   .venv/bin/python scripts/db_health.py $ARGS
   ```
   Where `$ARGS` comes from the user's invocation:
   - `/db-health` → no args (output to `docs/db_health.md`)
   - `/db-health --project django-13606` → pass through verbatim
   - `/db-health --output /tmp/health.md` → pass through verbatim

2. If the script exits with an error, print the error and stop.

3. Read the generated Markdown file and surface it to the user with `SendUserFile`.

4. Print a one-paragraph summary covering:
   - How many projects are indexed
   - The benchmark readiness verdict for each project (🟢/🟡/🔴)
   - The single most important gap (e.g. "django-13606 is at 23% embedding coverage — run enrichment before the next benchmark")

## Options understood

| Flag | Purpose |
|---|---|
| `--project <id>` | Focus on one project — shorter output, deeper subsystem table |
| `--output <path>` | Write report to a custom path |
| `--dsn <url>` | Override the database URL |
