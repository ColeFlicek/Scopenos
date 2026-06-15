# ACIP Kubernetes Deployment

Postgres runs **outside** the cluster so indexed data survives cluster recreation.
Redis stays in-cluster (stateless cache — cheap to lose).

## External Postgres setup (required first)

### TheHive (home server / K3d)

```bash
# Run pgvector Postgres as a standalone Docker container.
# Named volume survives container recreation; -p binds to all host interfaces
# so K3d pods can reach it via the Docker bridge gateway (172.21.0.1).
docker run -d \
  --name acip-postgres \
  --restart unless-stopped \
  -e POSTGRES_USER=acip \
  -e POSTGRES_PASSWORD=<password> \
  -e POSTGRES_DB=acip \
  -p 5432:5432 \
  -v acip-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg17

# Apply schema (one-time)
docker exec acip-postgres psql -U acip acip \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
cat schema.sql | docker exec -i acip-postgres psql -U acip acip

# DATABASE_URL for GitHub Actions secret:
#   postgresql://acip:<password>@172.21.0.1/acip
```

K3d cluster can be deleted and recreated freely — data is in the Docker volume.

### Production (Hetzner / DigitalOcean)

```bash
# Create a Hetzner Managed DB or DO Managed Postgres instance with pgvector.
# Apply schema once:
psql $DATABASE_URL -c "CREATE EXTENSION IF NOT EXISTS vector;"
psql $DATABASE_URL -f schema.sql

# DATABASE_URL for GitHub Actions secret:
#   postgresql://acip:<password>@<managed-host>:5432/acip
```

## GitHub Actions secrets

| Secret | Description |
|---|---|
| `KUBECONFIG` | base64-encoded kubeconfig for the cluster |
| `DATABASE_URL` | Full Postgres DSN pointing to external DB |
| `OPENAI_API_KEY` | For embeddings |
| `ANTHROPIC_API_KEY` | For LLM enrichment |
| `RESEND_API_KEY` | For email (optional) |
| `TS_OAUTH_CLIENT_ID` | Tailscale OAuth (CI access to home cluster) |
| `TS_OAUTH_SECRET` | Tailscale OAuth secret |

## K3d cluster setup (TheHive)

```bash
# Create with correct port mapping — port 3004 on host → Traefik HTTP inside cluster
k3d cluster create acip \
  --port 3004:80@loadbalancer \
  --k3s-arg "--tls-san=<your-server-ip>@server:0"

# Export kubeconfig (base64-encode for GitHub secret)
k3d kubeconfig get acip > ~/.kube/acip-config.yaml
base64 -w0 ~/.kube/acip-config.yaml  # paste this as KUBECONFIG secret
```

## First deploy

Push any commit — CI handles everything. Or manually:

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/configmap.yaml

# Secrets — set DATABASE_URL to your external Postgres DSN
kubectl create secret generic acip-secrets \
  --from-literal=DATABASE_URL="postgresql://acip:<pw>@172.21.0.1/acip" \
  --from-literal=REDIS_URL="redis://redis-svc.acip.svc.cluster.local:6379" \
  --from-literal=OPENAI_API_KEY="sk-..." \
  --from-literal=ANTHROPIC_API_KEY="sk-ant-..." \
  -n acip --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/redis.yaml
kubectl apply -f k8s/api-deployment.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/ingress.yaml
kubectl apply -f k8s/hpa-api.yaml
```

**Do not apply `k8s/postgres.yaml`** — that file is kept for reference only.

## Useful commands

```bash
# Check pod status
kubectl get pods -n acip

# Tail API logs
kubectl logs -l app=acip-api -n acip -f

# Tail worker logs
kubectl logs -l app=acip-worker -n acip -f

# Check queue depth
kubectl exec -it deployment/redis -n acip -- redis-cli llen rq:queue:acip-indexing

# Force rollout (after pushing new image)
kubectl rollout restart deployment/acip-api deployment/acip-worker -n acip

# Backup external Postgres
docker exec acip-postgres pg_dump -U acip --no-owner acip > acip-backup.sql

# Restore
docker exec -i acip-postgres psql -U acip acip < acip-backup.sql
```

## Scaling workers

```bash
# Manual
kubectl scale deployment acip-worker --replicas=3 -n acip

# Automatic (requires KEDA)
kubectl apply -f k8s/hpa-worker.yaml
```
