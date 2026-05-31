#!/bin/bash
#
# load-deploy-config.sh
#
# Source this script (don't execute it) to populate ADP deployment env vars
# from `config/deployment.yml`. Falls back to runtime-derived values if the
# file or individual fields are missing — so this is safe to source from any
# deploy script or GitHub Actions workflow without breaking existing deploys.
#
# Usage:
#   source platform/scripts/load-deploy-config.sh
#
# Exports (always):
#   ADP_ACCOUNT_ID         — AWS account ID hosting the ADP platform
#   ADP_REGION             — AWS region
#   ADP_ENVIRONMENT        — deployment environment (dev/staging/prod)
#   ADP_GITHUB_ORG         — GitHub org owning the repo
#   ADP_STATE_BUCKET       — Terraform state bucket name (derived from ADP_ACCOUNT_ID)
#
# Exports (only when customer_account is set):
#   ADP_CUSTOMER_ACCOUNT_ID    — the linked customer account to deploy into
#   ADP_CUSTOMER_AWS_LABEL     — vaulted credential label
#   ADP_DEPLOY_TARGET_ACCOUNT  — alias for ADP_CUSTOMER_ACCOUNT_ID (else ADP_ACCOUNT_ID)
#
# Each value follows this resolution order:
#   1. Existing env var (so callers can override per-invocation)
#   2. config/deployment.yml field, if file exists and field non-empty
#   3. Runtime fallback (aws sts get-caller-identity, AWS_REGION, etc.)

set -e

_LDC_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
_LDC_CONFIG_FILE="${_LDC_REPO_ROOT}/config/deployment.yml"

# -----------------------------------------------------------------------------
# Read a single field from config/deployment.yml using Python (universally
# available on EKS runners, macOS, Linux). Returns empty if file or field
# missing. Supports nested keys via "parent.child".
# -----------------------------------------------------------------------------
_ldc_read_field() {
  local field="$1"
  if [ ! -f "$_LDC_CONFIG_FILE" ]; then
    return 0
  fi
  python3 - "$_LDC_CONFIG_FILE" "$field" <<'PY' 2>/dev/null || true
import sys
from pathlib import Path

path, field = sys.argv[1], sys.argv[2]
text = Path(path).read_text()

# Lightweight parser: enough for `key: value` and one level of nesting.
# Avoids a PyYAML dependency. Strict-by-design to keep the surface tiny.
parts = field.split(".")
current_indent = 0
target_parent = parts[0] if len(parts) > 1 else None
target_key = parts[-1]
in_parent_block = (target_parent is None)

for raw in text.splitlines():
    line = raw.rstrip()
    if not line or line.lstrip().startswith("#"):
        continue
    stripped = line.lstrip()
    indent = len(line) - len(stripped)

    if target_parent is not None:
        if not in_parent_block:
            if indent == 0 and stripped.startswith(f"{target_parent}:"):
                in_parent_block = True
                continue
        else:
            if indent == 0:
                # Out of the parent block without finding key
                break
            if stripped.startswith(f"{target_key}:"):
                value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                print(value)
                sys.exit(0)
    else:
        if indent == 0 and stripped.startswith(f"{target_key}:"):
            value = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            print(value)
            sys.exit(0)
PY
}

# -----------------------------------------------------------------------------
# Resolve a single config value: env override → config file → runtime fallback
# -----------------------------------------------------------------------------
_ldc_resolve() {
  local env_name="$1"
  local field="$2"
  local fallback_cmd="$3"

  # 1. Existing env var wins (use eval for portable indirect expansion)
  local existing
  existing=$(eval "echo \"\${$env_name:-}\"")
  if [ -n "$existing" ]; then
    echo "$existing"
    return
  fi

  # 2. config file
  local from_file
  from_file=$(_ldc_read_field "$field")
  if [ -n "$from_file" ]; then
    echo "$from_file"
    return
  fi

  # 3. fallback (may itself be empty)
  if [ -n "$fallback_cmd" ]; then
    eval "$fallback_cmd" 2>/dev/null || echo ""
  fi
}

# -----------------------------------------------------------------------------
# Resolve fields
# -----------------------------------------------------------------------------

ADP_REGION=$(_ldc_resolve ADP_REGION region "echo \${AWS_REGION:-\$(aws configure get region 2>/dev/null || echo us-east-1)}")
export ADP_REGION
export AWS_REGION="${AWS_REGION:-$ADP_REGION}"

ADP_ENVIRONMENT=$(_ldc_resolve ADP_ENVIRONMENT environment "echo dev")
export ADP_ENVIRONMENT

ADP_ACCOUNT_ID=$(_ldc_resolve ADP_ACCOUNT_ID account_id "aws sts get-caller-identity --query Account --output text")
export ADP_ACCOUNT_ID

ADP_GITHUB_ORG=$(_ldc_resolve ADP_GITHUB_ORG github_org "git -C $_LDC_REPO_ROOT config --get remote.origin.url 2>/dev/null | sed -E 's#.*github.com[:/]([^/]+)/.*#\\1#'")
export ADP_GITHUB_ORG

if [ -n "$ADP_ACCOUNT_ID" ]; then
  export ADP_STATE_BUCKET="adp-terraform-state-${ADP_ACCOUNT_ID}"
fi

# -----------------------------------------------------------------------------
# Optional cross-account block
# -----------------------------------------------------------------------------
ADP_CUSTOMER_ACCOUNT_ID=$(_ldc_resolve ADP_CUSTOMER_ACCOUNT_ID customer_account.account_id "")
ADP_CUSTOMER_AWS_LABEL=$(_ldc_resolve ADP_CUSTOMER_AWS_LABEL customer_account.aws_label "")
ADP_CUSTOMER_USER_ID=$(_ldc_resolve ADP_CUSTOMER_USER_ID customer_account.user_id "")
ADP_GATEWAY_URL=$(_ldc_resolve ADP_GATEWAY_URL customer_account.gateway_url "echo http://bedrockgateway.adp-gateway")

if [ -n "$ADP_CUSTOMER_ACCOUNT_ID" ]; then
  export ADP_CUSTOMER_ACCOUNT_ID
  export ADP_CUSTOMER_AWS_LABEL
  export ADP_CUSTOMER_USER_ID
  export ADP_GATEWAY_URL
  export ADP_DEPLOY_TARGET_ACCOUNT="$ADP_CUSTOMER_ACCOUNT_ID"

  # Resolve the platform-account API Gateway invoke URL BEFORE the assume
  # swaps creds — once we hold customer-account creds, this SSM read would
  # hit the customer's SSM (wrong account). assume-customer-creds.py uses
  # this to SigV4-sign /internal/v1/credential-assume-role against the
  # platform's API GW (per EPIC #1107 Phase 2).
  #
  # FAIL FAST if missing: per EPIC #1107 the SigV4 path is now the only
  # supported auth. Falling back to shared-secret would mask IAM
  # misconfiguration and silently use a deprecated path.
  if [ -z "${ADP_GATEWAY_API_URL:-}" ]; then
    ADP_GATEWAY_API_URL=$(aws ssm get-parameter \
      --name "/adp/${ADP_ENVIRONMENT}/gateway/apigw-invoke-url" \
      --query Parameter.Value --output text 2>/dev/null || echo "")
    if [ -z "$ADP_GATEWAY_API_URL" ]; then
      echo "ERROR: SSM /adp/${ADP_ENVIRONMENT}/gateway/apigw-invoke-url is empty." >&2
      echo "  This SSM param is published by gateway-infra terraform; if missing," >&2
      echo "  re-apply gateway-infra against the platform account, OR override" >&2
      echo "  ADP_GATEWAY_API_URL in env. See EPIC #1107." >&2
      return 1
    fi
    export ADP_GATEWAY_API_URL
  fi

  # In cross-account mode, Terraform state lives in the CUSTOMER's bucket,
  # not the platform's. The customer's bootstrap phase created this bucket
  # in their account, and the assumed credentials have access to it.
  export ADP_STATE_BUCKET="adp-terraform-state-${ADP_CUSTOMER_ACCOUNT_ID}"

  # Assume the customer-linked role via the gateway.
  #
  # IMPORTANT: this branch is intended for the ADP-managed track only —
  # scripts/workflows running inside ADP's platform pod that have no direct
  # creds for the customer's account. If you're a self-hosted operator
  # running from a laptop or your own CI with direct AWS creds, the
  # gateway is unreachable (in-cluster service DNS) and this call will
  # error. In that case, remove the customer_account block from
  # config/deployment.yml and set account_id (top-level) directly.
  # See config/deployment.yml.example for the decision matrix.
  #
  # FATAL if the assume fails — falling back to platform creds would silently
  # deploy to the wrong account. If you're a self-hosted operator, remove the
  # customer_account block from config/deployment.yml and set account_id
  # (top-level) directly. See config/deployment.yml.example.
  if [ -z "${ADP_SKIP_CROSS_ACCOUNT_ASSUME:-}" ]; then
    _LDC_ASSUME_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/assume-customer-creds.py"
    if [ -x "$_LDC_ASSUME_SCRIPT" ]; then
      _LDC_ASSUME_OUTPUT=$("$_LDC_ASSUME_SCRIPT" 2>&1 >/tmp/.ldc-creds.$$) || {
        # Cross-account assume failed. In ADP-managed mode (ADP_CUSTOMER_ACCOUNT_ID
        # is set), this MUST be fatal — falling back to platform creds would silently
        # deploy to the wrong account. See issue #1031 for the cascade this caused.
        echo "ERROR: cross-account assume to ${ADP_CUSTOMER_ACCOUNT_ID} failed." >&2
        echo "$_LDC_ASSUME_OUTPUT" >&2
        echo "  Refusing to fall back to platform creds — would deploy to the wrong account." >&2
        echo "  If you're running from a laptop or non-ADP CI, remove the" >&2
        echo "  customer_account block from config/deployment.yml and set" >&2
        echo "  account_id (top-level) directly. See deployment.yml.example." >&2
        rm -f /tmp/.ldc-creds.$$
        return 1
      }
      if [ -s /tmp/.ldc-creds.$$ ]; then
        # shellcheck disable=SC1090
        . /tmp/.ldc-creds.$$
        # Diagnostic on stderr (the python script also prints to stderr; this is here
        # so the helper's caller can see it even when sourced from a workflow step)
        echo "$_LDC_ASSUME_OUTPUT" >&2
      fi
      rm -f /tmp/.ldc-creds.$$
    fi
  fi
else
  export ADP_DEPLOY_TARGET_ACCOUNT="$ADP_ACCOUNT_ID"
fi

unset _LDC_REPO_ROOT _LDC_CONFIG_FILE _LDC_ASSUME_SCRIPT _LDC_ASSUME_OUTPUT
unset -f _ldc_read_field _ldc_resolve
