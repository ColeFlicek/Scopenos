#!/usr/bin/env bash
# Launch a scoped Scopenos Claude Code session in Docker.
#
# Each session type gets exactly the credentials it needs — nothing more.
# Credentials are read from the environment of the calling shell; they are
# never stored in the project repo or written into container images.
#
# Usage:
#   SESSION_TYPE=reader    bash scripts/launch_session.sh
#   SESSION_TYPE=indexer   ANTHROPIC_API_KEY=... OPENAI_API_KEY=... bash scripts/launch_session.sh
#   SESSION_TYPE=deployer  bash scripts/launch_session.sh
#   SESSION_TYPE=migrator  bash scripts/launch_session.sh
#   SESSION_TYPE=tester    bash scripts/launch_session.sh
#   SESSION_TYPE=provisioner bash scripts/launch_session.sh
#
# Required env per session type:
#   reader:      SCOPENOS_READ_PASSWORD
#   indexer:     SCOPENOS_CONTROL_RW_PASSWORD, ANTHROPIC_API_KEY, OPENAI_API_KEY
#   deployer:    (no DB — kubeconfig only)
#   migrator:    SCOPENOS_MIGRATOR_PASSWORD
#   tester:      SCOPENOS_TEST_RUNNER_PASSWORD  (or leave blank to use phronosis/phronosis default)
#   provisioner: SCOPENOS_PROVISIONER_PASSWORD
#
# Required files (for sessions with kubeconfig):
#   reader:   kubeconfigs/scopenos-reader.yaml
#   deployer: kubeconfigs/scopenos-deployer.yaml
#
# Required env for all sessions:
#   DB_HOST          Postgres host (default: 172.21.0.1)
#   REPO_PATH        Path to the Scopenos repo on the host (default: /workspace/ACIP)
#   SESSION_IMAGE    Docker image to run (default: scopenos-claude-session:latest)

set -euo pipefail

DB_HOST="${DB_HOST:-172.21.0.1}"
REPO_PATH="${REPO_PATH:-/workspace/ACIP}"
SESSION_IMAGE="${SESSION_IMAGE:-scopenos-claude-session:latest}"
KUBECONFIG_DIR="${KUBECONFIG_DIR:-$(dirname "$0")/../kubeconfigs}"
SESSION_TYPE="${SESSION_TYPE:?Usage: SESSION_TYPE=<type> bash $0}"

# Shared args: repo volume (mode varies per session), remove on exit
BASE_ARGS=(--rm -it --network=host)

case "$SESSION_TYPE" in

    reader)
        : "${SCOPENOS_READ_PASSWORD:?must set SCOPENOS_READ_PASSWORD}"
        KUBECONFIG_FILE="${KUBECONFIG_DIR}/scopenos-reader.yaml"
        [[ -f "$KUBECONFIG_FILE" ]] || {
            echo "ERROR: kubeconfig not found at ${KUBECONFIG_FILE}" >&2
            echo "Run: bash scripts/generate_session_kubeconfigs.sh" >&2
            exit 1
        }
        docker run "${BASE_ARGS[@]}" \
            -e DATABASE_URL="postgresql://scopenos_read:${SCOPENOS_READ_PASSWORD}@${DB_HOST}/scopenos_production" \
            -v "${REPO_PATH}:/workspace/ACIP:ro" \
            -v "${KUBECONFIG_FILE}:/root/.kube/config:ro" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    indexer)
        : "${SCOPENOS_CONTROL_RW_PASSWORD:?must set SCOPENOS_CONTROL_RW_PASSWORD}"
        : "${ANTHROPIC_API_KEY:?must set ANTHROPIC_API_KEY}"
        docker run "${BASE_ARGS[@]}" \
            -e DATABASE_URL="postgresql://scopenos_control_rw:${SCOPENOS_CONTROL_RW_PASSWORD}@${DB_HOST}/scopenos_production" \
            -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}" \
            -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
            -v "${REPO_PATH}:/workspace/ACIP" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    deployer)
        KUBECONFIG_FILE="${KUBECONFIG_DIR}/scopenos-deployer.yaml"
        [[ -f "$KUBECONFIG_FILE" ]] || {
            echo "ERROR: kubeconfig not found at ${KUBECONFIG_FILE}" >&2
            echo "Run: bash scripts/generate_session_kubeconfigs.sh" >&2
            exit 1
        }
        # No DATABASE_URL — deployer never touches the DB
        docker run "${BASE_ARGS[@]}" \
            -v "${REPO_PATH}:/workspace/ACIP:ro" \
            -v "${KUBECONFIG_FILE}:/root/.kube/config:ro" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    migrator)
        : "${SCOPENOS_MIGRATOR_PASSWORD:?must set SCOPENOS_MIGRATOR_PASSWORD}"
        docker run "${BASE_ARGS[@]}" \
            -e DATABASE_URL="postgresql://scopenos_migrator:${SCOPENOS_MIGRATOR_PASSWORD}@${DB_HOST}/scopenos_production" \
            -v "${REPO_PATH}:/workspace/ACIP:ro" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    tester)
        TEST_PW="${SCOPENOS_TEST_RUNNER_PASSWORD:-phronosis}"
        TEST_USER="${SCOPENOS_TEST_RUNNER_PASSWORD:+scopenos_test_runner}"
        TEST_USER="${TEST_USER:-phronosis}"
        TEST_DB="${TEST_DB:-phronosis_test}"
        docker run "${BASE_ARGS[@]}" \
            -e TEST_DATABASE_URL="postgresql://${TEST_USER}:${TEST_PW}@${DB_HOST}/${TEST_DB}" \
            -v "${REPO_PATH}:/workspace/ACIP" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    provisioner)
        : "${SCOPENOS_PROVISIONER_PASSWORD:?must set SCOPENOS_PROVISIONER_PASSWORD}"
        docker run "${BASE_ARGS[@]}" \
            -e PROVISIONER_DSN="postgresql://scopenos_provisioner:${SCOPENOS_PROVISIONER_PASSWORD}@${DB_HOST}/postgres" \
            -v "${REPO_PATH}:/workspace/ACIP:ro" \
            -w /workspace/ACIP \
            "$SESSION_IMAGE"
        ;;

    *)
        echo "Unknown session type: ${SESSION_TYPE}" >&2
        echo "Valid types: reader | indexer | deployer | migrator | tester | provisioner" >&2
        exit 1
        ;;
esac
