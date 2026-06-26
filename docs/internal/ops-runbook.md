# Scopenos Ops Runbook

Procedures for operating the production Scopenos instance. Internal only.

---

## Deploy

**Trigger:** Push to `main` branch. GitHub Actions CI runs:
1. Tests (pytest against Postgres service container)
2. Docker image build → push to GHCR
3. `kubectl apply` of all manifests in `k8s/`
4. Waits for Postgres, then rolls API and worker pods

**Secrets required in GitHub Actions:**
- `DATABASE_URL` — Postgres connection string
- `OPENAI_API_KEY` — for embeddings
- `ANTHROPIC_API_KEY` — for contract generation and enrichment
- `RESEND_API_KEY` — for signup emails
- `TAILSCALE_AUTH_KEY` — for VPN access to home cluster
- `KUBECONFIG` — base64-encoded kubeconfig for K3s on TheHive

**Manual deploy (emergency):**
```bash
export KUBECONFIG=~/.kube/theHive.config
kubectl apply -f k8s/
kubectl rollout restart deployment/scopenos-api -n scopenos
kubectl rollout restart deployment/scopenos-worker -n scopenos
```

**Check pod status:**
```bash
kubectl get pods -n scopenos
kubectl logs -n scopenos deployment/scopenos-api --tail=50
```

---

## Re-index a project

**From a developer machine (via MCP):**
```
index_project("/absolute/path/to/project", project_id="myapp")
```
Returns a `job_id`. Poll with `GET /api/jobs/{job_id}` or wait for the MCP tool response.

**From inside the cluster (for large repos):**
```bash
kubectl exec -n scopenos -it deployment/scopenos-worker -- \
  python3 -c "
import asyncio
from src.call_graph.storage import CallGraphDB
from src.indexer import IndexerService
async def main():
    db = CallGraphDB()
    await db.initialize()
    # ... run indexer
asyncio.run(main())
"
```

**After changing the embedding model:**
```
reembed_project("myapp")
```

---

## Add a demo repo

1. Clone the repo to `/tmp/scopenos-demos/<name>` on the server
2. Run `index_project("/tmp/scopenos-demos/<name>", project_id="<name>")`
3. Run `enrich_summaries("<name>", limit=2000)`
4. Mark as demo in DB:
```sql
INSERT INTO demo_projects (project_id, display_name, repo_url, added_at)
VALUES ('<name>', '<Display Name>', 'https://github.com/org/repo', NOW());
```
5. Verify with `get_project_home("<name>")` — check subsystems and chokepoints are sensible

---

## Rotate API keys

**Create a new user + key:**
```bash
kubectl exec -n scopenos -it deployment/scopenos-api -- \
  python3 scripts/create_user.py --email user@example.com --name "User Name" --plan free
```
Prints the API key. Send it to the user.

**Revoke a key:**
```sql
UPDATE api_keys SET revoked_at = NOW() WHERE key_hash = '<hash>';
```
Key hash is SHA-256 of the raw key.

**List all keys for a user:**
```sql
SELECT id, name, created_at, last_used FROM api_keys WHERE user_id = '<user_id>' AND revoked_at IS NULL;
```

---

## Diagnose a broken index

**Symptoms:** `get_project_home` returns empty subsystems, `query_similar_functions` returns no results, or `get_callers` returns an empty list for a function that clearly has callers.

**Check 1 — Is the project indexed?**
```
list_projects()
```
If the project is missing, run `index_project` again.

**Check 2 — Are functions in the DB?**
```sql
SELECT COUNT(*) FROM function_nodes WHERE project_id = '<id>';
SELECT COUNT(*) FROM function_embeddings WHERE project_id = '<id>';
```
If `function_embeddings` count is much lower than `function_nodes`, run `reembed_project`.

**Check 3 — Is the indexer job stuck?**
```bash
kubectl exec -n scopenos -it deployment/scopenos-worker -- \
  python3 -c "from rq import Queue; from redis import Redis; q = Queue(connection=Redis(host='scopenos-redis')); print(q.job_ids)"
```

**Check 4 — Parser errors?**
Look at worker logs:
```bash
kubectl logs -n scopenos deployment/scopenos-worker --tail=100 | grep -i "error\|exception"
```

**Check 5 — HNSW index missing?**
```sql
\d function_embeddings
```
If no HNSW index exists, run:
```sql
CREATE INDEX CONCURRENTLY ON function_embeddings USING hnsw (embedding vector_cosine_ops);
```

---

## Recover from a failed migration

**Symptom:** Deploy fails with schema errors or `asyncpg` connection errors.

**Step 1 — Check what schema the DB is on:**
```bash
kubectl exec -n scopenos -it deployment/scopenos-postgres-0 -- \
  psql -U scopenos -d scopenos -c "\dt"
```

**Step 2 — Revert to the last working image:**
```bash
kubectl set image deployment/scopenos-api api=ghcr.io/coleflicek/scopenos:<previous-tag> -n scopenos
kubectl set image deployment/scopenos-worker worker=ghcr.io/coleflicek/scopenos:<previous-tag> -n scopenos
```

**Step 3 — Apply the missing migration manually:**
Migrations are not tracked by a migration tool — schema changes are in `k8s/postgres.yaml` ConfigMap. Compare the current schema against the ConfigMap and apply the delta manually.

**Step 4 — Re-run the broken deploy after fixing the migration.**

---

## Network restriction (required before public launch)

`POST /index` and `POST /api/decisions` are intentionally unauthenticated (used by git hooks). Before public launch, restrict port 3004 to:
- The VPN subnet (Tailscale IP range for developer machines)
- The K8s pod CIDR (for internal service-to-service calls)

This is not currently configured. Add a Kubernetes NetworkPolicy or a firewall rule on the Hetzner/DO host.

---

## Useful one-liners

```bash
# Check health
curl http://100.71.88.106:3004/api/health | jq .

# List projects
curl http://100.71.88.106:3004/api/projects | jq '.[].id'

# Trigger a re-index via HTTP (requires API key)
curl -X POST http://100.71.88.106:3004/api/enrich-summaries/pytest \
  -H "X-API-Key: ph_..." \
  -H "Content-Type: application/json" \
  -d '{"limit": 500}'

# Tail API logs
kubectl logs -n scopenos -f deployment/scopenos-api

# Connect to Postgres directly
kubectl exec -n scopenos -it deployment/scopenos-postgres-0 -- \
  psql -U scopenos -d scopenos
```
