#!/usr/bin/env bash
# Index DeepWiki-generated wikis into OpenViking
# Wikis become searchable alongside code via the same MCP tools.
#
# Usage:
#   ./scripts/index-wikis.sh                    # Index all cached wikis
#   ./scripts/index-wikis.sh --repo org/repo    # Index wiki for one repo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

DEEPWIKI_API="http://deepwiki.${NAMESPACE}.svc.cluster.local:${DEEPWIKI_PORT}"
OV_API="http://openviking.${NAMESPACE}.svc.cluster.local:1933"
SINGLE_REPO=""

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --repo)
      SINGLE_REPO="$2"
      shift 2
      ;;
    --help)
      echo "Usage: ./scripts/index-wikis.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --repo org/repo    Index wiki for a single repo only"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "================================================"
echo "Index DeepWiki Wikis into OpenViking"
echo "================================================"
echo "DeepWiki: ${DEEPWIKI_API}"
echo "OpenViking: ${OV_API}"
echo "Namespace: ${NAMESPACE}"
echo "================================================"

# Create a K8s Job to fetch wikis from DeepWiki and index into OpenViking
# This runs inside the cluster so it can reach both services
JOB_NAME="deepwiki-index-$(date +%s)"

echo ""
echo "Creating indexing job: ${JOB_NAME}..."

export NAMESPACE SERVICE_ACCOUNT

cat <<JOBEOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: deepwiki-index
    app.kubernetes.io/part-of: agent-context-platform
spec:
  ttlSecondsAfterFinished: 86400
  backoffLimit: 2
  template:
    metadata:
      labels:
        app: deepwiki-index
    spec:
      serviceAccountName: ${SERVICE_ACCOUNT}
      containers:
      - name: indexer
        image: python:3.11-slim
        command: ["/bin/sh", "-c"]
        args:
          - |
            pip install --no-cache-dir requests 2>/dev/null
            python3 << 'PYEOF'
            import requests, json, os, sys, tempfile, time

            DEEPWIKI_API = "${DEEPWIKI_API}"
            OV_URL = "${OV_API}"
            OV_KEY = os.environ.get("ROOT_KEY", "")
            SINGLE_REPO = "${SINGLE_REPO}"

            ov_headers = {
                "X-OpenViking-Account": "default",
                "X-OpenViking-User": "default",
            }
            if OV_KEY:
                ov_headers["X-API-Key"] = OV_KEY

            def index_wiki_to_openviking(full_repo, wiki_markdown):
                """Index wiki markdown into OpenViking using temp_upload + add resource."""
                safe_name = full_repo.replace("/", "-")
                filename = f"deepwiki-{safe_name}.md"

                # Step 1: Upload wiki as temp file (multipart)
                boundary = "----DeepWikiUpload" + str(int(time.time()))
                body = (
                    "--" + boundary + "\r\n"
                    f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                    "Content-Type: text/markdown\r\n\r\n"
                ).encode() + wiki_markdown.encode("utf-8") + (
                    "\r\n--" + boundary + "--\r\n"
                ).encode()

                upload_resp = requests.post(
                    f"{OV_URL}/api/v1/resources/temp_upload",
                    headers={**ov_headers, "Content-Type": f"multipart/form-data; boundary={boundary}"},
                    data=body,
                    timeout=60
                )
                if not upload_resp.ok:
                    return False, f"temp_upload failed: HTTP {upload_resp.status_code}"

                temp_id = upload_resp.json().get("result", {}).get("temp_file_id", "")
                if not temp_id:
                    return False, "temp_upload returned no temp_file_id"

                # Step 2: Add as resource under viking://resources/deepwiki/
                add_resp = requests.post(
                    f"{OV_URL}/api/v1/resources",
                    headers={**ov_headers, "Content-Type": "application/json"},
                    json={
                        "temp_file_id": temp_id,
                        "to": f"viking://resources/deepwiki/{safe_name}-wiki.md",
                        "reason": f"DeepWiki architecture documentation for {full_repo}",
                        "wait": True,
                        "timeout": 120
                    },
                    timeout=120
                )
                if add_resp.ok:
                    return True, "OK"
                return False, f"add_resource failed: HTTP {add_resp.status_code} {add_resp.text[:200]}"

            # Step 1: Get list of cached wikis from DeepWiki
            print("Fetching cached wiki projects from DeepWiki...")
            try:
                resp = requests.get(f"{DEEPWIKI_API}/api/processed_projects", timeout=30)
                projects = resp.json() if resp.ok else []
            except Exception as e:
                print(f"ERROR: Could not fetch projects from DeepWiki: {e}")
                sys.exit(1)

            if not isinstance(projects, list):
                print(f"Unexpected response format: {type(projects)}")
                projects = []

            print(f"Found {len(projects)} cached wiki(s)")

            # Filter to single repo if specified
            if SINGLE_REPO:
                parts = SINGLE_REPO.split("/")
                if len(parts) == 2:
                    projects = [p for p in projects
                                if p.get("owner") == parts[0] and p.get("repo") == parts[1]]
                    print(f"Filtered to {len(projects)} project(s) matching {SINGLE_REPO}")

            success, failed = 0, 0

            for proj in projects:
                owner = proj.get("owner", "")
                repo = proj.get("repo", "")
                if not owner or not repo:
                    continue

                full_repo = f"{owner}/{repo}"
                print(f"\nIndexing wiki for {full_repo}...")

                # Step 2: Fetch the wiki content from DeepWiki cache
                try:
                    cache_resp = requests.get(
                        f"{DEEPWIKI_API}/api/wiki_cache",
                        params={"owner": owner, "repo": repo, "repo_type": "github", "language": "en"},
                        timeout=60
                    )
                    if not cache_resp.ok:
                        print(f"  SKIP: No cached wiki (HTTP {cache_resp.status_code})")
                        continue
                    wiki_data = cache_resp.json()
                    if not wiki_data:
                        print(f"  SKIP: Empty cache response")
                        continue
                except Exception as e:
                    print(f"  FAILED to fetch wiki: {e}")
                    failed += 1
                    continue

                # Step 3: Extract wiki pages as markdown
                # Cache structure: wiki_structure.pages[] and generated_pages{}
                wiki_structure = wiki_data.get("wiki_structure", {})
                generated_pages = wiki_data.get("generated_pages", {})
                pages = wiki_structure.get("pages", [])

                # Build combined markdown from all pages
                wiki_markdown = f"# {full_repo} — DeepWiki Documentation\n\n"
                wiki_markdown += f"Auto-generated architecture documentation for [{full_repo}](https://github.com/{full_repo})\n\n"
                wiki_markdown += "---\n\n"

                for page in pages:
                    page_id = page.get("id", "")
                    title = page.get("title", "Untitled")
                    content = page.get("content", "")
                    # Also check generated_pages for richer content
                    if page_id in generated_pages:
                        gen_content = generated_pages[page_id].get("content", "")
                        if gen_content and len(gen_content) > len(content):
                            content = gen_content
                    if content:
                        wiki_markdown += f"## {title}\n\n{content}\n\n---\n\n"

                if len(wiki_markdown) < 500:
                    print(f"  SKIP: Wiki too short ({len(wiki_markdown)} chars)")
                    continue

                print(f"  Wiki: {len(pages)} page(s), {len(wiki_markdown)} chars")

                # Step 4: Index into OpenViking via temp_upload + add resource
                ok, msg = index_wiki_to_openviking(full_repo, wiki_markdown)
                if ok:
                    print(f"  OK: Indexed into OpenViking")
                    success += 1
                else:
                    print(f"  FAILED: {msg}")
                    failed += 1

            print(f"\n{'='*50}")
            print(f"Indexing Summary: {success} indexed, {failed} failed")
            print(f"{'='*50}")
            sys.exit(1 if failed > 0 and success == 0 else 0)
            PYEOF
        env:
        - name: ROOT_KEY
          valueFrom:
            secretKeyRef:
              name: agent-context-secrets
              key: openviking-root-key
              optional: true
        resources:
          requests:
            cpu: "250m"
            memory: "256Mi"
          limits:
            cpu: "500m"
            memory: "512Mi"
      restartPolicy: Never
JOBEOF

echo "  Job created: ${JOB_NAME}"

# Wait for completion
echo ""
echo "Waiting for indexing job to complete (timeout: 15m)..."
echo "  Follow logs: kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"

if kubectl wait --for=condition=complete "job/${JOB_NAME}" -n "${NAMESPACE}" --timeout=900s 2>/dev/null; then
  echo ""
  echo "Indexing job completed successfully!"
  kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --tail=20
else
  echo ""
  echo "WARNING: Job did not complete within timeout. Check logs:"
  echo "  kubectl logs job/${JOB_NAME} -n ${NAMESPACE}"
  kubectl logs "job/${JOB_NAME}" -n "${NAMESPACE}" --tail=10 2>/dev/null || true
fi

echo ""
echo "================================================"
echo "Wiki indexing job: ${JOB_NAME}"
echo "  Monitor: kubectl logs -f job/${JOB_NAME} -n ${NAMESPACE}"
echo "  Status:  kubectl get job/${JOB_NAME} -n ${NAMESPACE}"
echo "================================================"
