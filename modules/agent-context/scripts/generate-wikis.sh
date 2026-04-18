#!/usr/bin/env bash
# Generate DeepWiki documentation for repos in repos.txt
# Called by ingest-content.yml after OpenViking ingestion, or run manually.
#
# Usage:
#   ./scripts/generate-wikis.sh                    # All repos in repos.txt
#   ./scripts/generate-wikis.sh --repo org/repo    # Single repo
#   ./scripts/generate-wikis.sh --force            # Regenerate even if cached
#   ./scripts/generate-wikis.sh --list             # List cached wikis
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Source configuration and helpers
source "${SCRIPT_DIR}/_common.sh"
load_config "${ROOT_DIR}"

DEEPWIKI_API="http://deepwiki.${NAMESPACE}.svc.cluster.local:${DEEPWIKI_PORT}"
RESOLVED_REPOS_FILE="${ROOT_DIR}/${REPOS_FILE}"
FORCE=false
SINGLE_REPO=""
LIST_ONLY=false

# Parse arguments
while [[ $# -gt 0 ]]; do
  case $1 in
    --repo)
      SINGLE_REPO="$2"
      shift 2
      ;;
    --force)
      FORCE=true
      shift
      ;;
    --list)
      LIST_ONLY=true
      shift
      ;;
    --repos-file)
      RESOLVED_REPOS_FILE="$2"
      shift 2
      ;;
    --help)
      echo "Usage: ./scripts/generate-wikis.sh [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --repo org/repo    Generate wiki for a single repo"
      echo "  --force            Regenerate even if wiki already cached"
      echo "  --list             List all cached wikis and exit"
      echo "  --repos-file FILE  Path to repos file (default: from config.env)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

echo "================================================"
echo "DeepWiki — Generate Repository Documentation"
echo "================================================"
echo "DeepWiki API: ${DEEPWIKI_API}"
echo "Namespace:    ${NAMESPACE}"
echo "Force:        ${FORCE}"
echo "================================================"

# Helper: check if a wiki is already cached for a repo
wiki_is_cached() {
  local owner="$1"
  local repo="$2"
  local result
  # Use direct service DNS — runner is in the same cluster, no kubectl exec needed
  result=$(curl -sf "${DEEPWIKI_API}/api/wiki_cache?owner=${owner}&repo=${repo}&repo_type=github&language=en" 2>/dev/null || echo "")
  # If the response contains wiki pages, it's cached
  if echo "${result}" | grep -q '"pages"'; then
    return 0
  fi
  return 1
}

# Helper: trigger wiki generation for a repo via chat/completions/stream,
# then cache the result via POST /api/wiki_cache
generate_wiki() {
  local owner="$1"
  local repo="$2"
  local full_repo="${owner}/${repo}"

  echo "  Generating wiki for ${full_repo}..."

  # Step 1: Generate wiki content via the streaming chat endpoint.
  # This clones the repo, analyses it with the LLM, and returns markdown.
  # Uses direct service DNS — runner is in the same cluster, no kubectl exec needed.
  local content
  content=$(curl -sf -N -X POST "${DEEPWIKI_API}/chat/completions/stream" \
    -H "Content-Type: application/json" \
    -d "{
      \"repo_url\": \"https://github.com/${full_repo}\",
      \"messages\": [{\"role\": \"user\", \"content\": \"Generate a comprehensive architecture wiki. Cover: overview, architecture, key components, code organization, patterns, dependencies.\"}],
      \"provider\": \"openai\",
      \"model\": \"bedrock/global.anthropic.claude-sonnet-4-6\",
      \"language\": \"en\",
      \"type\": \"github\"
    }" \
    --connect-timeout 30 --max-time 900 2>/dev/null || echo "GENERATION_FAILED")

  if echo "${content}" | head -1 | grep -qi "GENERATION_FAILED\|Error with"; then
    echo "    FAILED: generation error — ${content:0:200}"
    return 1
  fi

  local content_len=${#content}
  if [ "${content_len}" -lt 500 ]; then
    echo "    FAILED: wiki too short (${content_len} chars) — ${content:0:200}"
    return 1
  fi

  echo "    Generated: ${content_len} chars"

  # Step 2: Cache the generated wiki via POST /api/wiki_cache
  # Build JSON payload for caching — use direct service DNS, no kubectl exec needed.
  local cache_result
  cache_result=$(python3 -c "
import requests, json, sys
content = sys.stdin.read()
wiki_page = {'id': 'overview', 'title': '${full_repo} Architecture Wiki', 'content': content, 'filePaths': [], 'importance': 'high', 'relatedPages': []}
req = {
    'repo': {'owner': '${owner}', 'repo': '${repo}', 'type': 'github'},
    'language': 'en',
    'wiki_structure': {'id': '${owner}-${repo}-wiki', 'title': '${full_repo} Wiki', 'description': 'Architecture documentation for ${full_repo}', 'pages': [wiki_page]},
    'generated_pages': {'overview': wiki_page},
    'provider': 'openai',
    'model': 'bedrock/global.anthropic.claude-sonnet-4-6'
}
resp = requests.post('${DEEPWIKI_API}/api/wiki_cache', json=req, timeout=30)
print('OK' if resp.ok else f'FAILED:{resp.status_code}')
" <<< "${content}" 2>/dev/null || echo "CACHE_FAILED")

  if echo "${cache_result}" | grep -q "OK"; then
    echo "    Cached: OK"
  else
    echo "    Cache: ${cache_result} (wiki generated but not cached)"
  fi

  return 0
}

# List cached wikis
if [ "${LIST_ONLY}" = "true" ]; then
  echo ""
  echo "Cached wikis:"
  curl -sf "${DEEPWIKI_API}/api/processed_projects" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    if isinstance(data, list):
        for p in data:
            print(f\"  {p.get('owner','?')}/{p.get('repo','?')} ({p.get('language','?')})\")
        print(f'Total: {len(data)} wikis')
    else:
        print(json.dumps(data, indent=2))
except Exception as e:
    print(f'Could not parse response: {e}')
" 2>/dev/null || echo "  Could not retrieve cached projects."
  exit 0
fi

# Build repo list
REPOS=()
if [ -n "${SINGLE_REPO}" ]; then
  REPOS=("${SINGLE_REPO}")
else
  if [ ! -f "${RESOLVED_REPOS_FILE}" ]; then
    echo "ERROR: Repos file not found: ${RESOLVED_REPOS_FILE}"
    exit 1
  fi
  while IFS= read -r line; do
    line=$(echo "${line}" | sed 's/#.*//' | xargs)
    [[ -z "${line}" ]] && continue
    REPOS+=("${line}")
  done < "${RESOLVED_REPOS_FILE}"
fi

echo "Repos to process: ${#REPOS[@]}"
echo ""

SUCCESS=0
SKIPPED=0
FAILED=0

for full_repo in "${REPOS[@]}"; do
  owner=$(echo "${full_repo}" | cut -d/ -f1)
  repo=$(echo "${full_repo}" | cut -d/ -f2)

  if [ -z "${owner}" ] || [ -z "${repo}" ]; then
    echo "  SKIP: invalid format '${full_repo}' (expected org/repo)"
    SKIPPED=$((SKIPPED + 1))
    continue
  fi

  # Check cache unless --force
  if [ "${FORCE}" != "true" ]; then
    if wiki_is_cached "${owner}" "${repo}"; then
      echo "  CACHED: ${full_repo} (use --force to regenerate)"
      SKIPPED=$((SKIPPED + 1))
      continue
    fi
  fi

  if generate_wiki "${owner}" "${repo}"; then
    SUCCESS=$((SUCCESS + 1))
  else
    FAILED=$((FAILED + 1))
  fi
done

echo ""
echo "================================================"
echo "Wiki Generation Summary"
echo "================================================"
echo "  Generated: ${SUCCESS}"
echo "  Skipped:   ${SKIPPED} (already cached)"
echo "  Failed:    ${FAILED}"
echo "================================================"

if [ "${FAILED}" -gt 0 ] && [ "${SUCCESS}" -eq 0 ]; then
  exit 1
fi
