#!/usr/bin/env bash
# Stage the shared contracts tree into the gateway's docker build context (#5146).
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# `contracts/orchestration-review/v1` holds ONE normative validator, shared by the
# gateway, the orchestration tick and the agent worker, so the producer and the
# consumer cannot drift into disagreeing (the #4029 failure). It has no
# `pyproject.toml` and is not pip-installed — deliberately, since installing it would
# reintroduce a per-consumer copy.
#
# The gateway image's docker build context is `modules/gateway`
# (`codebuild/bs-gateway-build.yml` does `cd modules/gateway && docker build .`), so
# the repository-root `contracts/` tree is OUTSIDE the context and no `COPY` in the
# Dockerfile can reach it. That is the same constraint
# `src/orchestration/manifests/orchestration-deployments.yaml` documents for itself.
# Rather than move a shared contract under one of its consumers, the build copies it
# into the context first. This script is that copy.
#
# Idempotent: re-running replaces the staged copy. Safe to run from a dirty tree.
#
# WHY A STAGED COPY IS SAFE HERE
# ------------------------------
# A copy in a build context is not a second source of truth: it is written by this
# script from the single source on every build, it is gitignored, and
# `python -m src.orchestration.review_contract_selfcheck` runs inside the built image
# as a build gate — so a stale or absent copy fails the build rather than shipping.
# Without that gate this script would be exactly the drift it is meant to prevent.
#
# Usage:
#   modules/gateway/scripts/stage-contracts.sh          # stage into modules/gateway/
#   modules/gateway/scripts/stage-contracts.sh --clean  # remove the staged copy

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GATEWAY_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${GATEWAY_DIR}/../.." && pwd)"

SOURCE="${REPO_ROOT}/contracts"
TARGET="${GATEWAY_DIR}/contracts"

if [[ "${1:-}" == "--clean" ]]; then
  rm -rf "${TARGET}"
  echo "Removed staged contracts at ${TARGET}"
  exit 0
fi

if [[ ! -d "${SOURCE}/orchestration-review/v1" ]]; then
  echo "ERROR: no contracts tree at ${SOURCE}/orchestration-review/v1" >&2
  echo "This script must run from a full repository checkout, not from inside the" >&2
  echo "docker build context." >&2
  exit 1
fi

rm -rf "${TARGET}"
mkdir -p "${TARGET}"

# Only the contract definitions. Test files and __pycache__ are excluded: the tests
# run in CI from the checkout, and shipping bytecode compiled by another interpreter
# into a runtime image is how an import picks up a stale module.
for contract in "${SOURCE}"/*/; do
  name="$(basename "${contract}")"
  mkdir -p "${TARGET}/${name}"
  # `find`-based copy rather than `cp -r` + delete, so nothing unwanted is ever
  # written into the context even momentarily.
  ( cd "${contract}" && find . \
      -name '__pycache__' -prune -o \
      -name 'test_*.py' -prune -o \
      -type f -print0 ) \
    | ( cd "${contract}" && xargs -0 -I{} cp -f --parents {} "${TARGET}/${name}/" )
done

# Vault delivery must decode the same approval-bound request as harness admission.
# Stage its dependency-free canonical module, never a hand-maintained copy.
mkdir -p "${TARGET}/harness-operation"
cp -f "${REPO_ROOT}/modules/harness/jobs/harness_jobs/identity.py" "${TARGET}/harness-operation/identity.py"

# Share only the dependency-light wire contract, not the adapter distribution.
mkdir -p "${TARGET}/jira-context"
cp -f "${REPO_ROOT}/modules/tools/adp_tools/jira_context.py" "${TARGET}/jira-context/contract.py"

# The paid-domain Gateway composes the canonical lease/fencing implementation.
# Stage the package from its owner; never duplicate its SQL/claim algorithms.
mkdir -p "${TARGET}/harness-jobs/harness_jobs"
find "${REPO_ROOT}/modules/harness/jobs/harness_jobs" -maxdepth 1 -name '*.py' -type f -exec cp -f {} "${TARGET}/harness-jobs/harness_jobs/" \;

# Chat persona grounding (#8113): the gateway renders the sandbox's system prompt from
# the same persona markdown the agent runtimes ship. Stage the rules tree so the image
# reads `contracts/chat-personas/<persona>.md`; a checkout reads the rules tree directly
# (src/agentauth/chat_persona_prompt.py). Never a hand-maintained copy.
mkdir -p "${TARGET}/chat-personas"
find "${REPO_ROOT}/modules/agent-factory/rules/personas" -maxdepth 1 -name '*.md' -type f -exec cp -f {} "${TARGET}/chat-personas/" \;

mkdir -p "${TARGET}/insights"
for source in projection.py results.py; do
  cp -f "${REPO_ROOT}/modules/insights/${source}" "${TARGET}/insights/${source}"
done

# Adapter verification shares only its stdlib observer with the gateway. Never
# stage the adapter application, provider code, configuration or dependencies.
mkdir -p "${TARGET}/tool-adapters"
cp -f "${REPO_ROOT}/modules/tools/adapters/runtime_evidence.py" "${TARGET}/tool-adapters/runtime_evidence.py"
( cd "${TARGET}/tool-adapters" && sha256sum runtime_evidence.py > runtime_evidence.sha256 )

# The README explains the no-install rule the staged copy depends on; keep it with
# the copy so a reader inside the image is not left guessing.
if [[ -f "${SOURCE}/README.md" ]]; then
  cp -f "${SOURCE}/README.md" "${TARGET}/README.md"
fi

staged="$(find "${TARGET}" -name '*.py' -o -name '*.json' | wc -l | tr -d ' ')"
if [[ ! -f "${TARGET}/orchestration-review/v1/models.py" ]]; then
  echo "ERROR: staging produced no orchestration-review validator at ${TARGET}" >&2
  exit 1
fi
if [[ ! -f "${TARGET}/orchestration-evaluation/v1/models.py" || ! -f "${TARGET}/orchestration-evaluation/v1/evaluation-receipt.golden.json" || ! -f "${TARGET}/orchestration-evaluation/v1/evaluation-receipt.schema.json" ]]; then
  echo "ERROR: staging produced an incomplete orchestration-evaluation contract at ${TARGET}" >&2
  exit 1
fi
if [[ ! -f "${TARGET}/multi-repository-evaluation/v1/models.py" || ! -f "${TARGET}/multi-repository-evaluation/v1/evaluation-receipt.golden.json" ]]; then
  echo "ERROR: staging produced an incomplete multi-repository-evaluation contract at ${TARGET}" >&2
  exit 1
fi
echo "Staged ${staged} contract file(s) into ${TARGET}"

# Exact stopped bootstrap qualification remains owned by the Superplane app.
cp -f "${REPO_ROOT}/modules/domain-apps/superplane/installation/evidence/bootstrap-worker-61fcfc.json" "${TARGET}/superplane-bootstrap-qualification.json"
cp -f "${REPO_ROOT}/modules/domain-apps/superplane/installation/evidence/bootstrap-management-worker-0025522e.json" "${TARGET}/superplane-bootstrap-management-qualification.json"
cp -f "${REPO_ROOT}/modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-1eb49977.json" "${TARGET}/superplane-bootstrap-reader-qualification.json"
cp -f "${REPO_ROOT}/modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-c8f44a0b.json" "${TARGET}/superplane-bootstrap-partial-reader-qualification.json"

# Closed bootstrap cleanup runs maintained native code inside the Gateway.
# Stage only packages, never installer scripts, inputs, tests or credentials.
mkdir -p "${TARGET}/superplane-bootstrap/superplane_bootstrap" "${TARGET}/superplane-contracts/superplane_contracts"
find "${REPO_ROOT}/modules/domain-apps/superplane/workspace_bootstrap/superplane_bootstrap" -maxdepth 1 -name '*.py' -type f -exec cp -f {} "${TARGET}/superplane-bootstrap/superplane_bootstrap/" \;
find "${REPO_ROOT}/modules/domain-apps/superplane/contracts/superplane_contracts" -maxdepth 1 -name '*.py' -type f -exec cp -f {} "${TARGET}/superplane-contracts/superplane_contracts/" \;
