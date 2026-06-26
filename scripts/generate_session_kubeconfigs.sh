#!/usr/bin/env bash
# Generate scoped kubeconfig files for Scopenos Claude Code sessions.
#
# Prerequisites:
#   kubectl apply -f k8s/session-rbac.yaml
#   (wait ~5s for token secrets to populate)
#
# Usage:
#   bash scripts/generate_session_kubeconfigs.sh [output-dir]
#   Output dir defaults to ./kubeconfigs/
#
# Generates:
#   <output-dir>/scopenos-reader.yaml   — read-only cluster view
#   <output-dir>/scopenos-deployer.yaml — patch deployments, read pods/logs
#
# These files contain a service account token. They do NOT expire unless the
# secret is deleted. Store them in a secrets manager, not in the repo.

set -euo pipefail

NAMESPACE="scopenos"
OUTPUT_DIR="${1:-./kubeconfigs}"
mkdir -p "$OUTPUT_DIR"

# Resolve cluster info from the current kubeconfig
CLUSTER_SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CLUSTER_CA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
CLUSTER_NAME=$(kubectl config view --minify -o jsonpath='{.clusters[0].name}')

generate_kubeconfig() {
    local session_name="$1"
    local secret_name="${session_name}-token"
    local output_file="${OUTPUT_DIR}/${session_name}.yaml"

    echo "[kubeconfig] Waiting for token secret ${secret_name}..."
    kubectl wait --for=jsonpath='{.data.token}' \
        secret/"$secret_name" \
        -n "$NAMESPACE" \
        --timeout=30s 2>/dev/null || {
        echo "[kubeconfig] ERROR: token not ready for ${secret_name}. Did you run: kubectl apply -f k8s/session-rbac.yaml?" >&2
        return 1
    }

    local token
    token=$(kubectl get secret "$secret_name" -n "$NAMESPACE" \
        -o jsonpath='{.data.token}' | base64 --decode)

    cat > "$output_file" <<KUBECONFIG
apiVersion: v1
kind: Config
preferences: {}
clusters:
  - name: ${CLUSTER_NAME}
    cluster:
      server: ${CLUSTER_SERVER}
      certificate-authority-data: ${CLUSTER_CA}
contexts:
  - name: ${session_name}@${CLUSTER_NAME}
    context:
      cluster: ${CLUSTER_NAME}
      user: ${session_name}
      namespace: ${NAMESPACE}
current-context: ${session_name}@${CLUSTER_NAME}
users:
  - name: ${session_name}
    user:
      token: ${token}
KUBECONFIG

    chmod 600 "$output_file"
    echo "[kubeconfig] Written: ${output_file}"
}

generate_kubeconfig scopenos-reader
generate_kubeconfig scopenos-deployer

echo ""
echo "Kubeconfigs written to ${OUTPUT_DIR}/"
echo "Mount these read-only into the appropriate Docker session containers:"
echo "  reader:   -v \$(pwd)/${OUTPUT_DIR}/scopenos-reader.yaml:/root/.kube/config:ro"
echo "  deployer: -v \$(pwd)/${OUTPUT_DIR}/scopenos-deployer.yaml:/root/.kube/config:ro"
echo ""
echo "IMPORTANT: Do not commit these files — they contain long-lived tokens."
echo "Add to .gitignore: kubeconfigs/"
