#!/usr/bin/env bash
set -euo pipefail

: "${ADP_CONNECTION_LABEL:?selected connection required}"
: "${TARGET_ACCOUNT_ID:?verified target account required}"
: "${EXPECTED_ASSUMED_ROLE_PREFIX:?approved exact assumed role required}"
: "${ADP_STATE_BUCKET:?operator-managed state bucket required}"
: "${ADP_STATE_KEY:?operator-managed state key required}"
: "${TF_VAR_region:?target region required}"
: "${TF_VAR_kms_key_arn:?approved EBS key required}"

identity="$(adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec aws sts get-caller-identity --output json)"
account="$(jq -r '.Account' <<<"$identity")"
role="$(jq -r '.Arn' <<<"$identity")"
test "$account" = "$TARGET_ACCOUNT_ID"
[[ "$role" == "$EXPECTED_ASSUMED_ROLE_PREFIX"/* ]]

# Discover under the selected account credentials, never the runner's identity.
export TF_VAR_sts_endpoint_security_group_ids
TF_VAR_sts_endpoint_security_group_ids="$(adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec \
  aws ec2 describe-vpc-endpoints --region "$TF_VAR_region" \
  --filters "Name=vpc-id,Values=${TF_VAR_vpc_id:?target VPC required}" "Name=service-name,Values=com.amazonaws.$TF_VAR_region.sts" \
  --query 'VpcEndpoints[?PrivateDnsEnabled && State == `available`].Groups[].GroupId' --output json)"

module="$(dirname "$0")/infra"
adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec terraform -chdir="$module" init -input=false \
  -backend-config="bucket=$ADP_STATE_BUCKET" \
  -backend-config="key=$ADP_STATE_KEY" \
  -backend-config="region=$TF_VAR_region" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec terraform -chdir="$module" apply -input=false -auto-approve
