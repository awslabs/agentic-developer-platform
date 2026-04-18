#!/usr/bin/env bash
# Ingest repos listed in repos.txt into OpenViking
# Usage: ./scripts/ingest-repos.sh [--repos-file repos.txt] [--no-wait]
#
# Reads a repos.txt file (one org/repo per line, # comments supported)
# and creates a K8s Job to ingest each repo into OpenViking.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

# Parse arguments
REPOS_FILE_ARG=""
WAIT=true
while [[ $# -gt 0 ]]; do
  case $1 in
    --repos-file)
      REPOS_FILE_ARG="$2"
      shift 2
      ;;
    --no-wait)
      WAIT=false
      shift
      ;;
    --help)
      echo "Usage: ./scripts/ingest-repos.sh [--repos-file repos.txt] [--no-wait]"
      echo ""
      echo "Options:"
      echo "  --repos-file FILE   Path to repos file (default: from config.env REPOS_FILE)"
      echo "  --no-wait           Don't wait for ingestion to complete"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Resolve repos file path
if [ -n "${REPOS_FILE_ARG}" ]; then
  RESOLVED_REPOS_FILE="${REPOS_FILE_ARG}"
elif [ -n "${REPOS_FILE:-}" ]; then
  # If REPOS_FILE is relative, resolve from ROOT_DIR
  if [[ "${REPOS_FILE}" != /* ]]; then
    RESOLVED_REPOS_FILE="${ROOT_DIR}/${REPOS_FILE}"
  else
    RESOLVED_REPOS_FILE="${REPOS_FILE}"
  fi
else
  RESOLVED_REPOS_FILE="${ROOT_DIR}/index_content/repos.txt"
fi

if [ ! -f "${RESOLVED_REPOS_FILE}" ]; then
  echo "ERROR: Repos file not found: ${RESOLVED_REPOS_FILE}"
  echo "Create it with one org/repo per line, or use --repos-file to specify a path."
  exit 1
fi

# Count repos (excluding comments and blank lines)
REPO_COUNT=$(grep -cE '^\s*[^#\s]' "${RESOLVED_REPOS_FILE}" 2>/dev/null || echo "0")
echo "================================================"
echo "OpenViking Repo Ingestion"
echo "================================================"
echo "Repos file: ${RESOLVED_REPOS_FILE}"
echo "Repo count: ${REPO_COUNT}"
echo "Namespace:  ${NAMESPACE}"
echo "================================================"

if [ "${REPO_COUNT}" -eq 0 ]; then
  echo "No repos to ingest. Add repos to ${RESOLVED_REPOS_FILE} (one per line: org/repo)"
  exit 0
fi

# Create/update the repos ConfigMap
echo ""
echo "[1/3] Creating repos ConfigMap..."
kubectl create configmap openviking-repos \
  --from-file=repos.txt="${RESOLVED_REPOS_FILE}" \
  -n "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# Generate a unique job name
JOB_NAME="openviking-ingest-$(date +%s)"

# Create the ingestion Job
echo ""
echo "[2/3] Creating ingestion job: ${JOB_NAME}..."

export NAMESPACE SERVICE_ACCOUNT

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: openviking-ingest
    app.kubernetes.io/part-of: agent-context-platform
spec:
  ttlSecondsAfterFinished: 86400
  backoffLimit: 3
  template:
    metadata:
      labels:
        app: openviking-ingest
    spec:
      serviceAccountName: ${SERVICE_ACCOUNT}
      containers:
      - name: ingest
        image: python:3.11-slim
        command: ["/bin/sh", "-c"]
        args:
          - |
            pip install --no-cache-dir openviking &&
            python3 -c "
            import openviking as ov
            import os, sys, time, re

            url = 'http://openviking.${NAMESPACE}.svc.cluster.local:1933'
            api_key = os.environ.get('ROOT_KEY', '')

            print(f'Connecting to OpenViking at {url}...')
            client = ov.SyncHTTPClient(url=url, api_key=api_key)
            client.initialize()

            # Read and validate repos (must match org/repo format)
            repo_pattern = re.compile(r'^[a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+$')
            repos = []
            skipped = 0
            with open('/config/repos.txt') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if repo_pattern.match(line):
                            repos.append(line)
                        else:
                            print(f'SKIP: invalid repo format: {line!r}')
                            skipped += 1
            if skipped:
                print(f'Skipped {skipped} invalid entries.')

            print(f'Ingesting {len(repos)} repos...')
            success, failed = 0, 0
            for i, repo in enumerate(repos):
                gh_url = f'https://github.com/{repo}'
                try:
                    client.add_resource(gh_url)
                    print(f'[{i+1}/{len(repos)}] Added: {gh_url}')
                    success += 1
                except Exception as e:
                    print(f'[{i+1}/{len(repos)}] Failed: {gh_url} -- {e}')
                    failed += 1

            print(f'')
            print(f'Ingestion submitted: {success} added, {failed} failed')

            if success > 0:
                print('Waiting for semantic processing (this may take a while)...')
                try:
                    client.wait_processed()
                    print('Semantic processing complete.')
                except Exception as e:
                    print(f'Warning: wait_processed failed: {e}')
                    print('Repos were submitted but processing may still be in progress.')

            client.close()
            print('Done.')
            sys.exit(1 if failed > 0 and success == 0 else 0)
            "
        env:
        - name: ROOT_KEY
          valueFrom:
            secretKeyRef:
              name: agent-context-secrets
              key: openviking-root-key
              optional: true
        volumeMounts:
        - name: config
          mountPath: /config
          readOnly: true
        resources:
          requests:
            cpu: "250m"
            memory: "256Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
      restartPolicy: Never
      volumes:
      - name: config
        configMap:
          name: openviking-repos
EOF

echo "  Job created: ${JOB_NAME}"

# Step 3: Optionally wait for completion
echo ""
echo "[3/3] Monitoring ingestion..."
echo "  Follow logs: kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"

if [ "${WAIT}" = "true" ]; then
  echo "  Waiting for job to complete (timeout: 30m)..."
  if kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${NAMESPACE}" --timeout=1800s 2>/dev/null; then
    echo ""
    echo "  Ingestion job completed successfully!"
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --tail=20
  else
    echo ""
    echo "  WARNING: Job did not complete within timeout. Check logs:"
    echo "    kubectl logs job/${JOB_NAME} -n ${NAMESPACE}"
    kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --tail=10 2>/dev/null || true
  fi
else
  echo "  --no-wait specified. Job running in background."
fi

echo ""
echo "================================================"
echo "Ingestion job: ${JOB_NAME}"
echo "  Monitor:  kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
echo "  Status:   kubectl get job/${JOB_NAME} -n ${NAMESPACE}"
echo "================================================"
